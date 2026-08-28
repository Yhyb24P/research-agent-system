import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
import httpx
from sqlalchemy import select

from researchd.backup import BackupError, backup_snapshot, restore_snapshot
from researchd.artifacts.provenance import ArtifactService
from researchd.artifacts.store import ContentAddressedArtifactStore
from researchd.context.builder import ContextBuilder
from researchd.context.redaction import DeterministicRedactor
from researchd.domain.criteria import acceptance_fingerprint
from researchd.domain.enums import Capability, DataClassification, VerificationOverall
from researchd.domain.ids import VerificationId
from researchd.domain.verification import VerificationResult
from researchd.executor.capability_broker import CapabilityBroker
from researchd.executor.contracts import ExecutorResult, GrantedWorkOrder, CommandLimits
from researchd.executor.sandbox import BubblewrapBackend
from researchd.executor.worker import LocalExecutorWorker
from researchd.executor.worktree import WorktreeManager
from researchd.models.cloud import CloudCallBudget, CloudPricing
from researchd.models.base import LocalModelUnavailable
from researchd.models.vllm import VLLMLocalModel
from researchd.executor.contracts import LocalAgentRequest
from researchd.orchestrator.engine import ResearchOrchestrator
from researchd.policy.engine import BudgetLimits, DeterministicPolicyEngine, RecordingPolicyEngine
from researchd.storage.models import AttemptRecord, AuditEventRecord, VerificationResultRecord, WorkOrderRecord
from researchd.observability import collect_metrics
from researchd.storage.models import ResearchRunRecord
from researchd.testing.faults import FaultInjector, InjectedFault
from test_orchestrator import _proposal, _review, make_orchestrator
from test_executor import FixingFakeLocalModel, fixture_repository, limits, sandbox_for


def test_metrics_snapshot_covers_cloud_and_workflow_records(tmp_path: Path) -> None:
    sessions, orchestrator, _, _ = make_orchestrator(tmp_path, cloud_responses=[_proposal(), _review()])
    run_id = orchestrator.create_run(workspace_id="ws_e2e", objective="metrics")
    asyncio.run(orchestrator.run(run_id, max_steps=30))
    metrics = collect_metrics(sessions, run_id=run_id)
    payload = metrics.as_dict()
    assert payload["cloud_calls"] == 2
    assert payload["cloud_statuses"] == {"COMPLETED": 2}
    assert payload["verifier_outcomes"] == {"pass": 1}
    assert "research_cloud_calls_total 2" in metrics.prometheus()


def test_sqlite_and_artifact_backup_restore_validates_checksums(tmp_path: Path) -> None:
    sessions, orchestrator, _, _ = make_orchestrator(tmp_path, cloud_responses=[_proposal(), _review()])
    run_id = orchestrator.create_run(workspace_id="ws_e2e", objective="backup")
    asyncio.run(orchestrator.run(run_id, max_steps=30))
    orphan = tmp_path / "artifacts" / "sha256" / "ff" / ("f" * 64)
    orphan.parent.mkdir(parents=True)
    orphan.write_bytes(b"unreferenced CAS residue")
    backup_dir = tmp_path / "backup"
    manifest = backup_snapshot(tmp_path / "orchestrator.db", tmp_path / "artifacts", backup_dir)
    assert "sha256/ff/" + "f" * 64 not in manifest.artifact_files
    restored_db = tmp_path / "restored.db"
    restored_artifacts = tmp_path / "restored-artifacts"
    assert restore_snapshot(backup_dir, restored_db, restored_artifacts) == manifest
    with pytest.raises(BackupError, match="already exist"):
        restore_snapshot(backup_dir, restored_db, tmp_path / "other-artifacts")
    assert restored_db.is_file() and restored_artifacts.is_dir()
    # The restored DB remains readable without reconstructing state from model output.
    import sqlite3
    with sqlite3.connect(restored_db) as connection:
        assert connection.execute("SELECT state FROM research_runs WHERE run_id = ?", (run_id,)).fetchone() == ("COMPLETED",)


def test_fault_injector_is_one_shot_and_auditable() -> None:
    injector = FaultInjector({"after_job_submit": InjectedFault("disk full")})
    with pytest.raises(InjectedFault, match="disk full"):
        injector.hit("after_job_submit")
    injector.hit("after_job_submit")
    assert injector.hits["after_job_submit"] == 2


