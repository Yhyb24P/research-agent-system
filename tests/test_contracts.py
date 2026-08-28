import json
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest
from pydantic import TypeAdapter, ValidationError

from researchd.domain.ids import AttemptId, WorkOrderId
from researchd.domain.attempt import Attempt
from researchd.domain.review import ReviewDecision
from researchd.domain.work_order import WorkOrder

ROOT = Path(__file__).parents[1]


def load_example(name: str) -> dict[str, Any]:
    value: dict[str, Any] = json.loads((ROOT / "examples" / name).read_text())
    return value


def test_every_example_json_has_a_typed_contract() -> None:
    WorkOrder.model_validate(load_example("sample_work_order.json"))
    ReviewDecision.model_validate(load_example("sample_review_decision.json"))


def test_invalid_review_decision_rejected() -> None:
    payload = dict(load_example("sample_review_decision.json"))
    payload["decision"] = "OVERRIDE_VERIFIER"
    with pytest.raises(ValidationError):
        ReviewDecision.model_validate(payload)


def test_invalid_work_order_capability_rejected() -> None:
    payload = dict(load_example("sample_work_order.json"))
    payload["requested_capabilities"] = ["host.shell"]
    with pytest.raises(ValidationError):
        WorkOrder.model_validate(payload)


def test_entity_id_types_cannot_be_confused_at_validation_boundary() -> None:
    assert TypeAdapter(WorkOrderId).validate_python("wo_123") == "wo_123"
    assert TypeAdapter(AttemptId).validate_python("att_123") == "att_123"
    with pytest.raises(ValidationError):
        TypeAdapter(WorkOrderId).validate_python("att_123")
    with pytest.raises(ValidationError):
        TypeAdapter(AttemptId).validate_python("wo_123")


def test_mutable_aggregate_timestamps_must_be_utc() -> None:
    now = datetime.now(UTC)
    Attempt(attempt_id=AttemptId("att_123"), work_order_id=WorkOrderId("wo_123"), created_at=now, updated_at=now)
    non_utc = now.astimezone(timezone(timedelta(hours=8)))
    with pytest.raises(ValidationError):
        Attempt(attempt_id=AttemptId("att_123"), work_order_id=WorkOrderId("wo_123"), created_at=non_utc, updated_at=non_utc)
