"""PX02-03: research lifecycle (init / status / interactive entry)."""

import functools
import json
import os
import socket
import subprocess
import sys
import threading
import time
from datetime import UTC, datetime
from http.server import ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy.orm import Session, sessionmaker

from researchd.api.control import LocalControlAPI
from researchd.api.web import make_handler
from researchd.client import lifecycle
from researchd.client.cli import main
from researchd.client.transport import ResearchClient, load_owner_token
from researchd.daemon.command_service import DurableDaemonCommandService
from researchd.daemon.composition import DaemonConfig
from researchd.daemon.contracts import DaemonCommandResult, WorkspaceCreateCommand
from researchd.daemon.runtime import ResearchDaemon
from researchd.daemon.startup import StartupBarrier, StartupPhase
from researchd.domain.base import DomainModel
from researchd.storage.db import create_sqlite_engine, session_factory
from researchd.storage.models import AgentRecord
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


def _free_port() -> int:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def _server(tmp_path: Path, *, ready: bool = True) -> tuple[ThreadingHTTPServer, int]:
    database = tmp_path / "lifecycle.db"
    migrate(database)
    sessions: sessionmaker[Session] = session_factory(create_sqlite_engine(database))
    api = LocalControlAPI(sessions)
    durable = DurableDaemonCommandService(sessions, _WorkspaceDispatcher())
    if ready:
        barrier = StartupBarrier({phase: lambda: None for phase in StartupPhase})
    else:

        def _explode() -> None:
            raise RuntimeError("phase exploded")

        barrier = StartupBarrier({phase: _explode for phase in StartupPhase})
    daemon = ResearchDaemon(barrier, durable)
    assert daemon.start().ready is ready
    handler = make_handler(api, daemon, control_token=TOKEN)
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, port


def _config_file(tmp_path: Path, port: int) -> Path:
    config = tmp_path / "researchd.json"
    config.write_text(
        json.dumps({
            "database": str(tmp_path / "researchd.db"),
            "artifact_root": str(tmp_path / "artifacts"),
            "state_root": str(tmp_path / "state"),
            "repositories": {},
            "job_commands": {},
            "host": "127.0.0.1",
            "port": port,
        }),
        encoding="utf-8",
    )
    return config


def _token_file(state_root: Path) -> None:
    state_root.mkdir(parents=True, exist_ok=True)
    token = state_root / "control.token"
    token.write_text(f"{TOKEN}\n", encoding="ascii")
    os.chmod(token, 0o600)


def test_researchd_argv_only_spawns_trusted_forms() -> None:
    path = Path("/tmp/lifecycle/researchd.json")
    assert lifecycle.researchd_argv(path, "init") == [
        sys.executable,
        "-m",
        "researchd.daemon.cli",
        "--config",
        str(path),
        "init",
    ]
    assert lifecycle.researchd_argv(path, "serve")[-1] == "serve"


