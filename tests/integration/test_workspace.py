from datetime import UTC, datetime, timedelta
import asyncio
import io
from pathlib import Path
import subprocess
import tarfile

import pytest
from sqlalchemy.orm import Session, sessionmaker

from researchd.adapters.a2a import A2AAdapter, EXECUTOR_RESULT_MEDIA_TYPE
from researchd.api.control import LocalControlAPI
from researchd.api.web import ControlResourceRouter
from researchd.artifacts.provenance import ArtifactService
from researchd.artifacts.store import ContentAddressedArtifactStore
from researchd.collaboration.contracts import AgentProfile, AgentRuntime, Delegation
from researchd.collaboration.delegation import DelegationService
from researchd.collaboration.gateway import CollaborationGateway
from researchd.collaboration.heterogeneous import A2ARemoteAgentAdapter
from researchd.collaboration.invocation import InvocationService
from researchd.collaboration.registry import AgentRegistryService
from researchd.collaboration.runtime import AgentAdapterCatalog
from researchd.domain.criteria import ArtifactCriterion
from researchd.domain.enums import (
    AgentAdapterKind,
    AgentTrustZone,
    DataClassification,
    DelegationPurpose,
    VerificationOverall,
)
from researchd.domain.ids import AgentId, AgentRuntimeId, DelegationId
from researchd.storage.db import create_sqlite_engine, session_factory
from researchd.storage.models import (
    AttemptRecord,
    ResearchRunRecord,
    WorkOrderRecord,
    WorkspaceGrantRecord,
    WorkspaceRecord,
    WorkspaceReconciliationRecord,
)
from researchd.verifier.contracts import VerificationInputs
from researchd.verifier.engine import VerifierEngine
from researchd.verifier.producers import TrustedObservationProducers
from researchd.workspace import (
    ArchiveWorkspaceTransport,
    GitWorktreeTransport,
    WorkspaceAccessMode,
    WorkspaceDelegationService,
    WorkspaceGrant,
    WorkspaceLimits,
    WorkspaceTransportKind,
)
from researchd.workspace.manifest import WorkspaceAdmissionError
from test_storage import migrate


def _database(tmp_path: Path) -> tuple[sessionmaker[Session], ArtifactService]:
    database = tmp_path / "workspace.db"
    migrate(database)
    sessions = session_factory(create_sqlite_engine(database))
    artifacts = ArtifactService(ContentAddressedArtifactStore(tmp_path / "artifacts"), sessions)
    now = datetime.now(UTC)
    criterion = ArtifactCriterion(
        criterion_id="workspace-diff",
        type="artifact",
        artifact_type="workspace-git-diff",
    )
    with sessions.begin() as session:
        session.add(WorkspaceRecord(
            workspace_id="ws_workspace",
            name="workspace",
            version=1,
            created_at=now,
            updated_at=now,
        ))
        session.flush()
        session.add(ResearchRunRecord(
            run_id="run_workspace",
            workspace_id="ws_workspace",
            objective="remote workspace",
            state="ACTIVE",
            max_iterations=8,
            max_cloud_calls=8,
            iterations_used=0,
            cloud_calls_used=0,
            cancellation_requested=False,
            version=1,
            created_at=now,
            updated_at=now,
        ))
        session.flush()
        session.add(WorkOrderRecord(
            work_order_id="wo_workspace",
            run_id="run_workspace",
            parent_work_order_id=None,
            objective="change bounded file",
            state="EXECUTING",
            idempotency_key="wo-workspace",
            contract={"acceptance": [criterion.model_dump(mode="json")]},
            revision_reason=None,
            approval_id=None,
            approval_grant_id=None,
            version=1,
            created_at=now,
            updated_at=now,
        ))
    registry = AgentRegistryService(sessions)
    registry.register_profile(AgentProfile(
        agent_id=AgentId("agent_workspace"),
        display_name="Workspace Agent",
        roles=("executor",),
        trust_zone=AgentTrustZone.REMOTE_PRIVATE,
    ))
    registry.register_runtime(AgentRuntime(
        runtime_id=AgentRuntimeId("runtime_workspace"),
        agent_id=AgentId("agent_workspace"),
        adapter_kind=AgentAdapterKind.A2A,
        runtime_name="Workspace runtime",
    ))
    delegations = DelegationService(sessions)
    delegations.create(Delegation(
        delegation_id=DelegationId("del_workspace"),
        run_id="run_workspace",
        work_order_id="wo_workspace",
        purpose=DelegationPurpose.EXECUTE,
        idempotency_key="del-workspace",
    ))
    delegations.assign(
        "del_workspace", agent_id="agent_workspace", runtime_id="runtime_workspace"
    )
    with sessions.begin() as session:
        session.add(AttemptRecord(
            attempt_id="att_workspace",
            work_order_id="wo_workspace",
            delegation_id="del_workspace",
            state="RUNNING",
            terminal_at=None,
            version=1,
            created_at=now,
            updated_at=now,
        ))
    return sessions, artifacts


