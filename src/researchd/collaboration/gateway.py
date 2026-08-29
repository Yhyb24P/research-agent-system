"""Gateway routing orchestration through adapters and recording collaboration facts."""
import hashlib
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from uuid import uuid4
from researchd.collaboration.contracts import AgentInvocationRequest, AgentInvocationResult, Delegation, ExecuteInvocationInput, InvocationInput, PlanInvocationInput, ResearchCriticResult, ReviewInvocationInput, SpecialistInvocationInput
from researchd.collaboration.delegation import DelegationService
from researchd.collaboration.invocation import InvocationService
from researchd.collaboration.selector import AgentSelector
from researchd.collaboration.runtime import AgentAdapter, AgentAdapterCatalog
from researchd.agents.cloud_lead import CloudLeadResult
from researchd.models.cloud import CloudCostMetadata
from researchd.agents.schemas import PlanProposal
from researchd.domain.review import ReviewDecision
from researchd.domain.enums import Capability, DelegationPurpose, InvocationStatus, NetworkMode
from researchd.domain.enums import AgentTrustZone
from researchd.domain.ids import AgentId, AgentRuntimeId, DelegationId, InvocationId
from researchd.context.builder import CloudContextSelection
from researchd.context.agent_context import AgentContextBuilder, AgentContextBundle, AgentContextSelection
from researchd.collaboration.adapters import CloudLeadAgentAdapter, LocalExecutorAgentAdapter
from researchd.executor.contracts import ExecutorResult, GrantedWorkOrder, SandboxSpec
from researchd.storage.models import AttemptRecord, WorkOrderRecord
from researchd.storage.models import (
    AgentInvocationRecord,
    AgentRecord,
    DelegationRecord,
    WorkspaceGrantRecord,
    WorkspaceTransportRecord,
)
from researchd.workspace.contracts import WorkspaceAccessMode, WorkspaceGrantBinding, WorkspaceTransportKind
from sqlalchemy import select


