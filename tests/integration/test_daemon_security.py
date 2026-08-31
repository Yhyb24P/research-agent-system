import json
import os
from pathlib import Path
from threading import Thread
from typing import cast

import httpx
import pytest

from researchd.api.control import LocalControlAPI
from researchd.api.web import serve_local_control
from researchd.daemon.contracts import DaemonCommandResult, RunCancelCommand
from researchd.daemon.runtime import ResearchDaemon
from researchd.daemon.security import (
    ControlCredentialError,
    create_control_token,
    load_control_token,
)
from researchd.daemon.startup import StartupBarrier, StartupPhase
from researchd.domain.base import DomainModel
from researchd.storage.db import create_sqlite_engine, session_factory


class RecordingDispatcher:
    def __init__(self) -> None:
        self.commands: list[DomainModel] = []

    def __call__(self, command: DomainModel) -> DaemonCommandResult:
        self.commands.append(command)
        assert isinstance(command, RunCancelCommand)
        return DaemonCommandResult(
            command_id=command.command_id,
            command_type="RunCancel",
            status="ACCEPTED",
            resource={"run_id": command.run_id},
        )


def _barrier() -> StartupBarrier:
    return StartupBarrier({phase: lambda: None for phase in StartupPhase})


def test_control_token_is_owner_only_and_never_replaced(tmp_path: Path) -> None:
    state = tmp_path / "state"
    token = create_control_token(state)
    path = state / "control.token"

    assert len(token) == 64
    assert path.read_text(encoding="ascii") == f"{token}\n"
    assert path.stat().st_mode & 0o777 == 0o600
    assert load_control_token(state) == token
    with pytest.raises(ControlCredentialError, match="already exists"):
        create_control_token(state)
    assert load_control_token(state) == token


def test_control_token_rejects_unsafe_permissions_and_format(tmp_path: Path) -> None:
    state = tmp_path / "state"
    create_control_token(state)
    path = state / "control.token"
    path.chmod(0o640)
    with pytest.raises(ControlCredentialError, match="permissions must be 0600"):
        load_control_token(state)

    path.chmod(0o600)
    path.write_text("not-a-token\n", encoding="ascii")
    os.chmod(path, 0o600)
    with pytest.raises(ControlCredentialError, match="invalid format"):
        load_control_token(state)


def test_mutable_http_surface_requires_valid_bearer_before_dispatch(tmp_path: Path) -> None:
    sessions = session_factory(create_sqlite_engine(tmp_path / "unused.db"))
    dispatcher = RecordingDispatcher()
    daemon = ResearchDaemon(_barrier(), dispatcher)
    assert daemon.start().ready
    token = "a" * 64
    server = serve_local_control(
        LocalControlAPI(sessions), daemon=daemon, port=0, control_token=token
    )
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address[:2]
    host_text = host.decode("ascii") if isinstance(host, bytes) else host
    base = f"http://{host_text}:{port}"
    try:
        assert httpx.get(f"{base}/api/health", timeout=5).status_code == 200
        assert httpx.get(f"{base}/api/runs", timeout=5).status_code == 401
        payload = {"command_id": "cmd_secure_cancel"}
        assert httpx.post(f"{base}/api/runs/run_1/cancel", json=payload, timeout=5).status_code == 401
        assert httpx.post(
            f"{base}/api/runs/run_1/cancel",
            json=payload,
            headers={"Authorization": "Bearer wrong"},
            timeout=5,
        ).status_code == 401
        assert dispatcher.commands == []

        response = httpx.post(
            f"{base}/api/runs/run_1/cancel",
            json=payload,
            headers={"Authorization": f"Bearer {token}"},
            timeout=5,
        )
        assert response.status_code == 202
        command = cast(RunCancelCommand, dispatcher.commands[0])
        assert command.actor_type == "HUMAN"
        assert command.actor_id == "local-control-client"
        assert token not in json.dumps(response.json())
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
        daemon.stop()


def test_mutable_server_cannot_start_without_credential(tmp_path: Path) -> None:
    sessions = session_factory(create_sqlite_engine(tmp_path / "unused.db"))
    daemon = ResearchDaemon(_barrier(), RecordingDispatcher())
    with pytest.raises(ValueError, match="requires a local credential"):
        serve_local_control(LocalControlAPI(sessions), daemon=daemon, port=0)
