"""Validate qualification plans, evidence, and Gate acceptance records."""

from __future__ import annotations

import argparse
from collections.abc import Iterable
from datetime import UTC, datetime
import json
from pathlib import Path
import subprocess
from typing import Any

from jsonschema import Draft202012Validator, FormatChecker


ROOT = Path(__file__).resolve().parents[2]
SCHEMA_ROOT = ROOT / "schemas"
EXPECTED_GATES = frozenset({
    "IQ01", "IQ02", "IQ03",
    "DQ01", "DQ02", "DQ03", "DQ04", "DQ05",
    "RQ01", "RQ02", "RQ03",
})
FORBIDDEN_KEYS = frozenset({
    "api_key", "apikey", "password", "secret", "access_token", "refresh_token",
})


class QualificationValidationError(ValueError):
    """Raised when a qualification object is malformed or contradictory."""


def _load_object(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise QualificationValidationError(f"cannot read JSON object {path}: {error}") from error
    if not isinstance(value, dict):
        raise QualificationValidationError(f"JSON root must be an object: {path}")
    return value


def _validate_schema(value: dict[str, Any], schema_name: str) -> None:
    schema = _load_object(SCHEMA_ROOT / schema_name)
    validator = Draft202012Validator(schema, format_checker=FormatChecker())
    errors = sorted(validator.iter_errors(value), key=lambda item: tuple(str(part) for part in item.path))
    if errors:
        rendered = "; ".join(
            f"{'/'.join(str(part) for part in error.path) or '<root>'}: {error.message}"
            for error in errors
        )
        raise QualificationValidationError(rendered)


def _require_candidate(
    value: dict[str, Any], *, expected_commit: str | None, expected_tag: str | None
) -> None:
    if expected_commit is not None and value["candidate_commit"] != expected_commit:
        raise QualificationValidationError("candidate_commit does not match the expected candidate")
    if expected_tag is not None and value["candidate_tag"] != expected_tag:
        raise QualificationValidationError("candidate_tag does not match the expected candidate")


def _utc_timestamp(value: str, field: str) -> datetime:
    try:
        timestamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise QualificationValidationError(f"{field} is not a valid timestamp") from error
    if timestamp.tzinfo is None or timestamp.utcoffset() != UTC.utcoffset(timestamp):
        raise QualificationValidationError(f"{field} must be UTC")
    return timestamp


def _contains_forbidden_key(value: Any) -> bool:
    if isinstance(value, dict):
        return any(
            str(key).lower() in FORBIDDEN_KEYS
            or str(key).lower().endswith(("_secret", "_token", "_api_key"))
            or _contains_forbidden_key(item)
            for key, item in value.items()
        )
    if isinstance(value, list):
        return any(_contains_forbidden_key(item) for item in value)
    return False


def validate_plan(
    value: dict[str, Any], *, expected_commit: str | None = None, expected_tag: str | None = None
) -> None:
    _validate_schema(value, "qualification_plan.schema.json")
    _require_candidate(value, expected_commit=expected_commit, expected_tag=expected_tag)
    gates = value["gates"]
    identifiers = [item["gate_id"] for item in gates]
    if len(identifiers) != len(set(identifiers)):
        raise QualificationValidationError("qualification plan contains duplicate gate_id values")
    if set(identifiers) != EXPECTED_GATES:
        missing = sorted(EXPECTED_GATES - set(identifiers))
        extra = sorted(set(identifiers) - EXPECTED_GATES)
        raise QualificationValidationError(f"qualification plan Gate set mismatch: missing={missing}, extra={extra}")
    by_id = {item["gate_id"]: item for item in gates}
    for gate in gates:
        gate_id = gate["gate_id"]
        dependencies = gate["dependencies"]
        if gate_id in dependencies:
            raise QualificationValidationError(f"{gate_id} depends on itself")
        unknown = sorted(set(dependencies) - set(identifiers))
        if unknown:
            raise QualificationValidationError(f"{gate_id} has unknown dependencies: {unknown}")
        if gate["status"] == "WAIVED":
            raise QualificationValidationError(f"{gate_id} cannot be waived at Gate level")
        if gate["status"] == "PASSED":
            if not gate.get("accepted_evidence_ids"):
                raise QualificationValidationError(f"{gate_id} is PASSED without accepted evidence")
            incomplete = [dependency for dependency in dependencies if by_id[dependency]["status"] != "PASSED"]
            if incomplete:
                raise QualificationValidationError(f"{gate_id} passed before dependencies: {incomplete}")

    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(gate_id: str) -> None:
        if gate_id in visiting:
            raise QualificationValidationError(f"qualification plan dependency cycle includes {gate_id}")
        if gate_id in visited:
            return
        visiting.add(gate_id)
        for dependency in by_id[gate_id]["dependencies"]:
            visit(dependency)
        visiting.remove(gate_id)
        visited.add(gate_id)

    for identifier in identifiers:
        visit(identifier)
    all_passed = all(item["status"] == "PASSED" for item in gates)
    if (value["status"] == "COMPLETED") != all_passed:
        raise QualificationValidationError("plan status COMPLETED must exactly match all Gates PASSED")


def validate_evidence(
    value: dict[str, Any], *, expected_commit: str | None = None, expected_tag: str | None = None
) -> None:
    _validate_schema(value, "qualification_evidence.schema.json")
    _require_candidate(value, expected_commit=expected_commit, expected_tag=expected_tag)
    started = _utc_timestamp(value["started_at"], "started_at")
    completed = _utc_timestamp(value["completed_at"], "completed_at")
    if completed < started:
        raise QualificationValidationError("completed_at precedes started_at")
    if value.get("supersedes_evidence_id") == value["evidence_id"]:
        raise QualificationValidationError("evidence cannot supersede itself")
    if _contains_forbidden_key(value):
        raise QualificationValidationError("qualification evidence contains a forbidden credential key")
    checks = value["checks"]
    check_ids = [item["check_id"] for item in checks]
    if len(check_ids) != len(set(check_ids)):
        raise QualificationValidationError("qualification evidence contains duplicate check_id values")
    if any(not check_id.startswith(f"{value['gate_id']}-") for check_id in check_ids):
        raise QualificationValidationError("evidence check_id does not belong to gate_id")
    artifact_names = [item["name"] for item in value["artifacts"]]
    if len(artifact_names) != len(set(artifact_names)):
        raise QualificationValidationError("qualification evidence contains duplicate artifact names")

    hard_failure = any(item["severity"] == "HARD" and item["result"] == "FAIL" for item in checks)
    failures = any(item["result"] == "FAIL" for item in checks)
    inconclusive = any(item["result"] == "INCONCLUSIVE" for item in checks)
    result = value["result"]
    if hard_failure and result != "FAIL":
        raise QualificationValidationError("HARD check failure requires evidence result FAIL")
    if result == "PASS" and (failures or inconclusive):
        raise QualificationValidationError("PASS evidence contains a failed or inconclusive check")
    if result == "FAIL" and not failures:
        raise QualificationValidationError("FAIL evidence contains no failed check")
    if result == "INCONCLUSIVE" and (hard_failure or not inconclusive):
        raise QualificationValidationError("INCONCLUSIVE evidence must contain an inconclusive check and no HARD failure")
    if not failures and not inconclusive and result != "PASS":
        raise QualificationValidationError("all checks pass but evidence result is not PASS")


def validate_acceptance(
    value: dict[str, Any],
    *,
    evidence_by_id: dict[str, dict[str, Any]],
    expected_commit: str | None = None,
    expected_tag: str | None = None,
) -> None:
    _validate_schema(value, "qualification_acceptance.schema.json")
    _require_candidate(value, expected_commit=expected_commit, expected_tag=expected_tag)
    _utc_timestamp(value["reviewed_at"], "reviewed_at")
    if value.get("supersedes_acceptance_id") == value["acceptance_id"]:
        raise QualificationValidationError("acceptance cannot supersede itself")
    accepted_ids = value["accepted_evidence_ids"]
    missing = sorted(set(accepted_ids) - set(evidence_by_id))
    if missing:
        raise QualificationValidationError(f"acceptance references missing evidence: {missing}")
    accepted = [evidence_by_id[evidence_id] for evidence_id in accepted_ids]
    for evidence in accepted:
        if evidence["gate_id"] != value["gate_id"]:
            raise QualificationValidationError("acceptance references evidence from a different Gate")
        if evidence["candidate_commit"] != value["candidate_commit"] or evidence["candidate_tag"] != value["candidate_tag"]:
            raise QualificationValidationError("acceptance references evidence from a different candidate")
        if evidence["producer"]["actor_id"] == value["reviewer"]["actor_id"]:
            raise QualificationValidationError("evidence producer cannot approve its own Gate evidence")
    result = value["result"]
    failed_evidence = any(item["result"] == "FAIL" for item in accepted)
    hard_failed_checks = any(
        check["severity"] == "HARD" and check["result"] == "FAIL"
        for item in accepted
        for check in item["checks"]
    )
    if hard_failed_checks and result != "FAILED":
        raise QualificationValidationError("HARD evidence failure requires acceptance result FAILED")
    if result == "PASSED":
        if not accepted or any(item["result"] != "PASS" for item in accepted):
            raise QualificationValidationError("PASSED acceptance requires only PASS evidence")
        if value["hard_failures"]:
            raise QualificationValidationError("PASSED acceptance contains hard failures")
    elif result == "FAILED":
        if not failed_evidence:
            raise QualificationValidationError("FAILED acceptance has no failed evidence")
        if hard_failed_checks and not value["hard_failures"]:
            raise QualificationValidationError("FAILED acceptance omits its HARD evidence failure")
    else:
        if failed_evidence or value["hard_failures"]:
            raise QualificationValidationError("INCONCLUSIVE acceptance contains a failure claim")
        if not any(item["result"] == "INCONCLUSIVE" for item in accepted):
            raise QualificationValidationError("INCONCLUSIVE acceptance has no inconclusive evidence")


def _validate_supersession_chain(
    items_by_id: dict[str, dict[str, Any]], *, id_field: str, supersedes_field: str
) -> set[str]:
    superseded_by: dict[str, str] = {}
    for item_id, item in items_by_id.items():
        superseded_id = item.get(supersedes_field)
        if superseded_id is None:
            continue
        if superseded_id not in items_by_id:
            raise QualificationValidationError(f"{item_id} supersedes missing record {superseded_id}")
        previous = items_by_id[superseded_id]
        if previous["gate_id"] != item["gate_id"]:
            raise QualificationValidationError(f"{item_id} supersedes a record from a different Gate")
        if (
            previous["candidate_commit"] != item["candidate_commit"]
            or previous["candidate_tag"] != item["candidate_tag"]
        ):
            raise QualificationValidationError(f"{item_id} supersedes a record from a different candidate")
        if superseded_id in superseded_by:
            raise QualificationValidationError(
                f"{superseded_id} has multiple superseding records: "
                f"{superseded_by[superseded_id]}, {item_id}"
            )
        superseded_by[superseded_id] = item_id

    for item_id in items_by_id:
        seen: set[str] = set()
        cursor: str | None = item_id
        while cursor is not None:
            if cursor in seen:
                raise QualificationValidationError(f"{id_field} supersession cycle includes {cursor}")
            seen.add(cursor)
            cursor_value = items_by_id[cursor].get(supersedes_field)
            cursor = cursor_value if isinstance(cursor_value, str) else None
    return set(superseded_by)


def verify_git_candidate(repository: Path, *, commit: str, tag: str) -> None:
    try:
        resolved_commit = subprocess.check_output(
            ["git", "-C", str(repository), "rev-parse", commit], text=True, stderr=subprocess.STDOUT
        ).strip()
        resolved_tag = subprocess.check_output(
            ["git", "-C", str(repository), "rev-parse", f"{tag}^{{}}"], text=True, stderr=subprocess.STDOUT
        ).strip()
    except subprocess.CalledProcessError as error:
        raise QualificationValidationError("candidate commit or tag is not available in the repository") from error
    if resolved_commit != commit or resolved_tag != commit:
        raise QualificationValidationError("candidate tag does not dereference to candidate_commit")


def validate_bundle(
    *,
    plan: dict[str, Any] | None,
    evidence: Iterable[dict[str, Any]],
    acceptances: Iterable[dict[str, Any]],
    expected_commit: str | None = None,
    expected_tag: str | None = None,
    repository: Path | None = None,
) -> dict[str, Any]:
    evidence_items = list(evidence)
    acceptance_items = list(acceptances)
    if plan is not None:
        validate_plan(plan, expected_commit=expected_commit, expected_tag=expected_tag)
        expected_commit = expected_commit or plan["candidate_commit"]
        expected_tag = expected_tag or plan["candidate_tag"]
    if expected_commit is None or expected_tag is None:
        raise QualificationValidationError("expected candidate must be supplied directly or through a plan")
    if repository is not None:
        verify_git_candidate(repository, commit=expected_commit, tag=expected_tag)
    evidence_by_id: dict[str, dict[str, Any]] = {}
    for item in evidence_items:
        validate_evidence(item, expected_commit=expected_commit, expected_tag=expected_tag)
        evidence_id = item["evidence_id"]
        if evidence_id in evidence_by_id:
            raise QualificationValidationError(f"duplicate evidence_id: {evidence_id}")
        evidence_by_id[evidence_id] = item
    superseded_evidence_ids = _validate_supersession_chain(
        evidence_by_id,
        id_field="evidence_id",
        supersedes_field="supersedes_evidence_id",
    )
    acceptance_by_gate: dict[str, list[dict[str, Any]]] = {}
    acceptance_by_id: dict[str, dict[str, Any]] = {}
    acceptance_ids: set[str] = set()
    for item in acceptance_items:
        validate_acceptance(
            item,
            evidence_by_id=evidence_by_id,
            expected_commit=expected_commit,
            expected_tag=expected_tag,
        )
        acceptance_id = item["acceptance_id"]
        if acceptance_id in acceptance_ids:
            raise QualificationValidationError(f"duplicate acceptance_id: {acceptance_id}")
        acceptance_ids.add(acceptance_id)
        acceptance_by_id[acceptance_id] = item
        acceptance_by_gate.setdefault(item["gate_id"], []).append(item)
    superseded_acceptance_ids = _validate_supersession_chain(
        acceptance_by_id,
        id_field="acceptance_id",
        supersedes_field="supersedes_acceptance_id",
    )
    for acceptance in acceptance_items:
        stale = sorted(set(acceptance["accepted_evidence_ids"]) & superseded_evidence_ids)
        if stale:
            raise QualificationValidationError(
                f"acceptance {acceptance['acceptance_id']} references superseded evidence: {stale}"
            )
    if plan is not None:
        for gate in plan["gates"]:
            if gate["status"] != "PASSED":
                continue
            passed = [
                item
                for item in acceptance_by_gate.get(gate["gate_id"], [])
                if item["result"] == "PASSED" and item["acceptance_id"] not in superseded_acceptance_ids
            ]
            if len(passed) != 1:
                raise QualificationValidationError(f"PASSED Gate {gate['gate_id']} requires one PASSED acceptance")
            if set(passed[0]["accepted_evidence_ids"]) != set(gate["accepted_evidence_ids"]):
                raise QualificationValidationError(f"PASSED Gate {gate['gate_id']} evidence differs from its acceptance")
    return {
        "valid": True,
        "candidate_commit": expected_commit,
        "candidate_tag": expected_tag,
        "plan_present": plan is not None,
        "evidence_count": len(evidence_items),
        "acceptance_count": len(acceptance_items),
        "evidence_results": {
            "PASS": sum(item["result"] == "PASS" for item in evidence_items),
            "FAIL": sum(item["result"] == "FAIL" for item in evidence_items),
            "INCONCLUSIVE": sum(item["result"] == "INCONCLUSIVE" for item in evidence_items),
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", type=Path)
    parser.add_argument("--evidence", type=Path, action="append", default=[])
    parser.add_argument("--acceptance", type=Path, action="append", default=[])
    parser.add_argument("--expected-commit")
    parser.add_argument("--expected-tag")
    parser.add_argument("--repository", type=Path, help="verify that candidate_tag dereferences to candidate_commit")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    try:
        report = validate_bundle(
            plan=_load_object(args.plan) if args.plan else None,
            evidence=(_load_object(path) for path in args.evidence),
            acceptances=(_load_object(path) for path in args.acceptance),
            expected_commit=args.expected_commit,
            expected_tag=args.expected_tag,
            repository=args.repository,
        )
    except QualificationValidationError as error:
        report = {"valid": False, "error": str(error)}
    rendered = json.dumps(report, ensure_ascii=False, sort_keys=True)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered + "\n", encoding="utf-8")
    print(rendered)
    return 0 if report["valid"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
