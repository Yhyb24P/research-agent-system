from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import inspect, select, text
from sqlalchemy.exc import IntegrityError, StatementError
from sqlalchemy.orm import Session, sessionmaker

from researchd.domain.enums import AttemptState, JobState, ResearchRunState, WorkOrderState
from researchd.domain.transitions import InvalidTransition
from researchd.storage.db import create_sqlite_engine, session_factory
from researchd.storage.models import AttemptRecord, AuditEventRecord, JobRecord, ResearchRunRecord, WorkspaceRecord, WorkOrderRecord
from researchd.storage.repositories import AttemptRepository, EventRepository, JobRepository, ResearchRunRepository, WorkOrderRepository
from researchd.storage.transitions import ConcurrencyConflict, TransactionalTransitionService

ROOT = Path(__file__).parents[2]


def migrate(path: Path) -> None:
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("sqlalchemy.url", f"sqlite:///{path}")
    command.upgrade(config, "head")


def assert_migration_matches_models(path: Path) -> None:
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("sqlalchemy.url", f"sqlite:///{path}")
    command.check(config)


@pytest.fixture
def database(tmp_path: Path) -> tuple[Path, sessionmaker[Session]]:
    path = tmp_path / "workspace.db"
    migrate(path)
    assert_migration_matches_models(path)
    engine = create_sqlite_engine(path)
    sessions = session_factory(engine)
    now = datetime.now(UTC)
    with sessions.begin() as session:
        session.add(WorkspaceRecord(workspace_id="ws_test", name="test", version=1, created_at=now, updated_at=now))
        session.flush()
        session.add(ResearchRunRecord(run_id="run_test", workspace_id="ws_test", objective="test durability", state=ResearchRunState.ACTIVE.value, version=1, created_at=now, updated_at=now))
        session.flush()
        session.add(WorkOrderRecord(work_order_id="wo_test", run_id="run_test", parent_work_order_id=None, objective="execute bounded test", state=WorkOrderState.READY.value, idempotency_key="idempotency-test-0001", contract={"objective": "execute bounded test"}, version=1, created_at=now, updated_at=now))
        session.flush()
        session.add(AttemptRecord(attempt_id="att_test", work_order_id="wo_test", state=AttemptState.RUNNING.value, terminal_at=None, version=1, created_at=now, updated_at=now))
        session.flush()
        session.add(JobRecord(job_id="job_test", attempt_id="att_test", operation_id="op-test-0001", state=JobState.RUNNING.value, backend="fixture", native_handle="native-1", version=1, created_at=now, updated_at=now))
    return path, sessions


def transition_kwargs(event_type: str = "WORK_ORDER_DISPATCHED", metadata: Any = None) -> dict[str, Any]:
    return {"event_type": event_type, "actor_type": "controller", "actor_id": "controller-1", "correlation_id": "corr-test", "metadata": {} if metadata is None else metadata}


def test_migration_upgrade_from_empty_db_and_wal(tmp_path: Path) -> None:
    path = tmp_path / "empty.db"
    migrate(path)
    engine = create_sqlite_engine(path)
    expected = {"workspaces", "research_runs", "work_orders", "attempts", "jobs", "artifacts", "audit_events", "alembic_version"}
    assert expected <= set(inspect(engine).get_table_names())
    with engine.connect() as connection:
        assert connection.scalar(text("PRAGMA journal_mode")) == "wal"
        assert connection.scalar(text("PRAGMA foreign_keys")) == 1


def test_expected_version_concurrency_exactly_one_wins(database: tuple[Path, sessionmaker[Session]]) -> None:
    _, sessions = database
    service = TransactionalTransitionService(sessions)

    def dispatch() -> object:
        try:
            return service.transition_work_order("wo_test", 1, WorkOrderState.DISPATCHED, **transition_kwargs())
        except (ConcurrencyConflict, InvalidTransition) as error:
            return error

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: dispatch(), range(2)))
    assert results.count(2) == 1
    assert sum(isinstance(result, (ConcurrencyConflict, InvalidTransition)) for result in results) == 1
    with sessions() as session:
        row = session.get(WorkOrderRecord, "wo_test")
        assert row is not None and row.state == WorkOrderState.DISPATCHED.value and row.version == 2
        assert len(EventRepository(session).for_run("run_test")) == 1


