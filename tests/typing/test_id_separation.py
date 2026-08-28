from typing import cast

from researchd.domain.ids import AttemptId, WorkOrderId


def accepts_work_order_id(value: WorkOrderId) -> None:
    del value


work_order_id = cast(WorkOrderId, "wo_static")
attempt_id = cast(AttemptId, "att_static")

accepts_work_order_id(work_order_id)
accepts_work_order_id(attempt_id)  # type: ignore[arg-type]
