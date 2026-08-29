import asyncio
from datetime import UTC, datetime
from pathlib import Path
from threading import Thread
from typing import Any, cast

from alembic import command
from alembic.config import Config
import httpx
from pydantic import ValidationError
import pytest
from sqlalchemy import select, text
from sqlalchemy.orm import Session, sessionmaker

from researchd.api.agui import AGUIProjectionAdapter
from researchd.api.control import LocalControlAPI
from researchd.api.web import ControlCommandRouter, ControlResourceRouter, serve_local_control
from researchd.storage.db import create_sqlite_engine, session_factory
from researchd.storage.models import AuditEventRecord, ResearchRunRecord, WorkspaceRecord


ROOT = Path(__file__).parents[2]


def _config(path: Path) -> Config:
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("sqlalchemy.url", f"sqlite:///{path}")
    return config


def _seed_run(path: Path) -> sessionmaker[Session]:
    command.upgrade(_config(path), "head")
    sessions = session_factory(create_sqlite_engine(path))
    now = datetime.now(UTC)
    with sessions.begin() as session:
        session.add(WorkspaceRecord(workspace_id="ws_stream", name="stream", version=1, created_at=now, updated_at=now))
        session.flush()
        session.add(ResearchRunRecord(
            run_id="run_stream",
            workspace_id="ws_stream",
            objective="project an Agent collaboration run",
            state="ACTIVE",
            max_iterations=8,
            max_cloud_calls=24,
            iterations_used=0,
            cloud_calls_used=0,
            cancellation_requested=False,
            version=1,
            created_at=now,
            updated_at=now,
        ))
    return sessions


def _append_event(
    sessions: sessionmaker[Session],
    event_id: str,
    event_type: str,
    *,
    timestamp: datetime,
    entity_type: str = "work_order",
) -> None:
    with sessions.begin() as session:
        session.add(AuditEventRecord(
            event_id=event_id,
            event_type=event_type,
            run_id="run_stream",
            entity_type=entity_type,
            entity_id="run_stream" if entity_type == "research_run" else "wo_stream",
            actor_type="controller",
            actor_id="orchestrator",
            timestamp=timestamp,
            correlation_id="run_stream",
            causation_id=None,
            metadata_json={},
        ))


def test_0016_backfills_deterministically_and_trigger_continues_sequence(tmp_path: Path) -> None:
    path = tmp_path / "backfill.db"
    config = _config(path)
    command.upgrade(config, "0015")
    engine = create_sqlite_engine(path)
    with engine.begin() as connection:
        connection.execute(text(
            "INSERT INTO workspaces (workspace_id, name, version, created_at, updated_at) "
            "VALUES ('ws', 'legacy', 1, '2026-01-01 00:00:00', '2026-01-01 00:00:00')"
        ))
        connection.execute(text(
            "INSERT INTO research_runs "
            "(run_id, workspace_id, objective, state, version, created_at, updated_at) "
            "VALUES ('run', 'ws', 'legacy', 'ACTIVE', 1, '2026-01-01 00:00:00', '2026-01-01 00:00:00')"
        ))
        for event_id in ("evt_b", "evt_a"):
            connection.execute(text(
                "INSERT INTO audit_events "
                "(event_id, event_type, run_id, entity_type, entity_id, actor_type, actor_id, timestamp, correlation_id, causation_id, metadata) "
                "VALUES (:event_id, 'TEST', 'run', 'run', 'run', 'controller', 'c', '2026-01-01 00:00:00', 'run', NULL, '{}')"
            ), {"event_id": event_id})
    engine.dispose()

    command.upgrade(config, "head")
    upgraded = create_sqlite_engine(path)
    with upgraded.begin() as connection:
        rows = [tuple(row) for row in connection.execute(
            text("SELECT event_id, audit_seq FROM audit_events ORDER BY audit_seq")
        )]
        assert rows == [
            ("evt_a", 1),
            ("evt_b", 2),
        ]
        connection.execute(text(
            "INSERT INTO audit_events "
            "(event_id, event_type, run_id, entity_type, entity_id, actor_type, actor_id, timestamp, correlation_id, causation_id, metadata) "
            "VALUES ('evt_c', 'TEST', 'run', 'run', 'run', 'controller', 'c', '2026-01-01 00:00:00', 'run', NULL, '{}')"
        ))
        assert connection.scalar(text("SELECT audit_seq FROM audit_events WHERE event_id = 'evt_c'")) == 3
    command.check(config)


