"""PX05-03/04: handoff proposal authority, resolution semantics, and atomicity."""

from datetime import UTC, datetime
from pathlib import Path
from typing import Any, TypeVar, cast

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from researchd.collaboration.action_broker import AgentActionBroker
from researchd.collaboration.contracts import (
    AgentInvocationRequest,
    AgentProfile,
    AgentRuntime,
    Delegation,
    ExecuteInvocationInput,
)
from researchd.collaboration.delegation import DelegationService
from researchd.collaboration.handoff import (
    HandoffProposalAction,
    HandoffResolutionService,
)
from researchd.collaboration.invocation import InvocationService
from researchd.collaboration.registry import AgentRegistryService
from researchd.domain.enums import (
    AgentAdapterKind,
    AgentTrustZone,
    AttemptState,
    DelegationPurpose,
    HandoffMode,
    HandoffStatus,
    NetworkMode,
    ResearchRunState,
    WorkOrderState,
)
from researchd.domain.ids import AgentId, AgentRuntimeId, DelegationId, InvocationId
from researchd.executor.contracts import GrantedWorkOrder, SandboxSpec
from researchd.orchestrator.engine import ResearchOrchestrator
from researchd.policy.engine import DeterministicPolicyEngine, RecordingPolicyEngine
from researchd.storage.db import create_sqlite_engine, session_factory
from researchd.storage.models import (
    AgentInvocationRecord,
    AgentRecord,
    AgentRuntimeRecord,
    AuditEventRecord,
    AttemptRecord,
    DelegationRecord,
    HandoffProposalRecord,
    ResearchRunRecord,
    WorkOrderRecord,
    WorkspaceRecord,
)
from tests.integration.test_storage import migrate

ModelT = TypeVar("ModelT")


def _get(session: Session, model: type[ModelT], primary_key: str) -> ModelT:
    row = session.get(model, primary_key)
    assert row is not None
    return row


