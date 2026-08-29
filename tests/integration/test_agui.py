import asyncio
from datetime import UTC, datetime, timedelta
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

from researchd.api.agui import AGUIProjectionAdapter, CustomEvent
from researchd.api.control import LocalControlAPI
from researchd.api.web import ControlCommandRouter, ControlResourceRouter, serve_local_control
from researchd.policy.approval import ApprovalNotValid, ApprovalService
from researchd.storage.db import create_sqlite_engine, session_factory
from researchd.storage.models import (
    AuditEventRecord,
    CollaborationMessageRecord,
    ResearchRunRecord,
    WorkspaceRecord,
)


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


def test_controller_restart_preserves_monotonic_offsets_and_resume_cursor(tmp_path: Path) -> None:
    path = tmp_path / "restart.db"
    sessions = _seed_run(path)
    now = datetime.now(UTC)
    _append_event(sessions, "evt_before_restart", "PLAN_CREATED", timestamp=now)
    before = LocalControlAPI(sessions).events("run_stream")
    assert len(before) == 1 and before[0]["stream_offset"] == 1

    restarted = session_factory(create_sqlite_engine(path))
    _append_event(restarted, "evt_after_restart", "WORK_ORDER_CREATED", timestamp=now)
    api = LocalControlAPI(restarted)
    after = api.events("run_stream")
    assert [item["event_id"] for item in after] == ["evt_before_restart", "evt_after_restart"]
    assert [item["stream_offset"] for item in after] == [1, 2]
    resumed = AGUIProjectionAdapter(api).replay("run_stream", after_stream_offset=1)
    assert [item.stream_offset for item in resumed] == [2]


@pytest.mark.parametrize("classification", ("LOCAL_ONLY", "SECRET"))
def test_classified_collaboration_body_is_absent_from_agui_and_sse(
    tmp_path: Path, classification: str
) -> None:
    sessions = _seed_run(tmp_path / f"redaction-{classification}.db")
    marker = f"NEVER_EXPOSE_{classification}_BODY"
    now = datetime.now(UTC)
    message_id = f"msg_{classification.lower()}"
    with sessions.begin() as session:
        session.add(CollaborationMessageRecord(
            message_id=message_id,
            run_id="run_stream",
            work_order_id=None,
            sender_actor_type="agent",
            sender_actor_id="agent_sensitive",
            recipient_agent_id=None,
            purpose="STATUS",
            body=marker,
            classification=classification,
            metadata_json={},
            created_at=now,
        ))
        session.add(AuditEventRecord(
            event_id=f"evt_{classification.lower()}",
            event_type="COLLABORATION_MESSAGE_RECORDED",
            run_id="run_stream",
            entity_type="collaboration_message",
            entity_id=message_id,
            actor_type="controller",
            actor_id="collaboration-message-store",
            timestamp=now,
            correlation_id="run_stream",
            causation_id=None,
            metadata_json={"message_id": message_id},
        ))
    projected = AGUIProjectionAdapter(LocalControlAPI(sessions)).replay("run_stream")
    serialized = b"".join(item.as_sse() for item in projected).decode()
    assert marker not in serialized
    redacted = [item.event for item in projected if item.event.type == "CUSTOM"]
    assert len(redacted) == 1
    assert isinstance(redacted[0], CustomEvent)
    assert redacted[0].name == "researchd.message.redacted"
    assert redacted[0].value == {
        "message_id": message_id,
        "classification": classification,
    }


def test_high_volume_replay_has_exact_bounded_sequence(tmp_path: Path) -> None:
    sessions = _seed_run(tmp_path / "high-volume.db")
    now = datetime.now(UTC)
    count = 2_000
    with sessions.begin() as session:
        session.add_all([
            AuditEventRecord(
                event_id=f"evt_load_{index:04d}",
                event_type="PLAN_CREATED",
                run_id="run_stream",
                entity_type="plan",
                entity_id=f"plan_{index:04d}",
                actor_type="controller",
                actor_id="load-fixture",
                timestamp=now,
                correlation_id="run_stream",
                causation_id=None,
                metadata_json={"index": index},
            )
            for index in range(count)
        ])
    replay = AGUIProjectionAdapter(LocalControlAPI(sessions)).replay("run_stream")
    offsets = [item.stream_offset for item in replay[1:]]
    assert offsets == list(range(1, count + 1))
    assert len(set(offsets)) == count
    serialized_bytes = sum(len(item.as_sse()) for item in replay)
    assert serialized_bytes < 2_000_000


def test_simultaneous_typed_approval_commands_preserve_one_shot_authority(tmp_path: Path) -> None:
    sessions = _seed_run(tmp_path / "concurrent-commands.db")
    approvals = ApprovalService(sessions)
    parameters = {"work_order_id": "wo_stream", "capabilities": ["network.external"]}
    request = approvals.request(
        operation_type="work_order.capabilities",
        parameters=parameters,
        requested_by="agent_requester",
        reason="exercise typed command concurrency",
        risk_level="high",
        resource_scope={"work_order_id": "wo_stream"},
        budget_delta={},
        expires_at=datetime.now(UTC) + timedelta(minutes=5),
        one_shot=True,
    )
    grant = approvals.approve(request.approval_id, granted_by="human_reviewer")

    class ApprovalAPI:
        async def approve(self, work_order_id: str, grant_id: str) -> dict[str, Any]:
            await asyncio.to_thread(
                approvals.authorize,
                grant_id,
                operation_type="work_order.capabilities",
                parameters={"work_order_id": work_order_id, "capabilities": ["network.external"]},
            )
            return {"work_order_id": work_order_id, "authorized": True}

    router = ControlCommandRouter(cast(LocalControlAPI, ApprovalAPI()))

    async def invoke() -> object:
        try:
            return await router.post(
                "/api/work-orders/wo_stream/approve",
                {"grant_id": grant.grant_id},
            )
        except ApprovalNotValid as error:
            return error

    async def invoke_concurrently() -> list[object]:
        return list(await asyncio.gather(invoke(), invoke()))

    results = asyncio.run(invoke_concurrently())
    successes = [item for item in results if isinstance(item, tuple)]
    failures = [item for item in results if isinstance(item, ApprovalNotValid)]
    assert len(successes) == 1 and successes[0][0] == 200
    assert len(failures) == 1 and "already been used" in str(failures[0])
