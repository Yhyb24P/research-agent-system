"""Durable, exclusive GPU admission semantics.

This module reserves logical device IDs; it does not claim hardware isolation.
The target scheduler/container must enforce the returned device assignment.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from researchd.storage.models import GpuLeaseRecord


GPU_LEASED = "LEASED"
GPU_RELEASED = "RELEASED"


class GpuAdmissionError(RuntimeError):
    """The requested GPU allocation cannot be granted safely."""


@dataclass(frozen=True)
class GpuLease:
    lease_id: str
    job_id: str
    device_id: str


class GpuAdmissionController:
    """Single-controller durable exclusive allocation for configured devices."""

    def __init__(self, sessions: sessionmaker[Session], device_ids: tuple[str, ...]) -> None:
        normalized = tuple(dict.fromkeys(device_ids))
        if not normalized or any(not item for item in normalized):
            raise ValueError("GPU admission requires at least one nonempty device ID")
        self.sessions = sessions
        self.device_ids = normalized

    def acquire(self, job_id: str, count: int) -> tuple[GpuLease, ...]:
        if count < 0:
            raise ValueError("GPU count cannot be negative")
        if count == 0:
            return ()
        now = datetime.now(UTC)
        with self.sessions.begin() as session:
            existing = session.scalars(select(GpuLeaseRecord).where(
                GpuLeaseRecord.job_id == job_id,
                GpuLeaseRecord.state == GPU_LEASED,
            ).order_by(GpuLeaseRecord.device_id)).all()
            if existing:
                if len(existing) != count:
                    raise GpuAdmissionError("job already has a different GPU allocation")
                return tuple(GpuLease(item.lease_id, item.job_id, item.device_id) for item in existing)
            occupied = set(session.scalars(select(GpuLeaseRecord.device_id).where(GpuLeaseRecord.state == GPU_LEASED)).all())
            available = [device for device in self.device_ids if device not in occupied]
            if len(available) < count:
                raise GpuAdmissionError("insufficient exclusively available GPUs")
            leases = tuple(GpuLease(f"lease_{uuid4().hex}", job_id, device) for device in available[:count])
            session.add_all(GpuLeaseRecord(
                lease_id=item.lease_id, job_id=item.job_id, device_id=item.device_id,
                state=GPU_LEASED, created_at=now,
            ) for item in leases)
            return leases

    def release(self, job_id: str) -> tuple[str, ...]:
        now = datetime.now(UTC)
        with self.sessions.begin() as session:
            records = session.scalars(select(GpuLeaseRecord).where(
                GpuLeaseRecord.job_id == job_id,
                GpuLeaseRecord.state == GPU_LEASED,
            )).all()
            for record in records:
                record.state = GPU_RELEASED
                record.released_at = now
            return tuple(record.device_id for record in records)

    def active(self, job_id: str | None = None) -> tuple[GpuLease, ...]:
        with self.sessions() as session:
            query = select(GpuLeaseRecord).where(GpuLeaseRecord.state == GPU_LEASED)
            if job_id is not None:
                query = query.where(GpuLeaseRecord.job_id == job_id)
            records = session.scalars(query.order_by(GpuLeaseRecord.device_id)).all()
            return tuple(GpuLease(item.lease_id, item.job_id, item.device_id) for item in records)