class Fixture:
    def __init__(self, tmp_path: Path) -> None:
        database = tmp_path / "handoff.db"
        migrate(database)
        self.sessions: sessionmaker[Session] = session_factory(create_sqlite_engine(database))
        registry = AgentRegistryService(self.sessions)
        for agent_id, runtime_id in (("agent_a", "runtime_a"), ("agent_b", "runtime_b")):
            registry.register_profile(AgentProfile(
                agent_id=AgentId(agent_id), display_name=agent_id, roles=("executor",),
                trust_zone=AgentTrustZone.LOCAL_PRIVATE,
            ))
            registry.register_runtime(AgentRuntime(
                runtime_id=AgentRuntimeId(runtime_id), agent_id=AgentId(agent_id),
                adapter_kind=AgentAdapterKind.INTERNAL, runtime_name=agent_id,
            ))
            registry.acquire_runtime(runtime_id, owner_id="handoff-fixture", lease_seconds=3600)
        now = datetime.now(UTC)
        with self.sessions.begin() as session:
            session.add(WorkspaceRecord(workspace_id="ws_h", name="handoff", version=1, created_at=now, updated_at=now))
            session.flush()
            session.add(ResearchRunRecord(
                run_id="run_h", workspace_id="ws_h", objective="handoff fixture",
                state=ResearchRunState.ACTIVE.value, version=1, created_at=now, updated_at=now,
            ))
            session.flush()
            session.add(WorkOrderRecord(
                work_order_id="wo_exec", run_id="run_h", objective="execute",
                state=WorkOrderState.EXECUTING.value, idempotency_key="handoff-wo-0001",
                contract={
                    "proposal_id": "wo_exec", "objective": "execute",
                    "inputs": [], "requested_capabilities": [],
                    "constraints": {"network": "none", "writable_paths": []},
                    "budget": {"max_wall_seconds": 60}, "acceptance": [],
                    "expected_outputs": [],
                    "data_policy": {"default_classification": "LOCAL_ONLY"},
                    "evidence_refs": [],
                },
                version=1, created_at=now, updated_at=now,
            ))
        self.delegations = DelegationService(self.sessions)
        self.invocations = InvocationService(self.sessions)
        self.delegations.create(Delegation(
            delegation_id=DelegationId("del_exec"), run_id="run_h", work_order_id="wo_exec",
            purpose=DelegationPurpose.EXECUTE, required_roles=("executor",),
            idempotency_key="handoff-del-0001",
        ))
        self.delegations.assign("del_exec", agent_id="agent_a", runtime_id="runtime_a")
        with self.sessions.begin() as session:
            session.add(AttemptRecord(
                attempt_id="att_exec", work_order_id="wo_exec", delegation_id="del_exec",
                state=AttemptState.RUNNING.value, terminal_at=None, version=1,
                created_at=now, updated_at=now,
            ))
        self.invocations.start(AgentInvocationRequest(
            invocation_id=InvocationId("inv_exec"),
            delegation_id=DelegationId("del_exec"),
            run_id="run_h", work_order_id="wo_exec", attempt_id="att_exec",
            agent_id=AgentId("agent_a"), runtime_id=AgentRuntimeId("runtime_a"),
            purpose=DelegationPurpose.EXECUTE, input_sha256="0" * 64,
            typed_input=ExecuteInvocationInput(work_order=GrantedWorkOrder(
                attempt_id="att_exec", objective="execute",
                granted_capabilities=frozenset(),
                sandbox=SandboxSpec(attempt_id="att_exec", workspace="/workspace"),
            )),
        ))
        self.broker = AgentActionBroker(self.sessions)

    def action(self, *, action_id: str = "act_1", target: str | None = "agent_b",
               mode: HandoffMode = HandoffMode.CONTINUE, reason: str = "continue elsewhere",
               objective: str | None = None,
               artifact_ids: tuple[str, ...] = (),
               observation_ids: tuple[str, ...] = ()) -> HandoffProposalAction:
        return HandoffProposalAction(
            action_id=action_id,
            proposed_target_agent_id=AgentId(target) if target else None,
            requested_mode=mode, reason=reason,
            continuation_objective=objective,
            artifact_ids=artifact_ids, observation_ids=observation_ids,
        )

    def proposal(self, **kwargs: Any) -> str:
        return self.broker.submit_handoff(InvocationId("inv_exec"), self.action(**kwargs)).proposal_id

    def terminalize_source(self) -> None:
        """Move the fixture source delegation/attempt/invocation to terminal states."""
        now = datetime.now(UTC)
        with self.sessions.begin() as session:
            _get(session, DelegationRecord, "del_exec").state = "FAILED"
            attempt = _get(session, AttemptRecord, "att_exec")
            attempt.state = AttemptState.FAILED.value
            attempt.terminal_at = now
            invocation = _get(session, AgentInvocationRecord, "inv_exec")
            invocation.status = "FAILED"
            invocation.completed_at = now
            order = _get(session, WorkOrderRecord, "wo_exec")
            order.state = WorkOrderState.EXECUTION_FAILED.value
            order.version += 1
            order.updated_at = now
            run = _get(session, ResearchRunRecord, order.run_id)
            run.state = ResearchRunState.WAITING_EXTERNAL.value
            run.version += 1
            run.updated_at = now

    def controller(self) -> ResearchOrchestrator:
        from researchd.collaboration.gateway import CollaborationGateway
        from researchd.collaboration.selector import AgentSelector

        gateway = CollaborationGateway(
            delegations=self.delegations, invocations=self.invocations,
            selector=AgentSelector(self.sessions),
        )
        return ResearchOrchestrator(
            self.sessions, collaboration=gateway,
            policy=RecordingPolicyEngine(DeterministicPolicyEngine(), self.sessions),
            verifier=None,
        )


@pytest.fixture
def fixture(tmp_path: Path) -> Fixture:
    return Fixture(tmp_path)


