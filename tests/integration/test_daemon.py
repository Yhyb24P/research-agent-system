from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy import text

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
from researchd.runtime_sessions.contracts import ProcessLaunchSpec
from researchd.storage.db import create_sqlite_engine, session_factory
from researchd.storage.models import AuditEventRecord
from tests.integration.test_storage import migrate


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
