"""Regression contract for the PH02 HUMAN approval boundary."""

import asyncio
from pathlib import Path

from researchd.api.control import LocalControlAPI
from researchd.api.web import ControlCommandRouter
from researchd.daemon.contracts import DaemonCommandResult
from researchd.daemon.runtime import ResearchDaemon
from researchd.daemon.startup import StartupBarrier, StartupPhase
from researchd.domain.base import DomainModel
from researchd.storage.db import create_sqlite_engine, session_factory


def _barrier() -> StartupBarrier:
    return StartupBarrier({phase: lambda: None for phase in StartupPhase})


class _RecordingDispatcher:
    def __init__(self) -> None:
        self.commands: list[DomainModel] = []

    def __call__(self, command: DomainModel) -> DaemonCommandResult:
        self.commands.append(command)
        return DaemonCommandResult(
            command_id=str(getattr(command, "command_id")),
            command_type=type(command).__name__.removesuffix("Command"),
            status="ACCEPTED",
            resource={"approval_id": "approval_product"},
        )


def test_human_approval_route_uses_pending_approval_id_not_grant_id(tmp_path: Path) -> None:
    """The public HUMAN intent must never require a client-minted grant ID."""
    sessions = session_factory(create_sqlite_engine(tmp_path / "approval.db"))
    dispatcher = _RecordingDispatcher()
    daemon = ResearchDaemon(_barrier(), dispatcher)
    assert daemon.start().ready
    router = ControlCommandRouter(LocalControlAPI(sessions), daemon)

    status, response = asyncio.run(router.post("/api/approvals/approval_product/approve", {
        "command_id": "cmd_product_approval",
    }))

    assert status == 202
    assert response["status"] == "ACCEPTED"
    assert len(dispatcher.commands) == 1
    command = dispatcher.commands[0]
    assert type(command).__name__ == "ApprovalApproveCommand"
    assert getattr(command, "approval_id") == "approval_product"
    assert getattr(command, "actor_type") == "HUMAN"
    assert getattr(command, "actor_id") == "local-control-client"
    assert not hasattr(command, "grant_id")