def test_running_execution_invocation_can_propose_a_handoff(fixture: Fixture) -> None:
    proposal = fixture.proposal()
    with fixture.sessions() as session:
        row = session.get(HandoffProposalRecord, proposal)
        assert row is not None and row.status == HandoffStatus.PROPOSED.value
        assert row.source_agent_id == "agent_a" and row.proposed_target_agent_id == "agent_b"
        audit = session.scalars(select(AuditEventRecord).where(
            AuditEventRecord.event_type == "HANDOFF_PROPOSED",
        )).all()
        assert [event.actor_type for event in audit] == ["agent"]
        assert [event.actor_id for event in audit] == ["agent_a"]


def test_handoff_proposal_replay_is_idempotent(fixture: Fixture) -> None:
    first = fixture.proposal()
    second = fixture.broker.submit_handoff(
        InvocationId("inv_exec"), fixture.action(),
    ).proposal_id
    assert first == second
    with fixture.sessions() as session:
        assert len(list(session.scalars(select(HandoffProposalRecord)).all())) == 1


def test_handoff_identity_reuse_with_different_content_is_rejected(fixture: Fixture) -> None:
    fixture.proposal()
    with pytest.raises(ValueError, match="identity conflict"):
        fixture.broker.submit_handoff(
            InvocationId("inv_exec"),
            fixture.action(reason="changed my mind"),
        )


def test_terminal_invocation_cannot_propose_a_handoff(fixture: Fixture) -> None:
    fixture.terminalize_source()
    with pytest.raises(ValueError, match="running invocation"):
        fixture.proposal()


def test_non_execution_invocation_cannot_propose_a_handoff(fixture: Fixture) -> None:
    now = datetime.now(UTC)
    with fixture.sessions.begin() as session:
        session.add(DelegationRecord(
            delegation_id="del_plan", run_id="run_h", work_order_id="wo_exec",
            purpose=DelegationPurpose.PLAN.value, required_roles_json=["planner"],
            assigned_agent_id="agent_a", assigned_runtime_id="runtime_a",
            state="RUNNING", idempotency_key="handoff-del-plan", version=1,
            created_at=now, updated_at=now,
        ))
        session.flush()
        runtime = _get(session, AgentRuntimeRecord, "runtime_a")
        session.add(AgentInvocationRecord(
            invocation_id="inv_plan", delegation_id="del_plan", run_id="run_h",
            work_order_id="wo_exec", agent_id="agent_a", runtime_id="runtime_a",
            runtime_lease_id=runtime.runtime_lease_id,
            purpose=DelegationPurpose.PLAN.value, status="RUNNING",
            input_sha256="1" * 64, created_at=now,
        ))
    with pytest.raises(ValueError, match="execution-scoped"):
        fixture.broker.submit_handoff(InvocationId("inv_plan"), fixture.action())


def test_handoff_evidence_must_stay_inside_its_run(fixture: Fixture) -> None:
    foreign = "artifact://sha256/" + "0" * 64
    with pytest.raises(ValueError, match="outside its run"):
        fixture.broker.submit_handoff(
            InvocationId("inv_exec"),
            fixture.action(artifact_ids=(foreign,)),
        )


def test_unavailable_target_agent_is_rejected(fixture: Fixture) -> None:
    with fixture.sessions.begin() as session:
        _get(session, AgentRecord, "agent_b").enabled = False
    with pytest.raises(ValueError, match="unavailable"):
        fixture.proposal()


