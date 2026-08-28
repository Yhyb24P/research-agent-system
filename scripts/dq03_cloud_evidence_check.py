"""Validate the metadata and privacy shape of a DQ03 staging report."""

import argparse
import json
from pathlib import Path
from typing import Any


REQUIRED_METADATA = ("provider", "model", "tested_at_utc", "credential_reference", "retention_policy")
FORBIDDEN_KEYS = {"api_key", "apikey", "password", "secret", "access_token", "refresh_token"}


def _contains_forbidden_key(value: Any) -> bool:
    if isinstance(value, dict):
        return any(
            (str(key).lower() in FORBIDDEN_KEYS or str(key).lower().endswith(("_secret", "_token", "_api_key")))
            or _contains_forbidden_key(item)
            for key, item in value.items()
        )
    if isinstance(value, list):
        return any(_contains_forbidden_key(item) for item in value)
    return False


def validate(path: Path) -> dict[str, Any]:
    try:
        report = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError(f"cannot read cloud evidence: {path}") from error
    if not isinstance(report, dict):
        raise ValueError("cloud evidence root must be an object")
    metadata = report.get("metadata")
    failures: list[str] = []
    if not isinstance(metadata, dict):
        failures.append("metadata_missing")
    else:
        failures.extend(f"metadata_{field}_missing" for field in REQUIRED_METADATA if not metadata.get(field))
    if not isinstance(report.get("release_commit"), str) or not report["release_commit"]:
        failures.append("release_commit_missing")
    if not isinstance(report.get("scenarios"), list) or not report["scenarios"]:
        failures.append("scenarios_missing")
    if report.get("passed") is not True:
        failures.append("report_not_passed")
    if _contains_forbidden_key(report):
        failures.append("credential_material_present")
    return {"evidence_version": 1, "source": str(path), "failures": failures, "passed": not failures}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        result = validate(args.input)
    except ValueError as error:
        parser.error(str(error))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result))
    return 0 if result["passed"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
