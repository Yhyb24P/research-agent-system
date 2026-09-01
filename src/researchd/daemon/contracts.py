"""Versioned command/result contracts for the trusted daemon boundary."""

from typing import Literal

from pydantic import Field, model_validator

from researchd.domain.base import DomainModel


class DaemonCommand(DomainModel):
    command_version: Literal[1] = 1
    command_id: str = Field(min_length=1, max_length=128)
    actor_type: Literal["HUMAN", "SYSTEM"]
    actor_id: str = Field(min_length=1, max_length=128)


class ExternalCommandRequest(DomainModel):
    """Untrusted transport intent; actor identity is deliberately absent."""

    request_version: Literal[1] = 1
    command_id: str = Field(min_length=1, max_length=128)


class ExternalRunCancelRequest(ExternalCommandRequest):
    pass


class ExternalWorkOrderApproveRequest(ExternalCommandRequest):
    grant_id: str = Field(min_length=1, max_length=128)


class ExternalApprovalApproveRequest(ExternalCommandRequest):
    """A HUMAN approves the pending request identified by the route."""

    pass


class ExternalHumanDecisionRequest(ExternalCommandRequest):
    action: Literal["abort", "revise"]
    objective: str | None = Field(default=None, min_length=1, max_length=16_384)

    @model_validator(mode="after")
    def revision_requires_objective(self) -> "ExternalHumanDecisionRequest":
        if self.action == "revise" and self.objective is None:
            raise ValueError("revision objective is required")
        return self


class ExternalDaemonCommandResolveRequest(ExternalCommandRequest):
    """Operator reconciliation intent; the target receipt comes from the path."""

    resource_ref: dict[str, str] = Field(default_factory=dict)
    abandon: bool = False


class ExternalWorkspaceCreateRequest(ExternalCommandRequest):
    workspace_id: str = Field(min_length=1, max_length=128)
    name: str = Field(min_length=1, max_length=256)


class ExternalResearchTaskCreateRequest(ExternalCommandRequest):
    workspace_id: str = Field(min_length=1, max_length=128)
    objective: str = Field(min_length=1, max_length=16_384)
    run_id: str | None = Field(default=None, min_length=1, max_length=128)


class ExternalWorkOrderRejectRequest(ExternalCommandRequest):
    approval_id: str = Field(min_length=1, max_length=128)


class ExternalCollaborationMessageSendRequest(ExternalCommandRequest):
    message_id: str = Field(pattern=r"^msg_[A-Za-z0-9][A-Za-z0-9_-]*$")
    run_id: str = Field(min_length=1, max_length=128)
    work_order_id: str | None = Field(default=None, min_length=1, max_length=128)
    delegation_id: str | None = Field(default=None, min_length=1, max_length=128)
    invocation_id: str | None = Field(default=None, min_length=1, max_length=128)
    reply_to_message_id: str | None = Field(default=None, min_length=1, max_length=128)
    recipient_agent_id: str | None = Field(
        default=None, pattern=r"^agent_[A-Za-z0-9][A-Za-z0-9_-]*$"
    )
    purpose: Literal["DISCUSSION", "STATUS", "QUESTION", "DIRECTIVE", "NOTICE"]
    body: str = Field(min_length=1, max_length=32_768)
    classification: Literal[
        "PUBLIC", "CLOUD_SAFE", "PROJECT_PRIVATE", "LOCAL_ONLY", "SECRET"
    ] = "PROJECT_PRIVATE"


class ExternalArtifactIngressRequest(ExternalCommandRequest):
    """Bounded local bytes; host paths and actor identity are never accepted."""

    source_name: str = Field(min_length=1, max_length=255)
    content_base64: str = Field(max_length=5_592_408)
    mime_type: str = Field(min_length=1, max_length=256)
    classification: Literal[
        "PUBLIC", "CLOUD_SAFE", "PROJECT_PRIVATE", "LOCAL_ONLY", "SECRET"
    ] = "PROJECT_PRIVATE"
    message_id: str | None = Field(
        default=None, pattern=r"^msg_[A-Za-z0-9][A-Za-z0-9_-]*$"
    )
    recipient_agent_id: str | None = Field(
        default=None, pattern=r"^agent_[A-Za-z0-9][A-Za-z0-9_-]*$"
    )


class ExternalHandoffDecisionRequest(ExternalCommandRequest):
    decision: Literal["accept", "reject"]
    reason: str = Field(min_length=1, max_length=16_384)
    target_agent_id: str | None = Field(
        default=None, pattern=r"^agent_[A-Za-z0-9][A-Za-z0-9_-]*$"
    )

    @model_validator(mode="after")
    def target_only_applies_to_accept(self) -> "ExternalHandoffDecisionRequest":
        if self.decision == "reject" and self.target_agent_id is not None:
            raise ValueError("a rejected handoff cannot select a target Agent")
        return self


