"""Gateway routing orchestration through adapters and recording collaboration facts."""
import hashlib
from uuid import uuid4
from researchd.collaboration.contracts import AgentInvocationRequest, AgentInvocationResult, Delegation
from researchd.collaboration.delegation import DelegationService
from researchd.collaboration.invocation import InvocationService
from researchd.agents.cloud_lead import CloudLeadResult
from researchd.agents.schemas import PlanProposal
from researchd.domain.review import ReviewDecision
from researchd.domain.enums import DelegationPurpose, InvocationStatus
from researchd.domain.ids import AgentId, AgentRuntimeId, DelegationId, InvocationId
from researchd.context.builder import CloudContextSelection
from researchd.collaboration.adapters import CloudLeadAgentAdapter, LocalExecutorAgentAdapter
from researchd.executor.contracts import ExecutorResult
from researchd.storage.models import AttemptRecord, WorkOrderRecord


class CollaborationGateway:
    def __init__(self, cloud: CloudLeadAgentAdapter, executor: LocalExecutorAgentAdapter, *,
                 delegations: DelegationService | None = None, invocations: InvocationService | None = None,
                 agent_id: AgentId | None = None, runtime_id: AgentRuntimeId | None = None) -> None:
        self.cloud = cloud
        self.executor = executor
        self.delegations, self.invocations = delegations, invocations
        self.agent_id, self.runtime_id = agent_id, runtime_id

    @property
    def tracking_enabled(self) -> bool:
        return all(value is not None for value in (self.delegations, self.invocations, self.agent_id, self.runtime_id))

    def _start(self, run_id: str, purpose: DelegationPurpose, *, work_order_id: str | None = None) -> tuple[DelegationId, InvocationId] | None:
        if not self.tracking_enabled:
            return None
        delegation_id, invocation_id = DelegationId(f"del_{uuid4().hex}"), InvocationId(f"inv_{uuid4().hex}")
        assert self.delegations is not None and self.invocations is not None and self.agent_id is not None and self.runtime_id is not None
        self.delegations.create(Delegation(delegation_id=delegation_id, run_id=run_id, work_order_id=work_order_id, purpose=purpose, idempotency_key=f"{delegation_id}-orchestration"))
        self.delegations.assign(str(delegation_id), agent_id=str(self.agent_id), runtime_id=str(self.runtime_id))
        self.invocations.start(AgentInvocationRequest(invocation_id=invocation_id, delegation_id=delegation_id, run_id=run_id, work_order_id=work_order_id, agent_id=self.agent_id, runtime_id=self.runtime_id, purpose=purpose, input_sha256=hashlib.sha256(f"{run_id}:{purpose.value}:{work_order_id}".encode()).hexdigest()))
        return delegation_id, invocation_id

    def _finish(self, invocation_id: InvocationId | None, *, success: bool, output_type: str | None = None, output: dict[str, object] | None = None, reason: str | None = None) -> None:
        if invocation_id is not None:
            assert self.invocations is not None
            self.invocations.complete(AgentInvocationResult(invocation_id=invocation_id, status=InvocationStatus.SUCCEEDED if success else InvocationStatus.FAILED, output_type=output_type, output=output, reason_code=reason))

    async def plan(self, selection: CloudContextSelection) -> CloudLeadResult[PlanProposal]:
        tracking = self._start(selection.run_id, DelegationPurpose.PLAN)
        try:
            result = await self.cloud.plan(selection)
        except Exception as error:
            self._finish(tracking[1] if tracking else None, success=False, reason=type(error).__name__)
            raise
        self._finish(tracking[1] if tracking else None, success=True, output_type=type(result.output).__name__, output=result.output.model_dump(mode="json"))
        return result

    async def review(self, selection: CloudContextSelection) -> CloudLeadResult[ReviewDecision]:
        tracking = self._start(selection.run_id, DelegationPurpose.REVIEW, work_order_id=selection.work_order_id)
        try:
            result = await self.cloud.review(selection)
        except Exception as error:
            self._finish(tracking[1] if tracking else None, success=False, reason=type(error).__name__)
            raise
        self._finish(tracking[1] if tracking else None, success=True, output_type=type(result.output).__name__, output=result.output.model_dump(mode="json"))
        return result

    async def execute(self, work_order: WorkOrderRecord, attempt: AttemptRecord) -> ExecutorResult:
        tracking = self._start(work_order.run_id, DelegationPurpose.EXECUTE, work_order_id=work_order.work_order_id)
        try:
            result = await self.executor.execute(work_order, attempt)
        except Exception as error:
            self._finish(tracking[1] if tracking else None, success=False, reason=type(error).__name__)
            raise
        self._finish(tracking[1] if tracking else None, success=result.status == "execution_complete", output_type="ExecutorResult", output=result.model_dump(mode="json"), reason=None if result.status == "execution_complete" else result.status)
        return result

    async def cancel(self, attempt_id: str) -> None:
        await self.executor.cancel(attempt_id)
