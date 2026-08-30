"""Operator reconciliation of interrupted daemon command receipts."""

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

import pytest
from sqlalchemy import select, text
from sqlalchemy.orm import Session, sessionmaker

from researchd.daemon.contracts import DaemonCommandResolveCommand, DaemonCommandResult
from researchd.daemon.reconciliation import (
    DaemonCommandResolutionService,
    build_builtin_observers,
)
from researchd.daemon.startup import verify_audit_stream
from researchd.storage.db import create_sqlite_engine, session_factory
from researchd.storage.models import (
    AgentRecord,
    AgentRuntimeRecord,
    AuditEventRecord,
    DaemonCommandRecord,
    ResearchRunRecord,
    RuntimeSessionRecord,
    WorkspaceRecord,
    WorkOrderRecord,
)
from tests.integration.test_storage import migrate


def _database(tmp_path: Path) -> Path:
    database = tmp_path / "resolution.db"
    migrate(database)
    return database


def _sessions(tmp_path: Path) -> sessionmaker[Session]:
    return session_factory(create_sqlite_engine(_database(tmp_path)))


def _now() -> datetime:
    return datetime.now(UTC)


def _seed_receipt(
    sessions: sessionmaker[Session],
    command_id: str,
    command_type: str,
    *,
    status: str = "ACCEPTED",
) -> None:
    now = _now()
    with sessions.begin() as session:
        session.add(DaemonCommandRecord(
            command_id=command_id,
            command_type=command_type,
            command_version=1,
            request_sha256="f" * 64,
            actor_type="SYSTEM",
            actor_id="researchd-command-service",
            status=status,
            result_json=None,
            reason_code=None,
            created_at=now,
            updated_at=now,
        ))


def _seed_workspace(sessions: sessionmaker[Session]) -> None:
    now = _now()
    with sessions.begin() as session:
        if session.get(WorkspaceRecord, "ws_resolution") is None:
            session.add(WorkspaceRecord(
                workspace_id="ws_resolution",
                name="resolution",
                version=1,
                created_at=now,
                updated_at=now,
            ))


def _seed_run(
    sessions: sessionmaker[Session],
    run_id: str,
    *,
    state: str = "ACTIVE",
    cancellation_requested: bool = False,
) -> None:
    _seed_workspace(sessions)
    with sessions() as session:
        if session.get(ResearchRunRecord, run_id) is not None:
            return
    now = _now()
    with sessions.begin() as session:
        session.add(ResearchRunRecord(
            run_id=run_id,
            workspace_id="ws_resolution",
            objective="resolution fixture",
            state=state,
            max_iterations=8,
            max_cloud_calls=24,
            iterations_used=0,
            cloud_calls_used=0,
            cancellation_requested=cancellation_requested,
            version=1,
            created_at=now,
            updated_at=now,
        ))


def _seed_work_order(
    sessions: sessionmaker[Session],
    work_order_id: str,
    *,
    run_id: str = "run_resolution",
    state: str = "WAITING_APPROVAL",
    approval_grant_id: str | None = None,
) -> None:
    _seed_run(sessions, run_id)
    now = _now()
    with sessions.begin() as session:
        session.add(WorkOrderRecord(
            work_order_id=work_order_id,
            run_id=run_id,
            objective="resolution fixture order",
            state=state,
            idempotency_key=f"resolution-{work_order_id}",
            contract={},
            approval_grant_id=approval_grant_id,
            version=1,
            created_at=now,
            updated_at=now,
        ))


def _seed_decision_event(
    sessions: sessionmaker[Session],
    work_order_id: str,
    event_type: str,
) -> None:
    _seed_run(sessions, "run_resolution")
    now = _now()
    with sessions.begin() as session:
        session.add(AuditEventRecord(
            event_id=f"evt_resolution_{event_type.lower()}",
            event_type=event_type,
            run_id="run_resolution",
            entity_type="work_order",
            entity_id=work_order_id,
            actor_type="human",
            actor_id="human",
            timestamp=now,
            correlation_id=work_order_id,
            causation_id=None,
            metadata_json={},
        ))


