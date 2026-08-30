"""Closed typed-command dispatcher for daemon-owned runtime mutations."""

from researchd.domain.base import DomainModel
from researchd.runtime_sessions.contracts import (
    RuntimeSessionAttachCommand,
    RuntimeSessionStartCommand,
    RuntimeSessionStopCommand,
)
from researchd.supervisor.runtime import RuntimeSupervisor


class DaemonCommandDispatcher:
    """Route an explicit command union; unknown domain models fail closed."""

    def __init__(self, supervisor: RuntimeSupervisor) -> None:
        self.supervisor = supervisor

    def __call__(self, command: DomainModel) -> DomainModel:
        if isinstance(command, RuntimeSessionStartCommand):
            return self.supervisor.start(command)
        if isinstance(command, RuntimeSessionAttachCommand):
            return self.supervisor.attach(command)
        if isinstance(command, RuntimeSessionStopCommand):
            return self.supervisor.stop(command)
        raise TypeError(f"unsupported daemon command: {type(command).__name__}")


__all__ = ["DaemonCommandDispatcher"]
