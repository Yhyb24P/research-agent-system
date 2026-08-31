import asyncio
from pathlib import Path
from typing import cast
import pytest
from pydantic import ValidationError
from sqlalchemy.orm import Session, sessionmaker

from researchd.api.control import LocalControlAPI
from researchd.api.web import ControlCommandRouter
from researchd.daemon.contracts import (
    BackupCreateCommand,
    BackupVerifyCommand,
    CollaborationMessageSendCommand,
    DaemonCommand,
    DaemonCommandResult,
    ManagedAgentStartCommand,
    ResearchTaskCreateCommand,
    RestorePlanCommand,
    WorkOrderRejectCommand,
    WorkspaceCreateCommand,
)
from researchd.daemon.reconciliation import (
    DaemonCommandResolutionService,
    build_builtin_observers,
)
from researchd.daemon.runtime import DaemonNotReady, DaemonState, ResearchDaemon
from researchd.daemon.startup import StartupBarrier, StartupPhase
from researchd.domain.base import DomainModel
from researchd.runtime_sessions.contracts import RuntimeSession
from researchd.storage.db import create_sqlite_engine, session_factory


class RecordingDispatcher:
    def __init__(self, result: RuntimeSession) -> None:
        self.result = result
        self.commands: list[DomainModel] = []

    def __call__(self, command: DomainModel) -> DaemonCommandResult:
        self.commands.append(command)
        assert isinstance(command, ManagedAgentStartCommand)
        return DaemonCommandResult(
            command_id=command.command_id,
            command_type="ManagedAgentStart",
            status="ACCEPTED",
            resource=self.result.model_dump(mode="json"),
        )


def _barrier() -> StartupBarrier:
    return StartupBarrier({phase: lambda: None for phase in StartupPhase})


def _payload() -> dict[str, object]:
    return {
        "command_id": "cmd_start_1",
        "runtime_id": "runtime_test_1",
    }


