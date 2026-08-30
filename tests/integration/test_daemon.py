import asyncio
from collections.abc import Awaitable, Coroutine
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, cast

import pytest
from sqlalchemy import text

from researchd.agents.cloud_lead import CloudLeadAdapter
from researchd.artifacts.store import ContentAddressedArtifactStore
from researchd.collaboration.registry import AgentRegistryService
from researchd.context.builder import ContextBuilder
from researchd.context.redaction import DeterministicRedactor
from researchd.daemon.composition import DaemonApplication, DaemonConfig, compose_daemon
from researchd.daemon.contracts import (
    DaemonCommandResult,
    HumanDecisionCommand,
    RunCancelCommand,
    WorkOrderApproveCommand,
)
from researchd.daemon.dispatcher import DaemonCommandDispatcher
from researchd.daemon.runtime import DaemonNotReady, DaemonState, ResearchDaemon
from researchd.daemon.startup import (
    StartupBarrier,
    StartupAction,
    StartupPhase,
    StartupPhaseStatus,
    verify_audit_stream,
    verify_migration_head,
    verify_storage_sanity,
)
from researchd.domain.base import DomainModel
from researchd.domain.enums import Capability
from researchd.models.cloud import CloudCallBudget, CloudPricing
from researchd.orchestrator.engine import ResearchOrchestrator
from researchd.policy.approval import ApprovalService
from researchd.policy.engine import BudgetLimits, DeterministicPolicyEngine, RecordingPolicyEngine
from researchd.runtime_sessions.contracts import ProcessLaunchSpec
from researchd.runtime_sessions.service import RuntimeSessionService
from researchd.storage.db import create_sqlite_engine, session_factory
from researchd.storage.models import AuditEventRecord, WorkOrderRecord, WorkspaceRecord
from researchd.supervisor.runtime import RuntimeSupervisor
from tests.integration.test_orchestrator import (
    FakeCloud,
    FakeExecutor,
    FakeVerifier,
    cloud_configuration,
    collaboration_gateway,
    _proposal,
    _proposal_with_capability,
    _review,
)
from tests.integration.test_storage import migrate


def _supervisor(tmp_path: Path) -> RuntimeSupervisor:
    sessions = session_factory(create_sqlite_engine(tmp_path / "dispatcher.db"))
    return RuntimeSupervisor(RuntimeSessionService(sessions, AgentRegistryService(sessions)))


def _await(result: DomainModel | Awaitable[DomainModel]) -> DomainModel:
    return asyncio.run(cast(Coroutine[Any, Any, DomainModel], result))


def ordered_actions(
    observed: list[StartupPhase],
    *,
    fail_at: StartupPhase | None = None,
) -> dict[StartupPhase, StartupAction]:
    actions: dict[StartupPhase, StartupAction] = {}
    for phase in StartupPhase:
        def action(current: StartupPhase = phase) -> tuple[str, ...]:
            observed.append(current)
            if current is fail_at:
                raise ValueError("injected startup failure")
            return (current.value,)
        actions[phase] = action
    return actions


def test_startup_barrier_runs_frozen_order_and_reports_counts() -> None:
    observed: list[StartupPhase] = []
    barrier = StartupBarrier(ordered_actions(observed))

    report = barrier.run()

    assert report.ready
    assert observed == list(StartupPhase)
    assert all(item.status is StartupPhaseStatus.PASS for item in report.phases)
    assert all(item.affected_count == 1 for item in report.phases)


def test_startup_failure_stops_side_effects_and_keeps_daemon_non_ready() -> None:
    observed: list[StartupPhase] = []
    barrier = StartupBarrier(ordered_actions(
        observed,
        fail_at=StartupPhase.WORKTREE_RECOVERY,
    ))
    dispatched: list[object] = []
    daemon = ResearchDaemon(barrier, lambda command: dispatched.append(command))
    command = ProcessLaunchSpec(argv=("/usr/bin/true",), cwd="/tmp")

    with pytest.raises(DaemonNotReady):
        daemon.execute(command)
    report = daemon.start()

    assert not report.ready
    assert daemon.state is DaemonState.FAILED
    assert observed == list(StartupPhase)[:4]
    assert report.phases[3].status is StartupPhaseStatus.FAIL
    assert all(
        item.status is StartupPhaseStatus.SKIPPED
        for item in report.phases[4:]
    )
    assert daemon.health()["ready"] is False
    with pytest.raises(DaemonNotReady):
        daemon.execute(command)
    assert dispatched == []


