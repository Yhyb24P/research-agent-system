"""Versioned command/result contracts for the trusted daemon boundary."""

from typing import Literal

from pydantic import Field, model_validator

from researchd.domain.base import DomainModel


class DaemonCommand(DomainModel):
    command_version: Literal[1] = 1
    command_id: str = Field(min_length=1, max_length=128)
    actor_type: Literal["HUMAN", "SYSTEM"]
    actor_id: str = Field(min_length=1, max_length=128)


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


class DaemonCommandResult(DomainModel):
    command_version: Literal[1] = 1
    command_id: str
    command_type: str
    status: Literal["ACCEPTED", "REJECTED"]
    resource: dict[str, object] | None = None
    reason_code: str | None = None


__all__ = [
    "DaemonCommand",
    "DaemonCommandResult",
    "HumanDecisionCommand",
    "RunCancelCommand",
    "WorkOrderApproveCommand",
]
