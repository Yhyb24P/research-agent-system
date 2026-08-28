from typing import Any

from researchd.domain.base import DomainModel
from researchd.domain.ids import ArtifactId, ClaimId, ObservationId


class Observation(DomainModel):
    observation_id: ObservationId
    name: str
    value: Any
    source_artifact_refs: tuple[ArtifactId, ...]
    producer: str


class Claim(DomainModel):
    claim_id: ClaimId
    statement: str
    supporting_refs: tuple[str, ...] = ()