def test_run_init_delegates_to_researchd_and_forwards_exit_codes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[list[str]] = []

    def fake_run(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        seen.append(argv)
        return subprocess.CompletedProcess(argv, 0)

    monkeypatch.setattr("subprocess.run", fake_run)
    assert lifecycle.run_init(Path("/tmp/lifecycle/researchd.json")) == 0
    assert seen[0][-1] == "init"

    def failing_run(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(argv, 3)

    monkeypatch.setattr("subprocess.run", failing_run)
    assert lifecycle.run_init(Path("/tmp/lifecycle/researchd.json")) == 3


def test_status_reports_a_ready_daemon(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    server, port = _server(tmp_path, ready=True)
    try:
        config = _config_file(tmp_path, port)
        assert main(["--config", str(config), "status"]) == 0
        document = json.loads(capsys.readouterr().out)
        assert document["reachable"] is True
        assert document["state"] == "READY"
        assert document["ready"] is True
    finally:
        server.shutdown()
        server.server_close()


def test_status_reports_a_failed_daemon(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    server, port = _server(tmp_path, ready=False)
    try:
        config = _config_file(tmp_path, port)
        assert main(["--config", str(config), "status"]) == 1
        document = json.loads(capsys.readouterr().out)
        assert document["reachable"] is True
        assert document["state"] == "FAILED"
        assert document["ready"] is False
        phases = document["startup"]["phases"]
        assert phases[0]["status"] == "FAIL"
        assert phases[0]["error_type"] == "RuntimeError"
    finally:
        server.shutdown()
        server.server_close()


def test_status_reports_an_unreachable_daemon(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    config = _config_file(tmp_path, _free_port())
    assert main(["--config", str(config), "status"]) == 1
    assert json.loads(capsys.readouterr().out) == {"reachable": False}


def test_interactive_entry_never_bypasses_a_non_ready_daemon(
    tmp_path: Path,
) -> None:
    server, port = _server(tmp_path, ready=False)
    lines: list[str] = []
    try:
        config = _config_file(tmp_path, port)
        _token_file(tmp_path / "state")
        code = lifecycle.interactive_entry(
            config,
            spawn=False,
            input_fn=lambda: "quit",
            print_fn=lines.append,
        )
        assert code == 1
        assert not any("interactive shell" in line for line in lines)
        assert any("not ready" in line for line in lines)
    finally:
        server.shutdown()
        server.server_close()


def test_interactive_entry_refuses_when_no_daemon_is_reachable(
    tmp_path: Path,
) -> None:
    config = _config_file(tmp_path, _free_port())
    lines: list[str] = []
    code = lifecycle.interactive_entry(
        config,
        spawn=False,
        input_fn=lambda: "quit",
        print_fn=lines.append,
    )
    assert code == 1
    assert any("no researchd reachable" in line for line in lines)


def test_interactive_entry_enters_the_shell_of_a_ready_daemon(
    tmp_path: Path,
) -> None:
    server, port = _server(tmp_path, ready=True)
    try:
        config = _config_file(tmp_path, port)
        _token_file(tmp_path / "state")
        lines: list[str] = []
        inputs = iter(["frobnicate", "quit"])
        code = lifecycle.interactive_entry(
            config,
            spawn=False,
            input_fn=lambda: next(inputs),
            print_fn=lines.append,
        )
        assert code == 0
        assert any("interactive shell" in line for line in lines)
        assert "parse error: unknown command: frobnicate" in lines
    finally:
        server.shutdown()
        server.server_close()


def test_wait_for_ready_fails_fast_on_a_failed_daemon(tmp_path: Path) -> None:
    server, port = _server(tmp_path, ready=False)
    try:
        config = lifecycle.load_client_config(_config_file(tmp_path, port))
        started = time.monotonic()
        with pytest.raises(lifecycle.DaemonNotReadyError) as error:
            lifecycle.wait_for_ready(config, timeout=10)
        assert "state=FAILED" in str(error.value)
        assert "MIGRATION_CHECK" in str(error.value)
        assert time.monotonic() - started < 5
    finally:
        server.shutdown()
        server.server_close()


def test_interactive_entry_spawns_daemon_without_owning_its_lifecycle(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _config_file(tmp_path, _free_port())
    # Bootstrap through the client: it delegates to `researchd init` and
    # therefore never initializes or migrates the database itself.
    assert main(["--config", str(config), "init"]) == 0
    assert (tmp_path / "state" / "control.token").exists()

    spawned: list[subprocess.Popen[bytes]] = []
    real_spawn = lifecycle.spawn_daemon

    def capture_spawn(
        config_model: DaemonConfig,
        config_path: Path,
    ) -> subprocess.Popen[bytes]:
        process = real_spawn(config_model, config_path)
        spawned.append(process)
        return process

    monkeypatch.setattr(lifecycle, "spawn_daemon", capture_spawn)
    try:
        lines: list[str] = []
        code = lifecycle.interactive_entry(
            config,
            input_fn=lambda: "quit",
            print_fn=lines.append,
        )
        assert code == 0
        assert any("interactive shell" in line for line in lines)
        assert (tmp_path / "state" / "daemon.log").exists()

        # Closing the client window does not stop the controller it helped start.
        config_model = lifecycle.load_client_config(config)
        assert lifecycle.probe_health(config_model) is not None
        assert spawned[0].poll() is None
    finally:
        for process in spawned:
            process.terminate()
            process.wait(timeout=5)


def _terminate_live(processes: list[subprocess.Popen[bytes]]) -> None:
    for process in processes:
        if process.poll() is None:
            process.terminate()
            process.wait(timeout=5)


def _failed_health_server(tmp_path: Path, port: int) -> ThreadingHTTPServer:
    """A health endpoint that answers 503 with a FAILED startup report."""
    database = tmp_path / "failed_startup.db"
    migrate(database)
    sessions = session_factory(create_sqlite_engine(database))
    api = LocalControlAPI(sessions)
    durable = DurableDaemonCommandService(sessions, _WorkspaceDispatcher())

    def _explode() -> None:
        raise RuntimeError("phase exploded")

    barrier = StartupBarrier({phase: _explode for phase in StartupPhase})
    daemon = ResearchDaemon(barrier, durable)
    assert daemon.start().ready is False
    handler = make_handler(api, daemon, control_token=TOKEN)
    server = ThreadingHTTPServer(("127.0.0.1", port), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def test_daemon_restart_first_start_reaches_ready(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    port = _free_port()
    config = _config_file(tmp_path, port)
    assert main(["--config", str(config), "init"]) == 0
    assert (tmp_path / "state" / "control.token").exists()
    assert not (tmp_path / "state" / "daemon.identity.json").exists()

    spawned: list[subprocess.Popen[bytes]] = []
    real_spawn = lifecycle.spawn_daemon

    def capture_spawn(
        config_model: DaemonConfig,
        config_path: Path,
    ) -> subprocess.Popen[bytes]:
        process = real_spawn(config_model, config_path)
        spawned.append(process)
        return process

    monkeypatch.setattr(lifecycle, "spawn_daemon", capture_spawn)
    try:
        assert main(["--config", str(config), "daemon", "restart"]) == 0
        # First start without identity or daemon: log created, READY reached.
        assert (tmp_path / "state" / "daemon.log").exists()
        config_model = lifecycle.load_client_config(config)
        health = lifecycle.probe_health(config_model)
        assert health is not None and health.get("ready") is True
        assert (tmp_path / "state" / "daemon.identity.json").exists()
    finally:
        _terminate_live(spawned)


def test_daemon_restart_preserves_credentials_database_and_registrations(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    port = _free_port()
    config = _config_file(tmp_path, port)
    assert main(["--config", str(config), "init"]) == 0

    # Seed an Agent registration record directly (no aweswitch profile needed).
    database = tmp_path / "researchd.db"
    sessions = session_factory(create_sqlite_engine(database))
    now = datetime.now(UTC)
    with sessions.begin() as session:
        session.add(AgentRecord(
            agent_id="agent_restart_check",
            display_name="Restart check",
            trust_zone="LOCAL",
            version=1,
            created_at=now,
            updated_at=now,
        ))

    spawned: list[subprocess.Popen[bytes]] = []
    real_spawn = lifecycle.spawn_daemon

    def capture_spawn(
        config_model: DaemonConfig,
        config_path: Path,
    ) -> subprocess.Popen[bytes]:
        process = real_spawn(config_model, config_path)
        spawned.append(process)
        return process

    monkeypatch.setattr(lifecycle, "spawn_daemon", capture_spawn)
    try:
        assert main(["--config", str(config), "daemon", "restart"]) == 0
        config_model = lifecycle.load_client_config(config)
        token = load_owner_token(config_model.state_root)
        client = ResearchClient(lifecycle.base_url_for(config_model), token)
        try:
            envelope = client.post_command(
                "/api/workspaces",
                {"workspace_id": "ws_restart_check", "name": "Restart check"},
            )
            assert envelope["status"] == "ACCEPTED"
        finally:
            client.close()

        token_path = tmp_path / "state" / "control.token"
        token_before = token_path.stat()
        database_before = database.stat()
        old_identity = json.loads(
            (tmp_path / "state" / "daemon.identity.json").read_text(encoding="utf-8")
        )
        old_pid = old_identity["pid"]

        assert main(["--config", str(config), "daemon", "restart"]) == 0

        # The old strong-identity process is gone; a new one took over.
        with pytest.raises(ProcessLookupError):
            os.kill(old_pid, 0)
        new_identity = json.loads(
            (tmp_path / "state" / "daemon.identity.json").read_text(encoding="utf-8")
        )
        assert new_identity["pid"] != old_pid

        # The database and control credential are not recreated.
        assert token_path.stat().st_mtime_ns == token_before.st_mtime_ns
        assert token_path.stat().st_ino == token_before.st_ino
        assert database.stat().st_ino == database_before.st_ino

        # Agent/Workspace registration records survive the restart.
        client = ResearchClient(lifecycle.base_url_for(config_model), token)
        try:
            workspaces = client.get("/api/workspaces")
            assert any(
                item["workspace_id"] == "ws_restart_check" for item in workspaces
            )
            agents = client.get("/api/agents")
            assert any(item["agent_id"] == "agent_restart_check" for item in agents)
        finally:
            client.close()
    finally:
        _terminate_live(spawned)


def test_restart_refuses_second_instance_when_reachable_daemon_does_not_stop(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    server, port = _server(tmp_path, ready=True)
    try:
        config = _config_file(tmp_path, port)
        spawned: list[Path] = []

        def refuse_spawn(
            config_model: DaemonConfig,
            config_path: Path,
        ) -> subprocess.Popen[bytes]:
            spawned.append(config_path)
            return subprocess.Popen([sys.executable, "-c", "pass"])

        monkeypatch.setattr(lifecycle, "spawn_daemon", refuse_spawn)
        lines: list[str] = []
        code = lifecycle.restart_daemon(config, print_fn=lines.append)

        assert code == 1
        assert any("did not stop" in line for line in lines)
        assert spawned == []
    finally:
        server.shutdown()
        server.server_close()


def test_restart_fail_closed_on_spawn_oserror(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _config_file(tmp_path, _free_port())

    def broken_spawn(
        config_model: DaemonConfig,
        config_path: Path,
    ) -> subprocess.Popen[bytes]:
        raise OSError("spawn refused")

    monkeypatch.setattr(lifecycle, "spawn_daemon", broken_spawn)
    lines: list[str] = []
    code = lifecycle.restart_daemon(config, print_fn=lines.append)

    assert code == 1
    joined = "\n".join(lines)
    assert "researchd restart failed" in joined
    assert "spawn refused" in joined
    # Safe diagnostics: no control credential and no fixed spawn argv.
    assert TOKEN not in joined
    assert "researchd.daemon.cli" not in joined


def test_restart_fail_closed_on_failed_startup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    port = _free_port()
    config = _config_file(tmp_path, port)
    servers: list[ThreadingHTTPServer] = []

    def fake_spawn(
        config_model: DaemonConfig,
        config_path: Path,
    ) -> subprocess.Popen[bytes]:
        servers.append(_failed_health_server(tmp_path, port))
        return subprocess.Popen([sys.executable, "-c", "pass"])

    monkeypatch.setattr(lifecycle, "spawn_daemon", fake_spawn)
    try:
        lines: list[str] = []
        code = lifecycle.restart_daemon(config, print_fn=lines.append)

        assert code == 1
        joined = "\n".join(lines)
        assert "state=FAILED" in joined
        assert "MIGRATION_CHECK" in joined
        assert TOKEN not in joined
        assert "researchd.daemon.cli" not in joined
    finally:
        for server in servers:
            server.shutdown()
            server.server_close()


def test_restart_fail_closed_on_ready_timeout(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _config_file(tmp_path, _free_port())

    def idle_spawn(
        config_model: DaemonConfig,
        config_path: Path,
    ) -> subprocess.Popen[bytes]:
        return subprocess.Popen([sys.executable, "-c", "pass"])

    monkeypatch.setattr(lifecycle, "spawn_daemon", idle_spawn)
    real_wait = lifecycle.wait_for_ready
    monkeypatch.setattr(
        lifecycle, "wait_for_ready", functools.partial(real_wait, timeout=1.0)
    )

    lines: list[str] = []
    code = lifecycle.restart_daemon(config, print_fn=lines.append)

    assert code == 1
    joined = "\n".join(lines)
    assert "did not become reachable" in joined
    assert TOKEN not in joined
    assert "researchd.daemon.cli" not in joined
