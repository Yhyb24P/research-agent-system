"""Regression contracts for the first product-hardening P0 gaps.

These tests deliberately exercise the public daemon command boundary.  The
driver is a daemon service; it does not own workflow state or transition rows.
"""

import threading
from pathlib import Path
from typing import cast

from researchd.daemon.contracts import DaemonCommandResult, ResearchTaskCreateCommand
from researchd.daemon.dispatcher import ControlMutationAuthority, DaemonCommandDispatcher
from researchd.daemon.runtime import ResearchDaemon
from researchd.daemon.startup import StartupBarrier, StartupPhase
from researchd.domain.base import DomainModel
from researchd.storage.db import create_sqlite_engine, session_factory
from researchd.storage.models import Base
from researchd.supervisor.runtime import RuntimeSupervisor


def _barrier() -> StartupBarrier:
    return StartupBarrier({phase: lambda: None for phase in StartupPhase})


class _RecordingOrchestrator:
    def __init__(self) -> None:
        self.advanced = threading.Event()
        self.calls: list[str] = []

    async def advance(self, run_id: str) -> bool:
        self.calls.append(run_id)
        self.advanced.set()
        return False


class _TaskControl:
    def create_research_task(
        self,
        workspace_id: str,
        objective: str,
        *,
        run_id: str | None = None,
    ) -> dict[str, object]:
        assert workspace_id == "ws_product"
        assert objective == "advance without a private caller"
        assert run_id == "run_product"
        return {"run_id": run_id, "workspace_id": workspace_id, "state": "NEW"}


def test_task_create_wakes_daemon_owned_orchestration_driver(tmp_path: Path) -> None:
    """A typed task command must leave NEW without a private orchestrator caller."""
    from researchd.orchestrator.driver import OrchestrationDriver

    engine = create_sqlite_engine(tmp_path / "driver.db")
    Base.metadata.create_all(engine)
    sessions = session_factory(engine)
    orchestrator = _RecordingOrchestrator()
    from researchd.orchestrator.driver import OrchestrationTarget

    driver = OrchestrationDriver(cast(OrchestrationTarget, orchestrator), sessions)
    dispatcher = DaemonCommandDispatcher(
        cast(RuntimeSupervisor, object()),
        cast(ControlMutationAuthority, _TaskControl()),
        orchestration_driver=driver,
    )
    daemon = ResearchDaemon(_barrier(), dispatcher, orchestration_driver=driver)
    assert daemon.start().ready
    try:
        result = daemon.execute(ResearchTaskCreateCommand(
            command_id="cmd_product_task",
            actor_type="HUMAN",
            actor_id="local-control-client",
            workspace_id="ws_product",
            objective="advance without a private caller",
            run_id="run_product",
        ))
        assert isinstance(result, DaemonCommandResult)
        assert result.status == "ACCEPTED"
        assert orchestrator.advanced.wait(timeout=2)
        assert orchestrator.calls == ["run_product"]
    finally:
        daemon.stop()
