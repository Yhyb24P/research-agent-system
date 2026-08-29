"""Durable, bounded controller loop joining Cloud Lead, executor and verifier."""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from researchd.agents.cloud_lead import CloudLeadAdapter, CloudLeadResult
from researchd.collaboration.gateway import CollaborationGateway
from researchd.agents.schemas import PlanProposal, WorkOrderProposal
from researchd.context.builder import CloudContextSelection
from researchd.domain.enums import (
    AttemptState, Capability, DelegationPurpose, PolicyOutcome, ResearchRunState, ReviewDecisionKind,
    VerificationOverall, WorkOrderState,
)
from researchd.domain.review import ReviewDecision
from researchd.executor.contracts import ExecutorResult
from researchd.executor.jobs import JobManager
from researchd.models.cloud import CloudBudgetExceeded, CloudProviderUnavailable, CloudSchemaInvalid
from researchd.policy.approval import ApprovalService
from researchd.policy.engine import BudgetLimits, PolicyEvaluator, PolicyRequest, RecordingPolicyEngine
from researchd.storage.models import (
    AgentInteractionRecord, AttemptRecord, AuditEventRecord, ExecutorDispatchRecord, PlanRecord,
    ResearchRunRecord, ReviewDecisionRecord, VerificationResultRecord, WorkOrderRecord,
)
from researchd.storage.repositories import utc_now
from researchd.storage.transitions import TransactionalTransitionService
from researchd.domain.verification import VerificationResult


class ExecutionDriver(Protocol):
    async def execute(self, work_order: WorkOrderRecord, attempt: AttemptRecord) -> ExecutorResult: ...
    async def cancel(self, attempt_id: str) -> None: ...


class VerificationDriver(Protocol):
    def verify(self, work_order: WorkOrderRecord, attempt: AttemptRecord, result: ExecutorResult) -> VerificationResult: ...


@dataclass(frozen=True)
class OrchestrationLimits:
    max_iterations: int = 8
    max_cloud_calls: int = 24
    max_wall_seconds: int = 86_400


@dataclass(frozen=True)
class RunSnapshot:
    run_id: str
    state: ResearchRunState
    work_orders: tuple[tuple[str, WorkOrderState], ...]
    pending_approval_ids: tuple[str, ...]
    active_attempt_ids: tuple[str, ...]


class OrchestrationError(RuntimeError):
    pass