class ExternalManagedAgentStartRequest(ExternalCommandRequest):
    """Agent-scoped launch intent; the daemon resolves the launch spec."""

    runtime_id: str | None = Field(
        default=None, pattern=r"^runtime_[A-Za-z0-9][A-Za-z0-9_-]*$"
    )


class ExternalRemoteAgentAttachRequest(ExternalCommandRequest):
    """Intent to attach an installed remote A2A runtime; no endpoint is accepted."""

    runtime_id: str = Field(pattern=r"^runtime_[A-Za-z0-9][A-Za-z0-9_-]*$")


class ExternalRemoteAgentDetachRequest(ExternalCommandRequest):
    runtime_id: str = Field(pattern=r"^runtime_[A-Za-z0-9][A-Za-z0-9_-]*$")


class ExternalRemoteAgentRenewRequest(ExternalCommandRequest):
    runtime_id: str = Field(pattern=r"^runtime_[A-Za-z0-9][A-Za-z0-9_-]*$")


class ExternalBackupCreateRequest(ExternalCommandRequest):
    destination: str = Field(min_length=1, max_length=1024)
    candidate_commit: str = Field(pattern=r"^[0-9a-f]{40}$")
    candidate_tag: str = Field(pattern=r"^v[0-9]+\.[0-9]+\.[0-9]+-rc\.[0-9A-Za-z.-]+$")


class ExternalBackupVerifyRequest(ExternalCommandRequest):
    snapshot: str = Field(min_length=1, max_length=1024)


class ExternalRestorePlanRequest(ExternalCommandRequest):
    snapshot: str = Field(min_length=1, max_length=1024)
    database_destination: str = Field(min_length=1, max_length=1024)
    artifact_destination: str = Field(min_length=1, max_length=1024)
    expected_candidate_commit: str = Field(pattern=r"^[0-9a-f]{40}$")
    expected_candidate_tag: str = Field(pattern=r"^v[0-9]+\.[0-9]+\.[0-9]+-rc\.[0-9A-Za-z.-]+$")


class RunCancelCommand(DaemonCommand):
    run_id: str = Field(min_length=1, max_length=128)


class WorkOrderApproveCommand(DaemonCommand):
    work_order_id: str = Field(min_length=1, max_length=128)
    grant_id: str = Field(min_length=1, max_length=128)


class ApprovalApproveCommand(DaemonCommand):
    """External HUMAN intent; grant identity is controller-owned."""

    approval_id: str = Field(min_length=1, max_length=128)


class HumanDecisionCommand(DaemonCommand):
    work_order_id: str = Field(min_length=1, max_length=128)
    action: Literal["abort", "revise"]
    objective: str | None = Field(default=None, min_length=1, max_length=16_384)

    @model_validator(mode="after")
    def revision_requires_objective(self) -> "HumanDecisionCommand":
        if self.action == "revise" and self.objective is None:
            raise ValueError("revision objective is required")
        return self


class DaemonCommandResolveCommand(DaemonCommand):
    """Operator reconciliation of an interrupted ACCEPTED receipt.

    The outcome is derived from command-specific observation of the
    authoritative state; ``abandon`` only applies when that observation is
    undetermined and records an explicit operator decision.
    """

    target_command_id: str = Field(min_length=1, max_length=128)
    resource_ref: dict[str, str] = Field(default_factory=dict)
    abandon: bool = False


class WorkspaceCreateCommand(DaemonCommand):
    workspace_id: str = Field(min_length=1, max_length=128)
    name: str = Field(min_length=1, max_length=256)


class ResearchTaskCreateCommand(DaemonCommand):
    workspace_id: str = Field(min_length=1, max_length=128)
    objective: str = Field(min_length=1, max_length=16_384)
    run_id: str | None = Field(default=None, min_length=1, max_length=128)


class WorkOrderRejectCommand(DaemonCommand):
    work_order_id: str = Field(min_length=1, max_length=128)
    approval_id: str = Field(min_length=1, max_length=128)


class CollaborationMessageSendCommand(DaemonCommand):
    message_id: str = Field(pattern=r"^msg_[A-Za-z0-9][A-Za-z0-9_-]*$")
    run_id: str = Field(min_length=1, max_length=128)
    work_order_id: str | None = Field(default=None, min_length=1, max_length=128)
    delegation_id: str | None = Field(default=None, min_length=1, max_length=128)
    invocation_id: str | None = Field(default=None, min_length=1, max_length=128)
    reply_to_message_id: str | None = Field(default=None, min_length=1, max_length=128)
    recipient_agent_id: str | None = Field(
        default=None, pattern=r"^agent_[A-Za-z0-9][A-Za-z0-9_-]*$"
    )
    purpose: Literal["DISCUSSION", "STATUS", "QUESTION", "DIRECTIVE", "NOTICE"]
    body: str = Field(min_length=1, max_length=32_768)
    classification: Literal[
        "PUBLIC", "CLOUD_SAFE", "PROJECT_PRIVATE", "LOCAL_ONLY", "SECRET"
    ] = "PROJECT_PRIVATE"


