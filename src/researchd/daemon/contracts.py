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


class RunCancelCommand(DaemonCommand):
    run_id: str = Field(min_length=1, max_length=128)


class WorkOrderApproveCommand(DaemonCommand):
    work_order_id: str = Field(min_length=1, max_length=128)
    grant_id: str = Field(min_length=1, max_length=128)


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


class DaemonCommandResult(DomainModel):
    command_version: Literal[1] = 1
    command_id: str
    command_type: str
    status: Literal["ACCEPTED", "REJECTED"]
    resource: dict[str, object] | None = None
    reason_code: str | None = None


__all__ = [
    "DaemonCommand",
    "DaemonCommandResolveCommand",
    "DaemonCommandResult",
    "ExternalCommandRequest",
    "ExternalDaemonCommandResolveRequest",
    "ExternalHumanDecisionRequest",
    "ExternalRunCancelRequest",
    "ExternalWorkOrderApproveRequest",
    "HumanDecisionCommand",
    "RunCancelCommand",
    "WorkOrderApproveCommand",
]
