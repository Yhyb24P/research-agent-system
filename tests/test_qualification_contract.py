import copy
import hashlib
import json
from pathlib import Path
from typing import Any

import pytest

from researchd.qualification import (
    QualificationValidationError,
    validate_acceptance,
    validate_bundle,
    validate_evidence,
    validate_plan,
)


ROOT = Path(__file__).parents[1]


def _example(name: str) -> dict[str, Any]:
    value = json.loads((ROOT / "examples" / name).read_text())
    assert isinstance(value, dict)
    return value


def test_example_qualification_bundle_is_valid_but_not_a_pass_claim() -> None:
    plan = _example("qualification_plan.example.json")
    evidence = _example("qualification_evidence.example.json")
    acceptance = _example("qualification_acceptance.example.json")
    report = validate_bundle(plan=plan, evidence=[evidence], acceptances=[acceptance])
    assert report["valid"] is True
    assert report["evidence_results"] == {"PASS": 0, "FAIL": 0, "INCONCLUSIVE": 1}


def test_hard_failure_cannot_be_reported_as_pass_or_inconclusive() -> None:
    evidence = _example("qualification_evidence.example.json")
    evidence["checks"][0]["result"] = "FAIL"
    for result in ("PASS", "INCONCLUSIVE"):
        conflicting = copy.deepcopy(evidence)
        conflicting["result"] = result
        with pytest.raises(QualificationValidationError, match="HARD check failure"):
            validate_evidence(conflicting)


def test_plan_rejects_cycles_waivers_and_pass_without_evidence() -> None:
    plan = _example("qualification_plan.example.json")
    gates = {item["gate_id"]: item for item in plan["gates"]}
    gates["IQ01"]["dependencies"] = ["IQ02"]
    gates["IQ02"]["dependencies"] = ["IQ01"]
    with pytest.raises(QualificationValidationError, match="cycle"):
        validate_plan(plan)
    plan = _example("qualification_plan.example.json")
    plan["gates"][0]["status"] = "WAIVED"
    with pytest.raises(QualificationValidationError, match="cannot be waived"):
        validate_plan(plan)
    plan = _example("qualification_plan.example.json")
    plan["gates"][0]["status"] = "PASSED"
    with pytest.raises(QualificationValidationError, match="without accepted evidence"):
        validate_plan(plan)


def test_acceptance_rejects_self_approval_and_candidate_mismatch() -> None:
    evidence = _example("qualification_evidence.example.json")
    acceptance = _example("qualification_acceptance.example.json")
    acceptance["reviewer"]["actor_id"] = evidence["producer"]["actor_id"]
    with pytest.raises(QualificationValidationError, match="cannot approve"):
        validate_acceptance(acceptance, evidence_by_id={evidence["evidence_id"]: evidence})
    acceptance = _example("qualification_acceptance.example.json")
    acceptance["candidate_commit"] = "a" * 40
    with pytest.raises(QualificationValidationError, match="different candidate"):
        validate_acceptance(acceptance, evidence_by_id={evidence["evidence_id"]: evidence})


def test_bundle_requires_one_passed_acceptance_for_a_passed_gate() -> None:
    plan = _example("qualification_plan.example.json")
    gate = plan["gates"][0]
    gate["status"] = "PASSED"
    gate["accepted_evidence_ids"] = ["qe_iq01_pass"]
    evidence = _example("qualification_evidence.example.json")
    evidence.update({"evidence_id": "qe_iq01_pass", "gate_id": "IQ01", "result": "PASS"})
    evidence["checks"][0].update({"check_id": "IQ01-01", "result": "PASS"})
    with pytest.raises(QualificationValidationError, match="requires one PASSED acceptance"):
        validate_bundle(plan=plan, evidence=[evidence], acceptances=[])


