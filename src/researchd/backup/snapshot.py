"""Recoverable SQLite + content-addressed artifact snapshots."""

import hashlib
import json
import shutil
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any


class BackupError(RuntimeError):
    pass


@dataclass(frozen=True)
class BackupManifest:
    format_version: int
    database_sha256: str
    artifact_files: dict[str, str]

    def as_dict(self) -> dict[str, Any]:
        return {"format_version": self.format_version, "database_sha256": self.database_sha256, "artifact_files": dict(self.artifact_files)}


def backup_snapshot(database: Path, artifact_root: Path, destination: Path) -> BackupManifest:
    database = database.resolve(strict=True)
    artifact_root = artifact_root.resolve(strict=True)
    destination = destination.resolve()
    if destination.exists():
        raise BackupError("backup destination already exists")
    destination.mkdir(parents=True)
    destination_artifacts = destination / "artifacts"
    destination_artifacts.mkdir()
    database_copy = destination / "research.db"
    source = sqlite3.connect(str(database))
    target = sqlite3.connect(str(database_copy))
    try:
        source.execute("PRAGMA wal_checkpoint(FULL)")
        source.backup(target)
        target.commit()
    finally:
        target.close()
        source.close()
    referenced = _referenced_artifact_digests(database_copy)
    for digest in referenced:
        source_artifact = artifact_root / "sha256" / digest[:2] / digest
        if not source_artifact.is_file() or source_artifact.is_symlink():
            raise BackupError(f"referenced artifact is missing or unsafe: {digest}")
        destination_artifact = destination_artifacts / "sha256" / digest[:2] / digest
        destination_artifact.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source_artifact, destination_artifact)
    files = {
        str(path.relative_to(destination_artifacts)): _sha256(path)
        for path in sorted(destination_artifacts.rglob("*")) if path.is_file()
    }
    manifest = BackupManifest(1, _sha256(database_copy), files)
    (destination / "manifest.json").write_text(json.dumps(manifest.as_dict(), sort_keys=True, indent=2) + "\n")
    return manifest


def restore_snapshot(snapshot: Path, database_destination: Path, artifact_destination: Path) -> BackupManifest:
    snapshot = snapshot.resolve(strict=True)
    manifest_path = snapshot / "manifest.json"
    database_source = snapshot / "research.db"
    artifacts_source = snapshot / "artifacts"
    if not manifest_path.is_file() or not database_source.is_file() or not artifacts_source.is_dir():
        raise BackupError("snapshot is incomplete")
    try:
        raw = json.loads(manifest_path.read_text())
        manifest = BackupManifest(int(raw["format_version"]), str(raw["database_sha256"]), dict(raw["artifact_files"]))
    except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError) as error:
        raise BackupError("snapshot manifest is invalid") from error
    if manifest.format_version != 1 or _sha256(database_source) != manifest.database_sha256:
        raise BackupError("snapshot database checksum mismatch")
    actual_files = {str(path.relative_to(artifacts_source)): _sha256(path) for path in sorted(artifacts_source.rglob("*")) if path.is_file()}
    if actual_files != manifest.artifact_files:
        raise BackupError("snapshot artifact checksum mismatch")
    if _referenced_artifact_files(database_source) != set(manifest.artifact_files):
        raise BackupError("snapshot database/artifact reference mismatch")
    database_destination = database_destination.resolve()
    artifact_destination = artifact_destination.resolve()
    if database_destination.exists() or artifact_destination.exists():
        raise BackupError("restore destinations must not already exist")
    database_destination.parent.mkdir(parents=True, exist_ok=True)
    artifact_destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(database_source, database_destination)
    shutil.copytree(artifacts_source, artifact_destination)
    return manifest


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _referenced_artifact_digests(database: Path) -> set[str]:
    try:
        with sqlite3.connect(str(database)) as connection:
            rows = connection.execute("SELECT artifact_id FROM artifacts").fetchall()
    except sqlite3.Error as error:
        raise BackupError("snapshot artifact table is unreadable") from error
    digests: set[str] = set()
    for (artifact_id,) in rows:
        if not isinstance(artifact_id, str) or not artifact_id.startswith("artifact://sha256/"):
            raise BackupError("database contains an invalid artifact ID")
        digest = artifact_id.removeprefix("artifact://sha256/")
        if len(digest) != 64 or any(character not in "0123456789abcdef" for character in digest):
            raise BackupError("database contains an invalid artifact hash")
        digests.add(digest)
    return digests


def _referenced_artifact_files(database: Path) -> set[str]:
    return {f"sha256/{digest[:2]}/{digest}" for digest in _referenced_artifact_digests(database)}