def _seed_runtime_session(
    sessions: sessionmaker[Session],
    session_id: str,
    *,
    supervisor_state: str = "HEALTHY",
) -> None:
    # The one-active-session-per-runtime index forces a distinct runtime
    # for every concurrently seeded session.
    runtime_id = f"runtime_{session_id.removeprefix('rs_')}"
    agent_id = f"agent_{session_id.removeprefix('rs_')}"
    now = _now()
    with sessions.begin() as session:
        if session.get(AgentRecord, agent_id) is None:
            session.add(AgentRecord(
                agent_id=agent_id,
                display_name="Resolution Agent",
                roles_json=["executor"],
                skills_json=["runtime.test"],
                trust_zone="LOCAL_PRIVATE",
                constraints_json=[],
                labels_json={},
                enabled=True,
                profile_version=1,
                version=1,
                created_at=now,
                updated_at=now,
            ))
    with sessions.begin() as session:
        if session.get(AgentRuntimeRecord, runtime_id) is None:
            session.add(AgentRuntimeRecord(
                runtime_id=runtime_id,
                agent_id=agent_id,
                adapter_kind="PROCESS",
                runtime_name="Resolution process",
                protocols_json=[],
                metadata_json={},
                enabled=True,
                version=1,
                created_at=now,
                updated_at=now,
            ))
    with sessions.begin() as session:
        session.add(RuntimeSessionRecord(
            runtime_session_id=session_id,
            runtime_id=runtime_id,
            launch_mode="PROCESS",
            supervisor_state=supervisor_state,
            launch_spec_json={"argv": ["/usr/bin/sleep", "60"], "cwd": "/tmp"},
            external_identity_json={"pid": 4242} if supervisor_state != "RECONCILIATION_REQUIRED" else None,
            reattach_state="ATTACHED" if supervisor_state == "HEALTHY" else "NOT_APPLICABLE",
            version=1,
            created_at=now,
            updated_at=now,
        ))


def _service(sessions: sessionmaker[Session]) -> DaemonCommandResolutionService:
    return DaemonCommandResolutionService(sessions, build_builtin_observers(sessions))


def _resolve(
    sessions: sessionmaker[Session],
    target_command_id: str,
    resource_ref: dict[str, str],
    *,
    command_id: str = "cmd_resolve_1",
    abandon: bool = False,
) -> DaemonCommandResult:
    return _service(sessions).resolve(DaemonCommandResolveCommand(
        command_id=command_id,
        actor_type="HUMAN",
        actor_id="operator",
        target_command_id=target_command_id,
        resource_ref=resource_ref,
        abandon=abandon,
    ))


def _receipt(sessions: sessionmaker[Session], command_id: str) -> DaemonCommandRecord:
    with sessions() as session:
        receipt = session.get(DaemonCommandRecord, command_id)
    assert receipt is not None
    return receipt


def test_cancel_receipt_converges_completed_when_run_cancelled(tmp_path: Path) -> None:
    sessions = _sessions(tmp_path)
    _seed_run(sessions, "run_cancelled", state="CANCELLED", cancellation_requested=True)
    _seed_receipt(sessions, "cmd_cancel_lost", "RunCancelCommand")

    result = _resolve(sessions, "cmd_cancel_lost", {"run_id": "run_cancelled"})

    assert result.status == "ACCEPTED"
    assert result.command_type == "DaemonCommandResolve"
    resource = result.resource
    assert resource is not None
    assert resource["target_status"] == "COMPLETED"
    assert resource["target_reason_code"] is None
    target = _receipt(sessions, "cmd_cancel_lost")
    assert target.status == "COMPLETED"
    assert target.result_json is not None
    assert target.result_json["status"] == "ACCEPTED"
    assert target.result_json["resource"]["state"] == "CANCELLED"
    resolution = _receipt(sessions, "cmd_resolve_1")
    assert resolution.status == "COMPLETED"
    assert resolution.actor_type == "HUMAN"
    assert resolution.actor_id == "operator"
    assert resolution.result_json is not None
    assert resolution.result_json["resource"]["target_status"] == "COMPLETED"
    with sessions() as session:
        events = session.scalars(select(AuditEventRecord).where(
            AuditEventRecord.entity_type == "daemon_command",
        ).order_by(AuditEventRecord.audit_seq)).all()
    assert [event.event_type for event in events] == [
        "DAEMON_COMMAND_RESOLVED",
        "DAEMON_COMMAND_COMPLETED",
    ]
    resolved = events[0]
    assert resolved.entity_id == "cmd_cancel_lost"
    assert resolved.actor_id == "operator"
    assert resolved.metadata_json["target_status"] == "COMPLETED"
    verify_audit_stream(create_sqlite_engine(_database(tmp_path)))


