from enum import StrEnum
from typing import TypeVar

from researchd.domain.enums import AttemptState, ResearchRunState, WorkOrderState

StateT = TypeVar("StateT", bound=StrEnum)


class InvalidTransition(ValueError):
    pass


RUN_TRANSITIONS: dict[ResearchRunState, frozenset[ResearchRunState]] = {
    ResearchRunState.NEW: frozenset({ResearchRunState.PLANNING, ResearchRunState.CANCELLED}),
    ResearchRunState.PLANNING: frozenset({ResearchRunState.ACTIVE, ResearchRunState.WAITING_EXTERNAL, ResearchRunState.FAILED, ResearchRunState.CANCELLED}),
    ResearchRunState.ACTIVE: frozenset({ResearchRunState.REVIEWING, ResearchRunState.WAITING_HUMAN, ResearchRunState.WAITING_EXTERNAL, ResearchRunState.FAILED, ResearchRunState.CANCELLED}),
    ResearchRunState.REVIEWING: frozenset({ResearchRunState.ACTIVE, ResearchRunState.WAITING_HUMAN, ResearchRunState.WAITING_EXTERNAL, ResearchRunState.COMPLETED, ResearchRunState.FAILED, ResearchRunState.CANCELLED}),
    ResearchRunState.WAITING_HUMAN: frozenset({ResearchRunState.ACTIVE, ResearchRunState.FAILED, ResearchRunState.CANCELLED}),
    ResearchRunState.WAITING_EXTERNAL: frozenset({ResearchRunState.PLANNING, ResearchRunState.ACTIVE, ResearchRunState.REVIEWING, ResearchRunState.FAILED, ResearchRunState.CANCELLED}),
    ResearchRunState.COMPLETED: frozenset(),
    ResearchRunState.FAILED: frozenset(),
    ResearchRunState.CANCELLED: frozenset(),
}

WORK_ORDER_TRANSITIONS: dict[WorkOrderState, frozenset[WorkOrderState]] = {
    WorkOrderState.DRAFT: frozenset({WorkOrderState.POLICY_CHECK, WorkOrderState.CANCELLED}),
    WorkOrderState.POLICY_CHECK: frozenset({WorkOrderState.WAITING_APPROVAL, WorkOrderState.READY, WorkOrderState.FAILED, WorkOrderState.CANCELLED}),
    WorkOrderState.WAITING_APPROVAL: frozenset({WorkOrderState.POLICY_CHECK, WorkOrderState.FAILED, WorkOrderState.CANCELLED}),
    WorkOrderState.READY: frozenset({WorkOrderState.DISPATCHED, WorkOrderState.CANCELLED}),
    WorkOrderState.DISPATCHED: frozenset({WorkOrderState.EXECUTING, WorkOrderState.EXECUTION_FAILED, WorkOrderState.CANCELLED}),
    WorkOrderState.EXECUTING: frozenset({WorkOrderState.WAITING_JOB, WorkOrderState.VERIFYING, WorkOrderState.EXECUTION_FAILED, WorkOrderState.CANCELLED}),
    WorkOrderState.WAITING_JOB: frozenset({WorkOrderState.EXECUTING, WorkOrderState.EXECUTION_FAILED, WorkOrderState.CANCELLED}),
    WorkOrderState.VERIFYING: frozenset({WorkOrderState.REVIEW_READY, WorkOrderState.VERIFICATION_FAILED, WorkOrderState.CANCELLED}),
    WorkOrderState.EXECUTION_FAILED: frozenset({WorkOrderState.EXECUTING, WorkOrderState.REVISION_REQUIRED, WorkOrderState.FAILED, WorkOrderState.CANCELLED}),
    WorkOrderState.VERIFICATION_FAILED: frozenset({WorkOrderState.REVISION_REQUIRED, WorkOrderState.FAILED, WorkOrderState.CANCELLED}),
    WorkOrderState.REVIEW_READY: frozenset({WorkOrderState.REVIEWING, WorkOrderState.CANCELLED}),
    WorkOrderState.REVIEWING: frozenset({WorkOrderState.ACCEPTED, WorkOrderState.REVISION_REQUIRED, WorkOrderState.MORE_EVIDENCE_REQUIRED, WorkOrderState.HUMAN_REQUIRED, WorkOrderState.FAILED, WorkOrderState.CANCELLED}),
    WorkOrderState.MORE_EVIDENCE_REQUIRED: frozenset({WorkOrderState.REVISION_REQUIRED, WorkOrderState.FAILED, WorkOrderState.CANCELLED}),
    WorkOrderState.HUMAN_REQUIRED: frozenset({WorkOrderState.REVISION_REQUIRED, WorkOrderState.FAILED, WorkOrderState.CANCELLED}),
    WorkOrderState.ACCEPTED: frozenset(),
    WorkOrderState.REVISION_REQUIRED: frozenset(),
    WorkOrderState.FAILED: frozenset(),
    WorkOrderState.CANCELLED: frozenset(),
}

ATTEMPT_TRANSITIONS: dict[AttemptState, frozenset[AttemptState]] = {
    AttemptState.CREATED: frozenset({AttemptState.PREPARING, AttemptState.FAILED, AttemptState.CANCELLED}),
    AttemptState.PREPARING: frozenset({AttemptState.RUNNING, AttemptState.FAILED, AttemptState.CANCELLED}),
    AttemptState.RUNNING: frozenset({AttemptState.WAITING_JOB, AttemptState.VERIFYING, AttemptState.FAILED, AttemptState.CANCELLED}),
    AttemptState.WAITING_JOB: frozenset({AttemptState.RUNNING, AttemptState.FAILED, AttemptState.CANCELLED}),
    AttemptState.VERIFYING: frozenset({AttemptState.SUCCEEDED, AttemptState.FAILED, AttemptState.CANCELLED}),
    AttemptState.SUCCEEDED: frozenset(),
    AttemptState.FAILED: frozenset(),
    AttemptState.CANCELLED: frozenset(),
}


def require_transition(current: StateT, target: StateT, table: dict[StateT, frozenset[StateT]]) -> None:
    if target not in table[current]:
        raise InvalidTransition(f"transition {current.value} -> {target.value} is not allowed")
