"""Executable DQ04 corruption, identity, and path-boundary matrix."""

import asyncio
import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import threading
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from researchd.artifacts.provenance import ArtifactService
from researchd.artifacts.store import ContentAddressedArtifactStore
from researchd.agents.cloud_lead import CloudLeadAdapter
from researchd.backup import (
    BackupError,
    apply_snapshot_rotation,
    backup_snapshot,
    check_restored_snapshot,
    plan_snapshot_rotation,
    restore_snapshot,
)
from researchd.collaboration.invocation import InvocationService
from researchd.context.builder import ContextBuilder
from researchd.context.redaction import DeterministicRedactor
from researchd.domain.enums import DataClassification
from researchd.models.cloud import CloudCallBudget, CloudPricing
from researchd.orchestrator.engine import OrchestrationLimits, ResearchOrchestrator
from researchd.policy.engine import BudgetLimits, DeterministicPolicyEngine, RecordingPolicyEngine
from researchd.storage.db import create_sqlite_engine, session_factory
from researchd.storage.models import AgentInvocationRecord
from tests.integration.test_orchestrator import (
    FakeCloud,
    FakeExecutor,
    FakeVerifier,
    _proposal,
    _review,
    cloud_configuration,
    collaboration_gateway,
    make_orchestrator,
)

CANDIDATE_COMMIT = "0" * 40
CANDIDATE_TAG = "v0.0.0-rc.dq04-test"


def _backup(database: Path, artifacts: Path, snapshot: Path) -> Any:
    return backup_snapshot(
        database,
        artifacts,
        snapshot,
        candidate_commit=CANDIDATE_COMMIT,
        candidate_tag=CANDIDATE_TAG,
    )


def _restore(snapshot: Path, database: Path, artifacts: Path) -> Any:
    return restore_snapshot(
        snapshot,
        database,
        artifacts,
        expected_candidate_commit=CANDIDATE_COMMIT,
        expected_candidate_tag=CANDIDATE_TAG,
    )


def _populated_state(tmp_path: Path) -> tuple[Any, Path, Path, str]:
    sessions, orchestrator, _, _ = make_orchestrator(
        tmp_path, cloud_responses=[_proposal(), _review()]
    )
    run_id = orchestrator.create_run(workspace_id="ws_e2e", objective="DQ04 matrix")
    asyncio.run(orchestrator.run(run_id, max_steps=30))
    artifact = ArtifactService(
        ContentAddressedArtifactStore(tmp_path / "artifacts"), sessions
    ).register(
        b"DQ04 referenced payload",
        mime_type="text/plain",
        artifact_type="fixture",
        classification=DataClassification.PUBLIC,
        producer_type="test",
        producer_id="dq04",
    )
    return sessions, tmp_path / "orchestrator.db", tmp_path / "artifacts", artifact.sha256


def _rewrite_manifest(snapshot: Path, **changes: object) -> None:
    path = snapshot / "manifest.json"
    raw = json.loads(path.read_text(encoding="utf-8"))
    raw.update(changes)
    path.write_text(json.dumps(raw, sort_keys=True, indent=2) + "\n", encoding="utf-8")


def _orchestrator_for_restored_state(
    database: Path, artifacts: Path
) -> tuple[Any, ResearchOrchestrator]:
    sessions = session_factory(create_sqlite_engine(database))
    continued_proposal = _proposal().replace(
        "plan_nan_001", "plan_after_restore_001"
    ).replace("fix_nan_001", "fix_after_restore_001")
    model = FakeCloud([continued_proposal, _review()])
    builder = ContextBuilder(
        sessions,
        ContentAddressedArtifactStore(artifacts),
        DeterministicRedactor(),
    )
    cloud = CloudLeadAdapter(
        model,
        sessions,
        builder,
        configuration=cloud_configuration(),
        budget=CloudCallBudget(
            max_requests=3,
            max_input_bytes=100_000,
            max_response_bytes=100_000,
            max_output_tokens=512,
            max_total_tokens=2_000,
        ),
        pricing=CloudPricing(
            prompt_usd_per_million=Decimal("0"),
            completion_usd_per_million=Decimal("0"),
        ),
    )
    executor = FakeExecutor()
    orchestrator = ResearchOrchestrator(
        sessions,
        collaboration=collaboration_gateway(sessions, cloud, executor),
        policy=RecordingPolicyEngine(DeterministicPolicyEngine(), sessions),
        verifier=FakeVerifier(sessions),
        workspace_capabilities=frozenset(),
        user_capabilities=frozenset(),
        maximum_budget=BudgetLimits(100, 100, 0, 100, 100),
        limits=OrchestrationLimits(max_iterations=8, max_agent_turns=8),
    )
    return sessions, orchestrator


