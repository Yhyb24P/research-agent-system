import pytest
from typing import Any

from researchd.domain.enums import AttemptState, ResearchRunState, WorkOrderState
from researchd.domain.transitions import (
    ATTEMPT_TRANSITIONS,
    RUN_TRANSITIONS,
    WORK_ORDER_TRANSITIONS,
    InvalidTransition,
    require_transition,
)


@pytest.mark.parametrize(
    ("terminal", "candidate", "table"),
    [
        (ResearchRunState.COMPLETED, ResearchRunState.ACTIVE, RUN_TRANSITIONS),
        (ResearchRunState.FAILED, ResearchRunState.ACTIVE, RUN_TRANSITIONS),
        (ResearchRunState.CANCELLED, ResearchRunState.ACTIVE, RUN_TRANSITIONS),
        (WorkOrderState.ACCEPTED, WorkOrderState.REVIEWING, WORK_ORDER_TRANSITIONS),
        (WorkOrderState.FAILED, WorkOrderState.READY, WORK_ORDER_TRANSITIONS),
        (WorkOrderState.CANCELLED, WorkOrderState.READY, WORK_ORDER_TRANSITIONS),
        (AttemptState.SUCCEEDED, AttemptState.RUNNING, ATTEMPT_TRANSITIONS),
        (AttemptState.FAILED, AttemptState.RUNNING, ATTEMPT_TRANSITIONS),
        (AttemptState.CANCELLED, AttemptState.RUNNING, ATTEMPT_TRANSITIONS),
    ],
)
def test_terminal_state_transition_rejected(terminal: Any, candidate: Any, table: Any) -> None:
    with pytest.raises(InvalidTransition):
        require_transition(terminal, candidate, table)


def test_documented_transition_is_accepted() -> None:
    require_transition(WorkOrderState.READY, WorkOrderState.DISPATCHED, WORK_ORDER_TRANSITIONS)