def test_ready_daemon_dispatches_only_typed_commands() -> None:
    observed: list[StartupPhase] = []
    dispatched: list[object] = []

    def dispatch(command: object) -> str:
        dispatched.append(command)
        return "accepted"

    daemon = ResearchDaemon(
        StartupBarrier(ordered_actions(observed)),
        dispatch,
    )
    command = ProcessLaunchSpec(argv=("/usr/bin/true",), cwd="/tmp")

    assert daemon.start().ready
    assert daemon.execute(command) == "accepted"
    assert dispatched == [command]
    assert daemon.health()["state"] == "READY"
    with pytest.raises(TypeError, match="typed command"):
        daemon.execute("free text")  # type: ignore[arg-type]


def test_database_startup_checks_cover_schema_storage_and_audit(tmp_path: Path) -> None:
    database = tmp_path / "daemon.db"
    artifact_root = tmp_path / "artifacts"
    artifact_root.mkdir()
    migrate(database)
    engine = create_sqlite_engine(database)
    sessions = session_factory(engine)
    with sessions.begin() as session:
        session.add(AuditEventRecord(
            event_id="evt_daemon_startup",
            event_type="DAEMON_CHECK",
            run_id=None,
            entity_type="daemon",
            entity_id="researchd",
            actor_type="SYSTEM",
            actor_id="test",
            timestamp=datetime.now(UTC),
            correlation_id="daemon-check",
            causation_id=None,
            metadata_json={},
        ))

    verify_migration_head(engine)
    verify_storage_sanity(database, artifact_root)
    verify_audit_stream(engine)

    with engine.begin() as connection:
        connection.execute(text(
            "UPDATE audit_stream_clock SET next_seq = next_seq + 1 WHERE singleton = 1"
        ))
    with pytest.raises(RuntimeError, match="allocator"):
        verify_audit_stream(engine)


class _ControlStub:
    def __init__(self) -> None:
        self.calls: list[tuple[str, ...]] = []

    async def cancel_run(self, run_id: str) -> dict[str, object]:
        self.calls.append(("cancel", run_id))
        return {"run_id": run_id, "state": "CANCELLATION_REQUESTED"}

    async def approve(self, work_order_id: str, grant_id: str) -> dict[str, object]:
        self.calls.append(("approve", work_order_id, grant_id))
        return {"work_order_id": work_order_id, "state": "APPROVED"}

    def resolve_human(
        self,
        work_order_id: str,
        *,
        action: str,
        objective: str | None = None,
    ) -> dict[str, object]:
        self.calls.append(("human", work_order_id, action, objective or ""))
        return {"work_order_id": work_order_id, "action": action}


def test_dispatcher_fails_closed_without_control_authority(tmp_path: Path) -> None:
    dispatcher = DaemonCommandDispatcher(_supervisor(tmp_path))
    cancel = RunCancelCommand(
        command_id="cmd_cancel_closed",
        actor_type="HUMAN",
        actor_id="operator",
        run_id="run_1",
    )
    approve = WorkOrderApproveCommand(
        command_id="cmd_approve_closed",
        actor_type="HUMAN",
        actor_id="operator",
        work_order_id="wo_1",
        grant_id="grant_1",
    )
    decision = HumanDecisionCommand(
        command_id="cmd_decision_closed",
        actor_type="HUMAN",
        actor_id="operator",
        work_order_id="wo_1",
        action="abort",
    )

    with pytest.raises(RuntimeError, match="orchestrator mutation authority is not configured"):
        _await(dispatcher(cancel))
    with pytest.raises(RuntimeError, match="orchestrator mutation authority is not configured"):
        _await(dispatcher(approve))
    with pytest.raises(RuntimeError, match="orchestrator mutation authority is not configured"):
        dispatcher(decision)