class CollaborationGateway:
    def __init__(self, cloud: CloudLeadAgentAdapter | None = None, executor: LocalExecutorAgentAdapter | None = None, *,
                 delegations: DelegationService | None = None, invocations: InvocationService | None = None,
                 agent_id: AgentId | None = None, runtime_id: AgentRuntimeId | None = None,
                 selector: AgentSelector | None = None, catalog: AgentAdapterCatalog | None = None,
                 context_builder: AgentContextBuilder | None = None) -> None:
        self.cloud = cloud
        self.executor = executor
        self.delegations, self.invocations = delegations, invocations
        self.agent_id, self.runtime_id = agent_id, runtime_id
        self.selector = selector
        self.catalog = catalog
        self.context_builder = context_builder
        self._context_bundles: dict[str, AgentContextBundle] = {}

    def _canonical_request(self, tracking: tuple[DelegationId, InvocationId], typed_input: InvocationInput) -> AgentInvocationRequest:
        """Reconstruct the typed request after durable tracking has started."""
        if self.invocations is None or self.catalog is None:
            raise ValueError("canonical adapter routing requires invocation catalog")
        with self.invocations.sessions() as session:
            row = session.get(AgentInvocationRecord, str(tracking[1]))
        if row is None:
            raise ValueError("tracked invocation disappeared")
        runtime, _ = self.catalog.resolve(row.runtime_id)
        context_bundle = AgentContextBundle.model_validate(row.context_bundle_json) if row.context_bundle_json is not None else self._context_bundles.get(str(tracking[1]))
        if row.context_bundle_sha256 is not None and (context_bundle is None or context_bundle.bundle_sha256 != row.context_bundle_sha256):
            raise ValueError("persisted context bundle checksum mismatch")
        return AgentInvocationRequest(
            invocation_id=InvocationId(row.invocation_id), delegation_id=DelegationId(row.delegation_id),
            run_id=row.run_id, work_order_id=row.work_order_id, attempt_id=row.attempt_id,
            workspace_grant_id=row.workspace_grant_id,
            agent_id=AgentId(row.agent_id), runtime_id=AgentRuntimeId(row.runtime_id),
            purpose=DelegationPurpose(row.purpose), input_sha256=row.input_sha256,
            endpoint_ref=runtime.endpoint_ref,
            context_bundle=context_bundle, typed_input=typed_input,
        )

    async def _invoke_business(self, tracking: tuple[DelegationId, InvocationId], typed_input: InvocationInput, output_type: type[PlanProposal] | type[ReviewDecision]) -> CloudLeadResult[Any]:
        adapter = self._tracked_adapter(tracking)
        if adapter is None:
            raise ValueError("canonical adapter is unavailable")
        response = await adapter.invoke(self._canonical_request(tracking, typed_input))
        if response.status is not InvocationStatus.SUCCEEDED or response.output is None:
            raise ValueError(response.reason_code or "agent invocation failed")
        output = output_type.model_validate(response.output)
        return CloudLeadResult(
            output=output, interaction_id=str(tracking[1]),
            cost=CloudCostMetadata(attempts=1, prompt_tokens=0, completion_tokens=0, total_tokens=0, cost_usd=Decimal("0")),
        )

    def _typed_execute_input(self, work_order: WorkOrderRecord, attempt_id: str) -> ExecuteInvocationInput:
        """Build a host-path-free execution contract from the trusted WorkOrder."""
        contract = work_order.contract
        constraints = contract.get("constraints", {})
        network = NetworkMode(constraints.get("network", NetworkMode.NONE.value))
        requested = frozenset(Capability(value) for value in contract.get("requested_capabilities", ()))
        return ExecuteInvocationInput(work_order=GrantedWorkOrder(
            attempt_id=attempt_id,
            objective=work_order.objective,
            granted_capabilities=requested,
            sandbox=SandboxSpec(attempt_id=attempt_id, workspace="/workspace", network=network),
            workspace_grant=self._workspace_binding(attempt_id),
        ))

    def _workspace_binding(self, attempt_id: str) -> WorkspaceGrantBinding | None:
        if self.invocations is None:
            return None
        with self.invocations.sessions() as session:
            attempt = session.get(AttemptRecord, attempt_id)
            if attempt is None or attempt.delegation_id is None:
                return None
            grant = session.scalar(select(WorkspaceGrantRecord).where(
                WorkspaceGrantRecord.delegation_id == attempt.delegation_id
            ))
            if grant is None:
                return None
            if (
                grant.state != "ACTIVE"
                or grant.lease_expires_at is None
                or grant.lease_expires_at <= datetime.now(UTC)
                or grant.source_manifest_sha256 is None
            ):
                raise ValueError("execution workspace grant is not active and valid")
            transport = session.scalar(select(WorkspaceTransportRecord).where(
                WorkspaceTransportRecord.workspace_grant_id == grant.workspace_grant_id,
                WorkspaceTransportRecord.state == "ACTIVE",
            ).order_by(WorkspaceTransportRecord.created_at.desc()).limit(1))
            if transport is None:
                raise ValueError("execution workspace transport is missing")
            return WorkspaceGrantBinding(
                workspace_grant_id=grant.workspace_grant_id,
                transport_kind=WorkspaceTransportKind(grant.transport_kind),
                remote_workspace_handle=transport.remote_workspace_handle,
                access_mode=WorkspaceAccessMode(grant.access_mode),
                allowed_paths=tuple(grant.allowed_paths),
                excluded_paths=tuple(grant.excluded_paths),
                source_manifest_sha256=grant.source_manifest_sha256,
                lease_expires_at=grant.lease_expires_at,
            )

    @property
    def tracking_enabled(self) -> bool:
        return self.delegations is not None and self.invocations is not None and ((self.agent_id is not None and self.runtime_id is not None) or self.selector is not None)

    def _start(self, run_id: str, purpose: DelegationPurpose, *, work_order_id: str | None = None, attempt_id: str | None = None, required_roles: tuple[str, ...] = (), required_skills: tuple[str, ...] = (), required_trust_zones: tuple[AgentTrustZone, ...] = (), existing_delegation_id: str | None = None, typed_input: InvocationInput | None = None) -> tuple[DelegationId, InvocationId] | None:
        if typed_input is None:
            raise ValueError("typed invocation input is required")
        if (self.delegations is None or self.invocations is None) or (not self.tracking_enabled and existing_delegation_id is None):
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
            selected = self.selector.select(required_roles=required_roles, required_skills=required_skills, required_trust_zones=required_trust_zones)
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
            self.delegations.create(Delegation(
                delegation_id=delegation_id, run_id=run_id, work_order_id=work_order_id,
                purpose=purpose, required_roles=required_roles, required_skills=required_skills,
                required_trust_zones=required_trust_zones,
                idempotency_key=f"{delegation_id}-orchestration",
            ))
            self.delegations.assign(str(delegation_id), agent_id=str(agent_id), runtime_id=str(runtime_id))
        context_bundle = None
        if self.context_builder is not None:
            with self.delegations.sessions() as session:
                agent_row = session.get(AgentRecord, str(agent_id))
            if agent_row is None:
                raise ValueError("assigned Agent profile disappeared")
            context_bundle = self.context_builder.build(AgentContextSelection(
                target_agent_id=str(agent_id), target_runtime_id=str(runtime_id),
                target_trust_zone=AgentTrustZone(agent_row.trust_zone), purpose=purpose,
                run_id=run_id, work_order_id=work_order_id,
            ))
        workspace_grant_id = (
            typed_input.work_order.workspace_grant.workspace_grant_id
            if isinstance(typed_input, ExecuteInvocationInput)
            and typed_input.work_order.workspace_grant is not None
            else None
        )
        self.invocations.start(AgentInvocationRequest(invocation_id=invocation_id, delegation_id=delegation_id, run_id=run_id, work_order_id=work_order_id, attempt_id=attempt_id, workspace_grant_id=workspace_grant_id, agent_id=agent_id, runtime_id=runtime_id, purpose=purpose, input_sha256=hashlib.sha256(f"{run_id}:{purpose.value}:{work_order_id}:{attempt_id}".encode()).hexdigest(), endpoint_ref=endpoint_ref, context_bundle=context_bundle, typed_input=typed_input))
        if context_bundle is not None:
            self._context_bundles[str(invocation_id)] = context_bundle
        return delegation_id, invocation_id

    def prepare_execution(self, work_order: WorkOrderRecord) -> str | None:
        """Assign execution ownership before the immutable Attempt is created."""
        if not self.tracking_enabled or self.delegations is None:
            return None
        if self.selector is not None and (self.agent_id is None or self.runtime_id is None):
            selected = self.selector.select(required_roles=("executor",))
            if selected is None:
                raise ValueError("no eligible Agent runtime for delegation")
            agent_id, runtime_id = selected.agent_id, selected.runtime_id
        else:
            assert self.agent_id is not None and self.runtime_id is not None
            agent_id, runtime_id = self.agent_id, self.runtime_id
        delegation_id = DelegationId(f"del_{uuid4().hex}")
        self.delegations.create(Delegation(
            delegation_id=delegation_id, run_id=work_order.run_id,
            work_order_id=work_order.work_order_id, purpose=DelegationPurpose.EXECUTE,
            required_roles=("executor",), idempotency_key=f"{delegation_id}-orchestration",
        ))
        self.delegations.assign(str(delegation_id), agent_id=str(agent_id), runtime_id=str(runtime_id))
        return str(delegation_id)

    def _finish(self, invocation_id: InvocationId | None, *, success: bool, output_type: str | None = None, output: dict[str, object] | None = None, reason: str | None = None) -> None:
        if invocation_id is not None:
            assert self.invocations is not None
            status = InvocationStatus.SUCCEEDED if success else (InvocationStatus.CANCELLED if reason == "CANCELLED" else InvocationStatus.FAILED)
            self.invocations.complete(AgentInvocationResult(invocation_id=invocation_id, status=status, output_type=output_type, output=output, reason_code=reason))

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
                if self.cloud is None:
                    raise ValueError("Cloud Lead adapter is not configured")
                result = await self.cloud.plan(selection)
            elif isinstance(adapter, CloudLeadAgentAdapter):
                assert tracking is not None
                result = await self._invoke_business(tracking, PlanInvocationInput(context=selection), PlanProposal)
            else:
                assert tracking is not None
                result = await self._invoke_business(tracking, PlanInvocationInput(context=selection), PlanProposal)
        except Exception as error:
            self._finish(tracking[1] if tracking else None, success=False, reason=type(error).__name__)
            raise
        if tracking is not None and self.cloud is not None:
            self.cloud.bind_invocation(tracking[1], result.interaction_id)
        self._finish(tracking[1] if tracking else None, success=True, output_type=type(result.output).__name__, output=result.output.model_dump(mode="json"))
        return result

    async def review(self, selection: CloudContextSelection) -> CloudLeadResult[ReviewDecision]:
        tracking = self._start(selection.run_id, DelegationPurpose.REVIEW, work_order_id=selection.work_order_id, required_roles=("reviewer",), typed_input=ReviewInvocationInput(context=selection))
        try:
            adapter = self._tracked_adapter(tracking)
            if adapter is None:
                if self.cloud is None:
                    raise ValueError("Cloud Lead adapter is not configured")
                result = await self.cloud.review(selection)
            elif isinstance(adapter, CloudLeadAgentAdapter):
                assert tracking is not None
                result = await self._invoke_business(tracking, ReviewInvocationInput(context=selection), ReviewDecision)
            else:
                assert tracking is not None
                result = await self._invoke_business(tracking, ReviewInvocationInput(context=selection), ReviewDecision)
        except Exception as error:
            self._finish(tracking[1] if tracking else None, success=False, reason=type(error).__name__)
            raise
        if tracking is not None and self.cloud is not None:
            self.cloud.bind_invocation(tracking[1], result.interaction_id)
        self._finish(tracking[1] if tracking else None, success=True, output_type=type(result.output).__name__, output=result.output.model_dump(mode="json"))
        return result

    async def specialist(self, run_id: str, request: SpecialistInvocationInput) -> ResearchCriticResult:
        """Delegate a structured specialist review through the canonical Agent plane."""

        tracking = self._start(
            run_id,
            DelegationPurpose.SPECIALIST,
            required_roles=("specialist",),
            required_skills=("research.critique",),
            typed_input=request,
        )
        if tracking is None:
            raise ValueError("specialist delegation requires canonical tracking")
        try:
            adapter = self._tracked_adapter(tracking)
            if adapter is None:
                raise ValueError("specialist Agent adapter is unavailable")
            response = await adapter.invoke(self._canonical_request(tracking, request))
            if response.status is not InvocationStatus.SUCCEEDED or response.output is None:
                raise ValueError(response.reason_code or "specialist Agent invocation failed")
            result = ResearchCriticResult.model_validate(response.output)
        except Exception as error:
            self._finish(tracking[1], success=False, reason=type(error).__name__)
            raise
        self._finish(
            tracking[1],
            success=True,
            output_type="ResearchCriticResult",
            output=result.model_dump(mode="json"),
        )
        return result

    async def execute(self, work_order: WorkOrderRecord, attempt: AttemptRecord) -> ExecutorResult:
        typed_input = self._typed_execute_input(work_order, attempt.attempt_id)
        tracking = self._start(work_order.run_id, DelegationPurpose.EXECUTE, work_order_id=work_order.work_order_id, attempt_id=attempt.attempt_id, required_roles=("executor",), existing_delegation_id=attempt.delegation_id, typed_input=typed_input)
        try:
            adapter = self._tracked_adapter(tracking)
            if adapter is None:
                if self.executor is None:
                    raise ValueError("executor adapter is not configured")
                result = await self.executor.execute(work_order, attempt)
            elif isinstance(adapter, LocalExecutorAgentAdapter):
                result = await adapter.execute(work_order, attempt)
            else:
                if tracking is None:
                    raise ValueError("canonical adapter is unavailable")
                response = await adapter.invoke(self._canonical_request(tracking, typed_input))
                if response.status is not InvocationStatus.SUCCEEDED or response.output is None:
                    raise ValueError(response.reason_code or "agent invocation failed")
                result = ExecutorResult.model_validate(response.output)
        except Exception as error:
            self._finish(tracking[1] if tracking else None, success=False, reason=type(error).__name__)
            raise
        self._finish(tracking[1] if tracking else None, success=result.status == "execution_complete", output_type="ExecutorResult", output=result.model_dump(mode="json"), reason=None if result.status == "execution_complete" else result.status)
        return result

    async def cancel(self, attempt_id: str) -> None:
        if self.executor is not None:
            await self.executor.cancel(attempt_id)
        if self.invocations is not None:
            with self.invocations.sessions() as session:
                invocations = session.scalars(select(AgentInvocationRecord).where(
                    AgentInvocationRecord.attempt_id == attempt_id,
                    AgentInvocationRecord.status == InvocationStatus.RUNNING.value,
                )).all()
            for invocation in invocations:
                if self.catalog is not None:
                    _, adapter = self.catalog.resolve(invocation.runtime_id)
                    await adapter.cancel(invocation.invocation_id)
                invocation_id = invocation.invocation_id
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

    def recover_run(self, run_id: str) -> tuple[str, ...]:
        """Close invocations for which restart reconciliation found no result."""
        if self.invocations is None:
            return ()
        return self.invocations.recover_run(run_id)
