"""Capture repeatable DQ05 storage/backup evidence for a running deployment.

This probe is deliberately workload-agnostic: run it beside the agreed soak
workload and retain the JSON output with the release manifest. It never reads
artifact bytes or prompts.
"""

import argparse
import json
import platform
import subprocess
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from researchd.observability import StorageMetricsError, collect_storage_metrics


def _revision() -> str | None:
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "HEAD"], text=True, stderr=subprocess.DEVNULL
        ).strip()
    except (OSError, subprocess.CalledProcessError):
        return None


def capture(args: argparse.Namespace) -> dict[str, Any]:
    samples: list[dict[str, Any]] = []
    for index in range(args.samples):
        metrics = collect_storage_metrics(args.database, args.artifacts, args.backup)
        samples.append({
            "index": index,
            "captured_at_utc": datetime.now(UTC).isoformat(),
            **metrics.as_dict(),
        })
        if index + 1 < args.samples:
            time.sleep(args.interval_seconds)
    latest = samples[-1]
    violations: list[str] = []
    age = latest["backup_age_seconds"]
    if args.max_backup_age_seconds is not None and (
        age is None or age > args.max_backup_age_seconds
    ):
        violations.append("backup_age_seconds exceeds configured threshold")
    if args.max_cas_bytes is not None and latest["cas_size_bytes"] > args.max_cas_bytes:
        violations.append("cas_size_bytes exceeds configured threshold")
    return {
        "evidence_version": 1,
        "release_commit": _revision(),
        "host": {"python": platform.python_version(), "system": platform.platform()},
        "parameters": {
            "samples": args.samples,
            "interval_seconds": args.interval_seconds,
            "max_backup_age_seconds": args.max_backup_age_seconds,
            "max_cas_bytes": args.max_cas_bytes,
        },
        "samples": samples,
        "violations": violations,
        "passed": not violations,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--artifacts", type=Path, required=True)
    parser.add_argument("--backup", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--samples", type=int, default=2)
    parser.add_argument("--interval-seconds", type=float, default=60.0)
    parser.add_argument("--max-backup-age-seconds", type=float)
    parser.add_argument("--max-cas-bytes", type=int)
    args = parser.parse_args()
    if args.samples < 1 or args.interval_seconds < 0:
        parser.error("samples must be >= 1 and interval-seconds must be >= 0")
    try:
        evidence = capture(args)
    except StorageMetricsError as error:
        parser.error(str(error))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(evidence, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"output": str(args.output), "passed": evidence["passed"], "violations": evidence["violations"]}))
    return 0 if evidence["passed"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
