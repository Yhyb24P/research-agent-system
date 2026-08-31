"""PX02-02: authenticated client transport against the live control API."""

import re
import threading
from http.server import ThreadingHTTPServer
from pathlib import Path
from typing import cast

import pytest
from sqlalchemy.orm import Session, sessionmaker

from researchd.api.control import LocalControlAPI
from researchd.api.web import make_handler
from researchd.client.transport import (
    AuthenticationRequired,
    CommandNotAccepted,
    InvalidCommand,
    ResearchClient,
    StreamFrame,
    new_command_id,
)
from researchd.daemon.command_service import DurableDaemonCommandService
from researchd.daemon.contracts import DaemonCommandResult, WorkspaceCreateCommand
from researchd.daemon.runtime import ResearchDaemon
from researchd.daemon.startup import StartupBarrier, StartupPhase
from researchd.domain.base import DomainModel
from researchd.storage.db import create_sqlite_engine, session_factory
from tests.integration.test_storage import migrate

TOKEN = "f" * 64


class _WorkspaceDispatcher:
    """Records workspace commands; everything else fails closed."""

    def __init__(self) -> None:
        self.commands: list[DomainModel] = []

    def __call__(self, command: DomainModel) -> DaemonCommandResult:
        self.commands.append(command)
        assert isinstance(command, WorkspaceCreateCommand)
        return DaemonCommandResult(
            command_id=command.command_id,
            command_type="WorkspaceCreate",
            status="ACCEPTED",
            resource={
                "workspace_id": command.workspace_id,
                "name": command.name,
                "version": 1,
            },
        )


def _server(
    tmp_path: Path,
    *,
    with_daemon: bool = True,
) -> tuple[ThreadingHTTPServer, str, _WorkspaceDispatcher | None]:
    database = tmp_path / "client.db"
    migrate(database)
    sessions: sessionmaker[Session] = session_factory(create_sqlite_engine(database))
    api = LocalControlAPI(sessions)
    dispatcher: _WorkspaceDispatcher | None = None
    daemon: ResearchDaemon | None = None
    if with_daemon:
        dispatcher = _WorkspaceDispatcher()
        durable = DurableDaemonCommandService(sessions, dispatcher)
        barrier = StartupBarrier({phase: lambda: None for phase in StartupPhase})
        daemon = ResearchDaemon(barrier, durable)
        assert daemon.start().ready
    handler = make_handler(api, daemon, control_token=TOKEN)
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, f"http://127.0.0.1:{port}", dispatcher


def test_health_reports_ready_state(tmp_path: Path) -> None:
    server, base, _ = _server(tmp_path)
    try:
        with ResearchClient(base) as client:
            health = client.health()
        assert health["state"] == "READY"
        assert health["ready"] is True
    finally:
        server.shutdown()
        server.server_close()


def test_authenticated_reads_require_the_owner_token(tmp_path: Path) -> None:
    server, base, _ = _server(tmp_path)
    try:
        with ResearchClient(base) as anonymous:
            with pytest.raises(AuthenticationRequired):
                anonymous.get("/api/agents")
        with ResearchClient(base, "0" * 64) as forged:
            with pytest.raises(AuthenticationRequired):
                forged.get("/api/agents")
        with ResearchClient(base, TOKEN) as client:
            agents = client.get("/api/agents")
        assert agents == []
    finally:
        server.shutdown()
        server.server_close()


def test_post_command_generates_a_command_identity(tmp_path: Path) -> None:
    server, base, _ = _server(tmp_path)
    try:
        with ResearchClient(base, TOKEN) as client:
            envelope = client.post_command(
                "/api/workspaces",
                {"workspace_id": "ws_client", "name": "client"},
            )
        assert envelope["status"] == "ACCEPTED"
        assert envelope["command_type"] == "WorkspaceCreate"
        assert re.fullmatch(r"cmd_[0-9a-f]{32}", str(envelope["command_id"]))
    finally:
        server.shutdown()
        server.server_close()


def test_post_command_replays_and_conflicts_by_identity(tmp_path: Path) -> None:
    server, base, _ = _server(tmp_path)
    try:
        with ResearchClient(base, TOKEN) as client:
            first = client.post_command(
                "/api/workspaces",
                {"workspace_id": "ws_replay", "name": "replay"},
                command_id="cmd_replay_1",
            )
            replay = client.post_command(
                "/api/workspaces",
                {"workspace_id": "ws_replay", "name": "replay"},
                command_id="cmd_replay_1",
            )
            assert replay == first

            with pytest.raises(CommandNotAccepted) as conflict:
                client.post_command(
                    "/api/workspaces",
                    {"workspace_id": "ws_other", "name": "other"},
                    command_id="cmd_replay_1",
                )
        assert "reused" in str(conflict.value)
    finally:
        server.shutdown()
        server.server_close()


def test_extra_fields_are_rejected_before_dispatch(tmp_path: Path) -> None:
    server, base, _ = _server(tmp_path)
    try:
        with ResearchClient(base, TOKEN) as client:
            with pytest.raises(InvalidCommand):
                client.post_command(
                    "/api/workspaces",
                    {"workspace_id": "ws_bad", "name": "bad", "bogus": 1},
                )
    finally:
        server.shutdown()
        server.server_close()


def test_mutation_without_daemon_is_not_accepted(tmp_path: Path) -> None:
    server, base, _ = _server(tmp_path, with_daemon=False)
    try:
        with ResearchClient(base, TOKEN) as client:
            with pytest.raises(CommandNotAccepted):
                client.post_command(
                    "/api/workspaces",
                    {"workspace_id": "ws_nodaemon", "name": "no daemon"},
                )
    finally:
        server.shutdown()
        server.server_close()


def test_sse_stream_resumes_from_the_last_offset(tmp_path: Path) -> None:
    server, base, _ = _server(tmp_path)
    try:
        with ResearchClient(base, TOKEN) as client:
            client.post_command(
                "/api/workspaces",
                {"workspace_id": "ws_stream", "name": "stream"},
                command_id="cmd_stream_1",
            )
            frames = list(client.stream("/api/system-stream"))
        assert frames
        assert all(frame.offset is not None for frame in frames)
        offsets = [cast(int, frame.offset) for frame in frames]
        assert offsets == sorted(offsets)
        assert len(set(offsets)) == len(offsets)
        assert all(isinstance(frame.data, dict) for frame in frames)

        with ResearchClient(base, TOKEN) as client:
            tail = list(client.stream(
                "/api/system-stream",
                after=offsets[-1],
            ))
        resumed_offsets = [frame.offset for frame in tail]
        assert all(offset is None or offset > offsets[-1] for offset in resumed_offsets)
    finally:
        server.shutdown()
        server.server_close()


def test_generated_command_ids_are_unique_and_pattern_safe() -> None:
    generated = {new_command_id() for _ in range(256)}
    assert len(generated) == 256
    for command_id in generated:
        assert re.fullmatch(r"cmd_[0-9a-f]{32}", command_id)
        assert len(command_id) <= 128


def test_stream_frame_exposes_offset_and_payload() -> None:
    frame = StreamFrame(7, {"event": "ok"})
    assert frame.offset == 7
    assert frame.data == {"event": "ok"}