def test_dq04_04_corrupt_referenced_cas_fails_backup_without_partial_snapshot(
    tmp_path: Path,
) -> None:
    _, database, artifacts, digest = _populated_state(tmp_path)
    (artifacts / "sha256" / digest[:2] / digest).write_bytes(b"tampered")
    snapshot = tmp_path / "snapshot"
    with pytest.raises(BackupError, match="content does not match"):
        _backup(database, artifacts, snapshot)
    assert not snapshot.exists()


def test_dq04_02_online_backup_is_consistent_during_authoritative_writes(
    tmp_path: Path,
) -> None:
    _, database, artifacts, _ = _populated_state(tmp_path)
    started = threading.Event()
    stop = threading.Event()
    failures: list[BaseException] = []

    def writer() -> None:
        index = 0
        try:
            with sqlite3.connect(database) as lookup:
                run_id = str(lookup.execute("SELECT run_id FROM research_runs LIMIT 1").fetchone()[0])
            while not stop.is_set():
                payload = f"concurrent-{index}".encode()
                digest = hashlib.sha256(payload).hexdigest()
                artifact_path = artifacts / "sha256" / digest[:2] / digest
                artifact_path.parent.mkdir(parents=True, exist_ok=True)
                artifact_path.write_bytes(payload)
                now = "2026-08-30T00:00:00+00:00"
                order_id = f"wo_concurrent_{index}"
                attempt_id = f"att_concurrent_{index}"
                with sqlite3.connect(database, timeout=10) as connection:
                    connection.execute("PRAGMA foreign_keys=ON")
                    connection.execute("PRAGMA journal_mode=WAL")
                    connection.execute(
                        "INSERT INTO work_orders "
                        "(work_order_id, run_id, parent_work_order_id, objective, state, "
                        "idempotency_key, contract, revision_reason, approval_id, "
                        "approval_grant_id, version, created_at, updated_at) "
                        "VALUES (?, ?, NULL, 'concurrent backup write', "
                        "'CREATED', ?, '{}', NULL, NULL, NULL, 1, ?, ?)",
                        (order_id, run_id, f"concurrent-{index}", now, now),
                    )
                    connection.execute(
                        "INSERT INTO attempts "
                        "(attempt_id, work_order_id, delegation_id, state, terminal_at, "
                        "version, created_at, updated_at) "
                        "VALUES (?, ?, NULL, 'CREATED', NULL, 1, ?, ?)",
                        (attempt_id, order_id, now, now),
                    )
                    connection.execute(
                        "INSERT INTO artifacts "
                        "(artifact_id, sha256, size, mime_type, artifact_type, "
                        "classification, producer_type, producer_id, attempt_id, "
                        "relative_source_path, created_at) "
                        "VALUES (?, ?, ?, 'text/plain', 'fixture', 'PUBLIC', 'test', "
                        "'dq04-writer', ?, NULL, ?)",
                        (f"artifact://sha256/{digest}", digest, len(payload), attempt_id, now),
                    )
                    connection.execute(
                        "INSERT INTO audit_events "
                        "(event_id, event_type, run_id, entity_type, entity_id, actor_type, "
                        "actor_id, timestamp, correlation_id, causation_id, metadata) "
                        "VALUES (?, 'DQ04_CONCURRENT_WRITE', ?, 'attempt', ?, "
                        "'SYSTEM', 'dq04-writer', ?, ?, NULL, '{}')",
                        (f"evt_concurrent_{index}", run_id, attempt_id, now, f"corr_{index}"),
                    )
                    connection.commit()
                index += 1
                started.set()
        except BaseException as error:  # pragma: no cover - surfaced below
            failures.append(error)
            started.set()

    thread = threading.Thread(target=writer, daemon=True)
    thread.start()
    assert started.wait(timeout=10)
    snapshot = tmp_path / "concurrent-snapshot"
    try:
        _backup(database, artifacts, snapshot)
    finally:
        stop.set()
        thread.join(timeout=10)
    assert not thread.is_alive()
    assert not failures

    restored_db = tmp_path / "concurrent-restored.db"
    restored_artifacts = tmp_path / "concurrent-restored-artifacts"
    _restore(snapshot, restored_db, restored_artifacts)
    health = check_restored_snapshot(restored_db, restored_artifacts)
    assert health.healthy
    assert health.table_counts["attempts"] <= health.table_counts["work_orders"]


