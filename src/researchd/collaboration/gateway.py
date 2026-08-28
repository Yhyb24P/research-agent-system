"""Compatibility gateway for routing orchestration through canonical adapters."""
from researchd.agents.cloud_lead import CloudLeadResult
from researchd.agents.schemas import PlanProposal
from researchd.domain.review import ReviewDecision
from researchd.context.builder import CloudContextSelection
from researchd.collaboration.adapters import CloudLeadAgentAdapter, LocalExecutorAgentAdapter
from researchd.executor.contracts import ExecutorResult
from researchd.storage.models import AttemptRecord, WorkOrderRecord


class CollaborationGateway:
    def __init__(self, cloud: CloudLeadAgentAdapter, executor: LocalExecutorAgentAdapter) -> None:
        self.cloud = cloud
        self.executor = executor

    async def plan(self, selection: CloudContextSelection) -> CloudLeadResult[PlanProposal]:
        return await self.cloud.plan(selection)

    async def review(self, selection: CloudContextSelection) -> CloudLeadResult[ReviewDecision]:
        return await self.cloud.review(selection)

    async def execute(self, work_order: WorkOrderRecord, attempt: AttemptRecord) -> ExecutorResult:
        return await self.executor.execute(work_order, attempt)

    async def cancel(self, attempt_id: str) -> None:
        await self.executor.cancel(attempt_id)