class ResearchOrchestrator:
    """One controller instance; all transitions are persisted before side effects."""

    def __init__(
        self, sessions: sessionmaker[Session], cloud: CloudLeadAdapter | None = None,
        policy: RecordingPolicyEngine | PolicyEvaluator | None = None, executor: ExecutionDriver | None = None,
        verifier: VerificationDriver | None = None, *, collaboration: CollaborationGateway | None = None, approvals: ApprovalService | None = None,
        jobs: JobManager | None = None,
        workspace_capabilities: frozenset[Capability] = frozenset(),
        user_capabilities: frozenset[Capability] = frozenset(),
        maximum_budget: BudgetLimits = BudgetLimits(7200, 7200, 1200, 16_384, 16_384),
        limits: OrchestrationLimits = OrchestrationLimits(),
    ) -> None:
        if policy is None or verifier is None:
            raise TypeError("policy and verifier are required")
        if collaboration is None and (cloud is None or executor is None):
            raise TypeError("cloud and executor are required without collaboration")
        self.sessions = sessions
        self.cloud = cloud
        self.policy = policy
        self.executor = executor
        self.collaboration = collaboration
        self.verifier = verifier
        self.approvals = approvals
        self.jobs = jobs
        self.workspace_capabilities = workspace_capabilities
        self.user_capabilities = user_capabilities
        self.maximum_budget = maximum_budget
        self.limits = limits
        self.transitions = TransactionalTransitionService(sessions)

    def _legacy_cloud(self) -> CloudLeadAdapter:
        if self.cloud is None:
            raise OrchestrationError("Cloud Lead compatibility adapter is not configured")
        return self.cloud

    def _legacy_executor(self) -> ExecutionDriver:
        if self.executor is None:
            raise OrchestrationError("executor compatibility adapter is not configured")
        return self.executor

    def _agent_actor(self, work_order_id: str, *, fallback_type: str, fallback_id: str) -> tuple[str, str]:
        if self.collaboration is not None:
            agent_id = self.collaboration.assigned_agent_for(work_order_id)
            if agent_id is not None:
                return "agent", agent_id
            raise OrchestrationError(f"no assigned Agent for WorkOrder {work_order_id}")
        return fallback_type, fallback_id

    def create_run(self, *, workspace_id: str, objective: str, run_id: str | None = None) -> str:
        identifier = run_id or f"run_{uuid4().hex}"
        now = utc_now()
        with self.sessions.begin() as session:
            if session.get(ResearchRunRecord, identifier) is not None:
                raise OrchestrationError("run ID already exists")
            session.add(ResearchRunRecord(
                run_id=identifier, workspace_id=workspace_id, objective=objective,
                state=ResearchRunState.NEW.value, version=1, created_at=now, updated_at=now,
                max_iterations=self.limits.max_iterations, max_cloud_calls=self.limits.max_cloud_calls,
                iterations_used=0, cloud_calls_used=0, cancellation_requested=False,
            ))
            session.flush()
            session.add(AuditEventRecord(
                event_id=f"evt_{uuid4().hex}", event_type="RUN_CREATED", run_id=identifier,
                entity_type="research_run", entity_id=identifier, actor_type="controller",
                actor_id="orchestrator", timestamp=now, correlation_id=identifier,
                causation_id=None, metadata_json={"objective": objective},
            ))
        return identifier

    async def run(self, run_id: str, *, max_steps: int = 100) -> RunSnapshot:
        for _ in range(max_steps):
            progressed = await self.advance(run_id)
            snapshot = self.snapshot(run_id)
            if not progressed or snapshot.state in {
                ResearchRunState.COMPLETED, ResearchRunState.FAILED, ResearchRunState.CANCELLED,
                ResearchRunState.WAITING_HUMAN, ResearchRunState.WAITING_EXTERNAL,
            }:
                return snapshot
        raise OrchestrationError("orchestration step budget exhausted")

    async def advance(self, run_id: str) -> bool:
        run = self._run(run_id)
        state = ResearchRunState(run.state)
        if self._wall_budget_exceeded(run):
            self._transition_run(run_id, ResearchRunState.FAILED, "MAX_WALL_TIME_EXCEEDED")
            return False
        if state is ResearchRunState.NEW:
            self.transitions.transition_run(run_id, run.version, ResearchRunState.PLANNING,
                event_type="PLAN_REQUESTED", actor_type="controller", actor_id="orchestrator", correlation_id=run_id)
            return await self._plan(run_id)
        if state is ResearchRunState.PLANNING:
            return await self._plan(run_id)
        if state is ResearchRunState.WAITING_EXTERNAL:
            if self._has_reviewing_order(run_id):
                self._transition_run(run_id, ResearchRunState.REVIEWING, "EXTERNAL_WAIT_RESUMED")
                return True
            self._transition_run(run_id, ResearchRunState.PLANNING, "EXTERNAL_WAIT_RESUMED")
            return True
        if state is ResearchRunState.WAITING_HUMAN:
            return False
        if state is ResearchRunState.REVIEWING:
            return await self._review_next(run_id)
        if state is ResearchRunState.ACTIVE:
            return await self._advance_order(run_id)
        return False

    async def _plan(self, run_id: str) -> bool:
        run = self._run(run_id)
        if not self._consume_cloud_call(run_id, run):
            return False
        try:
            result = await (self.collaboration.plan(CloudContextSelection(run_id=run_id)) if self.collaboration else self._legacy_cloud().propose_plan(CloudContextSelection(run_id=run_id)))
        except (CloudProviderUnavailable, TimeoutError) as error:
            self._transition_run(run_id, ResearchRunState.WAITING_EXTERNAL, "CLOUD_PLAN_WAITING", {"error": type(error).__name__})
            return False
        except (CloudSchemaInvalid, CloudBudgetExceeded) as error:
            self._transition_run(run_id, ResearchRunState.FAILED, "CLOUD_PLAN_FAILED", {"error": type(error).__name__})
            return False
        self._persist_plan(run_id, result)
        self._transition_run(run_id, ResearchRunState.ACTIVE, "PLAN_CREATED", {"plan_id": result.output.proposal_id})
        return True

    def _persist_plan(self, run_id: str, result: CloudLeadResult[PlanProposal]) -> None:
        now = utc_now()
        actor_type, actor_id = "controller", "orchestrator"
        if self.collaboration is not None:
            assigned = self.collaboration.assigned_agent_for_run(run_id, DelegationPurpose.PLAN)
            if assigned is None:
                raise OrchestrationError(f"no assigned planning Agent for run {run_id}")
            actor_type, actor_id = "agent", assigned
        with self.sessions.begin() as session:
            session.add(PlanRecord(
                plan_id=result.output.proposal_id, run_id=run_id,
                proposal_json=result.output.model_dump(mode="json"), version=1,
                created_at=now, updated_at=now,
            ))
            for proposal in result.output.proposed_work_orders:
                self._add_work_order(session, run_id, proposal, parent=None, reason=None)
            session.add(AuditEventRecord(
                event_id=f"evt_{uuid4().hex}", event_type="PLAN_CREATED", run_id=run_id,
                entity_type="plan", entity_id=result.output.proposal_id, actor_type=actor_type,
                actor_id=actor_id, timestamp=now, correlation_id=run_id,
                causation_id=result.interaction_id, metadata_json={"work_order_count": len(result.output.proposed_work_orders)},
            ))

    def _add_work_order(self, session: Session, run_id: str, proposal: WorkOrderProposal, *, parent: str | None, reason: str | None) -> WorkOrderRecord:
        now = utc_now()
        identifier = f"wo_{uuid4().hex}"
        contract = proposal.model_dump(mode="json")
        contract["acceptance"] = [item.model_dump(mode="json") for item in proposal.acceptance]
        record = WorkOrderRecord(
            work_order_id=identifier, run_id=run_id, parent_work_order_id=parent,
            objective=proposal.objective, state=WorkOrderState.DRAFT.value,
            idempotency_key=f"{identifier}-dispatch", contract=contract, revision_reason=reason,
            approval_id=None, approval_grant_id=None, version=1, created_at=now, updated_at=now,
        )
        session.add(record)
        session.flush()
        session.add(AuditEventRecord(
            event_id=f"evt_{uuid4().hex}", event_type="WORK_ORDER_CREATED", run_id=run_id,
            entity_type="work_order", entity_id=identifier, actor_type="controller",
            actor_id="orchestrator", timestamp=now, correlation_id=identifier,
            causation_id=parent, metadata_json={"parent_work_order_id": parent, "revision_reason": reason},
        ))
        return record

    async def _advance_order(self, run_id: str) -> bool:
        order = self._next_order(run_id)
        if order is None:
            self._transition_run(run_id, ResearchRunState.REVIEWING, "RUN_REVIEW_READY")
            return True
        state = WorkOrderState(order.state)
        if state is WorkOrderState.DRAFT:
            self.transitions.transition_work_order(order.work_order_id, order.version, WorkOrderState.POLICY_CHECK,
                event_type="POLICY_EVALUATION_REQUESTED", actor_type="controller", actor_id="orchestrator", correlation_id=order.work_order_id)
            return True
        if state is WorkOrderState.POLICY_CHECK:
            return self._evaluate_policy(order)
        if state is WorkOrderState.WAITING_APPROVAL:
            return False
        if state is WorkOrderState.READY:
            return await self._dispatch(order)
        if state is WorkOrderState.EXECUTING:
            return await self._execute_or_resume(order)
        if state is WorkOrderState.EXECUTION_FAILED:
            refreshed = self._order(order.work_order_id)
            self.transitions.transition_work_order(refreshed.work_order_id, refreshed.version, WorkOrderState.REVISION_REQUIRED,
                event_type="REVISION_REQUIRED", actor_type="controller", actor_id="orchestrator", correlation_id=refreshed.work_order_id)
            self._create_revision(self._order(refreshed.work_order_id), "execution failed")
            return True
        if state is WorkOrderState.VERIFYING:
            return self._verify(order)
        if state is WorkOrderState.VERIFICATION_FAILED:
            refreshed = self._order(order.work_order_id)
            self.transitions.transition_work_order(refreshed.work_order_id, refreshed.version, WorkOrderState.REVISION_REQUIRED,
                event_type="REVISION_REQUIRED", actor_type="controller", actor_id="orchestrator", correlation_id=refreshed.work_order_id)
            self._create_revision(self._order(refreshed.work_order_id), "verification failed")
            return True
        if state is WorkOrderState.REVIEW_READY:
            self._transition_run(run_id, ResearchRunState.REVIEWING, "REVIEW_REQUESTED")
            self.transitions.transition_work_order(order.work_order_id, order.version, WorkOrderState.REVIEWING,
                event_type="REVIEW_REQUESTED", actor_type="controller", actor_id="orchestrator", correlation_id=order.work_order_id)
            return True
        if state is WorkOrderState.REVISION_REQUIRED:
            return self._create_revision(order, "revision requested")
        return False

    def _evaluate_policy(self, order: WorkOrderRecord) -> bool:
        contract = order.contract
        requested = frozenset(Capability(value) for value in contract.get("requested_capabilities", []))
        budget_json = contract.get("budget", {})
        requested_budget = BudgetLimits(
            int(budget_json.get("max_wall_seconds") or 0), int(budget_json.get("max_cpu_seconds") or 0),
            int(budget_json.get("max_gpu_seconds") or 0), int(budget_json.get("max_disk_mb") or 0),
            int(budget_json.get("max_output_mb") or 0),
        )
        approved: frozenset[Capability] = frozenset()
        if order.approval_grant_id and self.approvals is not None:
            approved = requested
        request = PolicyRequest(
            requested_capabilities=requested, workspace_capabilities=self.workspace_capabilities,
            user_capabilities=self.user_capabilities, approved_capabilities=approved,
            requested_budget=requested_budget, maximum_budget=self.maximum_budget,
            data_classification=contract.get("data_policy", {}).get("default_classification", "LOCAL_ONLY"),
        )
        if isinstance(self.policy, RecordingPolicyEngine):
            decision = self.policy.evaluate_and_record(order.run_id, order.work_order_id, request)
        else:
            decision = self.policy.evaluate(request)
        if decision.outcome is PolicyOutcome.ALLOW:
            self.transitions.transition_work_order(order.work_order_id, order.version, WorkOrderState.READY,
                event_type="POLICY_EVALUATED", actor_type="policy", actor_id="deterministic-policy", correlation_id=order.work_order_id,
                metadata={"outcome": decision.outcome.value, "reason_codes": list(decision.reason_codes)})
            return True
        if decision.outcome is PolicyOutcome.APPROVAL_REQUIRED and self.approvals is not None:
            requester_type, requester_id = self._agent_actor(order.work_order_id, fallback_type="cloud_lead", fallback_id="cloud-lead")
            approval = self.approvals.request(
                operation_type="work_order.capabilities", parameters={"work_order_id": order.work_order_id, "capabilities": sorted(requested)},
                requested_by=requester_id, reason=order.objective, risk_level="elevated", resource_scope={"run_id": order.run_id},
                budget_delta=budget_json, expires_at=datetime.now(UTC) + timedelta(hours=1),
                run_id=order.run_id, work_order_id=order.work_order_id,
                requester_actor_type=requester_type,
                requester_actor_id=requester_id,
            )
            with self.sessions.begin() as session:
                current = session.get(WorkOrderRecord, order.work_order_id)
                assert current is not None
                current.approval_id = approval.approval_id
                current.version += 1
                current.updated_at = utc_now()
            refreshed = self._order(order.work_order_id)
            self.transitions.transition_work_order(refreshed.work_order_id, refreshed.version, WorkOrderState.WAITING_APPROVAL,
                event_type="APPROVAL_REQUESTED", actor_type="policy", actor_id="deterministic-policy", correlation_id=refreshed.work_order_id,
                metadata={"approval_id": approval.approval_id})
            self._transition_run(order.run_id, ResearchRunState.WAITING_HUMAN, "APPROVAL_REQUESTED")
            return True
        self.transitions.transition_work_order(order.work_order_id, order.version, WorkOrderState.FAILED,
            event_type="POLICY_DENIED", actor_type="policy", actor_id="deterministic-policy", correlation_id=order.work_order_id,
            metadata={"reason_codes": list(decision.reason_codes)})
        self._transition_run(order.run_id, ResearchRunState.FAILED, "POLICY_DENIED")
        return False

    async def approve(self, work_order_id: str, grant_id: str) -> bool:
        order = self._order(work_order_id)
        if WorkOrderState(order.state) is not WorkOrderState.WAITING_APPROVAL or self.approvals is None or order.approval_id is None:
            raise OrchestrationError("work order is not awaiting approval")
        self.approvals.authorize(grant_id, operation_type="work_order.capabilities", parameters={"work_order_id": work_order_id, "capabilities": sorted(order.contract.get("requested_capabilities", []))})
        with self.sessions.begin() as session:
            current = session.get(WorkOrderRecord, work_order_id)
            assert current is not None
            current.approval_grant_id = grant_id
            current.version += 1
            current.updated_at = utc_now()
        refreshed = self._order(work_order_id)
        self.transitions.transition_work_order(work_order_id, refreshed.version, WorkOrderState.POLICY_CHECK,
            event_type="APPROVAL_GRANTED", actor_type="human", actor_id="approval", correlation_id=work_order_id,
            metadata={"grant_id": grant_id})
        self._transition_run(order.run_id, ResearchRunState.ACTIVE, "APPROVAL_RESUMED")
        return True

    async def _dispatch(self, order: WorkOrderRecord) -> bool:
        run = self._run(order.run_id)
        if run.cancellation_requested:
            self._cancel_order(order)
            return True
        if run.iterations_used >= run.max_iterations:
            self._transition_run(order.run_id, ResearchRunState.FAILED, "MAX_ITERATIONS_EXCEEDED")
            return False
        self._increment_iteration(order.run_id)
        self.transitions.transition_work_order(order.work_order_id, order.version, WorkOrderState.DISPATCHED,
            event_type="WORK_ORDER_DISPATCHED", actor_type="controller", actor_id="orchestrator", correlation_id=order.work_order_id)
        now = utc_now()
        attempt_id = f"att_{uuid4().hex}"
        delegation_id = self.collaboration.prepare_execution(order) if self.collaboration is not None else None
        with self.sessions.begin() as session:
            session.add(AttemptRecord(attempt_id=attempt_id, work_order_id=order.work_order_id, delegation_id=delegation_id, state=AttemptState.CREATED.value,
                terminal_at=None, version=1, created_at=now, updated_at=now))
            session.add(AuditEventRecord(event_id=f"evt_{uuid4().hex}", event_type="ATTEMPT_CREATED", run_id=order.run_id,
                entity_type="attempt", entity_id=attempt_id, actor_type="controller", actor_id="orchestrator",
                timestamp=now, correlation_id=order.work_order_id, causation_id=None, metadata_json={}))
        self.transitions.transition_attempt(attempt_id, 1, AttemptState.PREPARING, event_type="ATTEMPT_PREPARING",
            actor_type="controller", actor_id="orchestrator", correlation_id=attempt_id)
        self.transitions.transition_attempt(attempt_id, 2, AttemptState.RUNNING, event_type="ATTEMPT_RUNNING",
            actor_type="controller", actor_id="orchestrator", correlation_id=attempt_id)
        refreshed = self._order(order.work_order_id)
        self.transitions.transition_work_order(refreshed.work_order_id, refreshed.version, WorkOrderState.EXECUTING,
            event_type="EXECUTION_STARTED", actor_type="controller", actor_id="orchestrator", correlation_id=attempt_id,
            metadata={"attempt_id": attempt_id})
        return True

    def retry_attempt(self, work_order_id: str) -> str:
        """Retry an unchanged WorkOrder with a new immutable Attempt."""
        order = self._order(work_order_id)
        if WorkOrderState(order.state) is not WorkOrderState.EXECUTION_FAILED:
            raise OrchestrationError("only an execution-failed WorkOrder can be retried")
        run = self._run(order.run_id)
        if run.iterations_used >= run.max_iterations:
            self._transition_run(order.run_id, ResearchRunState.FAILED, "MAX_ITERATIONS_EXCEEDED")
            raise OrchestrationError("maximum iterations exceeded")
        self._increment_iteration(order.run_id)
        self.transitions.transition_work_order(order.work_order_id, order.version, WorkOrderState.EXECUTING,
            event_type="ATTEMPT_RETRY_REQUESTED", actor_type="controller", actor_id="orchestrator", correlation_id=order.work_order_id)
        now = utc_now()
        attempt_id = f"att_{uuid4().hex}"
        delegation_id = self.collaboration.prepare_execution(order) if self.collaboration is not None else None
        with self.sessions.begin() as session:
            session.add(AttemptRecord(attempt_id=attempt_id, work_order_id=order.work_order_id, delegation_id=delegation_id,
                state=AttemptState.CREATED.value, terminal_at=None, version=1, created_at=now, updated_at=now))
            session.add(AuditEventRecord(event_id=f"evt_{uuid4().hex}", event_type="ATTEMPT_CREATED",
                run_id=order.run_id, entity_type="attempt", entity_id=attempt_id, actor_type="controller",
                actor_id="orchestrator", timestamp=now, correlation_id=order.work_order_id,
                causation_id=None, metadata_json={"retry": True, "work_order_id": order.work_order_id}))
        self.transitions.transition_attempt(attempt_id, 1, AttemptState.PREPARING, event_type="ATTEMPT_PREPARING",
            actor_type="controller", actor_id="orchestrator", correlation_id=attempt_id)
        self.transitions.transition_attempt(attempt_id, 2, AttemptState.RUNNING, event_type="ATTEMPT_RUNNING",
            actor_type="controller", actor_id="orchestrator", correlation_id=attempt_id)
        return attempt_id

    async def _execute_or_resume(self, order: WorkOrderRecord) -> bool:
        attempt = self._latest_attempt(order.work_order_id)
        if attempt is None:
            raise OrchestrationError("EXECUTING WorkOrder has no Attempt")
        result = self._stored_execution_result(attempt.attempt_id)
        if result is None:
            result = await (self.collaboration.execute(order, attempt) if self.collaboration else self._legacy_executor().execute(order, attempt))
            self._store_execution_result(attempt.attempt_id, result)
        if result.status != "execution_complete":
            actor_type, actor_id = self._agent_actor(order.work_order_id, fallback_type="executor", fallback_id="local-executor")
            self.transitions.transition_attempt(attempt.attempt_id, attempt.version, AttemptState.FAILED,
                event_type="EXECUTION_FAILED", actor_type=actor_type, actor_id=actor_id, correlation_id=attempt.attempt_id)
            refreshed = self._order(order.work_order_id)
            self.transitions.transition_work_order(refreshed.work_order_id, refreshed.version, WorkOrderState.EXECUTION_FAILED,
                event_type="EXECUTION_FAILED", actor_type="controller", actor_id="orchestrator", correlation_id=attempt.attempt_id)
            return True
        self.transitions.transition_attempt(attempt.attempt_id, attempt.version, AttemptState.VERIFYING,
            event_type="VERIFICATION_STARTED", actor_type="controller", actor_id="orchestrator", correlation_id=attempt.attempt_id)
        refreshed = self._order(order.work_order_id)
        self.transitions.transition_work_order(refreshed.work_order_id, refreshed.version, WorkOrderState.VERIFYING,
            event_type="VERIFICATION_STARTED", actor_type="controller", actor_id="orchestrator", correlation_id=attempt.attempt_id)
        return True

    def _verify(self, order: WorkOrderRecord) -> bool:
        attempt = self._latest_attempt(order.work_order_id)
        if attempt is None:
            raise OrchestrationError("VERIFYING WorkOrder has no Attempt")
        result_json = self._stored_execution_result(attempt.attempt_id)
        if result_json is None:
            raise OrchestrationError("verification has no executor result")
        verification = self.verifier.verify(order, attempt, result_json)
        current_attempt = self._attempt(attempt.attempt_id)
        target_attempt = AttemptState.SUCCEEDED if verification.overall is VerificationOverall.PASS else AttemptState.FAILED
        self.transitions.transition_attempt(current_attempt.attempt_id, current_attempt.version, target_attempt,
            event_type="VERIFICATION_COMPLETED", actor_type="verifier", actor_id="verifier-v1", correlation_id=current_attempt.attempt_id)
        current = self._order(order.work_order_id)
        if verification.overall is VerificationOverall.PASS:
            self.transitions.transition_work_order(current.work_order_id, current.version, WorkOrderState.REVIEW_READY,
                event_type="REVIEW_READY", actor_type="controller", actor_id="orchestrator", correlation_id=current_attempt.attempt_id)
        else:
            self.transitions.transition_work_order(current.work_order_id, current.version, WorkOrderState.VERIFICATION_FAILED,
                event_type="VERIFICATION_FAILED", actor_type="verifier", actor_id="verifier-v1", correlation_id=current_attempt.attempt_id)
        return True

    async def _review_next(self, run_id: str) -> bool:
        order = self._review_order(run_id)
        if order is None:
            if self._has_revision(run_id):
                self._transition_run(run_id, ResearchRunState.ACTIVE, "REVISION_RESUMED")
                return True
            self._transition_run(run_id, ResearchRunState.COMPLETED, "RUN_COMPLETED")
            return True
        attempt = self._latest_attempt(order.work_order_id)
        if not self._consume_cloud_call(run_id, self._run(run_id)):
            return False
        try:
            result = await (self.collaboration.review(CloudContextSelection(
                run_id=run_id, work_order_id=order.work_order_id,
                verification_id=self._latest_verification_id(order.work_order_id),
            )) if self.collaboration else self._legacy_cloud().review(CloudContextSelection(
                run_id=run_id, work_order_id=order.work_order_id,
                verification_id=self._latest_verification_id(order.work_order_id),
            )))
        except (CloudProviderUnavailable, TimeoutError) as error:
            self._transition_run(run_id, ResearchRunState.WAITING_EXTERNAL, "CLOUD_REVIEW_WAITING", {"error": type(error).__name__})
            return False
        except (CloudSchemaInvalid, CloudBudgetExceeded) as error:
            self._transition_run(run_id, ResearchRunState.FAILED, "CLOUD_REVIEW_FAILED", {"error": type(error).__name__})
            return False
        if str(result.output.work_order_id) != order.work_order_id:
            self._transition_run(run_id, ResearchRunState.FAILED, "REVIEW_SCOPE_MISMATCH")
            raise OrchestrationError("cloud review references a different WorkOrder")
        self._persist_review(run_id, order, attempt, result)
        decision = result.output.decision
        current = self._order(order.work_order_id)
        if decision is ReviewDecisionKind.ACCEPT:
            self._assert_hard_verification(order.work_order_id)
            if self._latest_verification_id(order.work_order_id) not in result.output.evidence_refs:
                self._transition_run(run_id, ResearchRunState.FAILED, "REVIEW_EVIDENCE_INCOMPLETE")
                raise OrchestrationError("review must cite the latest verification result")
            self.transitions.transition_work_order(current.work_order_id, current.version, WorkOrderState.ACCEPTED,
                event_type="WORK_ORDER_ACCEPTED", actor_type="controller", actor_id="orchestrator", correlation_id=current.work_order_id)
            return True
        actor_type, actor_id = self._agent_actor(current.work_order_id, fallback_type="cloud_lead", fallback_id="cloud-lead")
        if decision is ReviewDecisionKind.HUMAN_REQUIRED:
            self.transitions.transition_work_order(current.work_order_id, current.version, WorkOrderState.HUMAN_REQUIRED,
                event_type="HUMAN_REQUIRED", actor_type=actor_type, actor_id=actor_id, correlation_id=current.work_order_id)
            self._transition_run(run_id, ResearchRunState.WAITING_HUMAN, "HUMAN_REQUIRED")
            return False
        if decision is ReviewDecisionKind.ABORT_RECOMMENDED:
            self.transitions.transition_work_order(current.work_order_id, current.version, WorkOrderState.FAILED,
                event_type="ABORT_RECOMMENDED", actor_type=actor_type, actor_id=actor_id, correlation_id=current.work_order_id)
            self._transition_run(run_id, ResearchRunState.FAILED, "ABORT_RECOMMENDED")
            return False
        if decision is ReviewDecisionKind.MORE_EVIDENCE:
            self.transitions.transition_work_order(current.work_order_id, current.version, WorkOrderState.MORE_EVIDENCE_REQUIRED,
                event_type="MORE_EVIDENCE_REQUIRED", actor_type=actor_type, actor_id=actor_id, correlation_id=current.work_order_id)
            current = self._order(current.work_order_id)
        self.transitions.transition_work_order(current.work_order_id, current.version, WorkOrderState.REVISION_REQUIRED,
            event_type="REVISION_REQUIRED", actor_type=actor_type, actor_id=actor_id, correlation_id=current.work_order_id,
            metadata={"decision": decision.value})
        # The next objective is carried into the new WorkOrder only; the dispatched
        # predecessor remains immutable and fully traceable.
        self._create_revision(self._order(current.work_order_id), "cloud requested revision", objective=result.output.requested_next_objective)
        self._transition_run(run_id, ResearchRunState.ACTIVE, "REVISION_RESUMED")
        return True

    def resolve_human(self, work_order_id: str, *, action: str, objective: str | None = None) -> bool:
        """Resolve a HUMAN_REQUIRED pause through an explicit controller command."""
        order = self._order(work_order_id)
        if WorkOrderState(order.state) is not WorkOrderState.HUMAN_REQUIRED:
            raise OrchestrationError("work order is not awaiting a human decision")
        if action == "abort":
            self.transitions.transition_work_order(work_order_id, order.version, WorkOrderState.FAILED,
                event_type="HUMAN_ABORTED", actor_type="human", actor_id="human", correlation_id=work_order_id)
            self._transition_run(order.run_id, ResearchRunState.FAILED, "HUMAN_ABORTED")
            return False
        if action != "revise":
            raise ValueError("human action must be revise or abort")
        self.transitions.transition_work_order(work_order_id, order.version, WorkOrderState.REVISION_REQUIRED,
            event_type="HUMAN_REVISION_REQUESTED", actor_type="human", actor_id="human", correlation_id=work_order_id)
        self._transition_run(order.run_id, ResearchRunState.ACTIVE, "HUMAN_RESUMED")
        self._create_revision(self._order(work_order_id), "human revision", objective=objective)
        return True

    def _persist_review(self, run_id: str, order: WorkOrderRecord, attempt: AttemptRecord | None, result: CloudLeadResult[ReviewDecision]) -> None:
        now = utc_now()
        with self.sessions.begin() as session:
            session.add(ReviewDecisionRecord(
                review_id=f"review_{uuid4().hex}", run_id=run_id, work_order_id=order.work_order_id,
                attempt_id=attempt.attempt_id if attempt else None, interaction_id=result.interaction_id,
                decision=result.output.decision.value, evidence_refs=list(result.output.evidence_refs),
                deficiencies=list(result.output.deficiencies), rationale=result.output.rationale,
                requested_next_objective=result.output.requested_next_objective,
                requested_evidence=list(result.output.requested_evidence), confidence=result.output.confidence,
                created_at=now,
            ))
            session.add(AuditEventRecord(
                event_id=f"evt_{uuid4().hex}", event_type="REVIEW_DECISION_RECORDED", run_id=run_id,
                entity_type="work_order", entity_id=order.work_order_id, actor_type=self._agent_actor(order.work_order_id, fallback_type="cloud_lead", fallback_id="cloud-lead")[0],
                actor_id=self._agent_actor(order.work_order_id, fallback_type="cloud_lead", fallback_id="cloud-lead")[1], timestamp=now, correlation_id=order.work_order_id,
                causation_id=result.interaction_id, metadata_json={"decision": result.output.decision.value},
            ))

    def _create_revision(self, order: WorkOrderRecord, reason: str, *, objective: str | None = None) -> bool:
        run = self._run(order.run_id)
        if run.iterations_used >= run.max_iterations:
            self._transition_run(order.run_id, ResearchRunState.FAILED, "MAX_ITERATIONS_EXCEEDED")
            return False
        proposal = WorkOrderProposal.model_validate(order.contract)
        if objective is not None:
            proposal = proposal.model_copy(update={"objective": objective})
        with self.sessions.begin() as session:
            self._add_work_order(session, order.run_id, proposal, parent=order.work_order_id, reason=reason)
        return True

    async def cancel(self, run_id: str) -> RunSnapshot:
        run = self._run(run_id)
        with self.sessions.begin() as session:
            current = session.get(ResearchRunRecord, run_id)
            assert current is not None
            current.cancellation_requested = True
            current.version += 1
            current.updated_at = utc_now()
        for attempt in self._active_attempts(run_id):
            # Cancellation is best-effort at the backend, but the state is never silently resumed.
            await (self.collaboration.cancel(attempt.attempt_id) if self.collaboration else self._legacy_executor().cancel(attempt.attempt_id))
            current_attempt = self._attempt(attempt.attempt_id)
            self.transitions.transition_attempt(current_attempt.attempt_id, current_attempt.version, AttemptState.CANCELLED,
                event_type="ATTEMPT_CANCELLED", actor_type="controller", actor_id="orchestrator", correlation_id=current_attempt.attempt_id)
        for order in self._active_orders(run_id):
            self._cancel_order(order)
        latest = self._run(run_id)
        if latest.state not in {ResearchRunState.COMPLETED, ResearchRunState.FAILED, ResearchRunState.CANCELLED}:
            self._transition_run(run_id, ResearchRunState.CANCELLED, "RUN_CANCELLED")
        return self.snapshot(run_id)

    def snapshot(self, run_id: str) -> RunSnapshot:
        with self.sessions() as session:
            run = session.get(ResearchRunRecord, run_id)
            if run is None:
                raise LookupError(run_id)
            orders = session.scalars(select(WorkOrderRecord).where(WorkOrderRecord.run_id == run_id).order_by(WorkOrderRecord.created_at)).all()
            approvals = tuple(order.approval_id for order in orders if order.approval_id and order.state == WorkOrderState.WAITING_APPROVAL.value)
            attempts = session.scalars(select(AttemptRecord).join(WorkOrderRecord).where(
                WorkOrderRecord.run_id == run_id,
                AttemptRecord.state.not_in((AttemptState.SUCCEEDED.value, AttemptState.FAILED.value, AttemptState.CANCELLED.value)),
            )).all()
            return RunSnapshot(run_id, ResearchRunState(run.state), tuple((item.work_order_id, WorkOrderState(item.state)) for item in orders), approvals, tuple(item.attempt_id for item in attempts))

    def recover(self, run_id: str) -> RunSnapshot:
        """Re-read durable dispatch results; no model call is made during recovery itself."""
        if self.jobs is not None:
            self.jobs.reconcile()
        for attempt in self._active_attempts(run_id):
            stored = self._stored_execution_result(attempt.attempt_id)
            if stored is not None:
                if self.collaboration is not None:
                    self.collaboration.reconcile_attempt(attempt.attempt_id, stored)
                with self.sessions.begin() as session:
                    session.add(AuditEventRecord(
                        event_id=f"evt_{uuid4().hex}", event_type="RECOVERY_EXECUTION_RECONCILED", run_id=run_id,
                        entity_type="attempt", entity_id=attempt.attempt_id, actor_type="controller",
                        actor_id="recovery", timestamp=utc_now(), correlation_id=attempt.attempt_id,
                        causation_id=None, metadata_json={"result_persisted": True},
                    ))
        return self.snapshot(run_id)

    def _assert_hard_verification(self, work_order_id: str) -> None:
        with self.sessions() as session:
            result = session.scalar(select(VerificationResultRecord).where(VerificationResultRecord.work_order_id == work_order_id).order_by(VerificationResultRecord.created_at.desc()).limit(1))
            if result is not None and result.overall != VerificationOverall.PASS.value:
                raise OrchestrationError("cloud ACCEPT cannot override hard verification")

    def _stored_execution_result(self, attempt_id: str) -> ExecutorResult | None:
        with self.sessions() as session:
            record = session.get(ExecutorDispatchRecord, attempt_id)
            if record is None or record.result_json is None or record.status != "COMPLETED":
                return None
            return ExecutorResult.model_validate(record.result_json)

    def _store_execution_result(self, attempt_id: str, result: ExecutorResult) -> None:
        with self.sessions.begin() as session:
            record = session.get(ExecutorDispatchRecord, attempt_id)
            now = utc_now()
            if record is None:
                session.add(ExecutorDispatchRecord(
                    attempt_id=attempt_id, status="COMPLETED", result_json=result.model_dump(mode="json"),
                    created_at=now, updated_at=now,
                ))
            elif record.result_json is None:
                record.status = "COMPLETED"
                record.result_json = result.model_dump(mode="json")
                record.updated_at = now

    def _latest_verification_id(self, work_order_id: str) -> str | None:
        with self.sessions() as session:
            row = session.execute(select(VerificationResultRecord.verification_id).where(VerificationResultRecord.work_order_id == work_order_id).order_by(VerificationResultRecord.created_at.desc()).limit(1)).first()
            return str(row[0]) if row else None

    def _increment_iteration(self, run_id: str) -> None:
        with self.sessions.begin() as session:
            run = session.get(ResearchRunRecord, run_id)
            assert run is not None
            run.iterations_used += 1
            run.version += 1
            run.updated_at = utc_now()

    def _consume_cloud_call(self, run_id: str, run: ResearchRunRecord) -> bool:
        if run.cloud_calls_used >= run.max_cloud_calls:
            self._transition_run(run_id, ResearchRunState.FAILED, "MAX_CLOUD_CALLS_EXCEEDED")
            return False
        with self.sessions.begin() as session:
            current = session.get(ResearchRunRecord, run_id)
            assert current is not None
            current.cloud_calls_used += 1
            current.version += 1
            current.updated_at = utc_now()
        return True

    def _wall_budget_exceeded(self, run: ResearchRunRecord) -> bool:
        return (datetime.now(UTC) - run.created_at).total_seconds() > self.limits.max_wall_seconds

    def _transition_run(self, run_id: str, target: ResearchRunState, event: str, metadata: dict[str, Any] | None = None) -> None:
        run = self._run(run_id)
        self.transitions.transition_run(run_id, run.version, target, event_type=event, actor_type="controller", actor_id="orchestrator", correlation_id=run_id, metadata=metadata)

    def _cancel_order(self, order: WorkOrderRecord) -> None:
        if WorkOrderState(order.state) is not WorkOrderState.CANCELLED:
            current = self._order(order.work_order_id)
            self.transitions.transition_work_order(current.work_order_id, current.version, WorkOrderState.CANCELLED,
                event_type="WORK_ORDER_CANCELLED", actor_type="controller", actor_id="orchestrator", correlation_id=current.work_order_id)

    def _run(self, run_id: str) -> ResearchRunRecord:
        with self.sessions() as session:
            result = session.get(ResearchRunRecord, run_id)
            if result is None:
                raise LookupError(run_id)
            session.expunge(result)
            return result

    def _order(self, work_order_id: str) -> WorkOrderRecord:
        with self.sessions() as session:
            result = session.get(WorkOrderRecord, work_order_id)
            if result is None:
                raise LookupError(work_order_id)
            session.expunge(result)
            return result

    def _attempt(self, attempt_id: str) -> AttemptRecord:
        with self.sessions() as session:
            result = session.get(AttemptRecord, attempt_id)
            if result is None:
                raise LookupError(attempt_id)
            session.expunge(result)
            return result

    def _next_order(self, run_id: str) -> WorkOrderRecord | None:
        with self.sessions() as session:
            query = select(WorkOrderRecord).where(WorkOrderRecord.run_id == run_id, WorkOrderRecord.state.not_in((WorkOrderState.ACCEPTED.value, WorkOrderState.FAILED.value, WorkOrderState.CANCELLED.value))).order_by(WorkOrderRecord.created_at)
            result = session.scalars(query).first()
            if result is not None:
                session.expunge(result)
            return result

    def _review_order(self, run_id: str) -> WorkOrderRecord | None:
        with self.sessions() as session:
            result = session.scalar(select(WorkOrderRecord).where(WorkOrderRecord.run_id == run_id, WorkOrderRecord.state == WorkOrderState.REVIEWING.value).order_by(WorkOrderRecord.created_at).limit(1))
            if result is not None:
                session.expunge(result)
            return result

    def _active_orders(self, run_id: str) -> list[WorkOrderRecord]:
        with self.sessions() as session:
            return list(session.scalars(select(WorkOrderRecord).where(WorkOrderRecord.run_id == run_id, WorkOrderRecord.state.not_in((WorkOrderState.ACCEPTED.value, WorkOrderState.FAILED.value, WorkOrderState.CANCELLED.value, WorkOrderState.REVISION_REQUIRED.value)))).all())

    def _active_attempts(self, run_id: str) -> list[AttemptRecord]:
        with self.sessions() as session:
            return list(session.scalars(select(AttemptRecord).join(WorkOrderRecord).where(WorkOrderRecord.run_id == run_id, AttemptRecord.state.not_in((AttemptState.SUCCEEDED.value, AttemptState.FAILED.value, AttemptState.CANCELLED.value)))).all())

    def _latest_attempt(self, work_order_id: str) -> AttemptRecord | None:
        with self.sessions() as session:
            result = session.scalar(select(AttemptRecord).where(AttemptRecord.work_order_id == work_order_id).order_by(AttemptRecord.created_at.desc()).limit(1))
            if result is not None:
                session.expunge(result)
            return result

    def _has_reviewing_order(self, run_id: str) -> bool:
        with self.sessions() as session:
            return session.scalar(select(WorkOrderRecord.work_order_id).where(WorkOrderRecord.run_id == run_id, WorkOrderRecord.state == WorkOrderState.REVIEWING.value).limit(1)) is not None

    def _has_revision(self, run_id: str) -> bool:
        with self.sessions() as session:
            return session.scalar(select(WorkOrderRecord.work_order_id).where(WorkOrderRecord.run_id == run_id, WorkOrderRecord.state == WorkOrderState.REVISION_REQUIRED.value).limit(1)) is not None
