"""Adapters for non-model Agent Runtimes; controller records remain authoritative."""
from typing import Any, Protocol
from researchd.adapters.a2a.adapter import A2AAdapter
from researchd.collaboration.contracts import AgentHealth, AgentInvocationRequest, AgentInvocationResult, AgentRuntime
from researchd.domain.enums import InvocationStatus


class HttpAgentClient(Protocol):
    async def invoke(self, endpoint: str, payload: dict[str, Any]) -> dict[str, Any]: ...


class ProcessAgentRunner(Protocol):
    async def invoke(self, command: tuple[str, ...], payload: dict[str, Any]) -> dict[str, Any]: ...


class A2ARemoteAgentAdapter:
    """Map one canonical Invocation to an A2A Task, never to a WorkOrder."""
    def __init__(self, delegate: A2AAdapter) -> None:
        self.delegate = delegate

    async def health(self, runtime: AgentRuntime) -> AgentHealth:
        return AgentHealth(healthy=bool(runtime.endpoint_ref), reason=None if runtime.endpoint_ref else "endpoint_ref missing")

    async def invoke(self, request: AgentInvocationRequest) -> AgentInvocationResult:
        if request.work_order_id is None or request.attempt_id is None or not isinstance(request.payload, dict):
            return AgentInvocationResult(invocation_id=request.invocation_id, status=InvocationStatus.FAILED, reason_code="A2A_SCOPE_REQUIRED")
        task = await self.delegate.dispatch(work_order_id=request.work_order_id, attempt_id=request.attempt_id, payload=request.payload)
        terminal = task.status.state in {"completed", "failed", "canceled", "rejected"}
        return AgentInvocationResult(invocation_id=request.invocation_id, status=InvocationStatus.SUCCEEDED if terminal else InvocationStatus.RUNNING, output_type="A2ATask", output=task.model_dump(mode="json"), reason_code=None if terminal else "A2A_TASK_NONTERMINAL")

    async def cancel(self, invocation_id: str) -> None:
        del invocation_id


class HttpAgentAdapter:
    def __init__(self, client: HttpAgentClient) -> None:
        self.client = client

    async def health(self, runtime: AgentRuntime) -> AgentHealth:
        return AgentHealth(healthy=runtime.endpoint_ref is not None, reason=None if runtime.endpoint_ref else "endpoint_ref missing")

    async def invoke(self, request: AgentInvocationRequest) -> AgentInvocationResult:
        if request.payload is None or not isinstance(request.payload, dict):
            return AgentInvocationResult(invocation_id=request.invocation_id, status=InvocationStatus.FAILED, reason_code="HTTP_PAYLOAD_REQUIRED")
        response = await self.client.invoke(request.runtime_id, request.payload)
        return AgentInvocationResult(invocation_id=request.invocation_id, status=InvocationStatus.SUCCEEDED, output_type="HttpAgentResult", output=response)

    async def cancel(self, invocation_id: str) -> None:
        del invocation_id


class LocalProcessAgentAdapter:
    def __init__(self, runner: ProcessAgentRunner, command: tuple[str, ...]) -> None:
        self.runner, self.command = runner, command

    async def health(self, runtime: AgentRuntime) -> AgentHealth:
        return AgentHealth(healthy=bool(self.command), reason=None if self.command else "process command missing")

    async def invoke(self, request: AgentInvocationRequest) -> AgentInvocationResult:
        if not isinstance(request.payload, dict):
            return AgentInvocationResult(invocation_id=request.invocation_id, status=InvocationStatus.FAILED, reason_code="PROCESS_PAYLOAD_REQUIRED")
        output = await self.runner.invoke(self.command, request.payload)
        return AgentInvocationResult(invocation_id=request.invocation_id, status=InvocationStatus.SUCCEEDED, output_type="ProcessAgentResult", output=output)

    async def cancel(self, invocation_id: str) -> None:
        del invocation_id
