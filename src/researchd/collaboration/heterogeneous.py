"""Adapters for non-model Agent Runtimes; controller records remain authoritative."""
import json
from typing import Any, Protocol
from urllib.parse import urlparse
import httpx
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker
from researchd.adapters.a2a.adapter import A2AAdapter
from researchd.adapters.a2a.codec import A2ACodecError, decode_executor_result, encode_granted_work_order
from researchd.collaboration.contracts import AgentHealth, AgentInvocationRequest, AgentInvocationResult, AgentRuntime, EvidenceInvocationInput, ExecuteInvocationInput, PlanInvocationInput, ReviewInvocationInput
from researchd.domain.enums import InvocationStatus
from researchd.executor.contracts import ExecutorResult
from researchd.runtime_sessions.launch_profiles import RuntimeLaunchProfileService
from researchd.storage.models import RuntimeSessionRecord


class HttpAgentClient(Protocol):
    async def invoke(self, endpoint: str, payload: dict[str, Any]) -> dict[str, Any]: ...


class ProcessAgentRunner(Protocol):
    async def invoke(self, command: tuple[str, ...], payload: dict[str, Any]) -> dict[str, Any]: ...


def _request_payload(request: AgentInvocationRequest) -> dict[str, Any] | None:
    payload: dict[str, Any] | None
    if isinstance(request.typed_input, ExecuteInvocationInput):
        payload = request.typed_input.work_order.model_dump(mode="json")
    elif isinstance(request.typed_input, (PlanInvocationInput, ReviewInvocationInput, EvidenceInvocationInput)):
        payload = request.typed_input.context.model_dump(mode="json")
    else:
        payload = None
    if payload is not None and request.context_bundle is not None:
        payload = {**payload, "agent_context": request.context_bundle.model_dump(mode="json")}
    return payload


class A2ARemoteAgentAdapter:
    """Map one canonical Invocation to an A2A Task, never to a WorkOrder."""
    def __init__(self, delegate: A2AAdapter) -> None:
        self.delegate = delegate

    async def health(self, runtime: AgentRuntime) -> AgentHealth:
        return AgentHealth(healthy=bool(runtime.endpoint_ref), reason=None if runtime.endpoint_ref else "endpoint_ref missing")

    async def invoke(self, request: AgentInvocationRequest) -> AgentInvocationResult:
        if (
            request.work_order_id is None
            or request.attempt_id is None
            or not isinstance(request.typed_input, ExecuteInvocationInput)
        ):
            return AgentInvocationResult(invocation_id=request.invocation_id, status=InvocationStatus.FAILED, reason_code="A2A_SCOPE_REQUIRED")
        message = encode_granted_work_order(request.typed_input.work_order)
        task = await self.delegate.dispatch(
            work_order_id=request.work_order_id,
            attempt_id=request.attempt_id,
            message=message,
            invocation_id=str(request.invocation_id),
        )
        status, reason = self._map_task_status(task.status.state)
        if status is not InvocationStatus.SUCCEEDED:
            return AgentInvocationResult(
                invocation_id=request.invocation_id,
                status=status,
                external_invocation_id=task.id,
                output_type="A2ATask",
                output=task.model_dump(mode="json"),
                reason_code=reason,
            )
        try:
            result = decode_executor_result(task, expected_attempt_id=request.attempt_id)
        except A2ACodecError:
            return AgentInvocationResult(
                invocation_id=request.invocation_id,
                status=InvocationStatus.FAILED,
                external_invocation_id=task.id,
                output_type="A2ATask",
                output=task.model_dump(mode="json"),
                reason_code="A2A_EXECUTOR_RESULT_INVALID",
            )
        return AgentInvocationResult(
            invocation_id=request.invocation_id,
            status=InvocationStatus.SUCCEEDED,
            external_invocation_id=task.id,
            output_type="ExecutorResult",
            output=result.model_dump(mode="json"),
        )

    @staticmethod
    def _map_task_status(state: str) -> tuple[InvocationStatus, str | None]:
        if state == "TASK_STATE_COMPLETED":
            return InvocationStatus.SUCCEEDED, None
        if state == "TASK_STATE_CANCELED":
            return InvocationStatus.CANCELLED, "A2A_TASK_CANCELLED"
        if state in {"TASK_STATE_FAILED", "TASK_STATE_REJECTED"}:
            return InvocationStatus.FAILED, f"A2A_TASK_{state.removeprefix('TASK_STATE_')}"
        if state == "TASK_STATE_AUTH_REQUIRED":
            return InvocationStatus.RUNNING, "A2A_TASK_AUTH_REQUIRED"
        if state == "TASK_STATE_INPUT_REQUIRED":
            return InvocationStatus.RUNNING, "A2A_TASK_INPUT_REQUIRED"
        return InvocationStatus.RUNNING, "A2A_TASK_NONTERMINAL"

    async def cancel(self, invocation_id: str) -> None:
        await self.delegate.cancel(invocation_id)


