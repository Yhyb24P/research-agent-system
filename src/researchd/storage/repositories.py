from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Protocol, TypeVar

from sqlalchemy import select
from sqlalchemy.orm import Session

from researchd.domain.enums import AttemptState, JobState, ResearchRunState, WorkOrderState
from researchd.storage.models import (
    ArtifactRecord,
    AttemptRecord,
    AuditEventRecord,
    JobRecord,
    ResearchRunRecord,
    WorkspaceRecord,
    WorkOrderRecord,
)

RecordT = TypeVar("RecordT")


class Repository(Protocol[RecordT]):
    def get(self, entity_id: str) -> RecordT | None: ...
    def add(self, record: RecordT) -> None: ...


class WorkspaceRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def get(self, entity_id: str) -> WorkspaceRecord | None:
        return self.session.get(WorkspaceRecord, entity_id)

    def add(self, record: WorkspaceRecord) -> None:
        self.session.add(record)


class ResearchRunRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def get(self, entity_id: str) -> ResearchRunRecord | None:
        return self.session.get(ResearchRunRecord, entity_id)

    def add(self, record: ResearchRunRecord) -> None:
        self.session.add(record)

    def active(self) -> Sequence[ResearchRunRecord]:
        terminal = tuple(state.value for state in (ResearchRunState.COMPLETED, ResearchRunState.FAILED, ResearchRunState.CANCELLED))
        return self.session.scalars(select(ResearchRunRecord).where(ResearchRunRecord.state.not_in(terminal))).all()


class WorkOrderRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def get(self, entity_id: str) -> WorkOrderRecord | None:
        return self.session.get(WorkOrderRecord, entity_id)

    def add(self, record: WorkOrderRecord) -> None:
        self.session.add(record)

    def active(self) -> Sequence[WorkOrderRecord]:
        terminal = tuple(state.value for state in (WorkOrderState.ACCEPTED, WorkOrderState.REVISION_REQUIRED, WorkOrderState.FAILED, WorkOrderState.CANCELLED))
        query = select(WorkOrderRecord).where(WorkOrderRecord.state.not_in(terminal)).order_by(WorkOrderRecord.created_at)
        return self.session.scalars(query).all()


class AttemptRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def get(self, entity_id: str) -> AttemptRecord | None:
        return self.session.get(AttemptRecord, entity_id)

    def add(self, record: AttemptRecord) -> None:
        self.session.add(record)

    def active(self) -> Sequence[AttemptRecord]:
        terminal = tuple(state.value for state in (AttemptState.SUCCEEDED, AttemptState.FAILED, AttemptState.CANCELLED))
        query = select(AttemptRecord).where(AttemptRecord.state.not_in(terminal)).order_by(AttemptRecord.created_at)
        return self.session.scalars(query).all()


class JobRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def get(self, entity_id: str) -> JobRecord | None:
        return self.session.get(JobRecord, entity_id)

    def add(self, record: JobRecord) -> None:
        self.session.add(record)

    def get_by_operation_id(self, operation_id: str) -> JobRecord | None:
        return self.session.scalar(select(JobRecord).where(JobRecord.operation_id == operation_id))

    def active(self) -> Sequence[JobRecord]:
        terminal = tuple(state.value for state in (JobState.SUCCEEDED, JobState.FAILED, JobState.CANCELLED, JobState.LOST))
        query = select(JobRecord).where(JobRecord.state.not_in(terminal)).order_by(JobRecord.created_at)
        return self.session.scalars(query).all()


class ArtifactRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def get(self, entity_id: str) -> ArtifactRecord | None:
        return self.session.get(ArtifactRecord, entity_id)

    def add(self, record: ArtifactRecord) -> None:
        self.session.add(record)


class EventRepository:
    """Append/query-only event repository; mutation methods intentionally absent."""

    def __init__(self, session: Session) -> None:
        self.session = session

    def append(self, event: AuditEventRecord) -> None:
        self.session.add(event)

    def for_run(self, run_id: str) -> Sequence[AuditEventRecord]:
        query = select(AuditEventRecord).where(AuditEventRecord.run_id == run_id).order_by(AuditEventRecord.timestamp, AuditEventRecord.event_id)
        return self.session.scalars(query).all()


def utc_now() -> datetime:
    return datetime.now(UTC)
