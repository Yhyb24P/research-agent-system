from researchd.agents.cloud_lead import CloudLeadAdapter
from researchd.context.builder import CloudContextSelection
from researchd.executor.contracts import ExecutorResult, GrantedWorkOrder
from researchd.storage.models import AttemptRecord, WorkOrderRecord
from researchd.agents.schemas import PlanProposal
from researchd.domain.review import ReviewDecision
from researchd.agents.cloud_lead import CloudLeadResult
from researchd.executor.worker import LocalExecutorWorker
from pydantic import BaseModel
from researchd.collaboration.contracts import AgentHealth, AgentInvocationRequest, AgentInvocationResult, AgentRuntime, ExecuteInvocationInput, PlanInvocationInput, ReviewInvocationInput
from researchd.domain.enums import InvocationStatus


class CloudLeadAgentAdapter:
    """Canonical adapter preserving the existing CloudLeadAdapter implementation."""
    def __init__(self, delegate: CloudLeadAdapter) -> None:
        self.delegate = delegate

    async def health(self, runtime: AgentRuntime) -> AgentHealth:
        del runtime
        return AgentHealth(healthy=True)

    async def invoke(self, request: AgentInvocationRequest) -> AgentInvocationResult:
        typed = request.typed_input
        selection = typed.context if isinstance(typed, (PlanInvocationInput, ReviewInvocationInput)) else request.payload
        if not isinstance(selection, CloudContextSelection):
            return AgentInvocationResult(invocation_id=request.invocation_id, status=InvocationStatus.FAILED, reason_code="CONTEXT_SELECTION_REQUIRED")
        if request.purpose.value == "PLAN":
            output_obj: BaseModel = (await self.delegate.propose_plan(selection)).output
        elif request.purpose.value == "REVIEW":
            output_obj = (await self.delegate.review(selection)).output
        else:
            return AgentInvocationResult(invocation_id=request.invocation_id, status=InvocationStatus.FAILED, reason_code="UNSUPPORTED_PURPOSE")
        return AgentInvocationResult(invocation_id=request.invocation_id, status=InvocationStatus.SUCCEEDED, output_type=type(output_obj).__name__, output=output_obj.model_dump(mode="json"))

    async def cancel(self, invocation_id: str) -> None:
        del invocation_id

    async def plan(self, selection: CloudContextSelection) -> CloudLeadResult[PlanProposal]:
        return await self.delegate.propose_plan(selection)

    async def review(self, selection: CloudContextSelection) -> CloudLeadResult[ReviewDecision]:
        return await self.delegate.review(selection)


class LocalExecutorAgentAdapter:
    """Canonical adapter preserving the capability-brokered local executor."""
    def __init__(self, delegate: LocalExecutorWorker) -> None:
        self.delegate = delegate

    async def health(self, runtime: AgentRuntime) -> AgentHealth:
        del runtime
        return AgentHealth(healthy=True)

    async def invoke(self, request: AgentInvocationRequest) -> AgentInvocationResult:
        typed = request.typed_input
        work_order = typed.work_order if isinstance(typed, ExecuteInvocationInput) else request.payload
        if not isinstance(work_order, GrantedWorkOrder):
            return AgentInvocationResult(invocation_id=request.invocation_id, status=InvocationStatus.FAILED, reason_code="GRANTED_WORK_ORDER_REQUIRED")
        result: ExecutorResult = await self.delegate.execute(work_order)
        return AgentInvocationResult(invocation_id=request.invocation_id, status=InvocationStatus.SUCCEEDED if result.status == "execution_complete" else InvocationStatus.FAILED, output_type="ExecutorResult", output=result.model_dump(mode="json"), reason_code=None if result.status == "execution_complete" else result.status)

    async def cancel(self, invocation_id: str) -> None:
        del invocation_id

    async def execute(self, work_order: WorkOrderRecord, attempt: AttemptRecord) -> ExecutorResult:
        del attempt
        return await self.delegate.execute(work_order)  # type: ignore[arg-type]
