from datetime import datetime, timedelta
from typing import Literal

from pydantic import Field, field_validator

from researchd.domain.base import DomainModel
from researchd.domain.enums import DataClassification
from researchd.domain.ids import ArtifactId, AttemptId


class Artifact(DomainModel):
    artifact_id: ArtifactId
    sha256: str = Field(pattern=r"^[A-Fa-f0-9]{64}$")
    size: int = Field(ge=0)
    mime_type: str
    artifact_type: str
    classification: DataClassification
    producer_type: Literal["executor", "verifier", "controller", "tool"]
    producer_id: str
    attempt_id: AttemptId | None = None
    relative_source_path: str | None = None
    created_at: datetime

    @field_validator("created_at")
    @classmethod
    def created_at_must_be_utc(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() != timedelta(0):
            raise ValueError("created_at must be timezone-aware UTC")
        return value
