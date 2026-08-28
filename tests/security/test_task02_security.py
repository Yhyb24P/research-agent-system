from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from concurrent.futures import ThreadPoolExecutor
from alembic import command
from alembic.config import Config
from pydantic import TypeAdapter
from sqlalchemy import event, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from researchd.artifacts.provenance import ArtifactService, DerivationError
from researchd.artifacts.store import ArtifactCorruptionError, ContentAddressedArtifactStore
from researchd.context.builder import ContextBuilder, EgressDenied
from researchd.context.cloud_bundle import CloudContextBundle
from researchd.context.redaction import DeterministicRedactor
from researchd.domain.enums import Capability, DataClassification, PolicyOutcome, ResearchRunState, WorkOrderState
from researchd.policy.approval import ApprovalNotValid, ApprovalService, parameter_hash
from researchd.policy.engine import BudgetLimits, DeterministicPolicyEngine, PolicyRequest
from researchd.storage.db import create_sqlite_engine, session_factory
from researchd.storage.models import ArtifactDerivationRecord, ArtifactRecord, ApprovalGrantRecord, ResearchRunRecord, WorkspaceRecord, WorkOrderRecord

ROOT = Path(__file__).parents[2]
SECRET_FIXTURE = (Path(__file__).parent / "fixtures" / "secret_marker.txt").read_text().strip()


def migrate(path: Path) -> None:
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("sqlalchemy.url", f"sqlite:///{path}")
    command.upgrade(config, "head")
    command.check(config)


@pytest.fixture
def security_boundary(tmp_path: Path) -> tuple[sessionmaker[Session], ContentAddressedArtifactStore, ArtifactService, ContextBuilder]:
    database = tmp_path / "security.db"
    migrate(database)
    sessions = session_factory(create_sqlite_engine(database))
    now = datetime.now(UTC)
    with sessions.begin() as session:
        session.add(WorkspaceRecord(workspace_id="ws_sec", name="security", version=1, created_at=now, updated_at=now))
        session.flush()
        session.add(ResearchRunRecord(run_id="run_sec", workspace_id="ws_sec", objective="prove egress safety", state=ResearchRunState.ACTIVE.value, version=1, created_at=now, updated_at=now))
        session.flush()
        session.add(WorkOrderRecord(work_order_id="wo_sec", run_id="run_sec", parent_work_order_id=None, objective="build safe context", state=WorkOrderState.READY.value, idempotency_key="security-idempotency-0001", contract={}, version=1, created_at=now, updated_at=now))
    store = ContentAddressedArtifactStore(tmp_path / "artifact-store")
    artifacts = ArtifactService(store, sessions)
    builder = ContextBuilder(sessions, store, DeterministicRedactor(secret_literals=[SECRET_FIXTURE], filesystem_prefixes=["/home/private/project"]))
    return sessions, store, artifacts, builder


class CloudMockSink:
    def __init__(self) -> None:
        self.received: list[bytes] = []

    def send(self, bundle: CloudContextBundle) -> None:
        self.received.append(TypeAdapter(CloudContextBundle).dump_json(bundle))


def register(artifacts: ArtifactService, data: bytes, classification: DataClassification, artifact_type: str = "fixture") -> ArtifactRecord:
    return artifacts.register(data, mime_type="text/plain", artifact_type=artifact_type, classification=classification, producer_type="tool", producer_id="security-fixture")


def test_local_only_bytes_never_reach_cloud_mock(security_boundary: tuple[sessionmaker[Session], ContentAddressedArtifactStore, ArtifactService, ContextBuilder]) -> None:
    _, _, artifacts, builder = security_boundary
    local = register(artifacts, b"LOCAL-ONLY-BYTES-71f9", DataClassification.LOCAL_ONLY)
    safe = register(artifacts, b"safe observation", DataClassification.CLOUD_SAFE)
    sink = CloudMockSink()
    sink.send(builder.build(run_id="run_sec", work_order_id="wo_sec", artifact_ids=[safe.artifact_id]))
    assert b"LOCAL-ONLY-BYTES-71f9" not in sink.received[0]
    with pytest.raises(EgressDenied):
        sink.send(builder.build(run_id="run_sec", work_order_id="wo_sec", artifact_ids=[local.artifact_id]))
    assert len(sink.received) == 1