def _result(tmp_path: Path) -> RuntimeSession:
    from datetime import UTC, datetime

    now = datetime.now(UTC)
    return RuntimeSession.model_validate({
        "runtime_session_id": "runtime_session_managed_test_1",
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


def test_managed_start_is_rejected_before_daemon_ready(tmp_path: Path) -> None:
    sessions = session_factory(create_sqlite_engine(tmp_path / "unused.db"))
    dispatcher = RecordingDispatcher(_result(tmp_path))
    daemon = ResearchDaemon(_barrier(), dispatcher)
    router = ControlCommandRouter(LocalControlAPI(sessions), daemon)

    with pytest.raises(DaemonNotReady):
        asyncio.run(router.post("/api/agents/agent_test_1/start", _payload()))
    assert dispatcher.commands == []


def test_managed_start_crosses_typed_ready_gate(tmp_path: Path) -> None:
    sessions = session_factory(create_sqlite_engine(tmp_path / "unused.db"))
    dispatcher = RecordingDispatcher(_result(tmp_path))
    daemon = ResearchDaemon(_barrier(), dispatcher)
    assert daemon.start().ready
    router = ControlCommandRouter(LocalControlAPI(sessions), daemon)

    status, response = asyncio.run(
        router.post("/api/agents/agent_test_1/start", _payload())
    )

    assert status == 202
    assert response["command_version"] == 1
    assert response["command_id"] == "cmd_start_1"
    assert response["command_type"] == "ManagedAgentStart"
    assert response["status"] == "ACCEPTED"
    resource = response["resource"]
    assert isinstance(resource, dict)
    assert resource["runtime_session_id"] == "runtime_session_managed_test_1"
    command = dispatcher.commands[0]
    assert isinstance(command, ManagedAgentStartCommand)
    assert command.agent_id == "agent_test_1"
    assert command.runtime_id == "runtime_test_1"
    assert command.actor_type == "HUMAN"
    assert command.actor_id == "local-control-client"


def test_external_http_request_cannot_claim_trusted_actor(tmp_path: Path) -> None:
    sessions = session_factory(create_sqlite_engine(tmp_path / "unused.db"))
    dispatcher = RecordingDispatcher(_result(tmp_path))
    daemon = ResearchDaemon(_barrier(), dispatcher)
    assert daemon.start().ready
    router = ControlCommandRouter(LocalControlAPI(sessions), daemon)
    payload = _payload()
    payload["actor_type"] = "SYSTEM"
    payload["actor_id"] = "forged-system"

    with pytest.raises(ValidationError):
        asyncio.run(router.post("/api/agents/agent_test_1/start", payload))

    assert dispatcher.commands == []


def test_external_http_request_cannot_carry_launch_body(tmp_path: Path) -> None:
    sessions = session_factory(create_sqlite_engine(tmp_path / "unused.db"))
    dispatcher = RecordingDispatcher(_result(tmp_path))
    daemon = ResearchDaemon(_barrier(), dispatcher)
    assert daemon.start().ready
    router = ControlCommandRouter(LocalControlAPI(sessions), daemon)
    payload = _payload()
    payload["launch_spec"] = {"argv": ["/bin/sh", "-c", "id"], "cwd": "/"}

    with pytest.raises(ValidationError):
        asyncio.run(router.post("/api/agents/agent_test_1/start", payload))

    assert dispatcher.commands == []


def test_legacy_runtime_session_launch_routes_are_disabled(tmp_path: Path) -> None:
    sessions = session_factory(create_sqlite_engine(tmp_path / "unused.db"))
    dispatcher = RecordingDispatcher(_result(tmp_path))
    daemon = ResearchDaemon(_barrier(), dispatcher)
    assert daemon.start().ready
    router = ControlCommandRouter(LocalControlAPI(sessions), daemon)

    status, response = asyncio.run(
        router.post("/api/runtime-sessions/start", _payload())
    )
    assert status == 404
    assert response == {"error": "unknown command"}
    status, response = asyncio.run(
        router.post("/api/runtime-sessions/attach", _payload())
    )
    assert status == 404
    assert dispatcher.commands == []


def test_managed_start_requires_daemon(tmp_path: Path) -> None:
    sessions = session_factory(create_sqlite_engine(tmp_path / "unused.db"))
    router = ControlCommandRouter(LocalControlAPI(sessions))

    with pytest.raises(RuntimeError, match="requires researchd"):
        asyncio.run(router.post("/api/agents/agent_test_1/start", _payload()))


_CONTROL_MUTATIONS: tuple[tuple[str, dict[str, object]], ...] = (
    ("/api/runs/run_gate/cancel", {
        "command_id": "cmd_cancel_gate",
    }),
    # PH02: the grant-based work-orders approve route is gone; the HUMAN
    # intent names the pending approval and carries no grant id.
    ("/api/approvals/appr_gate/approve", {
        "command_id": "cmd_approve_gate",
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


def _failed_barrier() -> StartupBarrier:
    def action(phase: StartupPhase) -> None:
        if phase is StartupPhase.AUDIT_STREAM_HEALTH:
            raise RuntimeError("daemon command outcome requires operator reconciliation")

    return StartupBarrier({phase: (lambda current=phase: action(current)) for phase in StartupPhase})


def _no_dispatch(command: DomainModel) -> None:
    raise AssertionError("readiness-gated dispatcher must not serve resolutions")


def _seeded_resolution(tmp_path: Path) -> tuple[sessionmaker[Session], DaemonCommandResolutionService]:
    from tests.integration.test_daemon_resolution import (
        _seed_receipt,
        _seed_run,
        _sessions,
    )

    sessions = _sessions(tmp_path)
    _seed_run(sessions, "run_web", state="CANCELLED", cancellation_requested=True)
    _seed_receipt(sessions, "cmd_web_lost", "RunCancelCommand")
    service = DaemonCommandResolutionService(sessions, build_builtin_observers(sessions))
    return sessions, service


def test_resolve_route_is_reachable_while_daemon_failed(tmp_path: Path) -> None:
    sessions, service = _seeded_resolution(tmp_path)
    daemon = ResearchDaemon(_failed_barrier(), _no_dispatch)
    assert daemon.start().ready is False
    router = ControlCommandRouter(LocalControlAPI(sessions), daemon, resolution=service)

    status, payload = asyncio.run(router.post(
        "/api/daemon-commands/cmd_web_lost/resolve",
        {"command_id": "cmd_resolve_web", "resource_ref": {"run_id": "run_web"}},
    ))

    assert status == 202
    assert payload["command_type"] == "DaemonCommandResolve"
    assert payload["status"] == "ACCEPTED"
    assert payload["resource"]["target_status"] == "COMPLETED"
    assert payload["resource"]["target_command_id"] == "cmd_web_lost"
    assert daemon.state is DaemonState.FAILED


def test_resolve_route_rejects_terminal_and_missing_targets(tmp_path: Path) -> None:
    sessions, service = _seeded_resolution(tmp_path)
    daemon = ResearchDaemon(_failed_barrier(), _no_dispatch)
    daemon.start()
    router = ControlCommandRouter(LocalControlAPI(sessions), daemon, resolution=service)

    asyncio.run(router.post(
        "/api/daemon-commands/cmd_web_lost/resolve",
        {"command_id": "cmd_resolve_first", "resource_ref": {"run_id": "run_web"}},
    ))
    status, payload = asyncio.run(router.post(
        "/api/daemon-commands/cmd_web_lost/resolve",
        {"command_id": "cmd_resolve_second", "resource_ref": {"run_id": "run_web"}},
    ))
    assert status == 409
    assert payload["reason_code"] == "receipt_not_pending"

    status, payload = asyncio.run(router.post(
        "/api/daemon-commands/cmd_ghost/resolve",
        {"command_id": "cmd_resolve_ghost", "resource_ref": {"run_id": "run_web"}},
    ))
    assert status == 409
    assert payload["reason_code"] == "target_missing"


def test_resolve_route_rejects_untrusted_request_fields(tmp_path: Path) -> None:
    sessions, service = _seeded_resolution(tmp_path)
    daemon = ResearchDaemon(_failed_barrier(), _no_dispatch)
    daemon.start()
    router = ControlCommandRouter(LocalControlAPI(sessions), daemon, resolution=service)
    payload = {
        "command_id": "cmd_resolve_forged",
        "resource_ref": {"run_id": "run_web"},
        "actor_type": "SYSTEM",
        "actor_id": "forged-system",
    }

    with pytest.raises(ValidationError):
        asyncio.run(router.post("/api/daemon-commands/cmd_web_lost/resolve", payload))


def test_resolve_route_requires_resolution_service(tmp_path: Path) -> None:
    sessions = session_factory(create_sqlite_engine(tmp_path / "unused.db"))
    daemon = ResearchDaemon(_failed_barrier(), _no_dispatch)
    router = ControlCommandRouter(LocalControlAPI(sessions), daemon)

    with pytest.raises(RuntimeError, match="receipt resolution is not configured"):
        asyncio.run(router.post(
            "/api/daemon-commands/cmd_x/resolve",
            {"command_id": "cmd_resolve_none", "resource_ref": {}},
        ))


_FAMILY_ROUTES: tuple[tuple[str, dict[str, object]], ...] = (
    ("/api/workspaces", {
        "command_id": "cmd_ws_family",
        "workspace_id": "ws_family",
        "name": "family",
    }),
    ("/api/runs", {
        "command_id": "cmd_task_family",
        "workspace_id": "ws_family",
        "objective": "family task",
    }),
    ("/api/work-orders/wo_family/reject", {
        "command_id": "cmd_reject_family",
        "approval_id": "apr_family",
    }),
    ("/api/collaboration-messages", {
        "command_id": "cmd_msg_family",
        "message_id": "msg_family",
        "run_id": "run_family",
        "purpose": "DIRECTIVE",
        "body": "stay in scope",
    }),
    ("/api/backups/create", {
        "command_id": "cmd_backup_family",
        "destination": "/tmp/family-snap",
        "candidate_commit": "e" * 40,
        "candidate_tag": "v1.0.0-rc.80",
    }),
    ("/api/backups/verify", {
        "command_id": "cmd_verify_family",
        "snapshot": "/tmp/family-snap",
    }),
    ("/api/restores/plan", {
        "command_id": "cmd_plan_family",
        "snapshot": "/tmp/family-snap",
        "database_destination": "/tmp/family-restore.db",
        "artifact_destination": "/tmp/family-restore-artifacts",
        "expected_candidate_commit": "e" * 40,
        "expected_candidate_tag": "v1.0.0-rc.80",
    }),
)


class _FamilyDispatcher:
    """Accepts only the PX00-05 command families; everything else fails."""

    def __init__(self) -> None:
        self.commands: list[DomainModel] = []

    def __call__(self, command: DomainModel) -> DaemonCommandResult:
        self.commands.append(command)
        assert isinstance(command, (
            BackupCreateCommand,
            BackupVerifyCommand,
            CollaborationMessageSendCommand,
            ResearchTaskCreateCommand,
            RestorePlanCommand,
            WorkOrderRejectCommand,
            WorkspaceCreateCommand,
        ))
        return DaemonCommandResult(
            command_id=command.command_id,
            command_type=type(command).__name__.removesuffix("Command"),
            status="ACCEPTED",
            resource={"accepted": True},
        )


@pytest.mark.parametrize(("path", "payload"), _FAMILY_ROUTES)
def test_family_routes_cross_ready_gate(tmp_path: Path, path: str, payload: dict[str, object]) -> None:
    sessions = session_factory(create_sqlite_engine(tmp_path / "unused.db"))
    dispatcher = _FamilyDispatcher()
    daemon = ResearchDaemon(_barrier(), dispatcher)
    assert daemon.start().ready
    router = ControlCommandRouter(LocalControlAPI(sessions), daemon)

    status, response = asyncio.run(router.post(path, payload))

    assert status == 202
    assert response["status"] == "ACCEPTED"
    assert response["command_id"] == payload["command_id"]
    command = cast(DaemonCommand, dispatcher.commands[-1])
    assert command.actor_type == "HUMAN"
    assert command.actor_id == "local-control-client"


@pytest.mark.parametrize(("path", "payload"), _FAMILY_ROUTES)
def test_family_routes_are_rejected_before_daemon_ready(
    tmp_path: Path, path: str, payload: dict[str, object],
) -> None:
    sessions = session_factory(create_sqlite_engine(tmp_path / "unused.db"))
    dispatcher = _FamilyDispatcher()
    daemon = ResearchDaemon(_barrier(), dispatcher)
    router = ControlCommandRouter(LocalControlAPI(sessions), daemon)

    with pytest.raises(DaemonNotReady):
        asyncio.run(router.post(path, payload))
    assert dispatcher.commands == []


@pytest.mark.parametrize(("path", "payload"), _FAMILY_ROUTES)
def test_family_routes_require_daemon(
    tmp_path: Path, path: str, payload: dict[str, object],
) -> None:
    sessions = session_factory(create_sqlite_engine(tmp_path / "unused.db"))
    router = ControlCommandRouter(LocalControlAPI(sessions))

    with pytest.raises(RuntimeError, match="requires researchd"):
        asyncio.run(router.post(path, payload))


def test_family_routes_reject_forged_actor_fields(tmp_path: Path) -> None:
    sessions = session_factory(create_sqlite_engine(tmp_path / "unused.db"))
    dispatcher = _FamilyDispatcher()
    daemon = ResearchDaemon(_barrier(), dispatcher)
    daemon.start()
    router = ControlCommandRouter(LocalControlAPI(sessions), daemon)
    payload = dict(_FAMILY_ROUTES[0][1])
    payload["actor_type"] = "SYSTEM"
    payload["actor_id"] = "forged-system"

    with pytest.raises(ValidationError):
        asyncio.run(router.post("/api/workspaces", payload))
    assert dispatcher.commands == []