class HandoffDecisionCommand(DaemonCommand):
    proposal_id: str = Field(pattern=r"^handoff_[A-Za-z0-9][A-Za-z0-9_-]*$")
    decision: Literal["accept", "reject"]
    reason: str = Field(min_length=1, max_length=16_384)
    target_agent_id: str | None = Field(
        default=None, pattern=r"^agent_[A-Za-z0-9][A-Za-z0-9_-]*$"
    )

    @model_validator(mode="after")
    def target_only_applies_to_accept(self) -> "HandoffDecisionCommand":
        if self.decision == "reject" and self.target_agent_id is not None:
            raise ValueError("a rejected handoff cannot select a target Agent")
        return self


class ManagedAgentStartCommand(DaemonCommand):
    """Agent-scoped launch intent; only the daemon may see a launch spec."""

    agent_id: str = Field(pattern=r"^agent_[A-Za-z0-9][A-Za-z0-9_-]*$")
    runtime_id: str | None = Field(
        default=None, pattern=r"^runtime_[A-Za-z0-9][A-Za-z0-9_-]*$"
    )


class RemoteAgentAttachCommand(DaemonCommand):
    runtime_id: str = Field(pattern=r"^runtime_[A-Za-z0-9][A-Za-z0-9_-]*$")


class RemoteAgentDetachCommand(DaemonCommand):
    runtime_id: str = Field(pattern=r"^runtime_[A-Za-z0-9][A-Za-z0-9_-]*$")


class RemoteAgentRenewCommand(DaemonCommand):
    runtime_id: str = Field(pattern=r"^runtime_[A-Za-z0-9][A-Za-z0-9_-]*$")


class BackupCreateCommand(DaemonCommand):
    destination: str = Field(min_length=1, max_length=1024)
    candidate_commit: str = Field(pattern=r"^[0-9a-f]{40}$")
    candidate_tag: str = Field(pattern=r"^v[0-9]+\.[0-9]+\.[0-9]+-rc\.[0-9A-Za-z.-]+$")


class BackupVerifyCommand(DaemonCommand):
    snapshot: str = Field(min_length=1, max_length=1024)


class RestorePlanCommand(DaemonCommand):
    snapshot: str = Field(min_length=1, max_length=1024)
    database_destination: str = Field(min_length=1, max_length=1024)
    artifact_destination: str = Field(min_length=1, max_length=1024)
    expected_candidate_commit: str = Field(pattern=r"^[0-9a-f]{40}$")
    expected_candidate_tag: str = Field(pattern=r"^v[0-9]+\.[0-9]+\.[0-9]+-rc\.[0-9A-Za-z.-]+$")


class DaemonCommandResult(DomainModel):
    command_version: Literal[1] = 1
    command_id: str
    command_type: str
    status: Literal["ACCEPTED", "REJECTED"]
    resource: dict[str, object] | None = None
    reason_code: str | None = None


__all__ = [
    "BackupCreateCommand",
    "ApprovalApproveCommand",
    "BackupVerifyCommand",
    "CollaborationMessageSendCommand",
    "DaemonCommand",
    "DaemonCommandResolveCommand",
    "DaemonCommandResult",
    "ExternalBackupCreateRequest",
    "ExternalArtifactIngressRequest",
    "ExternalApprovalApproveRequest",
    "ExternalBackupVerifyRequest",
    "ExternalCommandRequest",
    "ExternalCollaborationMessageSendRequest",
    "ExternalDaemonCommandResolveRequest",
    "ExternalHandoffDecisionRequest",
    "ExternalHumanDecisionRequest",
    "ExternalManagedAgentStartRequest",
    "ExternalRemoteAgentAttachRequest",
    "ExternalRemoteAgentDetachRequest",
    "ExternalRemoteAgentRenewRequest",
    "ExternalResearchTaskCreateRequest",
    "ExternalRestorePlanRequest",
    "ExternalRunCancelRequest",
    "ExternalWorkOrderApproveRequest",
    "ExternalWorkOrderRejectRequest",
    "ExternalWorkspaceCreateRequest",
    "HumanDecisionCommand",
    "HandoffDecisionCommand",
    "ManagedAgentStartCommand",
    "RemoteAgentAttachCommand",
    "RemoteAgentDetachCommand",
    "RemoteAgentRenewCommand",
    "ResearchTaskCreateCommand",
    "RestorePlanCommand",
    "RunCancelCommand",
    "WorkOrderApproveCommand",
    "WorkOrderRejectCommand",
    "WorkspaceCreateCommand",
]
