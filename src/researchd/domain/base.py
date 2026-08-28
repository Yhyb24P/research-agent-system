from datetime import datetime, timedelta

from pydantic import BaseModel, ConfigDict, field_validator


class DomainModel(BaseModel):
    """Strict immutable DTO base; aggregate persistence models arrive in Task 01."""

    model_config = ConfigDict(extra="forbid", frozen=True)


class MutableAggregate(BaseModel):
    """Mutable aggregate contract with optimistic concurrency metadata."""

    model_config = ConfigDict(extra="forbid", validate_assignment=True)

    version: int = 1
    created_at: datetime
    updated_at: datetime

    @field_validator("created_at", "updated_at")
    @classmethod
    def timestamp_must_be_aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() != timedelta(0):
            raise ValueError("timestamps must be timezone-aware UTC")
        return value