def _git_repository(root: Path) -> str:
    (root / "src").mkdir(parents=True)
    (root / "src" / "allowed.txt").write_text("before\n")
    (root / "src" / "private.txt").write_text("not delegated\n")
    subprocess.run(("git", "init", "-q", str(root)), check=True)
    subprocess.run(("git", "-C", str(root), "config", "user.email", "test@example.com"), check=True)
    subprocess.run(("git", "-C", str(root), "config", "user.name", "Test"), check=True)
    subprocess.run(("git", "-C", str(root), "add", "."), check=True)
    subprocess.run(("git", "-C", str(root), "commit", "-qm", "base"), check=True)
    return subprocess.run(
        ("git", "-C", str(root), "rev-parse", "HEAD"),
        check=True,
        text=True,
        stdout=subprocess.PIPE,
    ).stdout.strip()


def test_git_workspace_delegation_reconciles_to_artifact_then_verifier(tmp_path: Path) -> None:
    sessions, artifacts = _database(tmp_path)
    source = tmp_path / "source"
    revision = _git_repository(source)
    service = WorkspaceDelegationService(
        sessions,
        artifacts,
        (GitWorktreeTransport(tmp_path / "worktrees"),),
    )
    service.create(WorkspaceGrant(
        workspace_grant_id="wsg_git",
        delegation_id="del_workspace",
        source_workspace_id="ws_workspace",
        source_revision=revision,
        access_mode=WorkspaceAccessMode.READ_WRITE,
        allowed_paths=("src",),
        excluded_paths=("src/private.txt",),
        classification_ceiling=DataClassification.PROJECT_PRIVATE,
        limits=WorkspaceLimits(
            max_total_bytes=10_000,
            max_file_count=10,
            max_single_file_bytes=5_000,
        ),
        transport_kind=WorkspaceTransportKind.GIT_WORKTREE,
    ))
    provisioned = service.provision("wsg_git", source)
    remote = Path(provisioned.remote_workspace_handle)
    assert (remote / "src" / "allowed.txt").read_text() == "before\n"
    assert not (remote / "src" / "private.txt").exists()

    class WorkspaceA2AClient:
        async def send(self, payload: dict[str, object]) -> dict[str, object]:
            message = payload["message"]
            assert isinstance(message, dict)
            parts = message["parts"]
            assert isinstance(parts, list) and isinstance(parts[0], dict)
            work_order = parts[0]["data"]
            assert isinstance(work_order, dict)
            binding = work_order["workspace_grant"]
            assert isinstance(binding, dict) and binding["workspace_grant_id"] == "wsg_git"
            delegated_root = Path(str(binding["remote_workspace_handle"]))
            (delegated_root / "src" / "allowed.txt").write_text("after\n")
            return {
                "id": "task_workspace",
                "contextId": message["contextId"],
                "status": {"state": "TASK_STATE_COMPLETED"},
                "artifacts": [{
                    "artifactId": "executor_result",
                    "parts": [{
                        "data": {
                            "attempt_id": "att_workspace",
                            "status": "execution_complete",
                            "capability_results": [],
                            "reported_claims": ["workspace changed"],
                            "errors": [],
                        },
                        "mediaType": EXECUTOR_RESULT_MEDIA_TYPE,
                    }],
                }],
                "history": [message],
            }

        async def cancel(self, *, task_id: str, tenant: str | None = None) -> dict[str, object]:
            del tenant
            return {
                "id": task_id,
                "contextId": "ctx_run_workspace",
                "status": {"state": "TASK_STATE_CANCELED"},
            }

    catalog = AgentAdapterCatalog(sessions)
    catalog.register(
        AgentAdapterKind.A2A,
        A2ARemoteAgentAdapter(A2AAdapter(
            sessions,
            WorkspaceA2AClient(),
            remote_agent_id="agent.workspace",
        )),
    )
    gateway = CollaborationGateway(
        delegations=DelegationService(sessions),
        invocations=InvocationService(sessions),
        catalog=catalog,
    )
    with sessions() as session:
        order = session.get(WorkOrderRecord, "wo_workspace")
        attempt = session.get(AttemptRecord, "att_workspace")
        assert order is not None and attempt is not None
    execution = asyncio.run(gateway.execute(order, attempt))
    assert execution.status == "execution_complete"
    artifact_id = service.reconcile("wsg_git")
    assert (source / "src" / "allowed.txt").read_text() == "before\n"
    patch = artifacts.store.read(artifact_id)
    assert b"-before" in patch and b"+after" in patch

    criterion = ArtifactCriterion(
        criterion_id="workspace-diff",
        type="artifact",
        artifact_type="workspace-git-diff",
    )
    verification = VerifierEngine(
        sessions,
        TrustedObservationProducers(artifacts.store),
    ).verify(
        work_order_id="wo_workspace",
        attempt_id="att_workspace",
        criteria=(criterion,),
        inputs=VerificationInputs(),
    )
    assert verification.overall is VerificationOverall.PASS
    service.cleanup("wsg_git")
    assert not remote.exists()
    with sessions() as session:
        grant = session.get(WorkspaceGrantRecord, "wsg_git")
        reconciliation = session.query(WorkspaceReconciliationRecord).one()
        assert grant is not None and grant.state == "COMPLETED" and grant.cleanup_state == "CLEANED"
        assert reconciliation.result_artifact_id == artifact_id
    control = LocalControlAPI(sessions)
    projected = control.workspace_grants("run_workspace")
    assert projected[0]["result_artifact_id"] == artifact_id
    assert "workspace_grant" in {item["kind"] for item in control.timeline("run_workspace")}
    status, resource = ControlResourceRouter(control).get("/api/workspace-grants?run=run_workspace")
    assert status == 200 and isinstance(resource, list) and resource[0]["workspace_grant_id"] == "wsg_git"