def test_failed_acceptance_requires_failed_evidence() -> None:
    evidence = _example("qualification_evidence.example.json")
    acceptance = _example("qualification_acceptance.example.json")
    acceptance.update({"result": "FAILED", "hard_failures": ["unsubstantiated"]})
    with pytest.raises(QualificationValidationError, match="no failed evidence"):
        validate_acceptance(acceptance, evidence_by_id={evidence["evidence_id"]: evidence})


def test_bundle_rejects_missing_forked_and_stale_supersession_records() -> None:
    plan = _example("qualification_plan.example.json")
    original = _example("qualification_evidence.example.json")
    missing = copy.deepcopy(original)
    missing.update({"evidence_id": "qe_missing_parent", "supersedes_evidence_id": "qe_absent"})
    with pytest.raises(QualificationValidationError, match="supersedes missing record"):
        validate_bundle(plan=plan, evidence=[missing], acceptances=[])

    successor_a = copy.deepcopy(original)
    successor_a.update({"evidence_id": "qe_successor_a", "supersedes_evidence_id": original["evidence_id"]})
    successor_b = copy.deepcopy(original)
    successor_b.update({"evidence_id": "qe_successor_b", "supersedes_evidence_id": original["evidence_id"]})
    with pytest.raises(QualificationValidationError, match="multiple superseding records"):
        validate_bundle(plan=plan, evidence=[original, successor_a, successor_b], acceptances=[])

    acceptance = _example("qualification_acceptance.example.json")
    with pytest.raises(QualificationValidationError, match="references superseded evidence"):
        validate_bundle(
            plan=plan,
            evidence=[original, successor_a],
            acceptances=[acceptance],
        )


def test_bundle_recomputes_artifact_hash_and_rejects_unsafe_paths(tmp_path: Path) -> None:
    plan = _example("qualification_plan.example.json")
    evidence = _example("qualification_evidence.example.json")
    artifact_root = tmp_path / "bundle"
    artifact = artifact_root / "IQ03" / "trace.json"
    artifact.parent.mkdir(parents=True)
    artifact.write_bytes(b"immutable trace\n")
    evidence["artifacts"] = [{
        "name": "trace",
        "sha256": hashlib.sha256(artifact.read_bytes()).hexdigest(),
        "path": "IQ03/trace.json",
        "classification": "PROJECT_PRIVATE",
    }]
    report = validate_bundle(
        plan=plan,
        evidence=[evidence],
        acceptances=[],
        artifact_root=artifact_root,
    )
    assert report["valid"] is True

    artifact.write_bytes(b"tampered\n")
    with pytest.raises(QualificationValidationError, match="hash mismatch"):
        validate_bundle(plan=plan, evidence=[evidence], acceptances=[], artifact_root=artifact_root)

    evidence["artifacts"][0]["path"] = "../trace.json"
    with pytest.raises(QualificationValidationError, match="not bundle-relative"):
        validate_bundle(plan=plan, evidence=[evidence], acceptances=[], artifact_root=artifact_root)

    outside = tmp_path / "outside"
    outside.mkdir()
    outside_artifact = outside / "trace.json"
    outside_artifact.write_bytes(b"outside\n")
    (artifact_root / "escape").symlink_to(outside, target_is_directory=True)
    evidence["artifacts"][0].update({
        "path": "escape/trace.json",
        "sha256": hashlib.sha256(outside_artifact.read_bytes()).hexdigest(),
    })
    with pytest.raises(QualificationValidationError, match="escapes bundle root"):
        validate_bundle(plan=plan, evidence=[evidence], acceptances=[], artifact_root=artifact_root)


def test_bundle_requires_artifact_root_for_referenced_artifacts() -> None:
    plan = _example("qualification_plan.example.json")
    evidence = _example("qualification_evidence.example.json")
    evidence["artifacts"] = [{
        "name": "trace",
        "sha256": "0" * 64,
        "path": "IQ03/trace.json",
        "classification": "PROJECT_PRIVATE",
    }]
    with pytest.raises(QualificationValidationError, match="artifact_root is required"):
        validate_bundle(plan=plan, evidence=[evidence], acceptances=[])
