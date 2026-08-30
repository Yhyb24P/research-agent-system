import asyncio
from pathlib import Path
import pytest
from pydantic import ValidationError

from researchd.api.control import LocalControlAPI
from researchd.api.web import ControlCommandRouter
from researchd.daemon.contracts import DaemonCommandResult
from researchd.daemon.runtime import DaemonNotReady, ResearchDaemon
from researchd.daemon.startup import StartupBarrier, StartupPhase
from researchd.domain.base import DomainModel
from researchd.runtime_sessions.contracts import (
    ProcessLaunchSpec,
    ResolvedProcessLaunch,
    ResolvedRemoteHttpLaunch,
    RuntimeSession,
    RuntimeSessionStartCommand,
)
from researchd.storage.db import create_sqlite_engine, session_factory


class RecordingDispatcher:
    def __init__(self, result: RuntimeSession) -> None:
        self.result = result
        self.commands: list[DomainModel] = []

    def __call__(self, command: DomainModel) -> DaemonCommandResult:
        self.commands.append(command)
        assert isinstance(command, RuntimeSessionStartCommand)
        return DaemonCommandResult(
            command_id=command.command_id,
            command_type="RuntimeSessionStart",
            status="ACCEPTED",
            resource=self.result.model_dump(mode="json"),
        )


class FakeLaunchProfiles:
    def __init__(self, tmp_path: Path) -> None:
        self.process = ProcessLaunchSpec(argv=("/usr/bin/true",), cwd=str(tmp_path))

    def resolve_process(self, runtime_id: str) -> ResolvedProcessLaunch:
        assert runtime_id == "runtime_test_1"
        return ResolvedProcessLaunch(launch_spec=self.process, spec_sha256="a" * 64)

    def resolve_remote_http(self, runtime_id: str) -> ResolvedRemoteHttpLaunch:
        raise AssertionError(runtime_id)


def _barrier() -> StartupBarrier:
    return StartupBarrier({phase: lambda: None for phase in StartupPhase})


def _payload(tmp_path: Path) -> dict[str, object]:
    return {
        "command_id": "cmd_start_1",
        "runtime_session_id": "runtime_session_test_1",
        "runtime_id": "runtime_test_1",
    }


def _result(tmp_path: Path) -> RuntimeSession:
    from datetime import UTC, datetime

    now = datetime.now(UTC)
    return RuntimeSession.model_validate({
        "runtime_session_id": "runtime_session_test_1",
        "runtime_id": "runtime_test_1",
        "launch_mode": "PROCESS",
        "supervisor_state": "HEALTHY",
        "launch_spec": {"argv": ["/usr/bin/true"], "cwd": str(tmp_path)},
        "external_identity": {"pid": 123},
        "started_at": now,
        "last_health_at": now,
        "stopped_at": None,
        "exit_reason": None,
        "reattach_state": "NOT_APPLICABLE",
        "version": 2,
        "created_at": now,
        "updated_at": now,
    })


def test_runtime_http_command_is_rejected_before_daemon_ready(tmp_path: Path) -> None:
    sessions = session_factory(create_sqlite_engine(tmp_path / "unused.db"))
    dispatcher = RecordingDispatcher(_result(tmp_path))
    daemon = ResearchDaemon(_barrier(), dispatcher)
    router = ControlCommandRouter(
        LocalControlAPI(sessions), daemon, launch_profiles=FakeLaunchProfiles(tmp_path)
    )

    with pytest.raises(DaemonNotReady):
        asyncio.run(router.post("/api/runtime-sessions/start", _payload(tmp_path)))
    assert dispatcher.commands == []


def test_runtime_http_command_crosses_typed_ready_gate(tmp_path: Path) -> None:
    sessions = session_factory(create_sqlite_engine(tmp_path / "unused.db"))
    dispatcher = RecordingDispatcher(_result(tmp_path))
    daemon = ResearchDaemon(_barrier(), dispatcher)
    assert daemon.start().ready
    router = ControlCommandRouter(
        LocalControlAPI(sessions), daemon, launch_profiles=FakeLaunchProfiles(tmp_path)
    )

    status, response = asyncio.run(
        router.post("/api/runtime-sessions/start", _payload(tmp_path))
    )

    assert status == 202
    assert response["command_version"] == 1
    assert response["command_id"] == "cmd_start_1"
    assert response["command_type"] == "RuntimeSessionStart"
    assert response["status"] == "ACCEPTED"
    resource = response["resource"]
    assert isinstance(resource, dict)
    assert resource["runtime_session_id"] == "runtime_session_test_1"
    assert isinstance(dispatcher.commands[0], RuntimeSessionStartCommand)
    assert dispatcher.commands[0].actor_type == "HUMAN"
    assert dispatcher.commands[0].actor_id == "local-control-client"
    assert dispatcher.commands[0].launch_profile_sha256 == "a" * 64


