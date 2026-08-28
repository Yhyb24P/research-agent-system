from researchd.domain.base import DomainModel
from researchd.domain.enums import DataClassification


class VerificationInputs(DomainModel):
    metric_artifacts: dict[str, str] = {}
    reproducibility_artifacts: dict[str, tuple[str, ...]] = {}


class ObservationDraft(DomainModel):
    observation_id: str
    name: str
    value: object
    source_artifact_ids: tuple[str, ...] = ()
    source_step_ids: tuple[str, ...] = ()
    source_job_ids: tuple[str, ...] = ()
    producer_type: str
    producer_id: str
    producer_version: str
    classification: DataClassification

    def model_post_init(self, context: object) -> None:
        del context
        if not (self.source_artifact_ids or self.source_step_ids or self.source_job_ids):
            raise ValueError("every Observation must link at least one authoritative source")
        if not self.producer_type or not self.producer_id or not self.producer_version:
            raise ValueError("every Observation must identify its producer")
