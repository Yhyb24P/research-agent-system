"""Validate that retained DQ evidence belongs to one clean release baseline."""

import argparse
import json
from pathlib import Path
from typing import Any


_CLOUD_METADATA = ("provider", "model", "tested_at_utc", "credential_reference", "retention_policy")
_FORBIDDEN_CLOUD_KEYS = {"api_key", "apikey", "password", "secret", "access_token", "refresh_token"}


def _load(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError(f"cannot read JSON evidence: {path}") from error
    if not isinstance(value, dict):
        raise ValueError(f"evidence root must be an object: {path}")
    return value


def _has_forbidden_key(value: Any) -> bool:
    if isinstance(value, dict):
        return any(
            str(key).lower() in _FORBIDDEN_CLOUD_KEYS
            or str(key).lower().endswith(("_secret", "_token", "_api_key"))
            or _has_forbidden_key(item)
            for key, item in value.items()
        )
    if isinstance(value, list):
        return any(_has_forbidden_key(item) for item in value)
    return False


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
    tags = source.get("tags") if isinstance(source, dict) else None
    checks.append({"name": "manifest_rc_tag_present", "passed": isinstance(tags, list) and any(isinstance(tag, str) and tag.startswith("v1.0.0-rc.") for tag in tags)})
    checks.append({"name": "manifest_worktree_clean", "passed": isinstance(source, dict) and source.get("working_tree") == "clean"})
    checks.append({"name": "schema_head_present", "passed": isinstance(manifest.get("schema"), dict) and bool(manifest["schema"].get("alembic_head"))})
    dependencies = manifest.get("dependencies")
    inventory = dependencies.get("uv_lock_inventory") if isinstance(dependencies, dict) else None
    checks.append({"name": "lock_inventory_present", "passed": isinstance(inventory, dict) and isinstance(inventory.get("packages"), list) and bool(inventory["packages"])})
    if storage_evidence is not None:
        evidence = _load(storage_evidence)
        evidence_commit = evidence.get("release_commit")
        checks.append({"name": "storage_evidence_commit_matches", "passed": evidence_commit == commit})
        checks.append({"name": "storage_evidence_passed", "passed": evidence.get("passed") is True})
        samples = evidence.get("samples")
        latest = samples[-1] if isinstance(samples, list) and samples and isinstance(samples[-1], dict) else None
        checks.append({"name": "storage_evidence_samples_present", "passed": latest is not None})
        checks.append({"name": "storage_evidence_shape", "passed": latest is not None and all(key in latest for key in ("database_size_bytes", "wal_size_bytes", "cas_size_bytes", "backup_manifest_present"))})
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
            if name == "dr":
                health = evidence.get("restore_health")
                checks.append({"name": "dr_timings_present", "passed": all(isinstance(evidence.get(key), (int, float)) and evidence[key] >= 0 for key in ("backup_seconds", "restore_seconds"))})
                checks.append({"name": "dr_health_present", "passed": isinstance(health, dict) and health.get("healthy") is True and bool(health.get("schema_revision"))})
            if name == "cloud":
                metadata = evidence.get("metadata")
                checks.append({"name": "cloud_metadata_complete", "passed": isinstance(metadata, dict) and all(metadata.get(field) for field in _CLOUD_METADATA)})
                checks.append({"name": "cloud_scenarios_present", "passed": isinstance(evidence.get("scenarios"), list) and bool(evidence["scenarios"])})
                checks.append({"name": "cloud_credentials_absent", "passed": not _has_forbidden_key(evidence)})
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