class HttpAgentAdapter:
    def __init__(self, client: HttpAgentClient, *, max_payload_bytes: int = 256_000, max_output_bytes: int = 1_000_000) -> None:
        if max_payload_bytes <= 0 or max_output_bytes <= 0:
            raise ValueError("HTTP payload and output limits must be positive")
        self.client = client
        self.max_payload_bytes = max_payload_bytes
        self.max_output_bytes = max_output_bytes

    async def health(self, runtime: AgentRuntime) -> AgentHealth:
        if runtime.endpoint_ref is None:
            return AgentHealth(healthy=False, reason="endpoint_ref missing")
        parsed = urlparse(runtime.endpoint_ref)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
            return AgentHealth(healthy=False, reason="endpoint_ref is not a safe HTTP URL")
        return AgentHealth(healthy=True)

    async def invoke(self, request: AgentInvocationRequest) -> AgentInvocationResult:
        payload = _request_payload(request)
        if payload is None:
            return AgentInvocationResult(invocation_id=request.invocation_id, status=InvocationStatus.FAILED, reason_code="HTTP_PAYLOAD_REQUIRED")
        if len(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()) > self.max_payload_bytes:
            return AgentInvocationResult(invocation_id=request.invocation_id, status=InvocationStatus.FAILED, reason_code="HTTP_PAYLOAD_TOO_LARGE")
        if request.endpoint_ref is None:
            return AgentInvocationResult(invocation_id=request.invocation_id, status=InvocationStatus.FAILED, reason_code="HTTP_ENDPOINT_REQUIRED")
        parsed = urlparse(request.endpoint_ref)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
            return AgentInvocationResult(invocation_id=request.invocation_id, status=InvocationStatus.FAILED, reason_code="HTTP_ENDPOINT_UNSAFE")
        response = await self.client.invoke(request.endpoint_ref, payload)
        try:
            output_size = len(json.dumps(response, sort_keys=True, separators=(",", ":")).encode())
        except (TypeError, ValueError):
            return AgentInvocationResult(invocation_id=request.invocation_id, status=InvocationStatus.FAILED, reason_code="HTTP_OUTPUT_MALFORMED")
        if output_size > self.max_output_bytes:
            return AgentInvocationResult(invocation_id=request.invocation_id, status=InvocationStatus.FAILED, reason_code="HTTP_OUTPUT_TOO_LARGE")
        if isinstance(request.typed_input, ExecuteInvocationInput):
            try:
                typed_output = ExecutorResult.model_validate(response)
            except ValidationError:
                return AgentInvocationResult(invocation_id=request.invocation_id, status=InvocationStatus.FAILED, reason_code="HTTP_EXECUTOR_RESULT_INVALID")
            return AgentInvocationResult(invocation_id=request.invocation_id, status=InvocationStatus.SUCCEEDED, output_type="ExecutorResult", output=typed_output.model_dump(mode="json"))
        return AgentInvocationResult(invocation_id=request.invocation_id, status=InvocationStatus.SUCCEEDED, output_type="HttpAgentResult", output=response)

    async def cancel(self, invocation_id: str) -> None:
        del invocation_id