def test_restart_reopens_authoritative_state_and_recovers_active_entities(database: tuple[Path, sessionmaker[Session]]) -> None:
    path, sessions = database
    TransactionalTransitionService(sessions).transition_work_order(
        "wo_test", 1, WorkOrderState.DISPATCHED, **transition_kwargs()
    )
    sessions.kw["bind"].dispose()
    reopened = session_factory(create_sqlite_engine(path))
    with reopened() as session:
        runs = ResearchRunRepository(session).active()
        orders = WorkOrderRepository(session).active()
        attempts = AttemptRepository(session).active()
        jobs = JobRepository(session).active()
        assert [(row.run_id, row.state) for row in runs] == [("run_test", "ACTIVE")]
        assert [(row.work_order_id, row.state) for row in orders] == [("wo_test", "DISPATCHED")]
        assert [(row.attempt_id, row.state) for row in attempts] == [("att_test", "RUNNING")]
        assert [(row.job_id, row.state) for row in jobs] == [("job_test", "RUNNING")]
        assert JobRepository(session).get_by_operation_id("op-test-0001") is jobs[0]
        assert [event.event_type for event in EventRepository(session).for_run("run_test")] == ["WORK_ORDER_DISPATCHED"]


def test_terminal_work_order_cannot_reopen(database: tuple[Path, sessionmaker[Session]]) -> None:
    _, sessions = database
    with sessions.begin() as session:
        row = session.get(WorkOrderRecord, "wo_test")
        assert row is not None
        row.state = WorkOrderState.ACCEPTED.value
    service = TransactionalTransitionService(sessions)
    with pytest.raises(InvalidTransition):
        service.transition_work_order("wo_test", 1, WorkOrderState.REVIEWING, **transition_kwargs("ILLEGAL_REOPEN"))
    with sessions() as session:
        assert session.scalar(select(AuditEventRecord).where(AuditEventRecord.event_type == "ILLEGAL_REOPEN")) is None


def test_event_and_transition_commit_atomically(database: tuple[Path, sessionmaker[Session]]) -> None:
    _, sessions = database
    service = TransactionalTransitionService(sessions)
    service.transition_work_order("wo_test", 1, WorkOrderState.DISPATCHED, **transition_kwargs())
    with sessions() as session:
        row = session.get(WorkOrderRecord, "wo_test")
        event = session.scalar(select(AuditEventRecord).where(AuditEventRecord.entity_id == "wo_test"))
        assert row is not None and row.state == "DISPATCHED" and row.version == 2
        assert event is not None and event.event_type == "WORK_ORDER_DISPATCHED"


def test_failed_event_insert_rolls_back_transition(database: tuple[Path, sessionmaker[Session]]) -> None:
    _, sessions = database
    service = TransactionalTransitionService(sessions)
    with pytest.raises((StatementError, TypeError)):
        service.transition_work_order("wo_test", 1, WorkOrderState.DISPATCHED, **transition_kwargs(metadata={"not_json": object()}))
    with sessions() as session:
        row = session.get(WorkOrderRecord, "wo_test")
        assert row is not None and row.state == "READY" and row.version == 1
        assert session.scalar(select(AuditEventRecord).where(AuditEventRecord.entity_id == "wo_test")) is None


def test_append_only_event_primary_key_prevents_overwrite(database: tuple[Path, sessionmaker[Session]]) -> None:
    _, sessions = database
    now = datetime.now(UTC)
    event = AuditEventRecord(event_id="evt_fixed", event_type="TEST", run_id="run_test", entity_type="run", entity_id="run_test", actor_type="controller", actor_id="c1", timestamp=now, correlation_id="corr", causation_id=None, metadata_json={})
    with sessions.begin() as session:
        session.add(event)
    with pytest.raises(IntegrityError):
        with sessions.begin() as session:
            session.add(AuditEventRecord(event_id="evt_fixed", event_type="REPLACEMENT", run_id="run_test", entity_type="run", entity_id="run_test", actor_type="controller", actor_id="c1", timestamp=now, correlation_id="corr", causation_id=None, metadata_json={}))
