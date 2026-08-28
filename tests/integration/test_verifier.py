import json
from typing import Any
from datetime import UTC, datetime
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from researchd.artifacts.provenance import ArtifactService
from researchd.artifacts.store import ContentAddressedArtifactStore
from researchd.domain.criteria import ArtifactCriterion, CommandCriterion, MetricCriterion, ReproCriterion, acceptance_fingerprint, normalized_acceptance
from researchd.domain.enums import AttemptState, Capability, DataClassification, ResearchRunState, VerificationOverall, WorkOrderState
from researchd.executor.contracts import CapabilityResult
from researchd.storage.db import create_sqlite_engine, session_factory
from researchd.storage.models import (
    AttemptRecord,
    ArtifactRecord,
    ClaimRecord,
    ExecutionStepRecord,
    ObservationRecord,
    ResearchRunRecord,
    VerificationResultRecord,
    WorkspaceRecord,
    WorkOrderRecord,
)
from researchd.storage.transitions import TransactionalTransitionService, TransitionPreconditionFailed
from researchd.verifier.contracts import VerificationInputs
from researchd.verifier.engine import ClaimRecorder, VerifierEngine
from researchd.verifier.producers import TrustedObservationProducers, VerificationRefused

ROOT = Path(__file__).parents[2]


def migrate(path: Path) -> None:
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("sqlalchemy.url", f"sqlite:///{path}")
    command.upgrade(config, "head")
    command.check(config)


class Fixture:
    def __init__(self, tmp_path: Path) -> None:
        migrate(tmp_path / "verifier.db")
        self.sessions = session_factory(create_sqlite_engine(tmp_path / "verifier.db"))
        self.store = ContentAddressedArtifactStore(tmp_path / "artifacts")
        self.artifacts = ArtifactService(self.store, self.sessions)
        self.engine = VerifierEngine(self.sessions, TrustedObservationProducers(self.store))
        now = datetime.now(UTC)
        with self.sessions.begin() as session:
            session.add(WorkspaceRecord(workspace_id="ws_verify", name="verify", version=1, created_at=now, updated_at=now))
            session.flush()
            session.add(ResearchRunRecord(run_id="run_verify", workspace_id="ws_verify", objective="verify evidence", state=ResearchRunState.ACTIVE.value, version=1, created_at=now, updated_at=now))
            session.flush()
            session.add(WorkOrderRecord(
                work_order_id="wo_verify", run_id="run_verify", parent_work_order_id=None,
                objective="verify fixture", state=WorkOrderState.VERIFYING.value,
                idempotency_key="verifier-idempotency-0001", contract={"acceptance": []},
                version=1, created_at=now, updated_at=now,
            ))
            session.flush()
            session.add(AttemptRecord(
                attempt_id="att_verify", work_order_id="wo_verify", state=AttemptState.VERIFYING.value,
                terminal_at=None, version=1, created_at=now, updated_at=now,
            ))

    def set_criteria(self, criteria: tuple[object, ...]) -> None:
        with self.sessions.begin() as session:
            order = session.get(WorkOrderRecord, "wo_verify")
            assert order is not None
            order.contract = {"acceptance": normalized_acceptance(criteria)}

    def artifact(self, payload: bytes, artifact_type: str) -> str:
        return self.artifacts.register(
            payload, mime_type="application/json" if artifact_type != "junit" else "application/xml",
            artifact_type=artifact_type, classification=DataClassification.LOCAL_ONLY,
            producer_type="tool", producer_id="verification-fixture", attempt_id="att_verify",
        ).artifact_id

    def step(self, step_id: str, exit_code: int) -> None:
        now = datetime.now(UTC)
        result = CapabilityResult(request_id=step_id, status="ok" if exit_code == 0 else "failed", exit_code=exit_code)
        with self.sessions.begin() as session:
            session.add(ExecutionStepRecord(
                step_id=step_id, attempt_id="att_verify", capability=Capability.TEST_RUN.value,
                parameters_sha256="a" * 64, status="COMPLETED",
                result_json=result.model_dump(mode="json"), created_at=now, updated_at=now,
            ))


@pytest.fixture
def fixture(tmp_path: Path) -> Fixture:
    return Fixture(tmp_path)


