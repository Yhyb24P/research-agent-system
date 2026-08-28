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
    shutil.copytree(artifact_root, destination_artifacts, dirs_exist_ok=True)
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