def test_secret_fixture_is_redacted_even_from_misclassified_safe_text(security_boundary: tuple[sessionmaker[Session], ContentAddressedArtifactStore, ArtifactService, ContextBuilder]) -> None:
    sessions, _, artifacts, builder = security_boundary
    with sessions.begin() as session:
        run = session.get(ResearchRunRecord, "run_sec")
        order = session.get(WorkOrderRecord, "wo_sec")
        assert run is not None and order is not None
        run.objective = f"goal containing {SECRET_FIXTURE}"
        order.objective = "inspect /home/private/project without exposing it"
    payload = f"{SECRET_FIXTURE}\nBearer abc.def\nOPENAI_API_KEY=sk-fixture\n/home/private/project/data"
    artifact = register(artifacts, payload.encode(), DataClassification.CLOUD_SAFE)
    sink = CloudMockSink()
    sink.send(builder.build(run_id="run_sec", work_order_id="wo_sec", artifact_ids=[artifact.artifact_id]))
    received = sink.received[0]
    assert SECRET_FIXTURE.encode() not in received
    assert b"abc.def" not in received and b"sk-fixture" not in received
    assert b"/home/private/project" not in received
    assert b"[REDACTED]" in received


def test_project_private_requires_and_substitutes_cloud_safe_derivation(security_boundary: tuple[sessionmaker[Session], ContentAddressedArtifactStore, ArtifactService, ContextBuilder]) -> None:
    _, _, artifacts, builder = security_boundary
    private = register(artifacts, b"private raw trajectory", DataClassification.PROJECT_PRIVATE)
    with pytest.raises(EgressDenied):
        builder.build(run_id="run_sec", work_order_id="wo_sec", artifact_ids=[private.artifact_id])
    derived = artifacts.derive(
        [private.artifact_id], b'{"first_nonfinite_step":14820}', mime_type="application/json",
        artifact_type="metrics_summary", classification=DataClassification.CLOUD_SAFE,
        producer="metrics-extractor", producer_version="1.0", parameters={"fields": ["first_nonfinite_step"]},
    )
    bundle = builder.build(run_id="run_sec", work_order_id="wo_sec", artifact_ids=[private.artifact_id])
    assert [item.artifact_id for item in bundle.selected_artifacts] == [derived.artifact_id]
    encoded = TypeAdapter(CloudContextBundle).dump_json(bundle)
    assert b"private raw trajectory" not in encoded
    assert b"first_nonfinite_step" in encoded


def test_unknown_classification_fails_closed(security_boundary: tuple[sessionmaker[Session], ContentAddressedArtifactStore, ArtifactService, ContextBuilder]) -> None:
    sessions, store, _, builder = security_boundary
    artifact_id, digest = store.put(b"unknown class bytes")
    with sessions.begin() as session:
        session.add(ArtifactRecord(
            artifact_id=artifact_id, sha256=digest, size=19, mime_type="text/plain",
            artifact_type="fixture", classification="UNKNOWN", producer_type="tool",
            producer_id="legacy-fixture", attempt_id=None, relative_source_path=None,
            created_at=datetime.now(UTC),
        ))
    with pytest.raises(EgressDenied, match="unknown"):
        builder.build(run_id="run_sec", work_order_id="wo_sec", artifact_ids=[artifact_id])


def test_artifact_classification_cannot_change_in_place(security_boundary: tuple[sessionmaker[Session], ContentAddressedArtifactStore, ArtifactService, ContextBuilder]) -> None:
    sessions, _, artifacts, _ = security_boundary
    artifact = register(artifacts, b"raw", DataClassification.LOCAL_ONLY)
    with pytest.raises(IntegrityError, match="artifact (classification|metadata) is immutable"):
        with sessions.begin() as session:
            record = session.get(ArtifactRecord, artifact.artifact_id)
            assert record is not None
            record.classification = DataClassification.CLOUD_SAFE.value


