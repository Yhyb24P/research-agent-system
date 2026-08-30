"""PX02-03: research lifecycle (init / status / interactive entry)."""

import json
import os
import socket
import subprocess
import sys
import threading
import time
from http.server import ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy.orm import Session, sessionmaker

from researchd.api.control import LocalControlAPI
from researchd.api.web import make_handler
from researchd.client import lifecycle
from researchd.client.cli import main
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
        assert "unknown command: frobnicate" in lines
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


def test_interactive_entry_spawns_and_terminates_researchd(tmp_path: Path) -> None:
    config = _config_file(tmp_path, _free_port())
    # Bootstrap through the client: it delegates to `researchd init` and
    # therefore never initializes or migrates the database itself.
    assert main(["--config", str(config), "init"]) == 0
    assert (tmp_path / "state" / "control.token").exists()

    lines: list[str] = []
    code = lifecycle.interactive_entry(
        config,
        input_fn=lambda: "quit",
        print_fn=lines.append,
    )
    assert code == 0
    assert any("interactive shell" in line for line in lines)
    assert (tmp_path / "state" / "daemon.log").exists()

    # The spawned daemon was terminated with the shell.
    config_model = lifecycle.load_client_config(config)
    assert lifecycle.probe_health(config_model) is None
