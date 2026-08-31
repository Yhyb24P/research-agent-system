"""Readiness-gated mutation boundary for the trusted local daemon."""

from collections.abc import Awaitable, Callable
from enum import StrEnum
from typing import Protocol

from researchd.domain.base import DomainModel
from researchd.daemon.startup import StartupBarrier, StartupReport


class DaemonState(StrEnum):
    CREATED = "CREATED"
    STARTING = "STARTING"
    READY = "READY"
    FAILED = "FAILED"
    STOPPED = "STOPPED"


class DaemonNotReady(RuntimeError):
    pass


class DaemonLifecycleService(Protocol):
    """A daemon-owned auxiliary service with no command authority."""

    def start(self) -> None: ...
    def stop(self) -> None: ...
    def health(self) -> dict[str, object]: ...


class ResearchDaemon:
    """Own startup ordering and reject every mutation until recovery passes."""

    def __init__(
        self,
        barrier: StartupBarrier,
        dispatcher: Callable[[DomainModel], object | Awaitable[object]],
        *,
        orchestration_driver: DaemonLifecycleService | None = None,
    ) -> None:
        self.barrier = barrier
        self.dispatcher = dispatcher
        self.state = DaemonState.CREATED
        self.startup_report: StartupReport | None = None
        self.orchestration_driver = orchestration_driver

    def start(self) -> StartupReport:
        if self.state is not DaemonState.CREATED:
            raise RuntimeError("researchd startup may only run once")
        self.state = DaemonState.STARTING
        self.startup_report = self.barrier.run()
        self.state = (
            DaemonState.READY
            if self.startup_report.ready
            else DaemonState.FAILED
        )
        if self.state is DaemonState.READY and self.orchestration_driver is not None:
            self.orchestration_driver.start()
        return self.startup_report

    def execute(self, command: DomainModel) -> object:
        if self.state is not DaemonState.READY:
            raise DaemonNotReady("researchd has not completed its startup barrier")
        if not isinstance(command, DomainModel):
            raise TypeError("researchd accepts only typed command models")
        return self.dispatcher(command)

    def stop(self) -> None:
        if self.state is DaemonState.STARTING:
            raise RuntimeError("researchd cannot stop while startup is running")
        if self.orchestration_driver is not None:
            self.orchestration_driver.stop()
        self.state = DaemonState.STOPPED

    def health(self) -> dict[str, object]:
        health: dict[str, object] = {
            "state": self.state.value,
            "ready": self.state is DaemonState.READY,
            "startup": (
                self.startup_report.as_dict()
                if self.startup_report is not None
                else None
            ),
        }
        if self.orchestration_driver is not None:
            health["orchestration_driver"] = self.orchestration_driver.health()
        return health