def test_cancel_receipt_converges_rejected_when_run_missing(tmp_path: Path) -> None:
    sessions = _sessions(tmp_path)
    _seed_receipt(sessions, "cmd_cancel_ghost", "RunCancelCommand")

    result = _resolve(sessions, "cmd_cancel_ghost", {"run_id": "run_missing"})

    assert result.status == "ACCEPTED"
    resource = result.resource
    assert resource is not None
    assert resource["target_status"] == "REJECTED"
    assert resource["target_reason_code"] == "target_missing"
    target = _receipt(sessions, "cmd_cancel_ghost")
    assert target.status == "REJECTED"
    assert target.reason_code == "target_missing"
    assert _receipt(sessions, "cmd_resolve_1").status == "COMPLETED"


def test_cancel_receipt_rejected_when_effect_absent_ignores_abandon(tmp_path: Path) -> None:
    sessions = _sessions(tmp_path)
    _seed_run(sessions, "run_active", state="ACTIVE")
    _seed_receipt(sessions, "cmd_cancel_absent", "RunCancelCommand")

    result = _resolve(
        sessions,
        "cmd_cancel_absent",
        {"run_id": "run_active"},
        command_id="cmd_resolve_absent",
        abandon=True,
    )

    resource = result.resource
    assert resource is not None
    assert resource["target_status"] == "REJECTED"
    assert resource["target_reason_code"] == "effect_absent"
    assert _receipt(sessions, "cmd_cancel_absent").status == "REJECTED"


def test_cancel_receipt_undetermined_without_abandon_persists_nothing(tmp_path: Path) -> None:
    sessions = _sessions(tmp_path)
    _seed_run(sessions, "run_partial", state="ACTIVE", cancellation_requested=True)
    _seed_receipt(sessions, "cmd_cancel_partial", "RunCancelCommand")

    result = _resolve(sessions, "cmd_cancel_partial", {"run_id": "run_partial"})

    resource = result.resource
    assert resource is not None
    assert resource["target_status"] == "UNDETERMINED"
    assert resource["target_reason_code"] == "cancellation_partial"
    assert _receipt(sessions, "cmd_cancel_partial").status == "ACCEPTED"
    with sessions() as session:
        assert session.get(DaemonCommandRecord, "cmd_resolve_1") is None
        assert session.scalar(select(AuditEventRecord.event_type).where(
            AuditEventRecord.event_type == "DAEMON_COMMAND_RESOLVED",
        )) is None


def test_undetermined_receipt_can_be_abandoned_by_operator(tmp_path: Path) -> None:
    sessions = _sessions(tmp_path)
    _seed_run(sessions, "run_partial", state="ACTIVE", cancellation_requested=True)
    _seed_receipt(sessions, "cmd_cancel_partial", "RunCancelCommand")

    result = _resolve(
        sessions,
        "cmd_cancel_partial",
        {"run_id": "run_partial"},
        command_id="cmd_resolve_abandon",
        abandon=True,
    )

    resource = result.resource
    assert resource is not None
    assert resource["target_status"] == "REJECTED"
    assert resource["target_reason_code"] == "OPERATOR_ABANDONED"
    target = _receipt(sessions, "cmd_cancel_partial")
    assert target.status == "REJECTED"
    assert target.reason_code == "OPERATOR_ABANDONED"
    assert _receipt(sessions, "cmd_resolve_abandon").status == "COMPLETED"


def test_terminal_receipt_cannot_be_resolved_twice(tmp_path: Path) -> None:
    sessions = _sessions(tmp_path)
    _seed_run(sessions, "run_done", state="CANCELLED", cancellation_requested=True)
    _seed_receipt(sessions, "cmd_cancel_done", "RunCancelCommand", status="COMPLETED")

    result = _resolve(sessions, "cmd_cancel_done", {"run_id": "run_done"})

    assert result.status == "REJECTED"
    assert result.reason_code == "receipt_not_pending"
    assert _receipt(sessions, "cmd_cancel_done").status == "COMPLETED"
    assert _receipt(sessions, "cmd_resolve_1").status == "REJECTED"


