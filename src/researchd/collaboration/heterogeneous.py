"""Adapters for non-model Agent Runtimes; controller records remain authoritative."""
import json
from typing import Any, Protocol
from urllib.parse import urlparse
from researchd.adapters.a2a.adapter import A2AAdapter
from researchd.collaboration.contracts import AgentHealth, AgentInvocationRequest, AgentInvocationResult, AgentRuntime, ExecuteInvocationInput
from researchd.domain.enums import InvocationStatus


class HttpAgentClient(Protocol):
    async def invoke(self, endpoint: str, payload: dict[str, Any]) -> dict[str, Any]: ...


class ProcessAgentRunner(Protocol):
    async def invoke(self, command: tuple[str, ...], payload: dict[str, Any]) -> dict[str, Any]: ...


def _request_payload(request: AgentInvocationRequest) -> dict[str, Any] | None:
    if isinstance(request.typed_input, ExecuteInvocationInput):
        return request.typed_input.work_order.model_dump(mode="json")
    return request.payload if isinstance(request.payload, dict) else None


class A2ARemoteAgentAdapter:
    """Map one canonical Invocation to an A2A Task, never to a WorkOrder."""
    def __init__(self, delegate: A2AAdapter) -> None:
        self.delegate = delegate

    async def health(self, runtime: AgentRuntime) -> AgentHealth:
        return AgentHealth(healthy=bool(runtime.endpoint_ref), reason=None if runtime.endpoint_ref else "endpoint_ref missing")

    async def invoke(self, request: AgentInvocationRequest) -> AgentInvocationResult:
        payload = _request_payload(request)
        if request.work_order_id is None or request.attempt_id is None or payload is None:
            return AgentInvocationResult(invocation_id=request.invocation_id, status=InvocationStatus.FAILED, reason_code="A2A_SCOPE_REQUIRED")
        task = await self.delegate.dispatch(work_order_id=request.work_order_id, attempt_id=request.attempt_id, payload=payload, invocation_id=str(request.invocation_id))
        terminal = task.status.state in {"completed", "failed", "canceled", "rejected"}
        return AgentInvocationResult(invocation_id=request.invocation_id, status=InvocationStatus.SUCCEEDED if terminal else InvocationStatus.RUNNING, output_type="A2ATask", output=task.model_dump(mode="json"), reason_code=None if terminal else "A2A_TASK_NONTERMINAL")

    async def cancel(self, invocation_id: str) -> None:
        del invocation_id


class HttpAgentAdapter:
    def __init__(self, client: HttpAgentClient, *, max_payload_bytes: int = 256_000) -> None:
        if max_payload_bytes <= 0:
            raise ValueError("max_payload_bytes must be positive")
        self.client = client
        self.max_payload_bytes = max_payload_bytes

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
        return AgentInvocationResult(invocation_id=request.invocation_id, status=InvocationStatus.SUCCEEDED, output_type="HttpAgentResult", output=response)

    async def cancel(self, invocation_id: str) -> None:
        del invocation_id


class LocalProcessAgentAdapter:
    def __init__(self, runner: ProcessAgentRunner, command: tuple[str, ...]) -> None:
        if not command or any(not part or "\x00" in part for part in command):
            raise ValueError("process command must contain nonempty NUL-free arguments")
        self.runner, self.command = runner, command

    async def health(self, runtime: AgentRuntime) -> AgentHealth:
        return AgentHealth(healthy=bool(self.command), reason=None if self.command else "process command missing")

    async def invoke(self, request: AgentInvocationRequest) -> AgentInvocationResult:
        payload = _request_payload(request)
        if payload is None:
            return AgentInvocationResult(invocation_id=request.invocation_id, status=InvocationStatus.FAILED, reason_code="PROCESS_PAYLOAD_REQUIRED")
        output = await self.runner.invoke(self.command, payload)
        return AgentInvocationResult(invocation_id=request.invocation_id, status=InvocationStatus.SUCCEEDED, output_type="ProcessAgentResult", output=output)

    async def cancel(self, invocation_id: str) -> None:
        cancel = getattr(self.runner, "cancel", None)
        if cancel is not None:
            await cancel(invocation_id)
