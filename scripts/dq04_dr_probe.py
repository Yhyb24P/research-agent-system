"""Measure backup/restore timings and emit DQ04 RPO/RTO evidence."""

import argparse
import json
import re
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from researchd.backup import (
    backup_snapshot,
    check_restored_snapshot,
    restore_snapshot,
    snapshot_size_bytes,
)


def _parse_time(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("timestamp must include a timezone")
    return parsed.astimezone(UTC)


def _parse_environment_fingerprint(value: str) -> str:
    if re.fullmatch(r"sha256:[0-9a-f]{64}", value) is None:
        raise argparse.ArgumentTypeError("environment fingerprint must be sha256:<64 lowercase hex>")
    return value


def run(args: argparse.Namespace) -> dict[str, Any]:
    started = datetime.now(UTC)
    backup_start = time.monotonic()
    manifest = backup_snapshot(
        args.database,
        args.artifacts,
        args.snapshot,
        candidate_commit=args.candidate_commit,
        candidate_tag=args.candidate_tag,
    )
    backup_seconds = time.monotonic() - backup_start
    backup_size = snapshot_size_bytes(args.snapshot)
    restore_db = args.restore_root / "restored.db"
    restore_artifacts = args.restore_root / "artifacts"
    restore_start = time.monotonic()
    restore_snapshot(
        args.snapshot,
        restore_db,
        restore_artifacts,
        expected_candidate_commit=args.candidate_commit,
        expected_candidate_tag=args.candidate_tag,
    )
    restore_seconds = time.monotonic() - restore_start
    validation_start = time.monotonic()
    health = check_restored_snapshot(restore_db, restore_artifacts)
    validation_seconds = time.monotonic() - validation_start
    snapshot_time = _parse_time(manifest.created_at_utc or started.isoformat())
    rpo_seconds = None
    if args.last_committed_at:
        rpo_seconds = max(0.0, (snapshot_time - _parse_time(args.last_committed_at)).total_seconds())
    return {
        "evidence_version": 1,
        "captured_at_utc": datetime.now(UTC).isoformat(),
        "release_commit": args.candidate_commit,
        "candidate_commit": manifest.candidate_commit,
        "candidate_tag": manifest.candidate_tag,
        "environment_fingerprint": args.environment_fingerprint,
        "snapshot": str(args.snapshot),
        "backup_seconds": backup_seconds,
        "backup_size_bytes": backup_size,
        "restore_seconds": restore_seconds,
        "validation_seconds": validation_seconds,
        "rto_seconds": restore_seconds + validation_seconds,
        "rpo_seconds": rpo_seconds,
        "backup_orphan_artifact_count": manifest.orphan_count,
        "backup_orphan_artifact_digests": list(manifest.orphan_artifact_digests),
        "restore_health": {
            "healthy": health.healthy,
            "schema_revision": health.schema_revision,
            "table_counts": health.table_counts,
            "artifacts_verified": health.artifacts_verified,
            "missing_count": health.missing_count,
            "corrupt_count": health.corrupt_count,
            "orphan_count": health.orphan_count,
        },
        "passed": health.healthy,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--artifacts", type=Path, required=True)
    parser.add_argument("--snapshot", type=Path, required=True)
    parser.add_argument("--restore-root", type=Path, required=True)
    parser.add_argument("--candidate-commit", required=True)
    parser.add_argument("--candidate-tag", required=True)
    parser.add_argument(
        "--environment-fingerprint",
        required=True,
        type=_parse_environment_fingerprint,
    )
    parser.add_argument("--last-committed-at", help="UTC-aware ISO timestamp for RPO measurement")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.snapshot.exists() or args.restore_root.exists():
        parser.error("snapshot and restore-root must not already exist")
    try:
        if args.last_committed_at:
            _parse_time(args.last_committed_at)
        evidence = run(args)
    except (OSError, ValueError, RuntimeError) as error:
        parser.error(str(error))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(evidence, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"output": str(args.output), "passed": evidence["passed"]}))
    return 0 if evidence["passed"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