def test_same_resolution_replays_stored_result(tmp_path: Path) -> None:
    sessions = _sessions(tmp_path)
    _seed_run(sessions, "run_replay", state="CANCELLED", cancellation_requested=True)
    _seed_receipt(sessions, "cmd_cancel_replay", "RunCancelCommand")

    first = _resolve(
        sessions,
        "cmd_cancel_replay",
        {"run_id": "run_replay"},
        command_id="cmd_resolve_replay",
    )
    replay = _resolve(
        sessions,
        "cmd_cancel_replay",
        {"run_id": "run_replay"},
        command_id="cmd_resolve_replay",
    )

    assert replay == first
    with sessions() as session:
        events = session.scalars(select(AuditEventRecord).where(
            AuditEventRecord.entity_type == "daemon_command",
        )).all()
    assert len(events) == 2


def test_later_resolution_of_converged_receipt_is_rejected(tmp_path: Path) -> None:
    sessions = _sessions(tmp_path)
    _seed_run(sessions, "run_later", state="CANCELLED", cancellation_requested=True)
    _seed_receipt(sessions, "cmd_cancel_later", "RunCancelCommand")

    _resolve(sessions, "cmd_cancel_later", {"run_id": "run_later"})
    later = _resolve(
        sessions,
        "cmd_cancel_later",
        {"run_id": "run_later"},
        command_id="cmd_resolve_later",
    )

    assert later.status == "REJECTED"
    assert later.reason_code == "receipt_not_pending"
    assert _receipt(sessions, "cmd_cancel_later").status == "COMPLETED"


def test_unknown_command_family_is_rejected(tmp_path: Path) -> None:
    sessions = _sessions(tmp_path)
    _seed_receipt(sessions, "cmd_mystery", "MysteryCommand")

    result = _resolve(sessions, "cmd_mystery", {"anything": "value"})

    assert result.status == "REJECTED"
    assert result.reason_code == "unsupported_command_family"
    assert _receipt(sessions, "cmd_mystery").status == "ACCEPTED"


def test_missing_target_receipt_is_rejected(tmp_path: Path) -> None:
    sessions = _sessions(tmp_path)

    result = _resolve(sessions, "cmd_ghost", {"run_id": "run_x"})

    assert result.status == "REJECTED"
    assert result.reason_code == "target_missing"


def test_invalid_resource_ref_is_undetermined(tmp_path: Path) -> None:
    sessions = _sessions(tmp_path)
    _seed_run(sessions, "run_ref", state="CANCELLED", cancellation_requested=True)
    _seed_receipt(sessions, "cmd_cancel_ref", "RunCancelCommand")

    result = _resolve(sessions, "cmd_cancel_ref", {})

    resource = result.resource
    assert resource is not None
    assert resource["target_status"] == "UNDETERMINED"
    assert resource["target_reason_code"] == "resource_ref_invalid"
    assert _receipt(sessions, "cmd_cancel_ref").status == "ACCEPTED"


def test_approve_receipt_outcomes_follow_observed_grant(tmp_path: Path) -> None:
    sessions = _sessions(tmp_path)
    _seed_work_order(sessions, "wo_waiting", state="WAITING_APPROVAL")
    _seed_receipt(sessions, "cmd_approve_waiting", "WorkOrderApproveCommand")
    waiting = _resolve(
        sessions,
        "cmd_approve_waiting",
        {"work_order_id": "wo_waiting", "grant_id": "grant_1"},
        command_id="cmd_resolve_approve_1",
    )
    assert waiting.resource is not None
    assert waiting.resource["target_status"] == "REJECTED"
    assert waiting.resource["target_reason_code"] == "effect_absent"

    _seed_work_order(
        sessions,
        "wo_advanced",
        state="POLICY_CHECK",
        approval_grant_id="grant_1",
    )
    _seed_receipt(sessions, "cmd_approve_advanced", "WorkOrderApproveCommand")
    advanced = _resolve(
        sessions,
        "cmd_approve_advanced",
        {"work_order_id": "wo_advanced", "grant_id": "grant_1"},
        command_id="cmd_resolve_approve_2",
    )
    assert advanced.resource is not None
    assert advanced.resource["target_status"] == "COMPLETED"

    _seed_work_order(
        sessions,
        "wo_other_grant",
        state="POLICY_CHECK",
        approval_grant_id="grant_other",
    )
    _seed_receipt(sessions, "cmd_approve_conflict", "WorkOrderApproveCommand")
    conflict = _resolve(
        sessions,
        "cmd_approve_conflict",
        {"work_order_id": "wo_other_grant", "grant_id": "grant_1"},
        command_id="cmd_resolve_approve_3",
    )
    assert conflict.resource is not None
    assert conflict.resource["target_status"] == "UNDETERMINED"
    assert conflict.resource["target_reason_code"] == "conflicting_grant"


