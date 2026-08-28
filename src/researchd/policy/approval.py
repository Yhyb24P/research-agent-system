from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from sqlalchemy import update
from sqlalchemy.engine import CursorResult
from sqlalchemy.orm import Session, sessionmaker

from researchd.artifacts.hashing import sha256_bytes
from researchd.artifacts.provenance import canonical_json
from researchd.domain.enums import ApprovalStatus
from researchd.storage.models import ApprovalGrantRecord, ApprovalRequestRecord


class ApprovalError(RuntimeError):
    pass


class ApprovalNotValid(ApprovalError):
    pass


def parameter_hash(operation_type: str, parameters: Mapping[str, Any]) -> tuple[str, str]:
    canonical = canonical_json({"operation_type": operation_type, "parameters": parameters})
    return canonical, sha256_bytes(canonical.encode())


class ApprovalService:
    def __init__(self, sessions: sessionmaker[Session]) -> None:
        self.sessions = sessions

    def request(
        self, *, operation_type: str, parameters: Mapping[str, Any], requested_by: str,
        reason: str, risk_level: str, resource_scope: Mapping[str, Any],
        budget_delta: Mapping[str, Any], expires_at: datetime, one_shot: bool = True,
    ) -> ApprovalRequestRecord:
        now = datetime.now(UTC)
        if expires_at.tzinfo is None or expires_at <= now:
            raise ApprovalError("approval request expiry must be an aware future timestamp")
        canonical, digest = parameter_hash(operation_type, parameters)
        record = ApprovalRequestRecord(
            approval_id=f"apr_{uuid4().hex}", operation_type=operation_type,
            canonical_parameters=canonical, parameter_sha256=digest, requested_by=requested_by,
            reason=reason, risk_level=risk_level, resource_scope=dict(resource_scope),
            budget_delta=dict(budget_delta), expires_at=expires_at, one_shot=one_shot,
            status=ApprovalStatus.PENDING.value, created_at=now,
        )
        with self.sessions.begin() as session:
            session.add(record)
        return record

    def approve(self, approval_id: str, *, granted_by: str, expires_at: datetime | None = None) -> ApprovalGrantRecord:
        now = datetime.now(UTC)
        with self.sessions.begin() as session:
            request = session.get(ApprovalRequestRecord, approval_id)
            if request is None:
                raise ApprovalError("approval request not found")
            if request.status != ApprovalStatus.PENDING.value or request.expires_at <= now:
                raise ApprovalNotValid("approval request is not pending and unexpired")
            grant_expiry = request.expires_at if expires_at is None else expires_at
            if grant_expiry.tzinfo is None or grant_expiry <= now or grant_expiry > request.expires_at:
                raise ApprovalError("grant expiry must be future and no later than request expiry")
            request.status = ApprovalStatus.APPROVED.value
            grant = ApprovalGrantRecord(
                grant_id=f"grant_{uuid4().hex}", approval_id=approval_id,
                parameter_sha256=request.parameter_sha256, granted_by=granted_by,
                expires_at=grant_expiry, one_shot=request.one_shot, used_at=None, created_at=now,
            )
            session.add(grant)
            session.flush()
            return grant

    def authorize(self, grant_id: str, *, operation_type: str, parameters: Mapping[str, Any]) -> None:
        _, digest = parameter_hash(operation_type, parameters)
        now = datetime.now(UTC)
        with self.sessions.begin() as session:
            grant = session.get(ApprovalGrantRecord, grant_id)
            if grant is None:
                raise ApprovalNotValid("approval grant not found")
            request = session.get(ApprovalRequestRecord, grant.approval_id)
            if request is None or request.status != ApprovalStatus.APPROVED.value:
                raise ApprovalNotValid("approval request is not approved")
            if digest != grant.parameter_sha256 or digest != request.parameter_sha256:
                raise ApprovalNotValid("operation parameters do not match approval hash")
            if grant.expires_at <= now:
                raise ApprovalNotValid("approval grant has expired")
            if not grant.one_shot:
                return
            result = session.execute(
                update(ApprovalGrantRecord)
                .where(ApprovalGrantRecord.grant_id == grant_id, ApprovalGrantRecord.used_at.is_(None), ApprovalGrantRecord.expires_at > now)
                .values(used_at=now)
            )
            cursor = result  # SQLAlchemy returns a cursor result for UPDATE.
            if not isinstance(cursor, CursorResult) or cursor.rowcount != 1:
                raise ApprovalNotValid("one-shot approval grant has already been used")
