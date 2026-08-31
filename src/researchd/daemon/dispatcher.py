"""Closed typed-command dispatcher for daemon-owned mutations."""

from collections.abc import Awaitable
from typing import Protocol

from researchd.collaboration.contracts import CollaborationMessage
from researchd.daemon.contracts import (
    BackupCreateCommand,
    BackupVerifyCommand,
    CollaborationMessageSendCommand,
    DaemonCommandResult,
    HumanDecisionCommand,
    HandoffDecisionCommand,
    ManagedAgentStartCommand,
    RemoteAgentAttachCommand,
    RemoteAgentDetachCommand,
    RemoteAgentRenewCommand,
    ResearchTaskCreateCommand,
    RestorePlanCommand,
    RunCancelCommand,
    WorkOrderApproveCommand,
    WorkOrderRejectCommand,
    WorkspaceCreateCommand,
)
from researchd.domain.base import DomainModel
from researchd.domain.enums import CollaborationPurpose, DataClassification
from researchd.domain.ids import AgentId, DelegationId, InvocationId, MessageId
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
        backups: "BackupMutationAuthority | None" = None,
        managed_start: "ManagedAgentStartAuthority | None" = None,
        handoffs: "HandoffResolutionAuthority | None" = None,
        remote_attachments: "RemoteAttachmentAuthority | None" = None,
        orchestration_driver: "OrchestrationWakeAuthority | None" = None,
    ) -> None:
        self.supervisor = supervisor
        self.control = control
        self.backups = backups
        self.managed_start = managed_start
        self.handoffs = handoffs
        self.remote_attachments = remote_attachments
        self.orchestration_driver = orchestration_driver

    def __call__(self, command: DomainModel) -> DomainModel | Awaitable[DomainModel]:
        if isinstance(command, RuntimeSessionStartCommand):
            return self._accepted(command.command_id, "RuntimeSessionStart", self.supervisor.start(command))
        if isinstance(command, RuntimeSessionAttachCommand):
            return self._accepted(command.command_id, "RuntimeSessionAttach", self.supervisor.attach(command))
        if isinstance(command, RuntimeSessionStopCommand):
            return self._accepted(command.command_id, "RuntimeSessionStop", self.supervisor.stop(command))
        if isinstance(command, ManagedAgentStartCommand):
            internal = self._managed_start().resolve(
                command.agent_id,
                command.runtime_id,
                command_id=command.command_id,
                actor_type=command.actor_type,
                actor_id=command.actor_id,
            )
            if isinstance(internal, RuntimeSessionStartCommand):
                return self._accepted(command.command_id, "ManagedAgentStart", self.supervisor.start(internal))
            return self._accepted(command.command_id, "ManagedAgentStart", self.supervisor.attach(internal))
        if isinstance(command, RemoteAgentAttachCommand):
            return self._accepted(command.command_id, "RemoteAgentAttach", self._remote_attachments().attach(command.runtime_id))
        if isinstance(command, RemoteAgentDetachCommand):
            return self._accepted(command.command_id, "RemoteAgentDetach", self._remote_attachments().detach(command.runtime_id))
        if isinstance(command, RemoteAgentRenewCommand):
            return self._accepted(command.command_id, "RemoteAgentRenew", self._remote_attachments().renew(command.runtime_id))
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
        if isinstance(command, HandoffDecisionCommand):
            handoffs = self._handoffs()
            if command.decision == "accept":
                handoff_resource = handoffs.accept(
                    command.proposal_id, actor_type=command.actor_type,
                    actor_id=command.actor_id, reason=command.reason,
                    target_agent_id=command.target_agent_id,
                )
            else:
                handoff_resource = handoffs.reject(
                    command.proposal_id, actor_type=command.actor_type,
                    actor_id=command.actor_id, reason=command.reason,
                )
            return self._accepted(command.command_id, "HandoffDecision", handoff_resource)
        if isinstance(command, WorkspaceCreateCommand):
            control = self._control()
            resource = control.create_workspace(command.workspace_id, command.name)
            return self._accepted(command.command_id, "WorkspaceCreate", resource)
        if isinstance(command, ResearchTaskCreateCommand):
            control = self._control()
            resource = control.create_research_task(
                command.workspace_id,
                command.objective,
                run_id=command.run_id,
            )
            run_id = resource.get("run_id")
            if not isinstance(run_id, str):
                raise RuntimeError("ResearchTaskCreate did not return a run ID")
            if self.orchestration_driver is not None:
                self.orchestration_driver.wake(run_id)
            return self._accepted(command.command_id, "ResearchTaskCreate", resource)
        if isinstance(command, WorkOrderRejectCommand):
            control = self._control()
            resource = control.reject(
                command.work_order_id,
                command.approval_id,
                actor_type=command.actor_type,
                actor_id=command.actor_id,
            )
            return self._accepted(command.command_id, "WorkOrderReject", resource)
        if isinstance(command, CollaborationMessageSendCommand):
            control = self._control()
            message = CollaborationMessage(
                message_id=MessageId(command.message_id),
                run_id=command.run_id,
                work_order_id=command.work_order_id,
                delegation_id=(DelegationId(command.delegation_id) if command.delegation_id else None),
                invocation_id=(InvocationId(command.invocation_id) if command.invocation_id else None),
                reply_to_message_id=(MessageId(command.reply_to_message_id) if command.reply_to_message_id else None),
                sender_actor_type=command.actor_type,
                sender_actor_id=command.actor_id,
                recipient_agent_id=(
                    AgentId(command.recipient_agent_id)
                    if command.recipient_agent_id
                    else None
                ),
                purpose=CollaborationPurpose(command.purpose),
                body=command.body,
                classification=DataClassification(command.classification),
            )
            resource = control.send_collaboration_message(message)
            return self._accepted(command.command_id, "CollaborationMessageSend", resource)
        if isinstance(command, BackupCreateCommand):
            resource = self._backups().create_backup(
                command.destination,
                command.candidate_commit,
                command.candidate_tag,
            )
            return self._accepted(command.command_id, "BackupCreate", resource)
        if isinstance(command, BackupVerifyCommand):
            resource = self._backups().verify_backup(command.snapshot)
            return self._accepted(command.command_id, "BackupVerify", resource)
        if isinstance(command, RestorePlanCommand):
            resource = self._backups().plan_restore(
                command.snapshot,
                command.database_destination,
                command.artifact_destination,
                command.expected_candidate_commit,
                command.expected_candidate_tag,
            )
            return self._accepted(command.command_id, "RestorePlan", resource)
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

    def _backups(self) -> "BackupMutationAuthority":
        if self.backups is None:
            raise RuntimeError("backup mutation authority is not configured")
        return self.backups

    def _managed_start(self) -> "ManagedAgentStartAuthority":
        if self.managed_start is None:
            raise RuntimeError("managed agent start authority is not configured")
        return self.managed_start

    def _remote_attachments(self) -> "RemoteAttachmentAuthority":
        if self.remote_attachments is None:
            raise RuntimeError("remote attachment authority is not configured")
        return self.remote_attachments

    def _handoffs(self) -> "HandoffResolutionAuthority":
        if self.handoffs is None:
            raise RuntimeError("handoff resolution authority is not configured")
        return self.handoffs

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
    def create_workspace(self, workspace_id: str, name: str) -> dict[str, object]: ...
    def create_research_task(
        self,
        workspace_id: str,
        objective: str,
        *,
        run_id: str | None = None,
    ) -> dict[str, object]: ...
    def reject(
        self,
        work_order_id: str,
        approval_id: str,
        *,
        actor_type: str,
        actor_id: str,
    ) -> dict[str, object]: ...
    def send_collaboration_message(
        self,
        message: CollaborationMessage,
    ) -> dict[str, object]: ...


