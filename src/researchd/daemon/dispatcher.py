"""Closed typed-command dispatcher for daemon-owned mutations."""

from collections.abc import Awaitable
from typing import Protocol

from researchd.daemon.contracts import (
    DaemonCommandResult,
    HumanDecisionCommand,
    RunCancelCommand,
    WorkOrderApproveCommand,
)
from researchd.domain.base import DomainModel
from researchd.runtime_sessions.contracts import (
    RuntimeSessionAttachCommand,
    RuntimeSessionStartCommand,
    RuntimeSessionStopCommand,
)
from researchd.supervisor.runtime import RuntimeSupervisor


class DaemonCommandDispatcher:
    """Route an explicit command union; unknown domain models fail closed."""

    def __init__(
        self,
        supervisor: RuntimeSupervisor,
        control: "ControlMutationAuthority | None" = None,
    ) -> None:
        self.supervisor = supervisor
        self.control = control

    def __call__(self, command: DomainModel) -> DomainModel | Awaitable[DomainModel]:
        if isinstance(command, RuntimeSessionStartCommand):
            return self._accepted(command.command_id, "RuntimeSessionStart", self.supervisor.start(command))
        if isinstance(command, RuntimeSessionAttachCommand):
            return self._accepted(command.command_id, "RuntimeSessionAttach", self.supervisor.attach(command))
        if isinstance(command, RuntimeSessionStopCommand):
            return self._accepted(command.command_id, "RuntimeSessionStop", self.supervisor.stop(command))
        if isinstance(command, RunCancelCommand):
            return self._cancel(command)
        if isinstance(command, WorkOrderApproveCommand):
            return self._approve(command)
        if isinstance(command, HumanDecisionCommand):
            control = self._control()
            resource = control.resolve_human(
                command.work_order_id,
                action=command.action,
                objective=command.objective,
            )
            return self._accepted(command.command_id, "HumanDecision", resource)
        raise TypeError(f"unsupported daemon command: {type(command).__name__}")

    async def _cancel(self, command: RunCancelCommand) -> DaemonCommandResult:
        resource = await self._control().cancel_run(command.run_id)
        return self._accepted(command.command_id, "RunCancel", resource)

    async def _approve(self, command: WorkOrderApproveCommand) -> DaemonCommandResult:
        resource = await self._control().approve(command.work_order_id, command.grant_id)
        return self._accepted(command.command_id, "WorkOrderApprove", resource)

    def _control(self) -> "ControlMutationAuthority":
        if self.control is None:
            raise RuntimeError("orchestrator mutation authority is not configured")
        return self.control

    @staticmethod
    def _accepted(command_id: str, command_type: str, resource: DomainModel | dict[str, object]) -> DaemonCommandResult:
        payload = (
            resource.model_dump(mode="json")
            if isinstance(resource, DomainModel)
            else resource
        )
        return DaemonCommandResult(
            command_id=command_id,
            command_type=command_type,
            status="ACCEPTED",
            resource=payload,
        )


class ControlMutationAuthority(Protocol):
    async def cancel_run(self, run_id: str) -> dict[str, object]: ...
    async def approve(self, work_order_id: str, grant_id: str) -> dict[str, object]: ...
    def resolve_human(
        self,
        work_order_id: str,
        *,
        action: str,
        objective: str | None = None,
    ) -> dict[str, object]: ...


__all__ = ["DaemonCommandDispatcher"]
