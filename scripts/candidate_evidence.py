#!/usr/bin/env python3
"""Write a sanitized, hash-bound candidate-gate evidence summary."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from pathlib import Path


_COMMIT = re.compile(r"^[0-9a-f]{40}$")


def digest(path: Path) -> str:
    value = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            value.update(chunk)
    return value.hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=("preflight", "exact"), required=True)
    parser.add_argument("--candidate-tag")
    parser.add_argument("--candidate-commit", required=True)
    parser.add_argument("--checked-out-commit", required=True)
    parser.add_argument("--project-version", required=True)
    parser.add_argument("--wheel", type=Path, required=True)
    parser.add_argument("--sdist", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--sbom", type=Path, required=True)
    parser.add_argument("--e2e", type=Path, required=True)
    parser.add_argument("--failure-e2e", type=Path, required=True)
    parser.add_argument("--workflow-run-identity", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    if not _COMMIT.fullmatch(args.candidate_commit) or not _COMMIT.fullmatch(args.checked_out_commit):
        raise SystemExit("candidate and checked-out commits must be 40-hex")
    if args.mode == "exact" and (not args.candidate_tag or args.candidate_commit != args.checked_out_commit):
        raise SystemExit("exact mode requires a tag and matching candidate/checked-out commits")
    e2e = json.loads(args.e2e.read_text(encoding="utf-8"))
    if e2e.get("result") != "PASS":
        raise SystemExit("product E2E did not report PASS")
    failure_e2e = json.loads(args.failure_e2e.read_text(encoding="utf-8"))
    if failure_e2e.get("result") != "PASS":
        raise SystemExit("product failure-recovery E2E did not report PASS")
    manifest = json.loads(args.manifest.read_text(encoding="utf-8"))
    source = manifest.get("source", {})
    if source.get("commit") != args.checked_out_commit:
        raise SystemExit("release manifest commit does not match checkout")
    if args.mode == "exact" and args.candidate_tag not in source.get("tags", []):
        raise SystemExit("release manifest does not record candidate tag")

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps({
        "evidence_version": 1,
        "mode": args.mode,
        "candidate_tag": args.candidate_tag,
        "candidate_commit": args.candidate_commit,
        "checked_out_commit": args.checked_out_commit,
        "project_version": args.project_version,
        "wheel_sha256": digest(args.wheel),
        "sdist_sha256": digest(args.sdist),
        "release_manifest_sha256": digest(args.manifest),
        "sbom_sha256": digest(args.sbom),
        "product_e2e_result": e2e["result"],
        "product_e2e_sha256": digest(args.e2e),
        "product_failure_e2e_result": failure_e2e["result"],
        "product_failure_e2e_sha256": digest(args.failure_e2e),
        "workflow_run_identity": args.workflow_run_identity,
        "qualification_claim": (
            "SOFTWARE_CANDIDATE_GATE_ONLY" if args.mode == "exact" else "PREFLIGHT_ONLY"
        ),
    }, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