def test_stream_offsets_order_equal_timestamps_and_resume_without_duplicates(tmp_path: Path) -> None:
    sessions = _seed_run(tmp_path / "stream.db")
    same_time = datetime.now(UTC)
    _append_event(sessions, "evt_z", "APPROVAL_REQUESTED", timestamp=same_time)
    _append_event(sessions, "evt_a", "VERIFICATION_COMPLETED", timestamp=same_time)

    api = LocalControlAPI(sessions)
    events = api.events("run_stream")
    assert [event["event_id"] for event in events] == ["evt_z", "evt_a"]
    assert [event["stream_offset"] for event in events] == sorted(event["stream_offset"] for event in events)
    tail = api.events("run_stream", after_stream_offset=cast(int, events[0]["stream_offset"]))
    assert [event["event_id"] for event in tail] == ["evt_a"]
    status, resource = ControlResourceRouter(api).get(
        f"/api/events/run_stream?after={events[0]['stream_offset']}"
    )
    assert status == 200 and isinstance(resource, dict)
    assert [event["event_id"] for event in resource["events"]] == ["evt_a"]


def test_agui_projection_has_snapshot_lifecycle_custom_activity_and_delta(tmp_path: Path) -> None:
    sessions = _seed_run(tmp_path / "projection.db")
    now = datetime.now(UTC)
    _append_event(sessions, "evt_run", "RUN_CREATED", timestamp=now, entity_type="research_run")
    _append_event(sessions, "evt_approval", "APPROVAL_REQUESTED", timestamp=now)
    _append_event(sessions, "evt_attempt", "ATTEMPT_RUNNING", timestamp=now)
    _append_event(sessions, "evt_plan", "PLAN_CREATED", timestamp=now)

    projection = AGUIProjectionAdapter(LocalControlAPI(sessions))
    replay = projection.replay("run_stream")
    assert [item.event.type for item in replay] == [
        "STATE_SNAPSHOT",
        "RUN_STARTED",
        "CUSTOM",
        "ACTIVITY_SNAPSHOT",
        "STATE_DELTA",
    ]
    offsets = [item.stream_offset for item in replay[1:]]
    resumed = projection.replay("run_stream", after_stream_offset=offsets[1])
    assert [item.stream_offset for item in resumed] == offsets[2:]
    assert replay[1].as_sse().startswith(f"id: {offsets[0]}\nevent: ag-ui\ndata: ".encode())


class _CommandSpy:
    def __init__(self) -> None:
        self.calls: list[tuple[str, ...]] = []

    async def cancel_run(self, run_id: str) -> dict[str, Any]:
        self.calls.append(("cancel", run_id))
        return {"run_id": run_id}

    async def approve(self, work_order_id: str, grant_id: str) -> dict[str, Any]:
        self.calls.append(("approve", work_order_id, grant_id))
        return {"work_order_id": work_order_id}

    def resolve_human(self, work_order_id: str, *, action: str, objective: str | None = None) -> dict[str, Any]:
        self.calls.append(("human", work_order_id, action, objective or ""))
        return {"work_order_id": work_order_id}


def test_ui_commands_are_typed_and_cannot_submit_arbitrary_mutation_events() -> None:
    spy = _CommandSpy()
    router = ControlCommandRouter(cast(LocalControlAPI, spy))
    assert asyncio.run(router.post("/api/runs/run_1/cancel", {}))[0] == 200
    assert asyncio.run(router.post("/api/work-orders/wo_1/approve", {"grant_id": "grant_1"}))[0] == 200
    assert asyncio.run(router.post(
        "/api/work-orders/wo_1/human-decision",
        {"action": "revise", "objective": "narrow the task"},
    ))[0] == 200
    calls_before = list(spy.calls)
    assert asyncio.run(router.post("/api/events/run_1", {"event_type": "RUN_COMPLETED"}))[0] == 404
    assert spy.calls == calls_before
    with pytest.raises(ValidationError):
        asyncio.run(router.post("/api/runs/run_1/cancel", {"event_type": "RUN_COMPLETED"}))
    with pytest.raises(ValidationError):
        asyncio.run(router.post("/api/work-orders/wo_1/human-decision", {"action": "revise"}))


def test_loopback_sse_honors_last_event_id(tmp_path: Path) -> None:
    sessions = _seed_run(tmp_path / "http-stream.db")
    now = datetime.now(UTC)
    _append_event(sessions, "evt_first", "PLAN_CREATED", timestamp=now)
    _append_event(sessions, "evt_second", "WORK_ORDER_CREATED", timestamp=now)
    api = LocalControlAPI(sessions)
    first_offset = cast(int, api.events("run_stream")[0]["stream_offset"])
    server = serve_local_control(api, port=0)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        host, port = server.server_address[:2]
        host_text = host.decode("ascii") if isinstance(host, bytes) else host
        response = httpx.get(
            f"http://{host_text}:{port}/api/runs/run_stream/stream",
            headers={"Last-Event-ID": str(first_offset)},
            timeout=5,
        )
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/event-stream")
        assert "evt_first" not in response.text
        assert "evt_second" in response.text
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
