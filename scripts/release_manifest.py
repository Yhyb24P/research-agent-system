#!/usr/bin/env python3
"""Emit an auditable, dependency-free release baseline manifest.

The manifest intentionally records facts about the checked-out source and host;
it does not contain credentials, environment variables, prompts, or artifacts.
Run it from the repository root with the environment used for qualification.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import platform
import re
import shutil
import subprocess
import sys
import tomllib
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
MIGRATIONS = ROOT / "src" / "researchd" / "storage" / "migrations" / "versions"


def run(*args: str) -> str:
    return subprocess.check_output(args, cwd=ROOT, text=True, stderr=subprocess.STDOUT).strip()


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def schema_head() -> str:
    revisions: dict[str, str | None] = {}
    pattern = re.compile(
        r"^(revision|down_revision)(?:\s*:\s*[^=]+)?\s*=\s*(.*)$"
    )
    for path in sorted(MIGRATIONS.glob("*.py")):
        revision: str | None = None
        down_revision: str | None = None
        for line in path.read_text().splitlines():
            match = pattern.match(line.strip())
            if not match:
                continue
            if match.group(1) == "revision":
                revision = match.group(2).strip().strip("'\"")
            else:
                value = match.group(2).strip()
                down_revision = None if value in {"None", "null"} else value.strip("'\"")
        if revision:
            revisions[revision] = down_revision
    referenced = {down for down in revisions.values() if down is not None}
    heads = sorted(set(revisions) - referenced)
    if len(heads) != 1:
        raise RuntimeError(f"expected one migration head, found {heads}")
    return heads[0]


def package_versions() -> dict[str, str]:
    names = ("research-agent-system", "alembic", "httpx", "pydantic", "sqlalchemy", "pytest", "mypy")
    result: dict[str, str] = {}
    for name in names:
        try:
            result[name] = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            result[name] = "not-installed"
    return result


def lock_inventory() -> dict[str, Any]:
    with (ROOT / "uv.lock").open("rb") as stream:
        lock = tomllib.load(stream)
    packages = []
    for package in lock.get("package", []):
        if not isinstance(package, dict) or not package.get("name") or not package.get("version"):
            continue
        packages.append({
            "name": package["name"],
            "version": package["version"],
            "source": package.get("source", {}),
            "dependencies": sorted(
                item["name"] for item in package.get("dependencies", [])
                if isinstance(item, dict) and item.get("name")
            ),
        })
    return {
        "requires_python": lock.get("requires-python"),
        "package_count": len(packages),
        "packages": sorted(packages, key=lambda item: (str(item["name"]), str(item["version"]))),
    }


def build_manifest(require_clean: bool) -> dict[str, Any]:
    status = run("git", "status", "--porcelain")
    if require_clean and status:
        raise RuntimeError("working tree is dirty; commit the release baseline before generating a manifest")
    bwrap = shutil.which("bwrap")
    tracked = run("git", "ls-files", "-z").split("\x00")
    tracked = [item for item in tracked if item]
    line_count = sum((ROOT / item).read_text(errors="replace").count("\n") for item in tracked)
    return {
        "manifest_version": 2,
        "captured_at_utc": datetime.now(UTC).isoformat(),
        "source": {
            "commit": run("git", "rev-parse", "HEAD"),
            "tags": [tag for tag in run("git", "tag", "--points-at", "HEAD").splitlines() if tag],
            "branch": run("git", "branch", "--show-current"),
            "working_tree": "clean" if not status else "dirty",
            "tracked_file_count": len(tracked),
            "tracked_line_count": line_count,
        },
        "dependencies": {
            "uv_lock_sha256": sha256(ROOT / "uv.lock"),
            "packages": package_versions(),
            "uv_lock_inventory": lock_inventory(),
            "python": sys.version.split()[0],
        },
        "schema": {"alembic_head": schema_head()},
        "host": {
            "os": platform.platform(),
            "kernel": platform.release(),
            "architecture": platform.machine(),
            "bubblewrap": run(bwrap, "--version") if bwrap else "not-installed",
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, help="write JSON to this path instead of stdout")
    parser.add_argument("--allow-dirty", action="store_true", help="record a dirty tree as pending, instead of failing")
    args = parser.parse_args()
    manifest = build_manifest(require_clean=not args.allow_dirty)
    rendered = json.dumps(manifest, ensure_ascii=False, indent=2) + "\n"
    if args.output:
        args.output.write_text(rendered)
    else:
        print(rendered, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