def test_continue_accept_binds_new_delegation_and_attempt_to_target(fixture: Fixture) -> None:
    proposal_id = fixture.proposal()
    fixture.terminalize_source()
    service = HandoffResolutionService(fixture.sessions, fixture.controller())
    resolved = service.accept(
        proposal_id, actor_type="HUMAN", actor_id="operator",
        reason="take over", target_agent_id="agent_b",
    )
    assert resolved.status is HandoffStatus.ACCEPTED
    assert resolved.resolution_entity_type == "attempt"
    with fixture.sessions() as session:
        entity_id = resolved.resolution_entity_id
        assert entity_id is not None
        attempt = _get(session, AttemptRecord, entity_id)
        assert attempt.work_order_id == "wo_exec"
        delegation_id = attempt.delegation_id
        assert delegation_id is not None
        delegation = _get(session, DelegationRecord, delegation_id)
        assert delegation.assigned_agent_id == "agent_b"
        assert _get(session, WorkOrderRecord, "wo_exec").state == WorkOrderState.EXECUTING.value
        row = _get(session, HandoffProposalRecord, proposal_id)
        assert row.decision_actor_type == "HUMAN" and row.decision_actor_id == "operator"
        assert session.scalar(select(AuditEventRecord.event_type).where(
            AuditEventRecord.event_type == "HANDOFF_ACCEPTED",
        )) == "HANDOFF_ACCEPTED"


def test_continue_accept_replay_is_idempotent_and_conflicting_target_is_rejected(
    fixture: Fixture,
) -> None:
    proposal_id = fixture.proposal()
    fixture.terminalize_source()
    service = HandoffResolutionService(fixture.sessions, fixture.controller())
    first = service.accept(
        proposal_id, actor_type="HUMAN", actor_id="operator",
        reason="take over", target_agent_id="agent_b",
    )
    replay = service.accept(
        proposal_id, actor_type="HUMAN", actor_id="operator",
        reason="take over", target_agent_id="agent_b",
    )
    assert replay.resolution_entity_id == first.resolution_entity_id
    with pytest.raises(ValueError, match="different decision"):
        service.accept(
            proposal_id, actor_type="HUMAN", actor_id="operator",
            reason="actually use the other one", target_agent_id="agent_a",
        )
    with fixture.sessions() as session:
        attempts = session.scalars(select(AttemptRecord).where(
            AttemptRecord.attempt_id.like("att_handoff_%"),
        )).all()
        assert len(attempts) == 1


def test_revise_accept_creates_a_parented_work_order(fixture: Fixture) -> None:
    with fixture.sessions.begin() as session:
        _get(session, WorkOrderRecord, "wo_exec").state = WorkOrderState.VERIFICATION_FAILED.value
        _get(session, ResearchRunRecord, "run_h").state = ResearchRunState.WAITING_EXTERNAL.value
    proposal_id = fixture.broker.submit_handoff(
        InvocationId("inv_exec"),
        fixture.action(
            action_id="act_revise", target=None, mode=HandoffMode.REVISE,
            reason="wrong approach", objective="retry with a different strategy",
        ),
    ).proposal_id
    with fixture.sessions.begin() as session:
        _get(session, DelegationRecord, "del_exec").state = "FAILED"
        _get(session, AttemptRecord, "att_exec").state = AttemptState.FAILED.value
        _get(session, AgentInvocationRecord, "inv_exec").status = "FAILED"
    service = HandoffResolutionService(fixture.sessions, fixture.controller())
    resolved = service.accept(
        proposal_id, actor_type="HUMAN", actor_id="operator", reason="revise it",
    )
    assert resolved.resolution_entity_type == "work_order"
    entity_id = resolved.resolution_entity_id
    assert entity_id is not None
    with fixture.sessions() as session:
        revision = _get(session, WorkOrderRecord, entity_id)
        assert revision.parent_work_order_id == "wo_exec"
        assert revision.objective == "retry with a different strategy"
        assert _get(session, WorkOrderRecord, "wo_exec").state == WorkOrderState.REVISION_REQUIRED.value
        assert _get(session, ResearchRunRecord, "run_h").state == ResearchRunState.ACTIVE.value


