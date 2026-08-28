from pydantic import Field

from researchd.domain.base import DomainModel
from researchd.domain.enums import ReviewDecisionKind
from researchd.domain.ids import WorkOrderId


class ReviewDecision(DomainModel):
    decision: ReviewDecisionKind
    work_order_id: WorkOrderId
    evidence_refs: tuple[str, ...] = Field(min_length=1)
    deficiencies: tuple[str, ...]
    rationale: str = Field(min_length=1)
    requested_next_objective: str | None = None
    requested_evidence: tuple[str, ...]
    confidence: float | None = Field(default=None, ge=0, le=1)
