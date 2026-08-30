"""PX03-01: LocalVerificationDriver over the trusted verifier domain."""

import json
from pathlib import Path

import pytest

from researchd.domain.criteria import MetricCriterion, ReproCriterion
from researchd.domain.enums import VerificationOverall
from researchd.executor.contracts import CapabilityResult, ExecutorResult
from researchd.storage.models import AttemptRecord, WorkOrderRecord
from researchd.verifier.driver import LocalVerificationDriver
from researchd.verifier.producers import VerificationRefused
from tests.integration.test_verifier import Fixture


@pytest.fixture
def fixture(tmp_path: Path) -> Fixture:
    return Fixture(tmp_path)


def _driver(fixture: Fixture) -> LocalVerificationDriver:
    return LocalVerificationDriver(fixture.sessions, fixture.store)


def _result(artifact_ids: tuple[str, ...] = ()) -> ExecutorResult:
    return ExecutorResult(
        attempt_id="att_verify",
        status="execution_complete",
        capability_results=tuple(
            CapabilityResult(
                request_id=f"cap_{index}",
                status="ok",
                output_artifact_id=artifact_id,
            )
            for index, artifact_id in enumerate(artifact_ids)
        ),
        reported_claims=("all done",),
        errors=(),
    )


def _order_and_attempt(fixture: Fixture) -> tuple[WorkOrderRecord, AttemptRecord]:
    with fixture.sessions() as session:
        order = session.get(WorkOrderRecord, "wo_verify")
        attempt = session.get(AttemptRecord, "att_verify")
        assert order is not None
        assert attempt is not None
        return order, attempt


def test_driver_passes_from_an_immutable_metrics_artifact(
    fixture: Fixture,
) -> None:
    artifact_id = fixture.artifact(json.dumps({"score": 1}).encode(), "metrics")
    criterion = MetricCriterion(
        criterion_id="c_score", type="metric", metric="score", operator=">=", threshold=1
    )
    fixture.set_criteria((criterion,))
    order, attempt = _order_and_attempt(fixture)
    result = _driver(fixture).verify(order, attempt, _result((artifact_id,)))
    assert result.overall is VerificationOverall.PASS


def test_driver_fails_when_the_metric_misses_the_threshold(
    fixture: Fixture,
) -> None:
    artifact_id = fixture.artifact(json.dumps({"score": 0}).encode(), "metrics")
    criterion = MetricCriterion(
        criterion_id="c_score", type="metric", metric="score", operator=">=", threshold=1
    )
    fixture.set_criteria((criterion,))
    order, attempt = _order_and_attempt(fixture)
    result = _driver(fixture).verify(order, attempt, _result((artifact_id,)))
    assert result.overall is VerificationOverall.FAIL


def test_driver_ignores_executor_claims(fixture: Fixture) -> None:
    # The executor claims success while the immutable artifact fails the
    # threshold: the outcome must follow the artifact, not the claim.
    artifact_id = fixture.artifact(json.dumps({"score": 0}).encode(), "metrics")
    criterion = MetricCriterion(
        criterion_id="c_score", type="metric", metric="score", operator=">=", threshold=1
    )
    fixture.set_criteria((criterion,))
    order, attempt = _order_and_attempt(fixture)
    result = _driver(fixture).verify(order, attempt, _result((artifact_id,)))
    assert result.overall is VerificationOverall.FAIL
    assert result.criteria[0].reason_code == "VERIFICATION_METRIC_FAILED"


def test_driver_refuses_a_result_referencing_an_unknown_artifact(
    fixture: Fixture,
) -> None:
    criterion = MetricCriterion(
        criterion_id="c_score", type="metric", metric="score", operator=">=", threshold=1
    )
    fixture.set_criteria((criterion,))
    order, attempt = _order_and_attempt(fixture)
    unknown = "artifact://sha256/" + "0" * 64
    with pytest.raises(VerificationRefused, match="unknown artifact"):
        _driver(fixture).verify(order, attempt, _result((unknown,)))


def test_driver_refuses_a_metric_criterion_without_a_metrics_artifact(
    fixture: Fixture,
) -> None:
    criterion = MetricCriterion(
        criterion_id="c_score", type="metric", metric="score", operator=">=", threshold=1
    )
    fixture.set_criteria((criterion,))
    order, attempt = _order_and_attempt(fixture)
    with pytest.raises(VerificationRefused, match="no metrics artifact"):
        _driver(fixture).verify(order, attempt, _result())


def test_driver_refuses_a_contract_without_acceptance_criteria(
    fixture: Fixture,
) -> None:
    # The fixture contract starts with an empty acceptance list; zero
    # criteria would auto-pass with no evidence, so the driver refuses.
    order, attempt = _order_and_attempt(fixture)
    with pytest.raises(VerificationRefused, match="no acceptance criteria"):
        _driver(fixture).verify(order, attempt, _result())


def test_driver_maps_reproducibility_artifacts(fixture: Fixture) -> None:
    artifacts = tuple(
        fixture.artifact(json.dumps({"run_id": run_id, "success": success}).encode(), "reproducibility")
        for run_id, success in (("seed-1", True), ("seed-2", True), ("seed-3", True))
    )
    criterion = ReproCriterion(
        criterion_id="c_repro", type="reproducibility", runs=3, required_successes=3
    )
    fixture.set_criteria((criterion,))
    order, attempt = _order_and_attempt(fixture)
    result = _driver(fixture).verify(order, attempt, _result(artifacts))
    assert result.overall is VerificationOverall.PASS
    assert len(result.criteria[0].observation_refs) == 3


def test_compose_daemon_wires_the_local_verification_driver(tmp_path: Path) -> None:
    from researchd.daemon.composition import DaemonConfig, compose_daemon

    config = DaemonConfig.model_validate({
        "database": tmp_path / "researchd.db",
        "artifact_root": tmp_path / "artifacts",
        "state_root": tmp_path / "state",
        "repositories": {},
        "job_commands": {},
    })
    application = compose_daemon(config)
    orchestrator = application.api.orchestrator
    assert orchestrator is not None
    assert isinstance(orchestrator.verifier, LocalVerificationDriver)