def test_dispatcher_wraps_control_mutations_in_versioned_envelope(tmp_path: Path) -> None:
    control = _ControlStub()
    dispatcher = DaemonCommandDispatcher(_supervisor(tmp_path), control)

    cancel_result = _await(dispatcher(RunCancelCommand(
        command_id="cmd_cancel_env",
        actor_type="HUMAN",
        actor_id="operator",
        run_id="run_1",
    )))
    assert isinstance(cancel_result, DaemonCommandResult)
    assert cancel_result.command_version == 1
    assert cancel_result.command_id == "cmd_cancel_env"
    assert cancel_result.command_type == "RunCancel"
    assert cancel_result.status == "ACCEPTED"
    assert cancel_result.resource == {"run_id": "run_1", "state": "CANCELLATION_REQUESTED"}

    approve_result = _await(dispatcher(WorkOrderApproveCommand(
        command_id="cmd_approve_env",
        actor_type="HUMAN",
        actor_id="operator",
        work_order_id="wo_1",
        grant_id="grant_1",
    )))
    assert isinstance(approve_result, DaemonCommandResult)
    assert approve_result.command_version == 1
    assert approve_result.command_id == "cmd_approve_env"
    assert approve_result.command_type == "WorkOrderApprove"
    assert approve_result.status == "ACCEPTED"
    assert approve_result.resource == {"work_order_id": "wo_1", "state": "APPROVED"}

    decision_result = dispatcher(HumanDecisionCommand(
        command_id="cmd_decision_env",
        actor_type="HUMAN",
        actor_id="operator",
        work_order_id="wo_1",
        action="revise",
        objective="narrow the task",
    ))
    assert isinstance(decision_result, DaemonCommandResult)
    assert decision_result.command_version == 1
    assert decision_result.command_id == "cmd_decision_env"
    assert decision_result.command_type == "HumanDecision"
    assert decision_result.status == "ACCEPTED"
    assert decision_result.resource == {"work_order_id": "wo_1", "action": "revise"}
    assert control.calls == [
        ("cancel", "run_1"),
        ("approve", "wo_1", "grant_1"),
        ("human", "wo_1", "revise", "narrow the task"),
    ]


def test_dispatcher_rejects_unknown_command_models(tmp_path: Path) -> None:
    dispatcher = DaemonCommandDispatcher(_supervisor(tmp_path), _ControlStub())

    with pytest.raises(TypeError, match="unsupported daemon command"):
        dispatcher(ProcessLaunchSpec(argv=("/usr/bin/true",), cwd="/tmp"))


def _composed_application(tmp_path: Path) -> DaemonApplication:
    database = tmp_path / "researchd.db"
    migrate(database)
    application = compose_daemon(DaemonConfig(
        database=database,
        artifact_root=tmp_path / "artifacts",
        state_root=tmp_path / "state",
    ))
    assert application.daemon.start().ready
    return application


def _driver_orchestrator(
    application: DaemonApplication,
    tmp_path: Path,
    cloud_responses: list[str],
) -> tuple[Any, ResearchOrchestrator, ApprovalService]:
    """Drive durable runs to a human gate with the existing reference doubles."""
    sessions = session_factory(create_sqlite_engine(application.config.database))
    now = datetime.now(UTC)
    with sessions.begin() as session:
        if session.get(WorkspaceRecord, "ws_e2e") is None:
            session.add(WorkspaceRecord(
                workspace_id="ws_e2e", name="e2e", version=1, created_at=now, updated_at=now,
            ))
    builder = ContextBuilder(
        sessions,
        ContentAddressedArtifactStore(tmp_path / "driver-artifacts"),
        DeterministicRedactor(),
    )
    cloud = CloudLeadAdapter(
        FakeCloud(cloud_responses), sessions, builder,
        configuration=cloud_configuration(),
        budget=CloudCallBudget(
            max_requests=3, max_input_bytes=100_000, max_response_bytes=100_000,
            max_output_tokens=512, max_total_tokens=2_000,
        ),
        pricing=CloudPricing(prompt_usd_per_million=Decimal("0"), completion_usd_per_million=Decimal("0")),
    )
    approvals = ApprovalService(sessions)
    orchestrator = ResearchOrchestrator(
        sessions,
        collaboration=collaboration_gateway(sessions, cloud, FakeExecutor()),
        policy=RecordingPolicyEngine(DeterministicPolicyEngine(), sessions),
        verifier=FakeVerifier(sessions),
        approvals=approvals,
        workspace_capabilities=frozenset({Capability.NETWORK_EXTERNAL}),
        user_capabilities=frozenset({Capability.NETWORK_EXTERNAL}),
        maximum_budget=BudgetLimits(100, 100, 0, 100, 100),
    )
    return sessions, orchestrator, approvals


