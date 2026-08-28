from pydantic import Field, NonNegativeInt, PositiveInt, field_validator

from researchd.domain.base import DomainModel
from researchd.domain.criteria import AcceptanceCriterion
from researchd.domain.enums import Capability, DataClassification, NetworkMode
from researchd.domain.ids import RunId, WorkOrderId


class Budget(DomainModel):
    max_wall_seconds: PositiveInt | None = None
    max_cpu_seconds: NonNegativeInt | None = None
    max_gpu_seconds: NonNegativeInt | None = None
    max_disk_mb: NonNegativeInt | None = None
    max_output_mb: NonNegativeInt | None = None


class WorkOrderConstraints(DomainModel):
    network: NetworkMode = NetworkMode.NONE
    writable_paths: tuple[str, ...] = ()


class DataPolicy(DomainModel):
    default_classification: DataClassification


class WorkOrder(DomainModel):
    work_order_id: WorkOrderId
    run_id: RunId
    parent_work_order_id: WorkOrderId | None = None
    objective: str = Field(min_length=1)
    inputs: tuple[str, ...]
    requested_capabilities: tuple[Capability, ...]
    constraints: WorkOrderConstraints
    budget: Budget
    acceptance: tuple[AcceptanceCriterion, ...]
    expected_outputs: tuple[str, ...]
    data_policy: DataPolicy
    idempotency_key: str = Field(min_length=16)

    @field_validator("requested_capabilities")
    @classmethod
    def capabilities_must_be_unique(cls, value: tuple[Capability, ...]) -> tuple[Capability, ...]:
        if len(value) != len(set(value)):
            raise ValueError("requested capabilities must be unique")
        return value

