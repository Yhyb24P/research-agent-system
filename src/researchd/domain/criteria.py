from typing import Annotated, Literal

from pydantic import Field, PositiveInt, TypeAdapter, field_validator, model_validator
import math
import hashlib
import json
from typing import Any, cast

from researchd.domain.base import DomainModel


class CommandCriterion(DomainModel):
    criterion_id: str
    type: Literal["command"]
    command_id: str
    expected_exit_code: int = 0
    junit_artifact_id: str | None = None
    severity: Literal["hard", "advisory"] = "hard"


class MetricCriterion(DomainModel):
    criterion_id: str
    type: Literal["metric"]
    metric: str
    operator: Literal["==", "!=", ">", ">=", "<", "<="]
    threshold: float
    severity: Literal["hard", "advisory"] = "hard"

    @field_validator("threshold")
    @classmethod
    def threshold_must_be_finite(cls, value: float) -> float:
        if not math.isfinite(value):
            raise ValueError("metric threshold must be finite")
        return value


class ArtifactCriterion(DomainModel):
    criterion_id: str
    type: Literal["artifact"]
    artifact_type: str
    min_count: PositiveInt = 1
    severity: Literal["hard", "advisory"] = "hard"


class ReproCriterion(DomainModel):
    criterion_id: str
    type: Literal["reproducibility"]
    runs: PositiveInt
    required_successes: PositiveInt
    severity: Literal["hard", "advisory"] = "hard"

    @model_validator(mode="after")
    def successes_cannot_exceed_runs(self) -> "ReproCriterion":
        if self.required_successes > self.runs:
            raise ValueError("required_successes cannot exceed runs")
        return self


AcceptanceCriterion = Annotated[
    CommandCriterion | MetricCriterion | ArtifactCriterion | ReproCriterion,
    Field(discriminator="type"),
]


def normalized_acceptance(value: Any) -> list[dict[str, Any]]:
    criteria = TypeAdapter(tuple[AcceptanceCriterion, ...]).validate_python(value)
    identifiers = [criterion.criterion_id for criterion in criteria]
    if len(identifiers) != len(set(identifiers)):
        raise ValueError("acceptance criterion IDs must be unique")
    return cast(list[dict[str, Any]], TypeAdapter(tuple[AcceptanceCriterion, ...]).dump_python(criteria, mode="json"))


def acceptance_fingerprint(value: Any) -> str:
    canonical = json.dumps(normalized_acceptance(value), sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)
    return hashlib.sha256(canonical.encode()).hexdigest()