class PilotExecutor:
    def __init__(self, sessions: Any, repository: Path, root: Path) -> None:
        self.sessions = sessions
        self.repository = repository
        self.worktrees = WorktreeManager(root, sessions)
        self.artifacts = ArtifactService(ContentAddressedArtifactStore(root.parent / "pilot-artifacts"), sessions)
        self.worker = LocalExecutorWorker(
            FixingFakeLocalModel(),
            CapabilityBroker(BubblewrapBackend(), self.artifacts, sessions, command_limits=limits()),
            sessions,
        )

    async def execute(self, work_order: Any, attempt: Any) -> ExecutorResult:
        handle = self.worktrees.create(self.repository, repository_id="pilot-repository", attempt_id=attempt.attempt_id)
        return await self.worker.execute(GrantedWorkOrder(
            attempt_id=attempt.attempt_id, objective=work_order.objective,
            granted_capabilities=frozenset({Capability.WORKSPACE_WRITE, Capability.TEST_RUN}),
            sandbox=sandbox_for(handle.path, attempt.attempt_id),
        ))

    async def cancel(self, attempt_id: str) -> None:
        del attempt_id


class PilotVerifier:
    def __init__(self, sessions: Any) -> None:
        self.sessions = sessions

    def verify(self, work_order: Any, attempt: Any, result: ExecutorResult) -> VerificationResult:
        passed = any(item.status == "ok" and item.exit_code == 0 for item in result.capability_results)
        overall = VerificationOverall.PASS if passed else VerificationOverall.FAIL
        fingerprint = acceptance_fingerprint(work_order.contract.get("acceptance", []))
        identifier = VerificationId(f"ver_{attempt.attempt_id}")
        now = datetime.now(UTC)
        with self.sessions.begin() as session:
            session.add(VerificationResultRecord(
                verification_id=str(identifier), attempt_id=attempt.attempt_id, work_order_id=work_order.work_order_id,
                overall=overall.value, criteria_json=[], acceptance_sha256=fingerprint,
                verifier_version="pilot-verifier-v1", valid=True,
                classification=DataClassification.PUBLIC.value, created_at=now,
            ))
        return VerificationResult(
            verification_id=identifier, attempt_id=attempt.attempt_id, overall=overall, criteria=(),
            acceptance_sha256=fingerprint, verifier_version="pilot-verifier-v1", valid=True,
            classification=DataClassification.PUBLIC,
        )


def test_bounded_real_repository_pilot_produces_accepted_trace(tmp_path: Path) -> None:
    sessions, orchestrator, _, cloud_model = make_orchestrator(tmp_path, cloud_responses=[])
    repository = fixture_repository(tmp_path / "pilot-repo")
    pilot_proposal = _proposal().replace('"requested_capabilities": []', '"requested_capabilities": ["workspace.write", "test.run"]')
    cloud_model.responses[:] = [pilot_proposal, _review()]
    orchestrator.workspace_capabilities = frozenset({Capability.WORKSPACE_WRITE, Capability.TEST_RUN})
    orchestrator.user_capabilities = orchestrator.workspace_capabilities
    orchestrator.executor = PilotExecutor(sessions, repository, tmp_path / "worktrees")
    orchestrator.verifier = PilotVerifier(sessions)
    run_id = orchestrator.create_run(workspace_id="ws_e2e", objective="fix the bounded repository test")
    snapshot = asyncio.run(orchestrator.run(run_id, max_steps=40))
    assert snapshot.state.value == "COMPLETED"
    with sessions() as session:
        event_types = {item.event_type for item in session.scalars(select(AuditEventRecord).where(AuditEventRecord.run_id == run_id)).all()}
        assert {"WORK_ORDER_DISPATCHED", "VERIFICATION_COMPLETED", "REVIEW_DECISION_RECORDED", "WORK_ORDER_ACCEPTED", "RUN_COMPLETED"} <= event_types
        assert session.query(AttemptRecord).count() == 1


def test_vllm_timeout_injection_is_explicit_local_failure() -> None:
    def timeout(_: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("injected vLLM timeout")
    client = httpx.AsyncClient(transport=httpx.MockTransport(timeout), trust_env=False)
    model = VLLMLocalModel(base_url="http://127.0.0.1:8000", model="pilot", client=client)
    request = LocalAgentRequest(objective="bounded", prior_results=(), granted_capabilities=frozenset())
    with pytest.raises(LocalModelUnavailable, match="ReadTimeout"):
        asyncio.run(model.complete(request))
    asyncio.run(client.aclose())