def test_dq04_04_corrupt_snapshot_cas_fails_restore(tmp_path: Path) -> None:
    _, database, artifacts, digest = _populated_state(tmp_path)
    snapshot = tmp_path / "snapshot"
    _backup(database, artifacts, snapshot)
    (snapshot / "artifacts" / "sha256" / digest[:2] / digest).write_bytes(b"tampered")
    with pytest.raises(BackupError, match="content does not match"):
        _restore(snapshot, tmp_path / "restored.db", tmp_path / "restored-artifacts")


def test_dq04_05_corrupt_database_snapshot_fails_checksum(tmp_path: Path) -> None:
    _, database, artifacts, _ = _populated_state(tmp_path)
    snapshot = tmp_path / "snapshot"
    _backup(database, artifacts, snapshot)
    snapshot_db = snapshot / "research.db"
    payload = bytearray(snapshot_db.read_bytes())
    payload[len(payload) // 2] ^= 0xFF
    snapshot_db.write_bytes(payload)
    with pytest.raises(BackupError, match="database checksum mismatch"):
        _restore(snapshot, tmp_path / "restored.db", tmp_path / "restored-artifacts")


def test_dq04_05_database_corruption_is_rejected_even_if_manifest_hash_is_rewritten(
    tmp_path: Path,
) -> None:
    _, database, artifacts, _ = _populated_state(tmp_path)
    snapshot = tmp_path / "snapshot"
    _backup(database, artifacts, snapshot)
    snapshot_db = snapshot / "research.db"
    payload = bytearray(snapshot_db.read_bytes())
    payload[100:116] = b"DQ04-CORRUPTION!"
    snapshot_db.write_bytes(payload)
    _rewrite_manifest(snapshot, database_sha256=hashlib.sha256(payload).hexdigest())
    with pytest.raises(BackupError):
        _restore(snapshot, tmp_path / "restored.db", tmp_path / "restored-artifacts")


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ({"database_sha256": "f" * 64}, "database checksum mismatch"),
        ({"schema_revision": "9999"}, "schema revision mismatch"),
        ({"candidate_commit": "1" * 40}, "candidate identity mismatch"),
        ({"candidate_tag": "v0.0.0-rc.tampered"}, "candidate identity mismatch"),
    ],
)
def test_dq04_06_manifest_tamper_is_rejected(
    tmp_path: Path, change: dict[str, object], message: str
) -> None:
    _, database, artifacts, _ = _populated_state(tmp_path)
    snapshot = tmp_path / "snapshot"
    _backup(database, artifacts, snapshot)
    _rewrite_manifest(snapshot, **change)
    with pytest.raises(BackupError, match=message):
        _restore(snapshot, tmp_path / "restored.db", tmp_path / "restored-artifacts")


def test_dq04_06_artifact_manifest_path_traversal_is_rejected(tmp_path: Path) -> None:
    _, database, artifacts, digest = _populated_state(tmp_path)
    snapshot = tmp_path / "snapshot"
    _backup(database, artifacts, snapshot)
    manifest_path = snapshot / "manifest.json"
    raw = json.loads(manifest_path.read_text(encoding="utf-8"))
    raw["artifact_files"] = {f"../{digest}": digest}
    manifest_path.write_text(json.dumps(raw, sort_keys=True) + "\n", encoding="utf-8")
    with pytest.raises(BackupError, match="manifest is invalid"):
        _restore(snapshot, tmp_path / "restored.db", tmp_path / "restored-artifacts")


