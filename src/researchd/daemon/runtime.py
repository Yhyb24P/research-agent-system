"""Readiness-gated mutation boundary for the trusted local daemon."""

from collections.abc import Callable
from enum import StrEnum

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


class ResearchDaemon:
    """Own startup ordering and reject every mutation until recovery passes."""

    def __init__(
        self,
        barrier: StartupBarrier,
        dispatcher: Callable[[DomainModel], object],
    ) -> None:
        self.barrier = barrier
        self.dispatcher = dispatcher
        self.state = DaemonState.CREATED
        self.startup_report: StartupReport | None = None

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
        self.state = DaemonState.STOPPED

    def health(self) -> dict[str, object]:
        return {
            "state": self.state.value,
            "ready": self.state is DaemonState.READY,
            "startup": (
                self.startup_report.as_dict()
                if self.startup_report is not None
                else None
            ),
        }
