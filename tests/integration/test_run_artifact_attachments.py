"""Developer Preview trusted file ingress and Agent context coverage."""

from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from researchd.api.control import LocalControlAPI
from researchd.artifacts.attachments import RunArtifactAttachmentService
from researchd.artifacts.store import ContentAddressedArtifactStore
from researchd.context.agent_context import AgentContextBuilder, AgentContextSelection
from researchd.context.builder import ContextBuilder
from researchd.context.redaction import DeterministicRedactor
from researchd.domain.enums import (
    AgentTrustZone,
    DataClassification,
    DelegationPurpose,
)
from researchd.storage.db import create_sqlite_engine, session_factory
from researchd.storage.models import (
    AgentRecord,
    ArtifactRecord,
    AuditEventRecord,
    ResearchRunRecord,
    RunArtifactAttachmentRecord,
)
from tests.integration.test_storage import migrate


def _fixture(
    tmp_path: Path,
) -> tuple[RunArtifactAttachmentService, sessionmaker[Session], str]:
    database = tmp_path / "attachments.db"
    migrate(database)
    sessions = session_factory(create_sqlite_engine(database))
    api = LocalControlAPI(sessions)
    api.create_workspace("workspace_local", "Local")
    run_id = "run_attachment"
    now = datetime.now(UTC)
    with sessions.begin() as session:
        session.add(ResearchRunRecord(
            run_id=run_id,
            workspace_id="workspace_local",
            objective="inspect the attachment",
            state="NEW",
            max_iterations=8,
            max_agent_turns=24,
            iterations_used=0,
            agent_turns_used=0,
            cancellation_requested=False,
            version=1,
            created_at=now,
            updated_at=now,
        ))
        for agent_id in ("agent_planner", "agent_coder"):
            session.add(AgentRecord(
                agent_id=agent_id,
                display_name=agent_id,
                roles_json=["planner"],
                skills_json=[],
                trust_zone=AgentTrustZone.LOCAL_PRIVATE.value,
                constraints_json=[],
                labels_json={},
                max_parallel_delegations=1,
                enabled=True,
                profile_version=1,
                version=1,
                created_at=now,
                updated_at=now,
            ))
    return (
        RunArtifactAttachmentService(
            ContentAddressedArtifactStore(tmp_path / "cas"), sessions,
        ),
        sessions,
        run_id,
    )


def test_attachment_is_idempotent_audited_and_never_stores_host_path(
    tmp_path: Path,
) -> None:
    service, sessions, run_id = _fixture(tmp_path)
    first = service.attach(
        b"preview attachment",
        command_id="cmd_attach_one",
        run_id=run_id,
        source_name="notes.md",
        mime_type="text/markdown",
        classification=DataClassification.PROJECT_PRIVATE,
        actor_type="HUMAN",
        actor_id="local-control-client",
        recipient_agent_id="agent_planner",
    )
    replay = service.attach(
        b"preview attachment",
        command_id="cmd_attach_one",
        run_id=run_id,
        source_name="notes.md",
        mime_type="text/markdown",
        classification=DataClassification.PROJECT_PRIVATE,
        actor_type="HUMAN",
        actor_id="local-control-client",
        recipient_agent_id="agent_planner",
    )

    assert replay == first
    with sessions() as session:
        assert session.scalar(select(func.count()).select_from(ArtifactRecord)) == 1
        assert session.scalar(
            select(func.count()).select_from(RunArtifactAttachmentRecord)
        ) == 1
        event = session.scalar(select(AuditEventRecord).where(
            AuditEventRecord.entity_id == first.attachment_id,
        ))
        assert event is not None
        serialized = str(event.metadata_json)
        assert str(tmp_path) not in serialized
        assert "notes.md" not in serialized


def test_attachment_command_replay_with_different_bytes_fails_closed(
    tmp_path: Path,
) -> None:
    service, _, run_id = _fixture(tmp_path)
    service.attach(
        b"first",
        command_id="cmd_attach_replay",
        run_id=run_id,
        source_name="notes.md",
        mime_type="text/markdown",
        classification=DataClassification.LOCAL_ONLY,
        actor_type="HUMAN",
        actor_id="local-control-client",
    )

    with pytest.raises(ValueError, match="different payload"):
        service.attach(
            b"second",
            command_id="cmd_attach_replay",
            run_id=run_id,
            source_name="notes.md",
            mime_type="text/markdown",
            classification=DataClassification.LOCAL_ONLY,
            actor_type="HUMAN",
            actor_id="local-control-client",
        )


def test_recipient_scoped_attachment_enters_only_that_agents_context(
    tmp_path: Path,
) -> None:
    service, sessions, run_id = _fixture(tmp_path)
    attached = service.attach(
        b"planner-only context",
        command_id="cmd_attach_scope",
        run_id=run_id,
        source_name="brief.txt",
        mime_type="text/plain",
        classification=DataClassification.LOCAL_ONLY,
        actor_type="HUMAN",
        actor_id="local-control-client",
        recipient_agent_id="agent_planner",
    )
    store = service.store
    builder = AgentContextBuilder(ContextBuilder(
        sessions, store, DeterministicRedactor(),
    ))

    planner = builder.build(AgentContextSelection(
        target_agent_id="agent_planner",
        target_runtime_id="runtime_planner",
        target_trust_zone=AgentTrustZone.LOCAL_PRIVATE,
        purpose=DelegationPurpose.PLAN,
        run_id=run_id,
    ))
    coder = builder.build(AgentContextSelection(
        target_agent_id="agent_coder",
        target_runtime_id="runtime_coder",
        target_trust_zone=AgentTrustZone.LOCAL_PRIVATE,
        purpose=DelegationPurpose.EXECUTE,
        run_id=run_id,
    ))

    assert [item.artifact_id for item in planner.selected_context.selected_artifacts] == [
        attached.artifact_id,
    ]
    assert planner.selected_context.selected_artifacts[0].content == "planner-only context"
    assert coder.selected_context.selected_artifacts == ()
