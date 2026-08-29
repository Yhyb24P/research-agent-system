#!/usr/bin/env python3
"""Probe hash, atomic replacement, directory durability, and SQLite assumptions."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import subprocess
import tempfile
import threading
from pathlib import Path
from typing import Any
from uuid import uuid4


ROOT = Path(__file__).resolve().parents[1]


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _findmnt(path: Path) -> str | None:
    try:
        return subprocess.check_output(
            ("findmnt", "-n", "-T", str(path), "-o", "TARGET,SOURCE,FSTYPE,OPTIONS"),
            text=True,
            stderr=subprocess.STDOUT,
        ).strip()
    except (OSError, subprocess.CalledProcessError):
        return None


def _durable_replace(target: Path, data: bytes) -> None:
    temporary = target.parent / f".{target.name}.{uuid4().hex}.tmp"
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, target)
        directory = os.open(target.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        temporary.unlink(missing_ok=True)


def _atomic_visibility(root: Path, *, iterations: int) -> dict[str, Any]:
    target = root / "atomic-target.bin"
    payloads = (b"A" * 131_072, b"B" * 131_072)
    allowed = {_sha256(payload) for payload in payloads}
    _durable_replace(target, payloads[0])
    observed: set[str] = set()
    violations: list[str] = []
    stop = threading.Event()

    def read_replacements() -> None:
        while not stop.is_set():
            try:
                digest = _sha256(target.read_bytes())
            except FileNotFoundError:
                violations.append("replacement target disappeared")
                continue
            observed.add(digest)
            if digest not in allowed:
                violations.append(f"partial or unknown replacement hash: {digest}")

    reader = threading.Thread(target=read_replacements, name="dq01-atomic-reader")
    reader.start()
    try:
        for index in range(iterations):
            _durable_replace(target, payloads[index % 2])
    finally:
        stop.set()
        reader.join(timeout=10)
    if reader.is_alive():
        violations.append("atomic replacement reader did not terminate")
    return {
        "iterations": iterations,
        "allowed_sha256": sorted(allowed),
        "observed_sha256": sorted(observed),
        "violations": sorted(set(violations)),
        "passed": bool(observed) and not violations,
    }


def _sqlite_semantics(root: Path) -> dict[str, Any]:
    path = root / "probe.db"
    connection = sqlite3.connect(path)
    journal_mode = str(connection.execute("PRAGMA journal_mode=WAL").fetchone()[0]).lower()
    connection.execute("PRAGMA synchronous=FULL")
    synchronous = int(connection.execute("PRAGMA synchronous").fetchone()[0])
    connection.execute("CREATE TABLE probe (id INTEGER PRIMARY KEY, value TEXT NOT NULL)")
    connection.execute("INSERT INTO probe VALUES (1, 'committed-before-reopen')")
    connection.commit()
    connection.execute("BEGIN IMMEDIATE")
    connection.execute("INSERT INTO probe VALUES (2, 'must-roll-back')")
    connection.rollback()
    connection.close()

    reopened = sqlite3.connect(path)
    integrity = str(reopened.execute("PRAGMA integrity_check").fetchone()[0])
    rows_after_rollback = reopened.execute("SELECT id, value FROM probe ORDER BY id").fetchall()
    reopened.execute("INSERT INTO probe VALUES (3, 'committed-after-reopen')")
    reopened.commit()
    reopened.close()

    final = sqlite3.connect(path)
    final_rows = final.execute("SELECT id, value FROM probe ORDER BY id").fetchall()
    final_integrity = str(final.execute("PRAGMA integrity_check").fetchone()[0])
    final.close()
    passed = (
        journal_mode == "wal"
        and synchronous == 2
        and integrity == "ok"
        and rows_after_rollback == [(1, "committed-before-reopen")]
        and final_rows == [
            (1, "committed-before-reopen"),
            (3, "committed-after-reopen"),
        ]
        and final_integrity == "ok"
    )
    return {
        "journal_mode": journal_mode,
        "synchronous": synchronous,
        "integrity_before_commit": integrity,
        "integrity_after_commit": final_integrity,
        "rows_after_rollback": rows_after_rollback,
        "final_rows": final_rows,
        "passed": passed,
    }


def probe(root: Path, *, iterations: int) -> dict[str, Any]:
    target_root = root.resolve(strict=True)
    if not target_root.is_dir():
        raise ValueError("probe root must be a directory")
    with tempfile.TemporaryDirectory(prefix=".dq01-filesystem-", dir=target_root) as temporary:
        probe_root = Path(temporary)
        roundtrip = b"researchd-dq01-hash-roundtrip\x00" * 4096
        roundtrip_target = probe_root / "roundtrip.bin"
        _durable_replace(roundtrip_target, roundtrip)
        expected = _sha256(roundtrip)
        observed = _sha256(roundtrip_target.read_bytes())
        atomic = _atomic_visibility(probe_root, iterations=iterations)
        sqlite = _sqlite_semantics(probe_root)
        checks = {
            "hash_roundtrip": expected == observed,
            "atomic_replace_visibility": atomic["passed"],
            "sqlite_wal_full_integrity": sqlite["passed"],
        }
        return {
            "evidence_version": 1,
            "release_commit": subprocess.check_output(
                ("git", "-C", str(ROOT), "rev-parse", "HEAD"),
                text=True,
                stderr=subprocess.STDOUT,
            ).strip(),
            "target_root": str(target_root),
            "filesystem": _findmnt(target_root),
            "hash_roundtrip": {"expected_sha256": expected, "observed_sha256": observed},
            "atomic_replace": atomic,
            "sqlite": sqlite,
            "checks": checks,
            "failures": [name for name, passed in checks.items() if not passed],
            "passed": all(checks.values()),
        }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", required=True, type=Path, help="deployment filesystem directory to probe")
    parser.add_argument("--iterations", type=int, default=256)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if args.iterations < 2:
        parser.error("--iterations must be at least 2")
    report = probe(args.root, iterations=args.iterations)
    rendered = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    print(rendered, end="")
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered, encoding="utf-8")
    return 0 if report["passed"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