def test_corruption_detected_and_filename_traversal_cannot_choose_store_path(security_boundary: tuple[sessionmaker[Session], ContentAddressedArtifactStore, ArtifactService, ContextBuilder], tmp_path: Path) -> None:
    _, store, artifacts, _ = security_boundary
    outside = tmp_path / "escaped"
    artifact = artifacts.register(
        b"immutable bytes", mime_type="text/plain", artifact_type="log",
        classification=DataClassification.LOCAL_ONLY, producer_type="tool", producer_id="fixture",
        relative_source_path="../../escaped",
    )
    assert not outside.exists()
    assert store.path_for_hash(artifact.sha256).is_relative_to(store.root)
    with pytest.raises(ValueError):
        store.path_for_hash("../../escaped")
    store.path_for_hash(artifact.sha256).write_bytes(b"corrupted")
    with pytest.raises(ArtifactCorruptionError):
        store.read(artifact.artifact_id)


def test_derivation_provenance_is_complete(security_boundary: tuple[sessionmaker[Session], ContentAddressedArtifactStore, ArtifactService, ContextBuilder]) -> None:
    sessions, _, artifacts, _ = security_boundary
    first = register(artifacts, b"source one", DataClassification.LOCAL_ONLY)
    second = register(artifacts, b"source two", DataClassification.PROJECT_PRIVATE)
    derived = artifacts.derive(
        [first.artifact_id, second.artifact_id], b"safe aggregate", mime_type="text/plain",
        artifact_type="summary", classification=DataClassification.CLOUD_SAFE,
        producer="deterministic-extractor", producer_version="2.1", parameters={"mode": "allowlist"},
    )
    with sessions() as session:
        rows = session.scalars(select(ArtifactDerivationRecord).where(ArtifactDerivationRecord.derived_artifact_id == derived.artifact_id)).all()
        assert {row.source_artifact_id for row in rows} == {first.artifact_id, second.artifact_id}
        assert all(row.producer == "deterministic-extractor" and row.producer_version == "2.1" for row in rows)
        assert all(row.parameters_json == {"mode": "allowlist"} for row in rows)
        assert all(len(row.parameters_sha256) == 64 and len(row.transformation_sha256) == 64 for row in rows)


def test_private_source_cannot_be_derived_directly_as_public(security_boundary: tuple[sessionmaker[Session], ContentAddressedArtifactStore, ArtifactService, ContextBuilder]) -> None:
    _, _, artifacts, _ = security_boundary
    private = register(artifacts, b"private source", DataClassification.PROJECT_PRIVATE)
    with pytest.raises(DerivationError, match="CLOUD_SAFE"):
        artifacts.derive(
            [private.artifact_id], b"public bypass", mime_type="text/plain",
            artifact_type="summary", classification=DataClassification.PUBLIC,
            producer="bad-extractor", producer_version="1", parameters={},
        )


def test_failed_provenance_write_leaves_no_cloud_safe_metadata(security_boundary: tuple[sessionmaker[Session], ContentAddressedArtifactStore, ArtifactService, ContextBuilder]) -> None:
    sessions, _, artifacts, _ = security_boundary
    private = register(artifacts, b"atomic private source", DataClassification.PROJECT_PRIVATE)

    def fail_provenance(session: Session, flush_context: Any, instances: Any) -> None:
        del flush_context, instances
        if any(isinstance(item, ArtifactDerivationRecord) for item in session.new):
            raise RuntimeError("injected provenance failure")

    event.listen(Session, "before_flush", fail_provenance)
    try:
        with pytest.raises(RuntimeError, match="injected"):
            artifacts.derive(
                [private.artifact_id], b"atomic derived bytes", mime_type="text/plain",
                artifact_type="summary", classification=DataClassification.CLOUD_SAFE,
                producer="extractor", producer_version="1", parameters={},
            )
    finally:
        event.remove(Session, "before_flush", fail_provenance)
    with sessions() as session:
        assert session.scalar(select(ArtifactRecord).where(ArtifactRecord.producer_id == "extractor@1")) is None