def test_handoff_revision_replay_resumes_existing_child(fixture: Fixture) -> None:
    """A crash after child creation cannot strand its Run before the wake."""
    controller = fixture.controller()
    with fixture.sessions.begin() as session:
        _get(session, WorkOrderRecord, "wo_exec").state = WorkOrderState.VERIFICATION_FAILED.value
        _get(session, ResearchRunRecord, "run_h").state = ResearchRunState.WAITING_EXTERNAL.value

    revision_id = controller.create_handoff_revision(
        "wo_exec",
        objective="recover the revision",
        reason="handoff crash recovery",
        revision_work_order_id="wo_handoff_replay",
    )
    with fixture.sessions.begin() as session:
        # Crash injection: the child commit survived while the subsequent Run
        # transition did not.  Replaying the controller command must heal it.
        _get(session, ResearchRunRecord, "run_h").state = ResearchRunState.WAITING_EXTERNAL.value

    assert controller.create_handoff_revision(
        "wo_exec",
        objective="recover the revision",
        reason="handoff crash recovery",
        revision_work_order_id="wo_handoff_replay",
    ) == revision_id
    with fixture.sessions() as session:
        assert _get(session, ResearchRunRecord, "run_h").state == ResearchRunState.ACTIVE.value
        children = session.scalars(select(WorkOrderRecord).where(
            WorkOrderRecord.parent_work_order_id == "wo_exec",
        )).all()
        assert [row.work_order_id for row in children] == ["wo_handoff_replay"]


def test_revise_cannot_select_an_execution_target(fixture: Fixture) -> None:
    proposal_id = fixture.broker.submit_handoff(
        InvocationId("inv_exec"),
        fixture.action(action_id="act_revise2", mode=HandoffMode.REVISE, objective="redo"),
    ).proposal_id
    with fixture.sessions.begin() as session:
        _get(session, DelegationRecord, "del_exec").state = "FAILED"
        _get(session, AttemptRecord, "att_exec").state = AttemptState.FAILED.value
        _get(session, AgentInvocationRecord, "inv_exec").status = "FAILED"
        _get(session, WorkOrderRecord, "wo_exec").state = WorkOrderState.VERIFICATION_FAILED.value
    service = HandoffResolutionService(fixture.sessions, fixture.controller())
    with pytest.raises(ValueError, match="cannot select an execution target"):
        service.accept(
            proposal_id, actor_type="HUMAN", actor_id="operator",
            reason="no", target_agent_id="agent_b",
        )


def test_terminal_proposal_is_never_reopened(fixture: Fixture) -> None:
    proposal_id = fixture.proposal()
    fixture.terminalize_source()
    service = HandoffResolutionService(fixture.sessions, fixture.controller())
    service.reject(proposal_id, actor_type="HUMAN", actor_id="operator", reason="not now")
    with pytest.raises(ValueError, match="different decision"):
        service.accept(
            proposal_id, actor_type="HUMAN", actor_id="operator",
            reason="change of heart", target_agent_id="agent_b",
        )
    replay = service.reject(proposal_id, actor_type="HUMAN", actor_id="operator", reason="not now")
    assert replay.status is HandoffStatus.REJECTED
    with fixture.sessions() as session:
        assert _get(session, HandoffProposalRecord, proposal_id).status == HandoffStatus.REJECTED.value
        assert session.scalar(select(AuditEventRecord.event_type).where(
            AuditEventRecord.event_type == "HANDOFF_REJECTED",
        )) == "HANDOFF_REJECTED"


def test_accept_refuses_a_source_that_is_not_terminal(fixture: Fixture) -> None:
    proposal_id = fixture.proposal()
    service = HandoffResolutionService(fixture.sessions, fixture.controller())
    with pytest.raises(ValueError, match="not terminal"):
        service.accept(
            proposal_id, actor_type="HUMAN", actor_id="operator",
            reason="too early", target_agent_id="agent_b",
        )