def test_human_decision_receipt_outcomes_follow_audit_events(tmp_path: Path) -> None:
    sessions = _sessions(tmp_path)
    _seed_work_order(sessions, "wo_paused", state="HUMAN_REQUIRED")
    _seed_receipt(sessions, "cmd_decision_paused", "HumanDecisionCommand")
    paused = _resolve(
        sessions,
        "cmd_decision_paused",
        {"work_order_id": "wo_paused"},
        command_id="cmd_resolve_decision_1",
    )
    assert paused.resource is not None
    assert paused.resource["target_status"] == "REJECTED"
    assert paused.resource["target_reason_code"] == "effect_absent"

    _seed_work_order(sessions, "wo_aborted", state="FAILED")
    _seed_decision_event(sessions, "wo_aborted", "HUMAN_ABORTED")
    _seed_receipt(sessions, "cmd_decision_aborted", "HumanDecisionCommand")
    aborted = _resolve(
        sessions,
        "cmd_decision_aborted",
        {"work_order_id": "wo_aborted"},
        command_id="cmd_resolve_decision_2",
    )
    assert aborted.resource is not None
    assert aborted.resource["target_status"] == "COMPLETED"
    assert cast(dict[str, object], aborted.resource["observed_resource"])["resolved_by"] == "HUMAN_ABORTED"

    _seed_work_order(sessions, "wo_revised", state="REVISION_REQUIRED")
    _seed_decision_event(sessions, "wo_revised", "HUMAN_REVISION_REQUESTED")
    _seed_receipt(sessions, "cmd_decision_revised", "HumanDecisionCommand")
    revised = _resolve(
        sessions,
        "cmd_decision_revised",
        {"work_order_id": "wo_revised"},
        command_id="cmd_resolve_decision_3",
    )
    assert revised.resource is not None
    assert revised.resource["target_status"] == "COMPLETED"
    assert cast(dict[str, object], revised.resource["observed_resource"])["resolved_by"] == "HUMAN_REVISION_REQUESTED"


def test_runtime_session_receipt_outcomes_follow_supervisor_state(tmp_path: Path) -> None:
    sessions = _sessions(tmp_path)
    _seed_runtime_session(sessions, "rs_started", supervisor_state="HEALTHY")
    _seed_receipt(sessions, "cmd_start_settled", "RuntimeSessionStartCommand")
    settled = _resolve(
        sessions,
        "cmd_start_settled",
        {"runtime_session_id": "rs_started"},
        command_id="cmd_resolve_session_1",
    )
    assert settled.resource is not None
    assert settled.resource["target_status"] == "COMPLETED"

    _seed_runtime_session(sessions, "rs_lost", supervisor_state="LOST")
    _seed_receipt(sessions, "cmd_start_lost", "RuntimeSessionStartCommand")
    lost = _resolve(
        sessions,
        "cmd_start_lost",
        {"runtime_session_id": "rs_lost"},
        command_id="cmd_resolve_session_2",
    )
    assert lost.resource is not None
    assert lost.resource["target_status"] == "REJECTED"
    assert lost.resource["target_reason_code"] == "effect_absent"

    _seed_runtime_session(sessions, "rs_stuck", supervisor_state="RECONCILIATION_REQUIRED")
    _seed_receipt(sessions, "cmd_start_stuck", "RuntimeSessionStartCommand")
    stuck = _resolve(
        sessions,
        "cmd_start_stuck",
        {"runtime_session_id": "rs_stuck"},
        command_id="cmd_resolve_session_3",
    )
    assert stuck.resource is not None
    assert stuck.resource["target_status"] == "UNDETERMINED"
    assert stuck.resource["target_reason_code"] == "session_unsettled"

    _seed_runtime_session(sessions, "rs_stopped", supervisor_state="STOPPED")
    _seed_receipt(sessions, "cmd_stop_done", "RuntimeSessionStopCommand")
    stopped = _resolve(
        sessions,
        "cmd_stop_done",
        {"runtime_session_id": "rs_stopped"},
        command_id="cmd_resolve_session_4",
    )
    assert stopped.resource is not None
    assert stopped.resource["target_status"] == "COMPLETED"

    _seed_runtime_session(sessions, "rs_running", supervisor_state="HEALTHY")
    _seed_receipt(sessions, "cmd_stop_absent", "RuntimeSessionStopCommand")
    stop_absent = _resolve(
        sessions,
        "cmd_stop_absent",
        {"runtime_session_id": "rs_running"},
        command_id="cmd_resolve_session_5",
    )
    assert stop_absent.resource is not None
    assert stop_absent.resource["target_status"] == "REJECTED"
    assert stop_absent.resource["target_reason_code"] == "effect_absent"


