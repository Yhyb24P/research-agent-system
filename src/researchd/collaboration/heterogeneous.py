"""Adapters for non-model Agent Runtimes; controller records remain authoritative."""
import asyncio
import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable, Protocol
from urllib.parse import urlparse
import httpx
from pydantic import ValidationError, model_validator
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker
from researchd.adapters.a2a.adapter import A2AAdapter, A2AClient
from researchd.adapters.a2a.codec import (
    A2ACodecError,
    RemoteExecutionRequest,
    decode_executor_result,
    encode_granted_work_order,
    encode_remote_execution_request,
)
from researchd.adapters.a2a.schemas import A2A_PROTOCOL_VERSION
from researchd.adapters.a2a.sdk_client import OfficialA2AClient
from researchd.collaboration.action_broker import AgentActionBroker, AgentMessageAction
from researchd.collaboration.handoff import HandoffProposalAction
from researchd.collaboration.contracts import AgentHealth, AgentInvocationRequest, AgentInvocationResult, AgentRuntime, EvidenceInvocationInput, ExecuteInvocationInput, PlanInvocationInput, ReviewInvocationInput
from researchd.domain.enums import AgentAdapterKind, Capability, DelegationPurpose, InvocationStatus
from researchd.domain.ids import AgentId, AgentRuntimeId
from researchd.domain.base import DomainModel
from researchd.executor.capability_broker import CapabilityBroker
from researchd.executor.contracts import (
    ExecutorResult,
    GrantedWorkOrder,
    LocalAgentRequest,
    LocalAgentResponse,
)
from researchd.executor.worker import LocalExecutorWorker
from researchd.models.base import LocalModelUnavailable
from researchd.runtime_sessions.launch_profiles import RuntimeLaunchProfileService
from researchd.storage.models import (
    AgentInteractionRecord,
    AgentInvocationRecord,
    AgentRecord,
    AgentRuntimeRecord,
    RuntimeSessionRecord,
    WorkspaceGrantRecord,
    WorkspaceTransportRecord,
)


class HttpAgentClient(Protocol):
    async def invoke(self, endpoint: str, payload: dict[str, Any]) -> dict[str, Any]: ...


class ProcessAgentRunner(Protocol):
    async def invoke(self, command: tuple[str, ...], payload: dict[str, Any]) -> dict[str, Any]: ...


class ManagedAgentTurnRequest(DomainModel):
    invocation_id: str
    run_id: str
    purpose: DelegationPurpose
    work_order_id: str | None = None
    attempt_id: str | None = None
    allowed_capabilities: tuple[Capability, ...] = ()
    payload: dict[str, object]


class ManagedAgentTurnResponse(DomainModel):
    execution: LocalAgentResponse | None = None
    output: dict[str, object] | None = None
    agent_actions: tuple[AgentMessageAction | HandoffProposalAction, ...] = ()

    @model_validator(mode="after")
    def exactly_one_result_kind(self) -> "ManagedAgentTurnResponse":
        if (self.execution is None) == (self.output is None):
            raise ValueError("managed Agent turn requires exactly one result kind")
        return self


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


