import asyncio
from datetime import UTC, datetime
from pathlib import Path

import pytest
from alembic import command as alembic_command
from alembic.config import Config
from sqlalchemy import inspect, select, text

from researchd.daemon.command_service import (
    DaemonCommandConflict,
    DurableDaemonCommandService,
)
from researchd.daemon.contracts import DaemonCommandResult, RunCancelCommand
from researchd.daemon.startup import verify_audit_stream
from researchd.api.control import LocalControlAPI
from researchd.storage.db import create_sqlite_engine, session_factory
from researchd.storage.models import AuditEventRecord, DaemonCommandRecord
from tests.integration.test_storage import ROOT, assert_migration_matches_models, migrate


def _command(*, command_id: str = "cmd_durable_1", run_id: str = "run_1") -> RunCancelCommand:
    return RunCancelCommand(
        command_id=command_id,
        actor_type="HUMAN",
        actor_id="operator",
        run_id=run_id,
    )


def test_completed_command_is_durably_replayed_without_side_effect(tmp_path: Path) -> None:
    database = tmp_path / "commands.db"
    migrate(database)
    sessions = session_factory(create_sqlite_engine(database))
    calls: list[str] = []

    def dispatch(command: object) -> DaemonCommandResult:
        assert isinstance(command, RunCancelCommand)
        calls.append(command.run_id)
        return DaemonCommandResult(
            command_id=command.command_id,
            command_type="RunCancel",
            status="ACCEPTED",
            resource={"run_id": command.run_id},
        )

    service = DurableDaemonCommandService(sessions, dispatch)
    first = asyncio.run(service.execute(_command()))
    replay = asyncio.run(service.execute(_command()))

    assert replay == first
    assert calls == ["run_1"]
    with sessions() as session:
        receipt = session.get(DaemonCommandRecord, "cmd_durable_1")
        assert receipt is not None and receipt.status == "COMPLETED"
        events = session.scalars(select(AuditEventRecord).where(
            AuditEventRecord.entity_type == "daemon_command",
        ).order_by(AuditEventRecord.audit_seq)).all()
    assert [event.event_type for event in events] == [
        "DAEMON_COMMAND_ACCEPTED",
        "DAEMON_COMMAND_COMPLETED",
    ]
    assert [event.audit_seq for event in events] == [1, 2]


def test_command_identity_change_is_rejected(tmp_path: Path) -> None:
    database = tmp_path / "commands.db"
    migrate(database)
    sessions = session_factory(create_sqlite_engine(database))
    service = DurableDaemonCommandService(
        sessions,
        lambda command: DaemonCommandResult(
            command_id=command.command_id,  # type: ignore[attr-defined]
            command_type="RunCancel",
            status="ACCEPTED",
        ),
    )
    asyncio.run(service.execute(_command()))

    with pytest.raises(DaemonCommandConflict, match="different request"):
        asyncio.run(service.execute(_command(run_id="run_changed")))


def test_dispatch_failure_is_durable_rejected_result(tmp_path: Path) -> None:
    database = tmp_path / "commands.db"
    migrate(database)
    sessions = session_factory(create_sqlite_engine(database))
    calls = 0

    def reject(command: object) -> object:
        nonlocal calls
        del command
        calls += 1
        raise LookupError("missing target")

    service = DurableDaemonCommandService(sessions, reject)
    first = asyncio.run(service.execute(_command()))
    replay = asyncio.run(service.execute(_command()))

    assert first == replay
    assert first.status == "REJECTED"
    assert first.reason_code == "LookupError"
    assert calls == 1
    projection = LocalControlAPI(sessions).daemon_commands("REJECTED")
    assert projection == [{
        "command_id": "cmd_durable_1",
        "command_type": "RunCancelCommand",
        "command_version": 1,
        "actor_type": "HUMAN",
        "actor_id": "operator",
        "status": "REJECTED",
        "reason_code": "LookupError",
        "created_at": projection[0]["created_at"],
        "updated_at": projection[0]["updated_at"],
    }]
    with pytest.raises(ValueError, match="invalid daemon command status"):
        LocalControlAPI(sessions).daemon_commands("UNKNOWN")


def test_uncertain_reserved_command_is_not_replayed_and_blocks_health(
    tmp_path: Path,
) -> None:
    database = tmp_path / "commands.db"
    migrate(database)
    engine = create_sqlite_engine(database)
    sessions = session_factory(engine)
    now = datetime.now(UTC)
    command = _command()
    with sessions.begin() as session:
        session.add(DaemonCommandRecord(
            command_id=command.command_id,
            command_type=type(command).__name__,
            command_version=command.command_version,
            request_sha256=DurableDaemonCommandService._request_sha256(command),
            actor_type=command.actor_type,
            actor_id=command.actor_id,
            status="ACCEPTED",
            result_json=None,
            reason_code=None,
            created_at=now,
            updated_at=now,
        ))
    calls: list[object] = []
    service = DurableDaemonCommandService(sessions, lambda item: calls.append(item))

    result = asyncio.run(service.execute(command))

    assert result.status == "ACCEPTED"
    assert result.reason_code == "COMMAND_OUTCOME_UNKNOWN"
    assert calls == []
    with pytest.raises(RuntimeError, match="operator reconciliation"):
        verify_audit_stream(engine)


def test_current_0020_database_upgrades_to_daemon_receipts(tmp_path: Path) -> None:
    database = tmp_path / "current-0020.db"
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("sqlalchemy.url", f"sqlite:///{database}")
    alembic_command.upgrade(config, "0020")

    alembic_command.upgrade(config, "head")

    assert_migration_matches_models(database)
    engine = create_sqlite_engine(database)
    assert "daemon_commands" in inspect(engine).get_table_names()
    with engine.connect() as connection:
        assert connection.scalar(text("SELECT version_num FROM alembic_version")) == "0021"