def test_dq04_06_artifact_manifest_digest_tamper_is_rejected(tmp_path: Path) -> None:
    _, database, artifacts, _ = _populated_state(tmp_path)
    snapshot = tmp_path / "snapshot"
    _backup(database, artifacts, snapshot)
    manifest_path = snapshot / "manifest.json"
    raw = json.loads(manifest_path.read_text(encoding="utf-8"))
    artifact_path = next(iter(raw["artifact_files"]))
    raw["artifact_files"][artifact_path] = "f" * 64
    manifest_path.write_text(json.dumps(raw, sort_keys=True) + "\n", encoding="utf-8")
    with pytest.raises(BackupError, match="manifest is invalid"):
        _restore(snapshot, tmp_path / "restored.db", tmp_path / "restored-artifacts")


@pytest.mark.parametrize("link_kind", ["symlink", "hardlink"])
def test_dq04_07_snapshot_external_link_is_rejected(tmp_path: Path, link_kind: str) -> None:
    _, database, artifacts, _ = _populated_state(tmp_path)
    snapshot = tmp_path / "snapshot"
    _backup(database, artifacts, snapshot)
    database_member = snapshot / "research.db"
    external = tmp_path / "external.db"
    shutil.copy2(database_member, external)
    database_member.unlink()
    if link_kind == "symlink":
        database_member.symlink_to(external)
    else:
        os.link(external, database_member)
    with pytest.raises(BackupError, match="symlink|hard-linked"):
        _restore(snapshot, tmp_path / "restored.db", tmp_path / "restored-artifacts")


def test_dq04_08_plain_reference_divergence_is_rejected(tmp_path: Path) -> None:
    _, database, artifacts, _ = _populated_state(tmp_path)
    snapshot = tmp_path / "snapshot"
    _backup(database, artifacts, snapshot)
    snapshot_db = snapshot / "research.db"
    with sqlite3.connect(snapshot_db) as connection:
        changed = connection.execute(
            "UPDATE work_orders SET approval_id = 'missing_approval' "
            "WHERE work_order_id = (SELECT work_order_id FROM work_orders LIMIT 1)"
        ).rowcount
        connection.commit()
    assert changed > 0
    _rewrite_manifest(snapshot, database_sha256=hashlib.sha256(snapshot_db.read_bytes()).hexdigest())
    with pytest.raises(BackupError, match="orphan references"):
        _restore(snapshot, tmp_path / "restored.db", tmp_path / "restored-artifacts")


def test_dq04_08_clean_restore_covers_all_authoritative_tables(tmp_path: Path) -> None:
    _, database, artifacts, _ = _populated_state(tmp_path)
    snapshot = tmp_path / "snapshot"
    _backup(database, artifacts, snapshot)
    restored_db = tmp_path / "restored.db"
    restored_artifacts = tmp_path / "restored-artifacts"
    _restore(snapshot, restored_db, restored_artifacts)
    health = check_restored_snapshot(restored_db, restored_artifacts)
    assert health.healthy
    assert health.table_counts["research_runs"] == 1
    assert health.table_counts["work_orders"] >= 1
    assert health.table_counts["attempts"] >= 1
    assert health.table_counts["audit_events"] >= 1
    assert health.table_counts["agent_invocations"] >= 1
    assert health.table_counts["cloud_interaction_governance"] >= 1


def test_dq04_09_primary_loss_restore_uses_snapshot_only(tmp_path: Path) -> None:
    _, database, artifacts, _ = _populated_state(tmp_path)
    snapshot = tmp_path / "off-host-snapshot"
    _backup(database, artifacts, snapshot)

    unavailable = tmp_path / "unavailable-primary"
    unavailable.mkdir()
    shutil.move(database, unavailable / "orchestrator.db")
    shutil.move(artifacts, unavailable / "artifacts")
    assert not database.exists() and not artifacts.exists()

    restored_db = tmp_path / "recovered" / "orchestrator.db"
    restored_artifacts = tmp_path / "recovered" / "artifacts"
    _restore(snapshot, restored_db, restored_artifacts)
    assert check_restored_snapshot(restored_db, restored_artifacts).healthy