class LocalProcessAgentAdapter:
    def __init__(self, runner: ProcessAgentRunner, command: tuple[str, ...], *, max_output_bytes: int = 1_000_000) -> None:
        if not command or any(not part or "\x00" in part for part in command):
            raise ValueError("process command must contain nonempty NUL-free arguments")
        if max_output_bytes <= 0:
            raise ValueError("process output limit must be positive")
        self.runner, self.command = runner, command
        self.max_output_bytes = max_output_bytes

    async def health(self, runtime: AgentRuntime) -> AgentHealth:
        return AgentHealth(healthy=bool(self.command), reason=None if self.command else "process command missing")

    async def invoke(self, request: AgentInvocationRequest) -> AgentInvocationResult:
        payload = _request_payload(request)
        if payload is None:
            return AgentInvocationResult(invocation_id=request.invocation_id, status=InvocationStatus.FAILED, reason_code="PROCESS_PAYLOAD_REQUIRED")
        output = await self.runner.invoke(self.command, payload)
        try:
            output_size = len(json.dumps(output, sort_keys=True, separators=(",", ":")).encode())
        except (TypeError, ValueError):
            return AgentInvocationResult(invocation_id=request.invocation_id, status=InvocationStatus.FAILED, reason_code="PROCESS_OUTPUT_MALFORMED")
        if output_size > self.max_output_bytes:
            return AgentInvocationResult(invocation_id=request.invocation_id, status=InvocationStatus.FAILED, reason_code="PROCESS_OUTPUT_TOO_LARGE")
        if isinstance(request.typed_input, ExecuteInvocationInput):
            try:
                typed_output = ExecutorResult.model_validate(output)
            except ValidationError:
                return AgentInvocationResult(invocation_id=request.invocation_id, status=InvocationStatus.FAILED, reason_code="PROCESS_EXECUTOR_RESULT_INVALID")
            return AgentInvocationResult(invocation_id=request.invocation_id, status=InvocationStatus.SUCCEEDED, output_type="ExecutorResult", output=typed_output.model_dump(mode="json"))
        return AgentInvocationResult(invocation_id=request.invocation_id, status=InvocationStatus.SUCCEEDED, output_type="ProcessAgentResult", output=output)

    async def cancel(self, invocation_id: str) -> None:
        cancel = getattr(self.runner, "cancel", None)
        if cancel is not None:
            await cancel(invocation_id)


class HttpxAgentClient:
    """Bounded JSON transport to a registry-owned Agent endpoint."""

    async def invoke(self, endpoint: str, payload: dict[str, Any]) -> dict[str, Any]:
        async with httpx.AsyncClient(
            timeout=30.0,
            follow_redirects=False,
            trust_env=False,
        ) as client:
            response = await client.post(endpoint, json=payload)
            response.raise_for_status()
            decoded = response.json()
        if not isinstance(decoded, dict):
            raise ValueError("Agent endpoint returned non-object JSON")
        return decoded


class ManagedProcessAgentAdapter:
    """Route invocation to an already-supervised PROCESS Agent service."""

    def __init__(
        self,
        sessions: sessionmaker[Session],
        launch_profiles: RuntimeLaunchProfileService,
        client: HttpAgentClient,
    ) -> None:
        self.sessions = sessions
        self.launch_profiles = launch_profiles
        self.delegate = HttpAgentAdapter(client)

    def _require_live(self, runtime_id: object, endpoint: str | None) -> None:
        profile = self.launch_profiles.resolve_process(str(runtime_id))
        parsed = urlparse(endpoint or "")
        if (
            parsed.scheme != "http"
            or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("managed PROCESS runtime requires a safe loopback endpoint")
        with self.sessions() as session:
            row = session.scalar(select(RuntimeSessionRecord).where(
                RuntimeSessionRecord.runtime_id == str(runtime_id),
                RuntimeSessionRecord.supervisor_state == "HEALTHY",
                RuntimeSessionRecord.launch_profile_sha256 == profile.spec_sha256,
            ).order_by(RuntimeSessionRecord.started_at.desc()).limit(1))
        if row is None:
            raise ValueError("managed PROCESS runtime has no matching HEALTHY session")

    async def health(self, runtime: AgentRuntime) -> AgentHealth:
        try:
            self._require_live(runtime.runtime_id, runtime.endpoint_ref)
        except ValueError as error:
            return AgentHealth(healthy=False, reason=str(error))
        return await self.delegate.health(runtime)

    async def invoke(self, request: AgentInvocationRequest) -> AgentInvocationResult:
        try:
            self._require_live(request.runtime_id, request.endpoint_ref)
        except ValueError:
            return AgentInvocationResult(
                invocation_id=request.invocation_id,
                status=InvocationStatus.FAILED,
                reason_code="PROCESS_RUNTIME_UNAVAILABLE",
            )
        return await self.delegate.invoke(request)

    async def cancel(self, invocation_id: str) -> None:
        await self.delegate.cancel(invocation_id)