class GovernedA2ARemoteAgentAdapter:
    """Resolve every external A2A call from the authoritative runtime registry.

    This is intentionally distinct from the legacy directly-injected adapter:
    product composition must never inherit an endpoint, tenant, local workspace
    grant, or controller capability from an invocation payload.
    """

    def __init__(
        self,
        sessions: sessionmaker[Session],
        client_factory: Callable[[str], A2AClient] = OfficialA2AClient,
    ) -> None:
        self.sessions = sessions
        self.client_factory = client_factory

    async def health(self, runtime: AgentRuntime) -> AgentHealth:
        try:
            self._runtime(str(runtime.runtime_id), str(runtime.agent_id))
        except ValueError as error:
            return AgentHealth(healthy=False, reason=str(error))
        return AgentHealth(healthy=True)

    async def invoke(self, request: AgentInvocationRequest) -> AgentInvocationResult:
        if (
            request.purpose is not DelegationPurpose.EXECUTE
            or request.work_order_id is None
            or request.attempt_id is None
            or not isinstance(request.typed_input, ExecuteInvocationInput)
            or request.context_bundle is None
        ):
            return AgentInvocationResult(
                invocation_id=request.invocation_id,
                status=InvocationStatus.FAILED,
                reason_code="A2A_GOVERNED_SCOPE_REQUIRED",
            )
        try:
            runtime, tenant = self._runtime(
                str(request.runtime_id), str(request.agent_id),
            )
            context = request.context_bundle
            if (
                context.target_agent_id != str(request.agent_id)
                or context.target_runtime_id != str(request.runtime_id)
                or context.run_id != request.run_id
                or context.work_order_id != request.work_order_id
            ):
                raise ValueError("A2A context bundle scope mismatch")
            message = encode_remote_execution_request(RemoteExecutionRequest(
                invocation_id=str(request.invocation_id),
                run_id=request.run_id,
                work_order_id=request.work_order_id,
                attempt_id=request.attempt_id,
                objective=context.selected_context.objective or context.selected_context.goal,
                context=context,
            ))
            task = await A2AAdapter(
                self.sessions,
                self.client_factory(runtime.endpoint_ref or ""),
                remote_agent_id=str(runtime.agent_id),
            ).dispatch(
                work_order_id=request.work_order_id,
                attempt_id=request.attempt_id,
                message=message,
                tenant=tenant,
                invocation_id=str(request.invocation_id),
            )
        except Exception as error:
            return AgentInvocationResult(
                invocation_id=request.invocation_id,
                status=InvocationStatus.FAILED,
                reason_code=f"A2A_GOVERNED_{type(error).__name__}"[:128],
            )
        status, reason = A2ARemoteAgentAdapter._map_task_status(task.status.state)
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
            result = decode_executor_result(
                task,
                expected_attempt_id=request.attempt_id,
                forbid_capability_results=True,
            )
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

    async def cancel(self, invocation_id: str) -> None:
        with self.sessions() as session:
            invocation = session.get(AgentInvocationRecord, invocation_id)
        if invocation is None:
            raise ValueError("A2A invocation is missing")
        runtime, tenant = self._runtime(invocation.runtime_id, invocation.agent_id)
        await A2AAdapter(
            self.sessions,
            self.client_factory(runtime.endpoint_ref or ""),
            remote_agent_id=str(runtime.agent_id),
        ).cancel(invocation_id, tenant=tenant)

    def _runtime(self, runtime_id: str, agent_id: str) -> tuple[AgentRuntime, str | None]:
        with self.sessions() as session:
            row = session.scalar(select(AgentRuntimeRecord).join(
                AgentRecord,
                AgentRecord.agent_id == AgentRuntimeRecord.agent_id,
            ).where(
                AgentRuntimeRecord.runtime_id == runtime_id,
                AgentRuntimeRecord.agent_id == agent_id,
                AgentRuntimeRecord.adapter_kind == "A2A",
                AgentRuntimeRecord.enabled.is_(True),
                AgentRecord.enabled.is_(True),
                AgentRuntimeRecord.runtime_lease_id.is_not(None),
                AgentRuntimeRecord.lease_expires_at > datetime.now(UTC),
            ))
            if row is None:
                raise ValueError("A2A runtime is disabled, unleased, or mismatched")
            runtime = AgentRuntime(
                runtime_id=AgentRuntimeId(row.runtime_id),
                agent_id=AgentId(row.agent_id),
                adapter_kind=AgentAdapterKind(row.adapter_kind),
                runtime_name=row.runtime_name,
                endpoint_ref=row.endpoint_ref,
                framework=row.framework,
                model_provider=row.model_provider,
                model_name=row.model_name,
                protocols=tuple(row.protocols_json),
                metadata=dict(row.metadata_json),
            )
        endpoint = runtime.endpoint_ref or ""
        parsed = urlparse(endpoint)
        loopback = parsed.hostname in {"127.0.0.1", "::1", "localhost"}
        if (
            not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
            or (parsed.scheme != "https" and not (parsed.scheme == "http" and loopback))
        ):
            raise ValueError("A2A endpoint is not a governed HTTPS/loopback URL")
        if A2A_PROTOCOL_VERSION not in runtime.protocols:
            raise ValueError("A2A runtime does not declare A2A/1.0")
        tenant = runtime.metadata.get("a2a_tenant")
        if tenant is not None and (not tenant or len(tenant) > 128 or any(ord(char) < 32 for char in tenant)):
            raise ValueError("A2A tenant is invalid")
        return runtime, tenant


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

    def __init__(
        self,
        *,
        readiness_timeout_seconds: float = 5.0,
        readiness_poll_seconds: float = 0.05,
        read_timeout_seconds: float = 610.0,
    ) -> None:
        if readiness_timeout_seconds <= 0 or readiness_poll_seconds <= 0 or read_timeout_seconds <= 0:
            raise ValueError("managed Agent readiness bounds must be positive")
        self.readiness_timeout_seconds = readiness_timeout_seconds
        self.readiness_poll_seconds = readiness_poll_seconds
        self.read_timeout_seconds = read_timeout_seconds

    async def invoke(self, endpoint: str, payload: dict[str, Any]) -> dict[str, Any]:
        async with httpx.AsyncClient(
            # Generic adapter safety bound; it does not encode an Agent role.
            timeout=httpx.Timeout(connect=5.0, read=self.read_timeout_seconds, write=10.0, pool=5.0),
            follow_redirects=False,
            trust_env=False,
        ) as client:
            await self._wait_until_ready(client, endpoint)
            response = await client.post(endpoint, json=payload)
            response.raise_for_status()
            decoded = response.json()
        if not isinstance(decoded, dict):
            raise ValueError("Agent endpoint returned non-object JSON")
        return decoded

    async def _wait_until_ready(
        self,
        client: httpx.AsyncClient,
        endpoint: str,
    ) -> None:
        """Wait for protocol readiness, not merely for the bridge PID to exist."""
        parsed = urlparse(endpoint)
        health_endpoint = parsed._replace(path="/health", params="", query="", fragment="").geturl()
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self.readiness_timeout_seconds
        while True:
            try:
                remaining = max(deadline - loop.time(), 0.001)
                response = await client.get(
                    health_endpoint,
                    timeout=min(0.25, remaining),
                )
                if response.status_code == 200:
                    health = response.json()
                    if isinstance(health, dict) and health.get("healthy") is True:
                        return
            except (httpx.HTTPError, ValueError):
                pass
            if loop.time() >= deadline:
                raise httpx.ConnectTimeout("managed Agent endpoint did not become ready")
            await asyncio.sleep(self.readiness_poll_seconds)