def test_composed_daemon_executes_real_run_cancel(tmp_path: Path) -> None:
    application = _composed_application(tmp_path)
    orchestrator = application.api.orchestrator
    assert orchestrator is not None
    now = datetime.now(UTC)
    with application.api.sessions.begin() as session:
        session.add(WorkspaceRecord(
            workspace_id="ws_cancel", name="cancel", version=1, created_at=now, updated_at=now,
        ))
    run_id = orchestrator.create_run(workspace_id="ws_cancel", objective="cancel me")

    result = _await(cast(DomainModel | Awaitable[DomainModel], application.daemon.execute(RunCancelCommand(
        command_id="cmd_composed_cancel",
        actor_type="HUMAN",
        actor_id="operator",
        run_id=run_id,
    ))))

    assert isinstance(result, DaemonCommandResult)
    assert result.command_version == 1
    assert result.command_type == "RunCancel"
    assert result.status == "ACCEPTED"
    status = application.api.run_status(run_id)
    assert status["state"] == "CANCELLED"
    assert status["cancellation_requested"] is True


def test_composed_daemon_executes_real_work_order_approve(tmp_path: Path) -> None:
    application = _composed_application(tmp_path)
    _, driver, approvals = _driver_orchestrator(
        application, tmp_path, [_proposal_with_capability("network.external")],
    )
    run_id = driver.create_run(workspace_id="ws_e2e", objective="approved external step")
    snapshot = asyncio.run(driver.run(run_id, max_steps=10))
    assert snapshot.state.value == "WAITING_HUMAN" and snapshot.pending_approval_ids
    work_order_id = snapshot.work_orders[0][0]
    grant = approvals.approve(snapshot.pending_approval_ids[0], granted_by="test-human")

    result = _await(cast(DomainModel | Awaitable[DomainModel], application.daemon.execute(WorkOrderApproveCommand(
        command_id="cmd_composed_approve",
        actor_type="HUMAN",
        actor_id="operator",
        work_order_id=work_order_id,
        grant_id=grant.grant_id,
    ))))

    assert isinstance(result, DaemonCommandResult)
    assert result.command_type == "WorkOrderApprove"
    assert result.status == "ACCEPTED"
    assert result.resource is not None and result.resource["state"] == "POLICY_CHECK"
    assert application.api.work_order_status(work_order_id)["state"] == "POLICY_CHECK"
    assert application.api.run_status(run_id)["state"] == "ACTIVE"


def _drive_to_human_required(application: DaemonApplication, tmp_path: Path) -> tuple[str, str]:
    _, driver, _ = _driver_orchestrator(
        application, tmp_path, [_proposal(), _review("HUMAN_REQUIRED")],
    )
    run_id = driver.create_run(workspace_id="ws_e2e", objective="human decision")
    snapshot = asyncio.run(driver.run(run_id, max_steps=30))
    assert snapshot.state.value == "WAITING_HUMAN"
    work_order_id = snapshot.work_orders[0][0]
    assert application.api.work_order_status(work_order_id)["state"] == "HUMAN_REQUIRED"
    return run_id, work_order_id


def test_composed_daemon_executes_real_human_decision_revise(tmp_path: Path) -> None:
    application = _composed_application(tmp_path)
    run_id, work_order_id = _drive_to_human_required(application, tmp_path)

    result = application.daemon.execute(HumanDecisionCommand(
        command_id="cmd_composed_revise",
        actor_type="HUMAN",
        actor_id="operator",
        work_order_id=work_order_id,
        action="revise",
        objective="narrow the objective",
    ))

    assert isinstance(result, DaemonCommandResult)
    assert result.command_type == "HumanDecision"
    assert result.status == "ACCEPTED"
    status = application.api.work_order_status(work_order_id)
    assert status["state"] == "REVISION_REQUIRED"
    sessions = session_factory(create_sqlite_engine(application.config.database))
    with sessions() as session:
        orders = session.query(WorkOrderRecord).order_by(WorkOrderRecord.created_at).all()
        assert len(orders) == 2
        assert orders[1].parent_work_order_id == work_order_id
        assert orders[1].objective == "narrow the objective"
    assert application.api.run_status(run_id)["state"] == "ACTIVE"


def test_composed_daemon_executes_real_human_decision_abort(tmp_path: Path) -> None:
    application = _composed_application(tmp_path)
    run_id, work_order_id = _drive_to_human_required(application, tmp_path)

    result = application.daemon.execute(HumanDecisionCommand(
        command_id="cmd_composed_abort",
        actor_type="HUMAN",
        actor_id="operator",
        work_order_id=work_order_id,
        action="abort",
    ))

    assert isinstance(result, DaemonCommandResult)
    assert result.command_type == "HumanDecision"
    assert result.status == "ACCEPTED"
    assert application.api.work_order_status(work_order_id)["state"] == "FAILED"
    assert application.api.run_status(run_id)["state"] == "FAILED"
