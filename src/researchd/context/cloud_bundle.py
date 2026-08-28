from typing import Any

from researchd.domain.base import DomainModel


class CloudArtifactItem(DomainModel):
    artifact_id: str
    sha256: str
    mime_type: str
    artifact_type: str
    classification: str
    content: str


class CloudContextBundle(DomainModel):
    run_id: str
    work_order_id: str | None
    goal: str
    objective: str | None
    selected_artifacts: tuple[CloudArtifactItem, ...]
    observations: tuple["CloudObservationItem", ...] = ()
    verification: "CloudVerificationItem | None" = None


class CloudObservationItem(DomainModel):
    observation_id: str
    name: str
    value: Any
    source_artifact_ids: tuple[str, ...]
    producer_id: str
    producer_version: str
    classification: str


class CloudVerificationItem(DomainModel):
    verification_id: str
    overall: str
    criteria: tuple[dict[str, Any], ...]
    acceptance_sha256: str
    verifier_version: str
    classification: str


CloudContextBundle.model_rebuild()