class ManagedProcessAgentAdapter:
    """Route invocation to an already-supervised PROCESS Agent service."""

    def __init__(
        self,
        sessions: sessionmaker[Session],
        launch_profiles: RuntimeLaunchProfileService,
        client: HttpAgentClient,
        broker: CapabilityBroker,
        action_broker: AgentActionBroker,
        planning_capabilities: frozenset[Capability] = frozenset(),
    ) -> None:
        self.sessions = sessions
        self.launch_profiles = launch_profiles
        self.client = client
        self.broker = broker
        self.action_broker = action_broker
        self.planning_capabilities = planning_capabilities

    def _require_live(self, runtime_id: object) -> str:
        profile = self.launch_profiles.resolve_process(str(runtime_id))
        with self.sessions() as session:
            runtime = session.scalar(select(AgentRuntimeRecord).join(
                AgentRecord,
                AgentRecord.agent_id == AgentRuntimeRecord.agent_id,
            ).where(
                AgentRuntimeRecord.runtime_id == str(runtime_id),
                AgentRuntimeRecord.enabled.is_(True),
                AgentRecord.enabled.is_(True),
            ))
        if runtime is None:
            raise ValueError("managed PROCESS runtime is disabled or missing")
        endpoint = runtime.endpoint_ref
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
        assert endpoint is not None
        return endpoint

    async def health(self, runtime: AgentRuntime) -> AgentHealth:
        try:
            endpoint = self._require_live(runtime.runtime_id)
        except ValueError as error:
            return AgentHealth(healthy=False, reason=str(error))
        del endpoint
        return AgentHealth(healthy=True)

    async def invoke(self, request: AgentInvocationRequest) -> AgentInvocationResult:
        try:
            endpoint = self._require_live(request.runtime_id)
        except ValueError:
            return AgentInvocationResult(
                invocation_id=request.invocation_id,
                status=InvocationStatus.FAILED,
                reason_code="PROCESS_RUNTIME_UNAVAILABLE",
            )
        typed = request.typed_input
        if not isinstance(typed, ExecuteInvocationInput):
            return await self._invoke_business_turn(endpoint, request)
        try:
            work_order = self._controller_work_order(request, typed)
        except ValueError:
            return AgentInvocationResult(
                invocation_id=request.invocation_id,
                status=InvocationStatus.FAILED,
                reason_code="WORKSPACE_GRANT_UNAVAILABLE",
            )

        class EndpointModel:
            async def complete(inner_self, model_request: LocalAgentRequest) -> LocalAgentResponse:
                del inner_self
                try:
                    response = ManagedAgentTurnResponse.model_validate(await self.client.invoke(
                        endpoint,
                        ManagedAgentTurnRequest(
                            invocation_id=str(request.invocation_id),
                            run_id=request.run_id,
                            purpose=request.purpose,
                            work_order_id=request.work_order_id,
                            attempt_id=work_order.attempt_id,
                            payload=model_request.model_dump(mode="json"),
                        ).model_dump(mode="json"),
                    ))
                    self._submit_agent_actions(request, response)
                    if response.execution is None:
                        raise ValueError("managed execution turn omitted execution result")
                    return response.execution
                except Exception as error:
                    raise LocalModelUnavailable(type(error).__name__) from error

        result = await LocalExecutorWorker(
            EndpointModel(),
            self.broker,
            self.sessions,
        ).execute(work_order)
        return AgentInvocationResult(
            invocation_id=request.invocation_id,
            status=(
                InvocationStatus.SUCCEEDED
                if result.status == "execution_complete"
                else InvocationStatus.FAILED
            ),
            output_type="ExecutorResult",
            output=result.model_dump(mode="json"),
            reason_code=None if result.status == "execution_complete" else result.status,
        )

    async def _invoke_business_turn(
        self,
        endpoint: str,
        request: AgentInvocationRequest,
    ) -> AgentInvocationResult:
        payload = _request_payload(request)
        if payload is None:
            return AgentInvocationResult(
                invocation_id=request.invocation_id,
                status=InvocationStatus.FAILED,
                reason_code="MANAGED_TURN_PAYLOAD_REQUIRED",
            )
        self._record_interaction(request, payload)
        try:
            response = ManagedAgentTurnResponse.model_validate(await self.client.invoke(
                endpoint,
                ManagedAgentTurnRequest(
                    invocation_id=str(request.invocation_id), run_id=request.run_id,
                    purpose=request.purpose, work_order_id=request.work_order_id,
                    attempt_id=request.attempt_id, payload=payload,
                    allowed_capabilities=tuple(sorted(
                        self.planning_capabilities,
                        key=lambda capability: capability.value,
                    )),
                ).model_dump(mode="json"),
            ))
            self._submit_agent_actions(request, response)
            if response.output is None:
                raise ValueError("managed business turn omitted structured output")
        except Exception as error:
            self._complete_interaction(request, status="FAILED", reason_code=type(error).__name__[:128])
            return AgentInvocationResult(
                invocation_id=request.invocation_id,
                status=InvocationStatus.FAILED,
                reason_code=f"MANAGED_TURN_{type(error).__name__}"[:128],
            )
        self._complete_interaction(request, status="COMPLETED", reason_code=None, response_json=response.output)
        return AgentInvocationResult(
            invocation_id=request.invocation_id,
            status=InvocationStatus.SUCCEEDED,
            output_type=request.purpose.value,
            output=response.output,
        )

    def _record_interaction(self, request: AgentInvocationRequest, payload: dict[str, Any]) -> None:
        now = datetime.now(UTC)
        bundle_sha256 = (
            request.context_bundle.bundle_sha256
            if request.context_bundle is not None
            else hashlib.sha256(
                json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode(),
            ).hexdigest()
        )
        with self.sessions.begin() as session:
            session.add(AgentInteractionRecord(
                interaction_id=str(request.invocation_id),
                invocation_id=str(request.invocation_id),
                run_id=request.run_id,
                work_order_id=request.work_order_id,
                attempt_id=request.attempt_id,
                remote_agent_id=None, a2a_context_id=None, a2a_task_id=None,
                role="managed_agent", purpose=request.purpose.value,
                provider="managed-process", model="process-turn",
                bundle_sha256=bundle_sha256,
                response_type=request.purpose.value,
                response_json=None, status="IN_PROGRESS", reason_code=None,
                attempts=1, prompt_tokens=0, completion_tokens=0, total_tokens=0,
                cost_usd="0", provider_request_id=None,
                created_at=now, completed_at=None,
            ))

    def _complete_interaction(
        self,
        request: AgentInvocationRequest,
        *,
        status: str,
        reason_code: str | None,
        response_json: dict[str, object] | None = None,
    ) -> None:
        now = datetime.now(UTC)
        with self.sessions.begin() as session:
            row = session.get(AgentInteractionRecord, str(request.invocation_id))
            if row is None:
                return
            row.status = status
            row.reason_code = reason_code
            row.response_json = response_json
            row.completed_at = now

    def _submit_agent_actions(
        self,
        request: AgentInvocationRequest,
        response: ManagedAgentTurnResponse,
    ) -> None:
        for action in response.agent_actions:
            if isinstance(action, AgentMessageAction):
                self.action_broker.submit_message(request.invocation_id, action)
            else:
                self.action_broker.submit_handoff(request.invocation_id, action)

    async def cancel(self, invocation_id: str) -> None:
        del invocation_id

    def _controller_work_order(
        self,
        request: AgentInvocationRequest,
        typed: ExecuteInvocationInput,
    ) -> GrantedWorkOrder:
        grant_id = request.workspace_grant_id
        if grant_id is None:
            raise ValueError("managed execution requires a workspace grant")
        now = datetime.now(UTC)
        with self.sessions() as session:
            invocation = session.get(AgentInvocationRecord, str(request.invocation_id))
            grant = session.get(WorkspaceGrantRecord, grant_id)
            transport = session.scalar(select(WorkspaceTransportRecord).where(
                WorkspaceTransportRecord.workspace_grant_id == grant_id,
                WorkspaceTransportRecord.state == "ACTIVE",
            ).order_by(WorkspaceTransportRecord.created_at.desc()).limit(1))
        if (
            invocation is None
            or invocation.runtime_id != str(request.runtime_id)
            or invocation.workspace_grant_id != grant_id
            or grant is None
            or grant.state != "ACTIVE"
            or grant.lease_expires_at is None
            or grant.lease_expires_at <= now
            or transport is None
        ):
            raise ValueError("workspace authority is not active")
        workspace = Path(transport.remote_workspace_handle).resolve(strict=True)
        if not workspace.is_dir():
            raise ValueError("workspace transport is not a directory")
        sandbox = typed.work_order.sandbox.model_copy(update={"workspace": str(workspace)})
        return typed.work_order.model_copy(update={"sandbox": sandbox})
