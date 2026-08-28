from datetime import datetime

from researchd.domain.base import MutableAggregate
from researchd.domain.enums import AttemptState
from researchd.domain.ids import AttemptId, WorkOrderId


class Attempt(MutableAggregate):
    attempt_id: AttemptId
    work_order_id: WorkOrderId
    state: AttemptState = AttemptState.CREATED
    terminal_at: datetime | None = None

