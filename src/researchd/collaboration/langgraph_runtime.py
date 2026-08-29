"""Optional LangGraph runtime behind the canonical AgentAdapter contract."""

import asyncio
from typing import Any, Protocol

from pydantic import ValidationError

from researchd.collaboration.contracts import (
    AgentHealth,
    AgentInvocationRequest,
    AgentInvocationResult,
    AgentRuntime,
    ResearchCriticResult,
    SpecialistInvocationInput,
)
from researchd.domain.enums import AgentAdapterKind, InvocationStatus


class LangGraphExecutable(Protocol):
    """The small compiled-graph surface required by the adapter."""

    async def ainvoke(
        self,
        input: dict[str, Any],
        config: dict[str, Any] | None = None,
    ) -> dict[str, Any]: ...


class LangGraphAgentAdapter:
    """Routes structured invocations into explicitly registered compiled graphs."""

    def __init__(self) -> None:
        self._graphs: dict[str, LangGraphExecutable] = {}
        self._active: dict[str, asyncio.Task[dict[str, Any]]] = {}

    def register(self, runtime_id: str, graph: LangGraphExecutable) -> None:
        if runtime_id in self._graphs:
            raise ValueError(f"LangGraph runtime already registered: {runtime_id}")
        self._graphs[runtime_id] = graph

    async def health(self, runtime: AgentRuntime) -> AgentHealth:
        if runtime.adapter_kind is not AgentAdapterKind.LANGGRAPH or runtime.framework != "langgraph":
            return AgentHealth(healthy=False, reason="runtime is not a LangGraph Agent")
        if str(runtime.runtime_id) not in self._graphs:
            return AgentHealth(healthy=False, reason="compiled graph is not registered")
        return AgentHealth(healthy=True)

    async def invoke(self, request: AgentInvocationRequest) -> AgentInvocationResult:
        if not isinstance(request.typed_input, SpecialistInvocationInput):
            return AgentInvocationResult(
                invocation_id=request.invocation_id,
                status=InvocationStatus.FAILED,
                reason_code="SPECIALIST_INPUT_REQUIRED",
            )
        graph = self._graphs.get(str(request.runtime_id))
        if graph is None:
            return AgentInvocationResult(
                invocation_id=request.invocation_id,
                status=InvocationStatus.FAILED,
                reason_code="LANGGRAPH_RUNTIME_NOT_REGISTERED",
            )
        invocation_id = str(request.invocation_id)
        if invocation_id in self._active:
            return AgentInvocationResult(
                invocation_id=request.invocation_id,
                status=InvocationStatus.FAILED,
                reason_code="INVOCATION_ALREADY_RUNNING",
            )
        config: dict[str, Any] = {
            "configurable": {"thread_id": str(request.delegation_id)},
            "metadata": {
                "invocation_id": invocation_id,
                "agent_id": str(request.agent_id),
                "runtime_id": str(request.runtime_id),
            },
        }
        task = asyncio.create_task(
            graph.ainvoke({"request": request.typed_input.model_dump(mode="json")}, config)
        )
        self._active[invocation_id] = task
        try:
            state = await task
            output = ResearchCriticResult.model_validate(state.get("result"))
            return AgentInvocationResult(
                invocation_id=request.invocation_id,
                status=InvocationStatus.SUCCEEDED,
                output_type="ResearchCriticResult",
                output=output.model_dump(mode="json"),
            )
        except asyncio.CancelledError:
            return AgentInvocationResult(
                invocation_id=request.invocation_id,
                status=InvocationStatus.CANCELLED,
                reason_code="CANCELLED",
            )
        except ValidationError:
            return AgentInvocationResult(
                invocation_id=request.invocation_id,
                status=InvocationStatus.FAILED,
                reason_code="LANGGRAPH_OUTPUT_INVALID",
            )
        except Exception as error:
            return AgentInvocationResult(
                invocation_id=request.invocation_id,
                status=InvocationStatus.FAILED,
                reason_code=f"LANGGRAPH_{type(error).__name__.upper()}",
            )
        finally:
            self._active.pop(invocation_id, None)

    async def cancel(self, invocation_id: str) -> None:
        task = self._active.get(invocation_id)
        if task is not None:
            task.cancel()


__all__ = ["LangGraphAgentAdapter", "LangGraphExecutable"]
