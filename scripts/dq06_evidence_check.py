"""Validate that retained DQ evidence belongs to one clean release baseline."""

import argparse
import json
from pathlib import Path
from typing import Any


def _load(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError(f"cannot read JSON evidence: {path}") from error
    if not isinstance(value, dict):
        raise ValueError(f"evidence root must be an object: {path}")
    return value


def validate(
    manifest_path: Path,
    *,
    storage_evidence: Path | None = None,
    preflight_evidence: Path | None = None,
    dr_evidence: Path | None = None,
    cloud_evidence: Path | None = None,
) -> dict[str, Any]:
    manifest = _load(manifest_path)
    source = manifest.get("source")
    checks: list[dict[str, Any]] = []
    commit = source.get("commit") if isinstance(source, dict) else None
    checks.append({"name": "manifest_source_commit", "passed": isinstance(commit, str) and bool(commit)})
    checks.append({"name": "manifest_worktree_clean", "passed": isinstance(source, dict) and source.get("working_tree") == "clean"})
    checks.append({"name": "schema_head_present", "passed": isinstance(manifest.get("schema"), dict) and bool(manifest["schema"].get("alembic_head"))})
    if storage_evidence is not None:
        evidence = _load(storage_evidence)
        evidence_commit = evidence.get("release_commit")
        checks.append({"name": "storage_evidence_commit_matches", "passed": evidence_commit == commit})
        checks.append({"name": "storage_evidence_passed", "passed": evidence.get("passed") is True})
    if preflight_evidence is not None:
        evidence = _load(preflight_evidence)
        checks.append({"name": "preflight_evidence_commit_matches", "passed": evidence.get("release_commit") == commit})
        preflight_failures = evidence.get("failures")
        checks.append({"name": "preflight_evidence_passed", "passed": isinstance(preflight_failures, list) and not preflight_failures})
    for name, path in (("dr", dr_evidence), ("cloud", cloud_evidence)):
        if path is not None:
            evidence = _load(path)
            checks.append({"name": f"{name}_evidence_commit_matches", "passed": evidence.get("release_commit") == commit})
            checks.append({"name": f"{name}_evidence_passed", "passed": evidence.get("passed") is True})
    failures = [item["name"] for item in checks if not item["passed"]]
    return {"evidence_version": 1, "manifest": str(manifest_path), "checks": checks, "failures": failures, "passed": not failures}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--storage-evidence", type=Path)
    parser.add_argument("--preflight-evidence", type=Path)
    parser.add_argument("--dr-evidence", type=Path)
    parser.add_argument("--cloud-evidence", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        report = validate(
            args.manifest,
            storage_evidence=args.storage_evidence,
            preflight_evidence=args.preflight_evidence,
            dr_evidence=args.dr_evidence,
            cloud_evidence=args.cloud_evidence,
        )
    except ValueError as error:
        parser.error(str(error))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"output": str(args.output), "passed": report["passed"], "failures": report["failures"]}))
    return 0 if report["passed"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
