"""Contracts for workspace grants, leases, snapshots, transports, and reconciliation."""

from datetime import datetime, timedelta
from enum import StrEnum

from pydantic import Field, PositiveInt, field_validator, model_validator

from researchd.domain.base import DomainModel
from researchd.domain.enums import DataClassification


class WorkspaceAccessMode(StrEnum):
    READ_ONLY = "READ_ONLY"
    READ_WRITE = "READ_WRITE"


class WorkspaceTransportKind(StrEnum):
    GIT_WORKTREE = "GIT_WORKTREE"
    ARCHIVE = "ARCHIVE"


class WorkspaceGrantState(StrEnum):
    PENDING = "PENDING"
    PROVISIONING = "PROVISIONING"
    ACTIVE = "ACTIVE"
    RECONCILING = "RECONCILING"
    RECOVERING = "RECOVERING"
    COMPLETED = "COMPLETED"
    EXPIRED = "EXPIRED"
    CANCELLED = "CANCELLED"
    FAILED = "FAILED"


class ReconciliationMode(StrEnum):
    ARTIFACT_ONLY = "ARTIFACT_ONLY"


class ReconciliationState(StrEnum):
    PENDING = "PENDING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"


class CleanupState(StrEnum):
    PENDING = "PENDING"
    CLEANED = "CLEANED"
    FAILED = "FAILED"


class RenewalPolicy(StrEnum):
    DENY = "DENY"
    EXPLICIT = "EXPLICIT"


class WorkspaceLimits(DomainModel):
    max_total_bytes: PositiveInt = 100_000_000
    max_file_count: PositiveInt = 10_000
    max_single_file_bytes: PositiveInt = 50_000_000

    @model_validator(mode="after")
    def single_file_cannot_exceed_total(self) -> "WorkspaceLimits":
        if self.max_single_file_bytes > self.max_total_bytes:
            raise ValueError("max_single_file_bytes cannot exceed max_total_bytes")
        return self


class WorkspaceFile(DomainModel):
    path: str = Field(min_length=1)
    size: int = Field(ge=0)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class WorkspaceSnapshot(DomainModel):
    source_revision: str | None = None
    manifest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    files: tuple[WorkspaceFile, ...]
    total_bytes: int = Field(ge=0)
    file_count: int = Field(ge=0)


class WorkspaceGrant(DomainModel):
    workspace_grant_id: str = Field(min_length=1, max_length=128)
    delegation_id: str = Field(min_length=1, max_length=128)
    source_workspace_id: str = Field(min_length=1, max_length=128)
    source_revision: str | None = None
    source_manifest_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    access_mode: WorkspaceAccessMode
    allowed_paths: tuple[str, ...] = ()
    excluded_paths: tuple[str, ...] = ()
    classification_ceiling: DataClassification
    limits: WorkspaceLimits = WorkspaceLimits()
    lease_seconds: PositiveInt = Field(default=3600, le=86_400)
    renewal_policy: RenewalPolicy = RenewalPolicy.DENY
    transport_kind: WorkspaceTransportKind
    reconciliation_mode: ReconciliationMode = ReconciliationMode.ARTIFACT_ONLY


class ProvisionedWorkspace(DomainModel):
    transport_handle: dict[str, str]
    remote_workspace_handle: str = Field(min_length=1)


def require_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() != timedelta(0):
        raise ValueError("workspace lease timestamps must be UTC-aware")
    return value


class WorkspaceGrantBinding(DomainModel):
    workspace_grant_id: str = Field(min_length=1)
    transport_kind: WorkspaceTransportKind
    remote_workspace_handle: str = Field(min_length=1)
    access_mode: WorkspaceAccessMode
    allowed_paths: tuple[str, ...]
    excluded_paths: tuple[str, ...]
    source_manifest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    lease_expires_at: datetime

    _lease_expiry_utc = field_validator("lease_expires_at")(require_utc)


class ReconciliationPayload(DomainModel):
    payload: bytes
    mime_type: str = Field(min_length=1)
    artifact_type: str = Field(min_length=1)
    result_snapshot: WorkspaceSnapshot
    summary: str = Field(min_length=1)


class WorkspaceLease(DomainModel):
    started_at: datetime
    expires_at: datetime

    _started_utc = field_validator("started_at")(require_utc)
    _expires_utc = field_validator("expires_at")(require_utc)

    @model_validator(mode="after")
    def expiry_follows_start(self) -> "WorkspaceLease":
        if self.expires_at <= self.started_at:
            raise ValueError("workspace lease expiry must follow its start")
        return self