def test_researchctl_lists_and_resolves_receipts(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from researchd.cli.main import main as ctl_main

    sessions = _sessions(tmp_path)
    _seed_run(sessions, "run_ctl", state="CANCELLED", cancellation_requested=True)
    _seed_receipt(sessions, "cmd_ctl_lost", "RunCancelCommand")
    database = _database(tmp_path)

    assert ctl_main(
        argv=["--database", str(database), "daemon-command", "list", "--status", "ACCEPTED"],
    ) == 0
    listed = json.loads(capsys.readouterr().out)
    assert [item["command_id"] for item in listed] == ["cmd_ctl_lost"]

    assert ctl_main(argv=[
        "--database", str(database),
        "daemon-command", "resolve", "cmd_ctl_lost",
        "--resource-ref", "run_id=run_ctl",
        "--command-id", "cmd_resolve_ctl",
    ]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "ACCEPTED"
    assert payload["resource"]["target_status"] == "COMPLETED"

    assert ctl_main(argv=[
        "--database", str(database),
        "daemon-command", "resolve", "cmd_ctl_lost",
        "--resource-ref", "run_id=run_ctl",
        "--command-id", "cmd_resolve_ctl",
    ]) == 0
    assert json.loads(capsys.readouterr().out) == payload

    assert ctl_main(argv=[
        "--database", str(database),
        "daemon-command", "resolve", "cmd_ctl_lost",
        "--resource-ref", "run_id=run_ctl",
        "--command-id", "cmd_resolve_ctl_again",
    ]) == 1
    rejected = json.loads(capsys.readouterr().out)
    assert rejected["reason_code"] == "receipt_not_pending"


def test_resolution_rejects_invalid_resource_ref_syntax(
    tmp_path: Path,
) -> None:
    from researchd.cli.main import _parse_resource_ref

    assert _parse_resource_ref(["run_id=run_1", "grant_id=g1"]) == {
        "run_id": "run_1",
        "grant_id": "g1",
    }
    with pytest.raises(SystemExit, match="KEY=VALUE"):
        _parse_resource_ref(["no_separator"])


def test_resolution_clears_the_startup_blocker(tmp_path: Path) -> None:
    sessions = _sessions(tmp_path)
    _seed_run(sessions, "run_blocker", state="CANCELLED", cancellation_requested=True)
    _seed_receipt(sessions, "cmd_blocker", "RunCancelCommand")
    engine = create_sqlite_engine(_database(tmp_path))
    with engine.connect() as connection:
        assert int(connection.scalar(text(
            "SELECT COUNT(*) FROM daemon_commands WHERE status = 'ACCEPTED'",
        )) or 0) == 1
    with pytest.raises(RuntimeError, match="operator reconciliation"):
        verify_audit_stream(engine)

    _resolve(sessions, "cmd_blocker", {"run_id": "run_blocker"})

    with engine.connect() as connection:
        assert int(connection.scalar(text(
            "SELECT COUNT(*) FROM daemon_commands WHERE status = 'ACCEPTED'",
        )) or 0) == 0
    verify_audit_stream(engine)
