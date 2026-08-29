"""Gateway routing orchestration through adapters and recording collaboration facts."""
import hashlib
from uuid import uuid4
from researchd.collaboration.contracts import AgentInvocationRequest, AgentInvocationResult, Delegation, InvocationInput, PlanInvocationInput, ReviewInvocationInput
from researchd.collaboration.delegation import DelegationService
from researchd.collaboration.invocation import InvocationService
from researchd.collaboration.selector import AgentSelector
from researchd.collaboration.runtime import AgentAdapter, AgentAdapterCatalog
from researchd.agents.cloud_lead import CloudLeadResult
from researchd.agents.schemas import PlanProposal
from researchd.domain.review import ReviewDecision
from researchd.domain.enums import DelegationPurpose, InvocationStatus
from researchd.domain.ids import AgentId, AgentRuntimeId, DelegationId, InvocationId
from researchd.context.builder import CloudContextSelection
from researchd.collaboration.adapters import CloudLeadAgentAdapter, LocalExecutorAgentAdapter
from researchd.executor.contracts import ExecutorResult
from researchd.storage.models import AttemptRecord, WorkOrderRecord
from researchd.storage.models import DelegationRecord, AgentInvocationRecord
from sqlalchemy import select


class CollaborationGateway:
    def __init__(self, cloud: CloudLeadAgentAdapter, executor: LocalExecutorAgentAdapter, *,
                 delegations: DelegationService | None = None, invocations: InvocationService | None = None,
                 agent_id: AgentId | None = None, runtime_id: AgentRuntimeId | None = None,
                 selector: AgentSelector | None = None, catalog: AgentAdapterCatalog | None = None) -> None:
        self.cloud = cloud
        self.executor = executor
        self.delegations, self.invocations = delegations, invocations
        self.agent_id, self.runtime_id = agent_id, runtime_id
        self.selector = selector
        self.catalog = catalog

    @property
    def tracking_enabled(self) -> bool:
        return self.delegations is not None and self.invocations is not None and ((self.agent_id is not None and self.runtime_id is not None) or self.selector is not None)

    def _start(self, run_id: str, purpose: DelegationPurpose, *, work_order_id: str | None = None, attempt_id: str | None = None, required_roles: tuple[str, ...] = (), required_skills: tuple[str, ...] = (), existing_delegation_id: str | None = None, typed_input: InvocationInput | None = None) -> tuple[DelegationId, InvocationId] | None:
        if not self.tracking_enabled:
            return None
        delegation_id = DelegationId(existing_delegation_id) if existing_delegation_id else DelegationId(f"del_{uuid4().hex}")
        invocation_id = InvocationId(f"inv_{uuid4().hex}")
        assert self.delegations is not None and self.invocations is not None
        if existing_delegation_id is not None:
            with self.delegations.sessions() as session:
                assigned = session.get(DelegationRecord, existing_delegation_id)
            if assigned is None or assigned.assigned_agent_id is None or assigned.assigned_runtime_id is None:
                raise ValueError("delegation is not assigned")
            agent_id, runtime_id = AgentId(assigned.assigned_agent_id), AgentRuntimeId(assigned.assigned_runtime_id)
        elif self.selector is not None and (self.agent_id is None or self.runtime_id is None):
            selected = self.selector.select(required_roles=required_roles, required_skills=required_skills)
            if selected is None:
                raise ValueError("no eligible Agent runtime for delegation")
            agent_id, runtime_id = selected.agent_id, selected.runtime_id
        else:
            assert self.agent_id is not None and self.runtime_id is not None
            agent_id, runtime_id = self.agent_id, self.runtime_id
        endpoint_ref = None
        if self.catalog is not None:
            runtime, _ = self.catalog.resolve(str(runtime_id))
            endpoint_ref = runtime.endpoint_ref
        if existing_delegation_id is None:
            self.delegations.create(Delegation(delegation_id=delegation_id, run_id=run_id, work_order_id=work_order_id, purpose=purpose, idempotency_key=f"{delegation_id}-orchestration"))
            self.delegations.assign(str(delegation_id), agent_id=str(agent_id), runtime_id=str(runtime_id))
        self.invocations.start(AgentInvocationRequest(invocation_id=invocation_id, delegation_id=delegation_id, run_id=run_id, work_order_id=work_order_id, attempt_id=attempt_id, agent_id=agent_id, runtime_id=runtime_id, purpose=purpose, input_sha256=hashlib.sha256(f"{run_id}:{purpose.value}:{work_order_id}:{attempt_id}".encode()).hexdigest(), endpoint_ref=endpoint_ref, typed_input=typed_input))
        return delegation_id, invocation_id

    def prepare_execution(self, work_order: WorkOrderRecord) -> str | None:
        tracking = self._start(work_order.run_id, DelegationPurpose.EXECUTE, work_order_id=work_order.work_order_id, required_roles=("executor",))
        if tracking is None:
            return None
        assert self.invocations is not None
        # Preparation must not leave a synthetic Invocation running; remove the
        # provisional record and let execute() create the real invocation.
        with self.invocations.sessions.begin() as session:
            row = session.get(AgentInvocationRecord, str(tracking[1]))
            if row is not None:
                session.delete(row)
        return str(tracking[0])

    def _finish(self, invocation_id: InvocationId | None, *, success: bool, output_type: str | None = None, output: dict[str, object] | None = None, reason: str | None = None) -> None:
        if invocation_id is not None:
            assert self.invocations is not None
            self.invocations.complete(AgentInvocationResult(invocation_id=invocation_id, status=InvocationStatus.SUCCEEDED if success else InvocationStatus.FAILED, output_type=output_type, output=output, reason_code=reason))

    def _tracked_adapter(self, tracking: tuple[DelegationId, InvocationId] | None) -> AgentAdapter | None:
        if tracking is None or self.catalog is None or self.invocations is None:
            return None
        with self.invocations.sessions() as session:
            row = session.get(AgentInvocationRecord, str(tracking[1]))
        if row is None:
            raise ValueError("tracked invocation disappeared")
        _, adapter = self.catalog.resolve(row.runtime_id)
        return adapter

    async def plan(self, selection: CloudContextSelection) -> CloudLeadResult[PlanProposal]:
        tracking = self._start(selection.run_id, DelegationPurpose.PLAN, required_roles=("planner",), typed_input=PlanInvocationInput(context=selection))
        try:
            adapter = self._tracked_adapter(tracking)
            if adapter is None:
                result = await self.cloud.plan(selection)
            elif isinstance(adapter, CloudLeadAgentAdapter):
                result = await adapter.plan(selection)
            else:
                raise ValueError("assigned runtime adapter does not support PLAN")
        except Exception as error:
            self._finish(tracking[1] if tracking else None, success=False, reason=type(error).__name__)
            raise
        self._finish(tracking[1] if tracking else None, success=True, output_type=type(result.output).__name__, output=result.output.model_dump(mode="json"))
        return result

    async def review(self, selection: CloudContextSelection) -> CloudLeadResult[ReviewDecision]:
        tracking = self._start(selection.run_id, DelegationPurpose.REVIEW, work_order_id=selection.work_order_id, required_roles=("reviewer",), typed_input=ReviewInvocationInput(context=selection))
        try:
            adapter = self._tracked_adapter(tracking)
            if adapter is None:
                result = await self.cloud.review(selection)
            elif isinstance(adapter, CloudLeadAgentAdapter):
                result = await adapter.review(selection)
            else:
                raise ValueError("assigned runtime adapter does not support REVIEW")
        except Exception as error:
            self._finish(tracking[1] if tracking else None, success=False, reason=type(error).__name__)
            raise
        self._finish(tracking[1] if tracking else None, success=True, output_type=type(result.output).__name__, output=result.output.model_dump(mode="json"))
        return result

    async def execute(self, work_order: WorkOrderRecord, attempt: AttemptRecord) -> ExecutorResult:
        tracking = self._start(work_order.run_id, DelegationPurpose.EXECUTE, work_order_id=work_order.work_order_id, attempt_id=attempt.attempt_id, required_roles=("executor",), existing_delegation_id=attempt.delegation_id)
        try:
            adapter = self._tracked_adapter(tracking)
            if adapter is None:
                result = await self.executor.execute(work_order, attempt)
            elif isinstance(adapter, LocalExecutorAgentAdapter):
                result = await adapter.execute(work_order, attempt)
            else:
                raise ValueError("assigned runtime adapter does not support EXECUTE")
        except Exception as error:
            self._finish(tracking[1] if tracking else None, success=False, reason=type(error).__name__)
            raise
        self._finish(tracking[1] if tracking else None, success=result.status == "execution_complete", output_type="ExecutorResult", output=result.model_dump(mode="json"), reason=None if result.status == "execution_complete" else result.status)
        return result

    async def cancel(self, attempt_id: str) -> None:
        await self.executor.cancel(attempt_id)
        if self.invocations is not None:
            with self.invocations.sessions() as session:
                invocation_ids = session.scalars(select(AgentInvocationRecord.invocation_id).where(AgentInvocationRecord.attempt_id == attempt_id, AgentInvocationRecord.status == InvocationStatus.RUNNING.value)).all()
            for invocation_id in invocation_ids:
                self._finish(InvocationId(invocation_id), success=False, reason="CANCELLED")

    def assigned_agent_for(self, work_order_id: str) -> str | None:
        if self.delegations is None:
            return None
        with self.delegations.sessions() as session:
            row = session.scalar(select(DelegationRecord.assigned_agent_id).where(DelegationRecord.work_order_id == work_order_id).order_by(DelegationRecord.created_at.desc()).limit(1))
            return row

    def assigned_agent_for_run(self, run_id: str, purpose: DelegationPurpose) -> str | None:
        if self.delegations is None:
            return None
        with self.delegations.sessions() as session:
            return session.scalar(select(DelegationRecord.assigned_agent_id).where(DelegationRecord.run_id == run_id, DelegationRecord.purpose == purpose.value).order_by(DelegationRecord.created_at.desc()).limit(1))

    def reconcile_attempt(self, attempt_id: str, result: ExecutorResult) -> None:
        """Close a durable execution Invocation during controller recovery."""
        if self.invocations is None:
            return
        with self.invocations.sessions() as session:
            invocation_ids = session.scalars(select(AgentInvocationRecord.invocation_id).where(AgentInvocationRecord.attempt_id == attempt_id, AgentInvocationRecord.status == InvocationStatus.RUNNING.value)).all()
        for invocation_id in invocation_ids:
            self._finish(InvocationId(invocation_id), success=result.status == "execution_complete", output_type="ExecutorResult", output=result.model_dump(mode="json"), reason=None if result.status == "execution_complete" else result.status)