def test_retry_crash_leaves_no_executing_order_without_attempt(fixture: Fixture) -> None:
    proposal_id = fixture.proposal()
    fixture.terminalize_source()
    controller = fixture.controller()
    service = HandoffResolutionService(fixture.sessions, controller)

    class CrashOnCommit:
        def __init__(self, real: Any) -> None:
            self._real = real
            self._session: Any = None

        def __enter__(self) -> Any:
            self._session = self._real.__enter__()
            return self._session

        def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
            try:
                self._session.rollback()
            except Exception:
                pass
            raise RuntimeError("simulated crash before commit")

    class PoisonedSessions:
        def __init__(self, real: sessionmaker[Session]) -> None:
            self._real = real

        def __call__(self) -> Session:
            return self._real()

        def begin(self) -> Any:
            return CrashOnCommit(self._real.begin())

    crashed = False
    try:
        controller.sessions = cast(
            "sessionmaker[Session]", PoisonedSessions(controller.sessions),
        )
        service.accept(
            proposal_id, actor_type="HUMAN", actor_id="operator",
            reason="crash mid-commit", target_agent_id="agent_b",
        )
    except RuntimeError:
        crashed = True
    assert crashed
    with fixture.sessions() as session:
        assert _get(session, WorkOrderRecord, "wo_exec").state == WorkOrderState.EXECUTION_FAILED.value
        assert session.scalar(select(AttemptRecord).where(
            AttemptRecord.attempt_id.like("att_handoff_%"),
        )) is None

    # The clean retry replays the deterministic IDs and commits exactly one attempt.
    controller.sessions = fixture.sessions
    resolved = service.accept(
        proposal_id, actor_type="HUMAN", actor_id="operator",
        reason="retry after crash", target_agent_id="agent_b",
    )
    with fixture.sessions() as session:
        attempts = session.scalars(select(AttemptRecord).where(
            AttemptRecord.attempt_id.like("att_handoff_%"),
        )).all()
        assert len(attempts) == 1
        assert _get(session, WorkOrderRecord, "wo_exec").state == WorkOrderState.EXECUTING.value
        assert resolved.resolution_entity_id == attempts[0].attempt_id


def test_accept_effect_cannot_be_concurrently_rejected_before_decision_commit(
    fixture: Fixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    proposal_id = fixture.proposal()
    fixture.terminalize_source()
    service = HandoffResolutionService(fixture.sessions, fixture.controller())
    decide = service._decide

    def crash_before_decision(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("simulated crash before Handoff decision commit")

    monkeypatch.setattr(service, "_decide", crash_before_decision)
    with pytest.raises(RuntimeError, match="decision commit"):
        service.accept(
            proposal_id,
            actor_type="HUMAN",
            actor_id="operator",
            reason="accept and continue",
            target_agent_id="agent_b",
        )

    with fixture.sessions() as session:
        proposal = _get(session, HandoffProposalRecord, proposal_id)
        assert proposal.status == HandoffStatus.PROPOSED.value
        assert proposal.decision_actor_type == "HUMAN"
        assert proposal.resolution_entity_type == "agent"
        assert proposal.resolution_entity_id == "agent_b"
        assert session.scalar(select(AuditEventRecord.event_type).where(
            AuditEventRecord.event_type == "HANDOFF_ACCEPT_RESERVED",
            AuditEventRecord.entity_id == proposal_id,
        )) == "HANDOFF_ACCEPT_RESERVED"
        assert len(session.scalars(select(AttemptRecord).where(
            AttemptRecord.attempt_id.like("att_handoff_%"),
        )).all()) == 1

    monkeypatch.setattr(service, "_decide", decide)
    with pytest.raises(ValueError, match="acceptance is already in progress"):
        service.reject(
            proposal_id,
            actor_type="HUMAN",
            actor_id="operator",
            reason="reject after effect",
        )

    resolved = service.accept(
        proposal_id,
        actor_type="HUMAN",
        actor_id="operator",
        reason="resume accepted decision",
        target_agent_id="agent_b",
    )
    assert resolved.status is HandoffStatus.ACCEPTED
    assert resolved.resolution_entity_type == "attempt"
    with fixture.sessions() as session:
        assert len(session.scalars(select(AttemptRecord).where(
            AttemptRecord.attempt_id.like("att_handoff_%"),
        )).all()) == 1
