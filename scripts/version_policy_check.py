#!/usr/bin/env python3
"""Validate the one-to-one Git tag and Python distribution release policy.

Historical qualification evidence is immutable.  New product candidates use
``vX.Y.Z-rc.N`` Git tags and the PEP 440 distribution version ``X.Y.ZrcN``;
the final release maps ``vX.Y.Z`` to ``X.Y.Z``.  Branch commits may be
untagged, but a requested candidate tag must map exactly to project metadata.
Developer Preview versions use ``X.Y.ZrcN.devM`` and must remain untagged;
they bind publication through the immutable Preview manifest instead.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import tomllib
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
_RC_TAG = re.compile(r"^v(?P<version>\d+\.\d+\.\d+)-rc\.(?P<number>[1-9]\d*)$")
_FINAL_TAG = re.compile(r"^v(?P<version>\d+\.\d+\.\d+)$")
_RC_VERSION = re.compile(r"^(?P<version>\d+\.\d+\.\d+)rc(?P<number>[1-9]\d*)$")
_PREVIEW_VERSION = re.compile(
    r"^(?P<version>\d+\.\d+\.\d+)rc(?P<number>[1-9]\d*)\.dev(?P<dev>\d+)$"
)
_FINAL_VERSION = re.compile(r"^\d+\.\d+\.\d+$")


def project_version() -> str:
    with (ROOT / "pyproject.toml").open("rb") as stream:
        project = tomllib.load(stream).get("project", {})
    version = project.get("version") if isinstance(project, dict) else None
    if not isinstance(version, str):
        raise RuntimeError("project.version is missing")
    return version


def tag_for_version(version: str) -> str | None:
    if _PREVIEW_VERSION.fullmatch(version):
        return None
    if match := _RC_VERSION.fullmatch(version):
        return f"v{match.group('version')}-rc.{match.group('number')}"
    if _FINAL_VERSION.fullmatch(version):
        return f"v{version}"
    raise ValueError("project.version must be X.Y.ZrcN.devM, X.Y.ZrcN, or X.Y.Z")


def version_for_tag(tag: str) -> str:
    if match := _RC_TAG.fullmatch(tag):
        return f"{match.group('version')}rc{match.group('number')}"
    if match := _FINAL_TAG.fullmatch(tag):
        return match.group("version")
    raise ValueError("release tag must be vX.Y.Z-rc.N or vX.Y.Z")


def head_tags() -> list[str]:
    output = subprocess.check_output(
        ("git", "tag", "--points-at", "HEAD"), cwd=ROOT, text=True,
    )
    return sorted(
        tag for tag in output.splitlines()
        if _RC_TAG.fullmatch(tag) or _FINAL_TAG.fullmatch(tag)
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--candidate-tag", help="reserved or applied candidate tag to validate")
    parser.add_argument("--require-head-tag", action="store_true")
    args = parser.parse_args()

    version = project_version()
    expected_tag = tag_for_version(version)
    release_kind = "developer-preview" if expected_tag is None else "release"
    failures: list[str] = []
    if args.candidate_tag is not None:
        try:
            requested_version = version_for_tag(args.candidate_tag)
        except ValueError as error:
            failures.append(str(error))
        else:
            if expected_tag is None:
                failures.append(
                    "Developer Preview versions cannot be mapped to a release tag"
                )
            elif requested_version != version:
                failures.append(
                    f"candidate tag {args.candidate_tag} maps to {requested_version}, "
                    f"not project.version {version}"
                )
    tags = head_tags()
    if len(tags) > 1:
        failures.append("HEAD has more than one release tag")
    elif expected_tag is None and tags:
        failures.append("Developer Preview HEAD must not carry a release tag")
    elif tags and tags[0] != expected_tag:
        failures.append(
            f"HEAD tag {tags[0]} does not match project.version {version} "
            f"(expected {expected_tag})"
        )
    if args.require_head_tag:
        if expected_tag is None:
            failures.append("Developer Preview versions cannot require a release tag")
        elif tags != [expected_tag]:
            failures.append(f"HEAD must have exactly the release tag {expected_tag}")

    result = {
        "valid": not failures,
        "project_version": version,
        "release_kind": release_kind,
        "expected_tag": expected_tag,
        "head_release_tags": tags,
        "failures": failures,
    }
    print(json.dumps(result, sort_keys=True))
    return 0 if not failures else 1


if __name__ == "__main__":
    raise SystemExit(main())
