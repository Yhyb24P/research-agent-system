"""Bounded local file ingress and durable Run/Artifact association."""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from pathlib import PurePath

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from researchd.artifacts.provenance import ArtifactMetadataConflict
from researchd.artifacts.store import ContentAddressedArtifactStore
from researchd.domain.base import DomainModel
from researchd.domain.enums import DataClassification
from researchd.storage.models import (
    AgentRecord,
    ArtifactRecord,
    AuditEventRecord,
    CollaborationMessageRecord,
    ResearchRunRecord,
    RunArtifactAttachmentRecord,
)


MAX_ATTACHMENT_BYTES = 4 * 1024 * 1024


class RunArtifactAttachment(DomainModel):
    attachment_id: str
    run_id: str
    artifact_id: str
    sha256: str
    size: int
    mime_type: str
    classification: DataClassification
    source_name: str
    message_id: str | None = None
    recipient_agent_id: str | None = None
    created_at: datetime


class RunArtifactAttachmentService:
    """Admit bytes once, then expose only canonical IDs to downstream Agents."""

    def __init__(
        self,
        store: ContentAddressedArtifactStore,
        sessions: sessionmaker[Session],
    ) -> None:
        self.store = store
        self.sessions = sessions

    def attach(
        self,
        data: bytes,
        *,
        command_id: str,
        run_id: str,
        source_name: str,
        mime_type: str,
        classification: DataClassification,
        actor_type: str,
        actor_id: str,
        message_id: str | None = None,
        recipient_agent_id: str | None = None,
    ) -> RunArtifactAttachment:
        name = self._source_name(source_name)
        if len(data) > MAX_ATTACHMENT_BYTES:
            raise ValueError("attachment exceeds the 4 MiB Developer Preview limit")
        if not mime_type or len(mime_type) > 256 or "\x00" in mime_type:
            raise ValueError("attachment MIME type is invalid")
        digest = hashlib.sha256(data).hexdigest()
        artifact_id = f"artifact://sha256/{digest}"
        now = datetime.now(UTC)
        attachment_id = f"attach_{hashlib.sha256(command_id.encode()).hexdigest()[:40]}"

        with self.sessions() as session:
            existing = session.scalar(select(RunArtifactAttachmentRecord).where(
                RunArtifactAttachmentRecord.command_id == command_id,
            ))
            if existing is not None:
                artifact = session.get(ArtifactRecord, existing.artifact_id)
                if artifact is None:
                    raise RuntimeError("attachment references a missing Artifact")
                self._assert_replay(
                    existing,
                    artifact_id=artifact_id,
                    run_id=run_id,
                    source_name=name,
                    message_id=message_id,
                    recipient_agent_id=recipient_agent_id,
                )
                return self._result(existing, artifact)
            self._validate_scope(
                session,
                run_id=run_id,
                message_id=message_id,
                recipient_agent_id=recipient_agent_id,
            )

        stored_id, stored_digest = self.store.put(data)
        if stored_id != artifact_id or stored_digest != digest:
            raise RuntimeError("content-addressed store returned an inconsistent identity")

        with self.sessions.begin() as session:
            self._validate_scope(
                session,
                run_id=run_id,
                message_id=message_id,
                recipient_agent_id=recipient_agent_id,
            )
            artifact = session.get(ArtifactRecord, artifact_id)
            if artifact is None:
                artifact = ArtifactRecord(
                    artifact_id=artifact_id,
                    sha256=digest,
                    size=len(data),
                    mime_type=mime_type,
                    artifact_type="user_attachment",
                    classification=classification.value,
                    producer_type="human",
                    producer_id=actor_id,
                    attempt_id=None,
                    relative_source_path=name,
                    created_at=now,
                )
                session.add(artifact)
            elif artifact.classification != classification.value:
                raise ArtifactMetadataConflict(
                    "same bytes already registered with a different immutable classification"
                )
            existing = session.scalar(select(RunArtifactAttachmentRecord).where(
                RunArtifactAttachmentRecord.command_id == command_id,
            ))
            if existing is not None:
                self._assert_replay(
                    existing,
                    artifact_id=artifact_id,
                    run_id=run_id,
                    source_name=name,
                    message_id=message_id,
                    recipient_agent_id=recipient_agent_id,
                )
                return self._result(existing, artifact)
            attachment = RunArtifactAttachmentRecord(
                attachment_id=attachment_id,
                command_id=command_id,
                run_id=run_id,
                artifact_id=artifact_id,
                message_id=message_id,
                recipient_agent_id=recipient_agent_id,
                source_name=name,
                actor_type=actor_type,
                actor_id=actor_id,
                created_at=now,
            )
            session.add(attachment)
            session.add(AuditEventRecord(
                event_id=f"evt_{attachment_id}",
                event_type="RUN_ARTIFACT_ATTACHED",
                run_id=run_id,
                entity_type="run_artifact_attachment",
                entity_id=attachment_id,
                actor_type=actor_type,
                actor_id=actor_id,
                timestamp=now,
                correlation_id=run_id,
                causation_id=command_id,
                metadata_json={
                    "artifact_id": artifact_id,
                    "classification": classification.value,
                    "recipient_agent_id": recipient_agent_id,
                    "message_id": message_id,
                },
            ))
            session.flush()
            return self._result(attachment, artifact)

    @staticmethod
    def _source_name(value: str) -> str:
        if (
            not value
            or len(value) > 255
            or "\x00" in value
            or "/" in value
            or "\\" in value
            or PurePath(value).name != value
            or value in {".", ".."}
        ):
            raise ValueError("attachment source name must be a basename")
        return value

    @staticmethod
    def _validate_scope(
        session: Session,
        *,
        run_id: str,
        message_id: str | None,
        recipient_agent_id: str | None,
    ) -> None:
        if session.get(ResearchRunRecord, run_id) is None:
            raise LookupError(run_id)
        if message_id is not None:
            message = session.get(CollaborationMessageRecord, message_id)
            if message is None or message.run_id != run_id:
                raise ValueError("attachment message is outside the requested Run")
        if recipient_agent_id is not None:
            agent = session.get(AgentRecord, recipient_agent_id)
            if agent is None or not agent.enabled:
                raise ValueError("attachment recipient is not an enabled Agent")

    @staticmethod
    def _assert_replay(
        existing: RunArtifactAttachmentRecord,
        *,
        artifact_id: str,
        run_id: str,
        source_name: str,
        message_id: str | None,
        recipient_agent_id: str | None,
    ) -> None:
        if (
            existing.artifact_id != artifact_id
            or existing.run_id != run_id
            or existing.source_name != source_name
            or existing.message_id != message_id
            or existing.recipient_agent_id != recipient_agent_id
        ):
            raise ValueError("attachment command ID was replayed with a different payload")

    @staticmethod
    def _result(
        attachment: RunArtifactAttachmentRecord,
        artifact: ArtifactRecord,
    ) -> RunArtifactAttachment:
        return RunArtifactAttachment(
            attachment_id=attachment.attachment_id,
            run_id=attachment.run_id,
            artifact_id=artifact.artifact_id,
            sha256=artifact.sha256,
            size=artifact.size,
            mime_type=artifact.mime_type,
            classification=DataClassification(artifact.classification),
            source_name=attachment.source_name,
            message_id=attachment.message_id,
            recipient_agent_id=attachment.recipient_agent_id,
            created_at=attachment.created_at,
        )


__all__ = [
    "MAX_ATTACHMENT_BYTES",
    "RunArtifactAttachment",
    "RunArtifactAttachmentService",
]