def test_approval_parameter_change_expiry_and_one_shot_replay_rejected(security_boundary: tuple[sessionmaker[Session], ContentAddressedArtifactStore, ArtifactService, ContextBuilder]) -> None:
    sessions, _, _, _ = security_boundary
    service = ApprovalService(sessions)
    expires = datetime.now(UTC) + timedelta(minutes=10)
    request = service.request(operation_type="git.push", parameters={"remote": "origin", "branch": "main"}, requested_by="local-executor", reason="publish reviewed result", risk_level="high", resource_scope={"repository": "repo-1"}, budget_delta={}, expires_at=expires, one_shot=True)
    grant = service.approve(request.approval_id, granted_by="user-1")
    with pytest.raises(ApprovalNotValid, match="parameters"):
        service.authorize(grant.grant_id, operation_type="git.push", parameters={"remote": "attacker", "branch": "main"})
    service.authorize(grant.grant_id, operation_type="git.push", parameters={"branch": "main", "remote": "origin"})
    with pytest.raises(ApprovalNotValid, match="already been used"):
        service.authorize(grant.grant_id, operation_type="git.push", parameters={"remote": "origin", "branch": "main"})
    with sessions.begin() as session:
        stored = session.get(ApprovalGrantRecord, grant.grant_id)
        assert stored is not None
        stored.used_at = None
        stored.expires_at = datetime.now(UTC) - timedelta(seconds=1)
    with pytest.raises(ApprovalNotValid, match="expired"):
        service.authorize(grant.grant_id, operation_type="git.push", parameters={"remote": "origin", "branch": "main"})


def test_canonical_approval_parameters_are_order_independent() -> None:
    assert parameter_hash("publish", {"a": 1, "nested": {"z": 2, "b": 3}}) == parameter_hash("publish", {"nested": {"b": 3, "z": 2}, "a": 1})


def test_concurrent_one_shot_approval_only_authorizes_once(security_boundary: tuple[sessionmaker[Session], ContentAddressedArtifactStore, ArtifactService, ContextBuilder]) -> None:
    sessions, _, _, _ = security_boundary
    service = ApprovalService(sessions)
    parameters = {"destination": "registry.example/project"}
    request = service.request(
        operation_type="external.upload", parameters=parameters, requested_by="executor",
        reason="publish result", risk_level="high", resource_scope={}, budget_delta={},
        expires_at=datetime.now(UTC) + timedelta(minutes=5), one_shot=True,
    )
    grant = service.approve(request.approval_id, granted_by="user")

    def attempt() -> bool:
        try:
            service.authorize(grant.grant_id, operation_type="external.upload", parameters=parameters)
            return True
        except ApprovalNotValid:
            return False

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(lambda _: attempt(), range(2))) == [False, True]


def test_policy_is_deterministic_and_fails_closed() -> None:
    engine = DeterministicPolicyEngine()
    budget = BudgetLimits(100, 100, 0, 100, 10)
    allowed = frozenset({Capability.WORKSPACE_READ, Capability.TEST_RUN, Capability.NETWORK_EXTERNAL})
    request = PolicyRequest(frozenset({Capability.WORKSPACE_READ, Capability.NETWORK_EXTERNAL}), allowed, allowed, frozenset(), budget, budget, DataClassification.PROJECT_PRIVATE)
    first = engine.evaluate(request)
    assert first == engine.evaluate(request)
    assert first.outcome is PolicyOutcome.APPROVAL_REQUIRED
    assert first.effective_capabilities == (Capability.WORKSPACE_READ,)
    unknown = PolicyRequest(frozenset(), allowed, allowed, frozenset(), budget, budget, "UNKNOWN")
    assert engine.evaluate(unknown).outcome is PolicyOutcome.DENY
    excessive = PolicyRequest(frozenset({Capability.TEST_RUN}), allowed, allowed, frozenset(), BudgetLimits(101, 100, 0, 100, 10), budget, DataClassification.LOCAL_ONLY)
    assert engine.evaluate(excessive).outcome is PolicyOutcome.DENY


def test_gpu_budget_overage_is_denied() -> None:
    engine = DeterministicPolicyEngine()
    request = PolicyRequest(
        requested_capabilities=frozenset({Capability.JOB_SUBMIT_GPU}),
        workspace_capabilities=frozenset({Capability.JOB_SUBMIT_GPU}),
        user_capabilities=frozenset({Capability.JOB_SUBMIT_GPU}),
        approved_capabilities=frozenset({Capability.JOB_SUBMIT_GPU}),
        requested_budget=BudgetLimits(10, 10, 101, 10, 10),
        maximum_budget=BudgetLimits(10, 10, 100, 10, 10),
        data_classification=DataClassification.LOCAL_ONLY,
    )
    assert engine.evaluate(request).outcome is PolicyOutcome.DENY
