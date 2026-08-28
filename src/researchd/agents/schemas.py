from pydantic import Field, field_validator, model_validator

from researchd.domain.base import DomainModel
from researchd.domain.criteria import AcceptanceCriterion
from researchd.domain.enums import Capability
from researchd.domain.work_order import Budget, DataPolicy, WorkOrderConstraints


class HypothesisProposal(DomainModel):
    hypothesis_id: str
    statement: str = Field(min_length=1)
    priority: int = Field(ge=1)
    evidence_refs: tuple[str, ...] = ()


class WorkOrderProposal(DomainModel):
    proposal_id: str
    objective: str = Field(min_length=1)
    inputs: tuple[str, ...]
    requested_capabilities: tuple[Capability, ...]
    constraints: WorkOrderConstraints
    budget: Budget
    acceptance: tuple[AcceptanceCriterion, ...]
    expected_outputs: tuple[str, ...]
    data_policy: DataPolicy
    evidence_refs: tuple[str, ...] = ()

    @field_validator("requested_capabilities")
    @classmethod
    def capabilities_are_unique(cls, value: tuple[Capability, ...]) -> tuple[Capability, ...]:
        if len(value) != len(set(value)):
            raise ValueError("requested capabilities must be unique")
        return value


class PlanProposal(DomainModel):
    proposal_id: str
    hypotheses: tuple[HypothesisProposal, ...]
    proposed_work_orders: tuple[WorkOrderProposal, ...]
    risks: tuple[str, ...]
    required_evidence: tuple[str, ...]

    @model_validator(mode="after")
    def proposal_ids_are_unique(self) -> "PlanProposal":
        hypothesis_ids = [item.hypothesis_id for item in self.hypotheses]
        work_order_ids = [item.proposal_id for item in self.proposed_work_orders]
        if len(hypothesis_ids) != len(set(hypothesis_ids)) or len(work_order_ids) != len(set(work_order_ids)):
            raise ValueError("proposal IDs must be unique within a plan")
        return self


class EvidenceRequest(DomainModel):
    request_id: str
    question: str = Field(min_length=1)
    preferred_observations: tuple[str, ...]
    source_constraints: tuple[str, ...]
    evidence_refs: tuple[str, ...] = ()