def test_dq04_09_probe_records_complete_rpo_rto_inventory_metrics(tmp_path: Path) -> None:
    _, database, artifacts, _ = _populated_state(tmp_path)
    output = tmp_path / "dq04-report.json"
    completed = subprocess.run(
        [
            sys.executable,
            "scripts/dq04_dr_probe.py",
            "--database",
            str(database),
            "--artifacts",
            str(artifacts),
            "--snapshot",
            str(tmp_path / "probe-snapshot"),
            "--restore-root",
            str(tmp_path / "probe-restore"),
            "--candidate-commit",
            CANDIDATE_COMMIT,
            "--candidate-tag",
            CANDIDATE_TAG,
            "--environment-fingerprint",
            "sha256:" + "0" * 64,
            "--last-committed-at",
            "2026-08-30T00:00:00Z",
            "--output",
            str(output),
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stderr
    report = json.loads(output.read_text(encoding="utf-8"))
    assert report["candidate_commit"] == CANDIDATE_COMMIT
    assert report["candidate_tag"] == CANDIDATE_TAG
    assert report["backup_size_bytes"] > 0
    assert report["backup_seconds"] >= 0
    assert report["restore_seconds"] >= 0
    assert report["validation_seconds"] >= 0
    assert report["rto_seconds"] >= report["restore_seconds"]
    assert report["rpo_seconds"] >= 0
    assert report["restore_health"]["healthy"] is True


def test_dq04_10_restored_runtime_is_reconciled_and_work_continues(tmp_path: Path) -> None:
    _, database, artifacts, _ = _populated_state(tmp_path)
    with sqlite3.connect(database) as connection:
        invocation_id, delegation_id, run_id = connection.execute(
            "SELECT invocation_id, delegation_id, run_id FROM agent_invocations LIMIT 1"
        ).fetchone()
        connection.execute(
            "UPDATE agent_invocations SET status = 'RUNNING', reason_code = NULL, "
            "output_type = NULL, output_json = NULL, completed_at = NULL "
            "WHERE invocation_id = ?",
            (invocation_id,),
        )
        connection.execute(
            "UPDATE delegations SET state = 'RUNNING', completed_at = NULL WHERE delegation_id = ?",
            (delegation_id,),
        )
        connection.commit()

    snapshot = tmp_path / "snapshot"
    _backup(database, artifacts, snapshot)
    restored_db = tmp_path / "restored.db"
    restored_artifacts = tmp_path / "restored-artifacts"
    _restore(snapshot, restored_db, restored_artifacts)

    sessions, orchestrator = _orchestrator_for_restored_state(restored_db, restored_artifacts)
    assert InvocationService(sessions).recover_run(str(run_id)) == (str(invocation_id),)
    with sessions() as session:
        invocation = session.get(AgentInvocationRecord, invocation_id)
        assert invocation is not None
        assert invocation.status == "FAILED"
        assert invocation.reason_code == "CONTROLLER_RESTARTED_BEFORE_EXTERNAL_BIND"

    continued_run = orchestrator.create_run(
        workspace_id="ws_e2e", objective="continue after DQ04 restore"
    )
    continued = asyncio.run(orchestrator.run(continued_run, max_steps=30))
    assert continued.state.value == "COMPLETED"


def test_dq04_11_rotation_retains_latest_and_protected_restore_point(tmp_path: Path) -> None:
    _, database, artifacts, _ = _populated_state(tmp_path)
    snapshot_root = tmp_path / "snapshots"
    snapshot_root.mkdir()
    for name in ("protected", "expired", "latest"):
        _backup(database, artifacts, snapshot_root / name)

    plan = plan_snapshot_rotation(
        snapshot_root,
        retain_latest=1,
        protected=frozenset({"protected"}),
    )
    assert set(plan.retained) == {"protected", "latest"}
    assert plan.delete == ("expired",)
    assert apply_snapshot_rotation(plan) == ("expired",)
    assert not (snapshot_root / "expired").exists()

    _restore(
        snapshot_root / "protected",
        tmp_path / "protected-restored.db",
        tmp_path / "protected-restored-artifacts",
    )
    _restore(
        snapshot_root / "latest",
        tmp_path / "latest-restored.db",
        tmp_path / "latest-restored-artifacts",
    )