def test_executor_claims_pass_but_junit_failure_hard_fails(fixture: Fixture) -> None:
    junit_id = fixture.artifact(
        (ROOT / "tests/fixtures/verification/junit_failed.xml").read_bytes(),
        "junit",
    )
    fixture.step("step_pytest", 0)
    criterion = CommandCriterion(
        criterion_id="c_pytest", type="command", command_id="step_pytest",
        expected_exit_code=0, junit_artifact_id=junit_id,
    )
    fixture.set_criteria((criterion,))
    claims = ClaimRecorder(fixture.sessions).record_executor_claims("att_verify", ("All tests passed",))
    result = fixture.engine.verify(
        work_order_id="wo_verify", attempt_id="att_verify",
        criteria=(criterion,), inputs=VerificationInputs(),
    )
    assert result.overall is VerificationOverall.FAIL
    assert result.criteria[0].reason_code == "VERIFICATION_JUNIT_FAILED"
    with pytest.raises(TransitionPreconditionFailed):
        TransactionalTransitionService(fixture.sessions).transition_work_order(
            "wo_verify", 1, WorkOrderState.REVIEW_READY,
            event_type="CLOUD_ACCEPT_ATTEMPT", actor_type="cloud_lead",
            actor_id="untrusted-cloud", correlation_id="att_verify",
        )
    with fixture.sessions() as session:
        assert session.get(ClaimRecord, claims[0]) is not None
        observations = session.scalars(select(ObservationRecord).where(ObservationRecord.attempt_id == "att_verify")).all()
        assert len(observations) == 2
        assert all(item.producer_id and (item.source_artifact_ids or item.source_step_ids or item.source_job_ids) for item in observations)


def test_missing_source_artifact_refuses_without_partial_evidence(fixture: Fixture) -> None:
    criterion = MetricCriterion(criterion_id="c_metric", type="metric", metric="loss", operator="<=", threshold=1.0)
    fixture.set_criteria((criterion,))
    missing = "artifact://sha256/" + "0" * 64
    with pytest.raises(VerificationRefused, match="missing"):
        fixture.engine.verify(
            work_order_id="wo_verify", attempt_id="att_verify", criteria=(criterion,),
            inputs=VerificationInputs(metric_artifacts={"loss": missing}),
        )
    with fixture.sessions() as session:
        assert session.scalar(select(VerificationResultRecord)) is None
        assert session.scalar(select(ObservationRecord)) is None


@pytest.mark.parametrize(
    ("operator", "value", "threshold", "expected"),
    [("==", 1.0, 1.0, True), (">=", 1.0, 1.0, True), ("<=", 1.0, 1.0, True), (">", 1.0, 1.0, False), ("<", 1.0, 1.0, False), ("!=", 1.0, 1.0, False)],
)
def test_metric_threshold_boundaries(fixture: Fixture, operator: str, value: float, threshold: float, expected: bool) -> None:
    artifact_id = fixture.artifact(json.dumps({"score": value}).encode(), "metrics")
    criterion = MetricCriterion.model_validate({"criterion_id": "c_score", "type": "metric", "metric": "score", "operator": operator, "threshold": threshold})
    fixture.set_criteria((criterion,))
    result = fixture.engine.verify(
        work_order_id="wo_verify", attempt_id="att_verify", criteria=(criterion,),
        inputs=VerificationInputs(metric_artifacts={"score": artifact_id}),
    )
    assert (result.overall is VerificationOverall.PASS) is expected


def test_artifact_criterion_validates_type_hash_and_count(fixture: Fixture) -> None:
    artifact_id = fixture.artifact(b"patch bytes", "patch")
    criterion = ArtifactCriterion(criterion_id="c_patch", type="artifact", artifact_type="patch", min_count=1)
    fixture.set_criteria((criterion,))
    passed = fixture.engine.verify(work_order_id="wo_verify", attempt_id="att_verify", criteria=(criterion,), inputs=VerificationInputs())
    assert passed.overall is VerificationOverall.PASS
    fixture.store.path_for_hash(artifact_id.removeprefix("artifact://sha256/")).write_bytes(b"corrupt")
    with pytest.raises(VerificationRefused, match="hash"):
        fixture.engine.verify(work_order_id="wo_verify", attempt_id="att_verify", criteria=(criterion,), inputs=VerificationInputs())


def test_artifact_identity_metadata_is_database_immutable(fixture: Fixture) -> None:
    artifact_id = fixture.artifact(b"immutable metadata", "patch")
    with pytest.raises(IntegrityError, match="metadata is immutable"):
        with fixture.sessions.begin() as session:
            record = session.get(ArtifactRecord, artifact_id)
            assert record is not None
            record.sha256 = "f" * 64


