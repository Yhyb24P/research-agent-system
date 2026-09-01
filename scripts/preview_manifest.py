#!/usr/bin/env python3
"""Create the immutable publication manifest consumed by install-preview.sh."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import tomllib
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
COMMIT = re.compile(r"^[0-9a-f]{40}$")
RC_TAG = re.compile(r"^v[0-9]+\.[0-9]+\.[0-9]+-rc\.[0-9A-Za-z.-]+$")
PREVIEW_VERSION = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+rc[0-9]+\.dev[0-9]+$")


def _git(*args: str) -> str:
    return subprocess.check_output(
        ("git", *args), cwd=ROOT, text=True, stderr=subprocess.STDOUT,
    ).strip()


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _version() -> str:
    with (ROOT / "pyproject.toml").open("rb") as stream:
        value = tomllib.load(stream)["project"]["version"]
    if not isinstance(value, str) or PREVIEW_VERSION.fullmatch(value) is None:
        raise RuntimeError("project version is not a Developer Preview version")
    return value


def build_manifest(
    wheel: Path,
    *,
    wheel_url: str,
    source_candidate_commit: str,
    source_candidate_tag: str,
) -> dict[str, object]:
    if _git("status", "--porcelain", "--untracked-files=no"):
        raise RuntimeError("tracked working tree must be clean")
    if not wheel.is_file() or wheel.suffix != ".whl":
        raise RuntimeError("wheel must be an existing .whl file")
    if not wheel_url.startswith("https://"):
        raise RuntimeError("wheel URL must use HTTPS")
    if COMMIT.fullmatch(source_candidate_commit) is None:
        raise RuntimeError("source candidate commit must be 40 lowercase hex characters")
    if RC_TAG.fullmatch(source_candidate_tag) is None:
        raise RuntimeError("source candidate tag is invalid")
    resolved = _git("rev-parse", f"refs/tags/{source_candidate_tag}^{{}}")
    if resolved != source_candidate_commit:
        raise RuntimeError("source candidate tag does not resolve to the supplied commit")
    return {
        "manifest_version": 1,
        "channel": "preview",
        "version": _version(),
        "preview_commit": _git("rev-parse", "HEAD"),
        "source_candidate": {
            "commit": source_candidate_commit,
            "tag": source_candidate_tag,
        },
        "wheel": {
            "filename": wheel.name,
            "url": wheel_url,
            "sha256": _sha256(wheel),
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--wheel", type=Path, required=True)
    parser.add_argument("--wheel-url", required=True)
    parser.add_argument("--source-candidate-commit", required=True)
    parser.add_argument("--source-candidate-tag", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    payload = build_manifest(
        args.wheel.resolve(strict=True),
        wheel_url=args.wheel_url,
        source_candidate_commit=args.source_candidate_commit,
        source_candidate_tag=args.source_candidate_tag,
    )
    args.output.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
