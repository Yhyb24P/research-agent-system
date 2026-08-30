"""Authoritative workspace record creation; the only write path to workspaces."""

from datetime import UTC, datetime

from sqlalchemy.orm import Session, sessionmaker

from researchd.storage.models import WorkspaceRecord


class WorkspaceError(RuntimeError):
    pass


class WorkspaceCreationService:
    """Create workspace records; existing identities are never overwritten."""

    def __init__(self, sessions: sessionmaker[Session]) -> None:
        self.sessions = sessions

    def create(self, workspace_id: str, name: str) -> WorkspaceRecord:
        now = datetime.now(UTC)
        with self.sessions.begin() as session:
            if session.get(WorkspaceRecord, workspace_id) is not None:
                raise WorkspaceError(f"workspace already exists: {workspace_id}")
            record = WorkspaceRecord(
                workspace_id=workspace_id,
                name=name,
                version=1,
                created_at=now,
                updated_at=now,
            )
            session.add(record)
        return record


__all__ = ["WorkspaceCreationService", "WorkspaceError"]