def test_reproducibility_aggregates_independent_runs(fixture: Fixture) -> None:
    artifacts = tuple(
        fixture.artifact(json.dumps({"run_id": run_id, "success": success}).encode(), "reproducibility")
        for run_id, success in (("seed-1", True), ("seed-2", False), ("seed-3", True))
    )
    criterion = ReproCriterion(criterion_id="c_repro", type="reproducibility", runs=3, required_successes=2)
    fixture.set_criteria((criterion,))
    result = fixture.engine.verify(
        work_order_id="wo_verify", attempt_id="att_verify", criteria=(criterion,),
        inputs=VerificationInputs(reproducibility_artifacts={"c_repro": artifacts}),
    )
    assert result.overall is VerificationOverall.PASS
    assert len(result.criteria[0].observation_refs) == 3


def test_observation_database_constraint_requires_source(fixture: Fixture) -> None:
    with pytest.raises(IntegrityError, match="has_source"):
        with fixture.sessions.begin() as session:
            session.add(ObservationRecord(
                observation_id="obs_invalid", attempt_id="att_verify", name="invalid", value_json=True,
                source_artifact_ids=[], source_step_ids=[], source_job_ids=[], producer_type="verifier",
                producer_id="fixture", producer_version="1", created_at=datetime.now(UTC),
                classification=DataClassification.LOCAL_ONLY.value,
            ))


def test_review_ready_requires_latest_valid_hard_pass(fixture: Fixture) -> None:
    service = TransactionalTransitionService(fixture.sessions)
    kwargs: dict[str, Any] = dict(event_type="VERIFICATION_PASSED", actor_type="controller", actor_id="controller", correlation_id="att_verify")
    with pytest.raises(TransitionPreconditionFailed):
        service.transition_work_order("wo_verify", 1, WorkOrderState.REVIEW_READY, **kwargs)
    artifact_id = fixture.artifact(b'{"score":1}', "metrics")
    criterion = MetricCriterion(criterion_id="c_score", type="metric", metric="score", operator=">=", threshold=1)
    fixture.set_criteria((criterion,))
    fixture.engine.verify(
        work_order_id="wo_verify", attempt_id="att_verify", criteria=(criterion,),
        inputs=VerificationInputs(metric_artifacts={"score": artifact_id}),
    )
    version = service.transition_work_order("wo_verify", 1, WorkOrderState.REVIEW_READY, **kwargs)
    assert version == 2


def test_review_ready_rejects_forged_empty_pass_summary(fixture: Fixture) -> None:
    criterion = MetricCriterion(criterion_id="c_required", type="metric", metric="score", operator=">=", threshold=1)
    fixture.set_criteria((criterion,))
    now = datetime.now(UTC)
    with fixture.sessions.begin() as session:
        session.add(VerificationResultRecord(
            verification_id="ver_forged", attempt_id="att_verify", work_order_id="wo_verify",
            overall="pass", criteria_json=[], acceptance_sha256=acceptance_fingerprint((criterion,)),
            verifier_version="forged", valid=True, created_at=now,
            classification=DataClassification.LOCAL_ONLY.value,
        ))
    with pytest.raises(TransitionPreconditionFailed):
        TransactionalTransitionService(fixture.sessions).transition_work_order(
            "wo_verify", 1, WorkOrderState.REVIEW_READY,
            event_type="FORGED_REVIEW_READY", actor_type="controller", actor_id="fixture",
            correlation_id="att_verify",
        )


def test_advisory_failure_does_not_override_hard_pass(fixture: Fixture) -> None:
    hard_id = fixture.artifact(b'{"hard":1}', "metrics")
    advisory_id = fixture.artifact(b'{"quality":0}', "metrics")
    hard = MetricCriterion(criterion_id="c_hard", type="metric", metric="hard", operator="==", threshold=1, severity="hard")
    advisory = MetricCriterion(criterion_id="c_advisory", type="metric", metric="quality", operator=">", threshold=0, severity="advisory")
    fixture.set_criteria((hard, advisory))
    result = fixture.engine.verify(
        work_order_id="wo_verify", attempt_id="att_verify", criteria=(hard, advisory),
        inputs=VerificationInputs(metric_artifacts={"hard": hard_id, "quality": advisory_id}),
    )
    assert result.overall is VerificationOverall.PASS
    assert [item.result.value for item in result.criteria] == ["pass", "fail"]