def test_archive_transport_is_bounded_and_rejects_returned_path_traversal(tmp_path: Path) -> None:
    sessions, artifacts = _database(tmp_path)
    source = tmp_path / "archive-source"
    source.mkdir()
    (source / "input.txt").write_text("input\n")
    transport = ArchiveWorkspaceTransport(tmp_path / "archives")
    service = WorkspaceDelegationService(sessions, artifacts, (transport,))
    grant = WorkspaceGrant(
        workspace_grant_id="wsg_archive",
        delegation_id="del_workspace",
        source_workspace_id="ws_workspace",
        access_mode=WorkspaceAccessMode.READ_WRITE,
        allowed_paths=("input.txt",),
        classification_ceiling=DataClassification.PUBLIC,
        limits=WorkspaceLimits(
            max_total_bytes=10_000,
            max_file_count=10,
            max_single_file_bytes=5_000,
        ),
        transport_kind=WorkspaceTransportKind.ARCHIVE,
    )
    service.create(grant)
    provisioned = service.provision("wsg_archive", source)
    with tarfile.open(provisioned.remote_workspace_handle, mode="r:") as archive:
        assert archive.getnames() == ["input.txt"]

    returned = tmp_path / "returned.tar"
    with tarfile.open(returned, mode="w") as archive:
        data = b"changed\n"
        info = tarfile.TarInfo("input.txt")
        info.size = len(data)
        archive.addfile(info, io.BytesIO(data))
    artifact_id = service.reconcile("wsg_archive", remote_result=returned)
    assert artifacts.store.read(artifact_id) == returned.read_bytes()

    malicious = io.BytesIO()
    with tarfile.open(fileobj=malicious, mode="w") as archive:
        data = b"escape"
        info = tarfile.TarInfo("../escape.txt")
        info.size = len(data)
        archive.addfile(info, io.BytesIO(data))
    malicious_path = tmp_path / "malicious.tar"
    malicious_path.write_bytes(malicious.getvalue())
    snapshot = transport.snapshot(grant, source)
    with pytest.raises(WorkspaceAdmissionError, match="traversal"):
        transport.reconcile(grant, provisioned, remote_result=malicious_path)


def test_workspace_grant_rejects_trust_overreach_and_expires_fail_closed(tmp_path: Path) -> None:
    sessions, artifacts = _database(tmp_path)
    service = WorkspaceDelegationService(
        sessions,
        artifacts,
        (ArchiveWorkspaceTransport(tmp_path / "archives"),),
    )
    with pytest.raises(RuntimeError, match="trust zone"):
        service.create(WorkspaceGrant(
            workspace_grant_id="wsg_private",
            delegation_id="del_workspace",
            source_workspace_id="ws_workspace",
            access_mode=WorkspaceAccessMode.READ_ONLY,
            classification_ceiling=DataClassification.LOCAL_ONLY,
            transport_kind=WorkspaceTransportKind.ARCHIVE,
        ))
    source = tmp_path / "expiry-source"
    source.mkdir()
    (source / "input.txt").write_text("input")
    service.create(WorkspaceGrant(
        workspace_grant_id="wsg_expiry",
        delegation_id="del_workspace",
        source_workspace_id="ws_workspace",
        access_mode=WorkspaceAccessMode.READ_ONLY,
        classification_ceiling=DataClassification.PUBLIC,
        transport_kind=WorkspaceTransportKind.ARCHIVE,
    ))
    service.provision("wsg_expiry", source)
    expired = service.expire_due(now=datetime.now(UTC) + timedelta(days=2))
    assert expired == ("wsg_expiry",)
    with pytest.raises(RuntimeError, match="not active"):
        service.reconcile("wsg_expiry", remote_result=Path("unused"))