class HandoffResolutionAuthority(Protocol):
    def accept(
        self, proposal_id: str, *, actor_type: str, actor_id: str,
        reason: str, target_agent_id: str | None = None,
    ) -> DomainModel: ...
    def reject(
        self, proposal_id: str, *, actor_type: str, actor_id: str,
        reason: str,
    ) -> DomainModel: ...


class BackupMutationAuthority(Protocol):
    def create_backup(
        self,
        destination: str,
        candidate_commit: str,
        candidate_tag: str,
    ) -> dict[str, object]: ...
    def verify_backup(self, snapshot: str) -> dict[str, object]: ...
    def plan_restore(
        self,
        snapshot: str,
        database_destination: str,
        artifact_destination: str,
        expected_candidate_commit: str,
        expected_candidate_tag: str,
    ) -> dict[str, object]: ...


class ManagedAgentStartAuthority(Protocol):
    def resolve(
        self,
        agent_id: str,
        runtime_id: str | None,
        *,
        command_id: str,
        actor_type: str,
        actor_id: str,
    ) -> RuntimeSessionStartCommand | RuntimeSessionAttachCommand: ...


class RemoteAttachmentAuthority(Protocol):
    def attach(self, runtime_id: str) -> dict[str, object]: ...
    def detach(self, runtime_id: str) -> dict[str, object]: ...
    def renew(self, runtime_id: str) -> dict[str, object]: ...


class OrchestrationWakeAuthority(Protocol):
    """Wake-only hook; advancement authority remains in the orchestrator."""

    def wake(self, run_id: str) -> None: ...


__all__ = [
    "BackupMutationAuthority",
    "ControlMutationAuthority",
    "DaemonCommandDispatcher",
    "ManagedAgentStartAuthority",
    "OrchestrationWakeAuthority",
    "RemoteAttachmentAuthority",
]