def test_external_http_request_cannot_claim_trusted_actor(tmp_path: Path) -> None:
    sessions = session_factory(create_sqlite_engine(tmp_path / "unused.db"))
    dispatcher = RecordingDispatcher(_result(tmp_path))
    daemon = ResearchDaemon(_barrier(), dispatcher)
    assert daemon.start().ready
    router = ControlCommandRouter(
        LocalControlAPI(sessions), daemon, launch_profiles=FakeLaunchProfiles(tmp_path)
    )
    payload = _payload(tmp_path)
    payload["actor_type"] = "SYSTEM"
    payload["actor_id"] = "forged-system"

    with pytest.raises(ValidationError):
        asyncio.run(router.post("/api/runtime-sessions/start", payload))

    assert dispatcher.commands == []


def test_external_http_request_cannot_override_process_launch_spec(tmp_path: Path) -> None:
    sessions = session_factory(create_sqlite_engine(tmp_path / "unused.db"))
    dispatcher = RecordingDispatcher(_result(tmp_path))
    daemon = ResearchDaemon(_barrier(), dispatcher)
    assert daemon.start().ready
    router = ControlCommandRouter(
        LocalControlAPI(sessions), daemon, launch_profiles=FakeLaunchProfiles(tmp_path)
    )
    payload = _payload(tmp_path)
    payload["launch_spec"] = {"argv": ["/bin/sh", "-c", "id"], "cwd": "/"}

    with pytest.raises(ValidationError):
        asyncio.run(router.post("/api/runtime-sessions/start", payload))

    assert dispatcher.commands == []


def test_runtime_http_command_requires_daemon(tmp_path: Path) -> None:
    sessions = session_factory(create_sqlite_engine(tmp_path / "unused.db"))
    router = ControlCommandRouter(
        LocalControlAPI(sessions), launch_profiles=FakeLaunchProfiles(tmp_path)
    )

    with pytest.raises(RuntimeError, match="requires researchd"):
        asyncio.run(router.post("/api/runtime-sessions/start", _payload(tmp_path)))


_CONTROL_MUTATIONS: tuple[tuple[str, dict[str, object]], ...] = (
    ("/api/runs/run_gate/cancel", {
        "command_id": "cmd_cancel_gate",
    }),
    ("/api/work-orders/wo_gate/approve", {
        "command_id": "cmd_approve_gate",
        "grant_id": "grant_gate",
    }),
    ("/api/work-orders/wo_gate/human-decision", {
        "command_id": "cmd_decision_gate",
        "action": "abort",
    }),
)


@pytest.mark.parametrize(("path", "payload"), _CONTROL_MUTATIONS)
def test_control_mutations_are_rejected_before_daemon_ready(
    tmp_path: Path, path: str, payload: dict[str, object],
) -> None:
    sessions = session_factory(create_sqlite_engine(tmp_path / "unused.db"))
    dispatcher = RecordingDispatcher(_result(tmp_path))
    daemon = ResearchDaemon(_barrier(), dispatcher)
    router = ControlCommandRouter(LocalControlAPI(sessions), daemon)

    with pytest.raises(DaemonNotReady):
        asyncio.run(router.post(path, payload))
    assert dispatcher.commands == []


@pytest.mark.parametrize(("path", "payload"), _CONTROL_MUTATIONS)
def test_control_mutations_require_daemon(
    tmp_path: Path, path: str, payload: dict[str, object],
) -> None:
    sessions = session_factory(create_sqlite_engine(tmp_path / "unused.db"))
    router = ControlCommandRouter(LocalControlAPI(sessions))

    with pytest.raises(RuntimeError, match="requires researchd"):
        asyncio.run(router.post(path, payload))
