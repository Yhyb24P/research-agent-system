"""Fail-closed SQLite and content-addressed artifact snapshots."""

import hashlib
import json
import os
import re
import shutil
import sqlite3
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

BACKUP_FORMAT_VERSION = 3

_COMMIT_PATTERN = re.compile(r"^[0-9a-f]{40}$")
_TAG_PATTERN = re.compile(r"^v[0-9]+\.[0-9]+\.[0-9]+-rc\.[0-9A-Za-z.-]+$")
_DIGEST_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_MANIFEST_KEYS = {
    "format_version",
    "database_sha256",
    "artifact_files",
    "candidate_commit",
    "candidate_tag",
    "orphan_artifact_digests",
    "schema_revision",
    "created_at_utc",
}

# Every authoritative table created by migrations 0001-0021 (alembic_version
# excluded). A migration that changes this set must update the restore gate.
AUTHORITATIVE_TABLES: tuple[str, ...] = (
    "agent_interactions",
    "agent_invocations",
    "agent_runtime_lease_events",
    "agent_runtimes",
    "agents",
    "approval_grants",
    "approval_requests",
    "artifact_derivations",
    "artifacts",
    "attempt_worktrees",
    "attempts",
    "audit_events",
    "audit_stream_clock",
    "claims",
    "cloud_interaction_governance",
    "collaboration_messages",
    "delegations",
    "daemon_commands",
    "execution_steps",
    "executor_dispatches",
    "gpu_leases",
    "jobs",
    "observations",
    "plans",
    "policy_decisions",
    "research_runs",
    "review_decisions",
    "runtime_session_commands",
    "runtime_sessions",
    "verification_results",
    "work_orders",
    "workspace_grants",
    "workspace_reconciliations",
    "workspace_snapshots",
    "workspace_transports",
    "workspaces",
)
AUTHORITATIVE_TRIGGERS: tuple[str, ...] = (
    "artifacts_classification_immutable",
    "artifacts_metadata_immutable",
    "audit_events_assign_seq",
    "cloud_interaction_governance_immutable",
    "observations_immutable",
    "verification_results_immutable",
)

# SQLite foreign_key_check covers declared constraints. These checks cover the
# remaining plain-string authoritative references.
_PLAIN_REFERENCE_CHECKS: tuple[tuple[str, str], ...] = (
    (
        "work_orders.approval_id->approval_requests",
        "SELECT COUNT(*) FROM work_orders WHERE approval_id IS NOT NULL "
        "AND approval_id NOT IN (SELECT approval_id FROM approval_requests)",
    ),
    (
        "work_orders.approval_grant_id->approval_grants",
        "SELECT COUNT(*) FROM work_orders WHERE approval_grant_id IS NOT NULL "
        "AND approval_grant_id NOT IN (SELECT grant_id FROM approval_grants)",
    ),
    (
        "agent_interactions.attempt_id->attempts",
        "SELECT COUNT(*) FROM agent_interactions WHERE attempt_id IS NOT NULL "
        "AND attempt_id NOT IN (SELECT attempt_id FROM attempts)",
    ),
)


class BackupError(RuntimeError):
    pass


@dataclass(frozen=True)
class BackupManifest:
    format_version: int
    database_sha256: str
    artifact_files: dict[str, str]
    candidate_commit: str
    candidate_tag: str
    orphan_artifact_digests: tuple[str, ...]
    schema_revision: str
    created_at_utc: str

    @property
    def orphan_count(self) -> int:
        return len(self.orphan_artifact_digests)

    def as_dict(self) -> dict[str, Any]:
        return {
            "format_version": self.format_version,
            "database_sha256": self.database_sha256,
            "artifact_files": dict(self.artifact_files),
            "candidate_commit": self.candidate_commit,
            "candidate_tag": self.candidate_tag,
            "orphan_artifact_digests": list(self.orphan_artifact_digests),
            "schema_revision": self.schema_revision,
            "created_at_utc": self.created_at_utc,
        }


@dataclass(frozen=True)
class RestoreHealthReport:
    schema_revision: str
    table_counts: dict[str, int]
    artifacts_verified: int
    missing_count: int
    corrupt_count: int
    orphan_count: int

    @property
    def healthy(self) -> bool:
        return self.missing_count == 0 and self.corrupt_count == 0 and self.orphan_count == 0


def backup_snapshot(
    database: Path,
    artifact_root: Path,
    destination: Path,
    *,
    candidate_commit: str,
    candidate_tag: str,
) -> BackupManifest:
    """Create a current-format snapshot without leaving a partial destination."""
    _validate_candidate(candidate_commit, candidate_tag)
    database = _regular_file(database, "database")
    artifact_root = _directory(artifact_root, "artifact root")
    destination = _new_path(destination, "backup destination")

    staging = Path(tempfile.mkdtemp(prefix=f".{destination.name}.staging-", dir=destination.parent))
    try:
        artifacts_copy = staging / "artifacts"
        artifacts_copy.mkdir()
        database_copy = staging / "research.db"
        source = sqlite3.connect(str(database))
        target = sqlite3.connect(str(database_copy))
        try:
            source.execute("PRAGMA wal_checkpoint(FULL)")
            source.backup(target)
            target.commit()
            # A portable snapshot is a single SQLite file. Do not preserve WAL
            # mode in the copy, otherwise read-only validation creates sidecars
            # inside the supposedly immutable snapshot tree.
            target.execute("PRAGMA journal_mode=DELETE")
        finally:
            target.close()
            source.close()

        referenced = _referenced_artifact_digests(database_copy)
        cas_files = _cas_files(artifact_root)
        missing = referenced - set(cas_files)
        if missing:
            raise BackupError(f"referenced artifact is missing or unsafe: {sorted(missing)[0]}")
        for digest in sorted(referenced):
            source_artifact = cas_files[digest]
            if _sha256(source_artifact) != digest:
                raise BackupError(f"referenced artifact content does not match its digest: {digest}")
            destination_artifact = artifacts_copy / "sha256" / digest[:2] / digest
            destination_artifact.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source_artifact, destination_artifact)
            if _sha256(destination_artifact) != digest:
                raise BackupError(f"copied artifact content does not match its digest: {digest}")

        artifact_files = {_artifact_relative_path(digest): digest for digest in sorted(referenced)}
        manifest = BackupManifest(
            format_version=BACKUP_FORMAT_VERSION,
            database_sha256=_sha256(database_copy),
            artifact_files=artifact_files,
            candidate_commit=candidate_commit,
            candidate_tag=candidate_tag,
            orphan_artifact_digests=tuple(sorted(set(cas_files) - referenced)),
            schema_revision=_schema_revision(database_copy),
            created_at_utc=datetime.now(UTC).isoformat(),
        )
        (staging / "manifest.json").write_text(
            json.dumps(manifest.as_dict(), sort_keys=True, indent=2) + "\n",
            encoding="utf-8",
        )
        if destination.exists():
            raise BackupError("backup destination already exists")
        staging.rename(destination)
        return manifest
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise


def restore_snapshot(
    snapshot: Path,
    database_destination: Path,
    artifact_destination: Path,
    *,
    expected_candidate_commit: str,
    expected_candidate_tag: str,
) -> BackupManifest:
    """Validate a snapshot completely, then restore it to new destinations."""
    _validate_candidate(expected_candidate_commit, expected_candidate_tag)
    snapshot = _directory(snapshot, "snapshot")
    _validate_snapshot_tree(snapshot)
    manifest = _load_manifest(snapshot / "manifest.json")
    if (
        manifest.candidate_commit != expected_candidate_commit
        or manifest.candidate_tag != expected_candidate_tag
    ):
        raise BackupError("snapshot candidate identity mismatch")

    database_source = _regular_file(snapshot / "research.db", "snapshot database", single_link=True)
    artifacts_source = _directory(snapshot / "artifacts", "snapshot artifacts")
    if _sha256(database_source) != manifest.database_sha256:
        raise BackupError("snapshot database checksum mismatch")
    if manifest.schema_revision != _schema_revision(database_source):
        raise BackupError("snapshot schema revision mismatch")

    actual_files = _snapshot_artifact_files(artifacts_source)
    if actual_files != manifest.artifact_files:
        raise BackupError("snapshot artifact checksum mismatch")
    if _referenced_artifact_files(database_source) != set(manifest.artifact_files):
        raise BackupError("snapshot database/artifact reference mismatch")
    source_health = check_restored_snapshot(database_source, artifacts_source)
    if not source_health.healthy:
        raise BackupError("snapshot artifact inventory is unhealthy")

    database_destination = _new_path(database_destination, "restore database destination")
    artifact_destination = _new_path(artifact_destination, "restore artifact destination")
    try:
        shutil.copy2(database_source, database_destination)
        shutil.copytree(artifacts_source, artifact_destination)
        if _sha256(database_destination) != manifest.database_sha256:
            raise BackupError("restored database checksum mismatch")
        restored_health = check_restored_snapshot(database_destination, artifact_destination)
        if not restored_health.healthy:
            raise BackupError("restored snapshot is unhealthy")
    except Exception:
        database_destination.unlink(missing_ok=True)
        if artifact_destination.exists():
            shutil.rmtree(artifact_destination)
        raise
    return manifest


def check_restored_snapshot(database: Path, artifact_root: Path) -> RestoreHealthReport:
    """Run database, relationship, and CAS checks on restored state."""
    database = _regular_file(database, "restored database")
    artifact_root = _directory(artifact_root, "restored artifact root")
    try:
        with sqlite3.connect(f"file:{database}?mode=ro", uri=True) as connection:
            integrity = connection.execute("PRAGMA integrity_check").fetchone()
            if integrity != ("ok",):
                raise BackupError("restored database integrity check failed")
            if connection.execute("PRAGMA foreign_key_check").fetchall():
                raise BackupError("restored database foreign-key check failed")
            available_tables = {
                str(row[0])
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                ).fetchall()
            }
            missing_tables = set(AUTHORITATIVE_TABLES) - available_tables
            if missing_tables:
                raise BackupError(
                    f"restored database is missing authoritative table: {sorted(missing_tables)[0]}"
                )
            available_triggers = {
                str(row[0])
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'trigger'"
                ).fetchall()
            }
            missing_triggers = set(AUTHORITATIVE_TRIGGERS) - available_triggers
            if missing_triggers:
                raise BackupError(
                    f"restored database is missing authoritative trigger: {sorted(missing_triggers)[0]}"
                )
            counts = {
                table: int(connection.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0])
                for table in AUTHORITATIVE_TABLES
            }
            for label, statement in _PLAIN_REFERENCE_CHECKS:
                if int(connection.execute(statement).fetchone()[0]):
                    raise BackupError(f"restored database has orphan references: {label}")
    except (OSError, sqlite3.Error, TypeError, IndexError) as error:
        if isinstance(error, BackupError):
            raise
        raise BackupError("restored database health check failed") from error

    digests = _referenced_artifact_digests(database)
    cas_files = _cas_files(artifact_root)
    missing = digests - set(cas_files)
    corrupt = {digest for digest in digests & set(cas_files) if _sha256(cas_files[digest]) != digest}
    verified = len(digests - missing - corrupt)
    orphans = set(cas_files) - digests
    return RestoreHealthReport(
        schema_revision=_schema_revision(database),
        table_counts=counts,
        artifacts_verified=verified,
        missing_count=len(missing),
        corrupt_count=len(corrupt),
        orphan_count=len(orphans),
    )


def snapshot_size_bytes(snapshot: Path) -> int:
    """Return the total regular-file size for a validated snapshot tree."""
    snapshot = _directory(snapshot, "snapshot")
    _validate_snapshot_tree(snapshot)
    return sum(path.stat().st_size for path in snapshot.rglob("*") if path.is_file())


def read_snapshot_manifest(snapshot: Path) -> BackupManifest:
    """Read a current-format manifest after validating its snapshot tree."""
    snapshot = _directory(snapshot, "snapshot")
    _validate_snapshot_tree(snapshot)
    return _load_manifest(snapshot / "manifest.json")


def _load_manifest(path: Path) -> BackupManifest:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict) or set(raw) != _MANIFEST_KEYS:
            raise ValueError("manifest fields do not match the current format")
        if int(raw["format_version"]) != BACKUP_FORMAT_VERSION:
            raise ValueError("unsupported snapshot format version")
        artifact_files = dict(raw["artifact_files"])
        orphans = tuple(str(value) for value in raw["orphan_artifact_digests"])
        manifest = BackupManifest(
            format_version=int(raw["format_version"]),
            database_sha256=str(raw["database_sha256"]),
            artifact_files={str(key): str(value) for key, value in artifact_files.items()},
            candidate_commit=str(raw["candidate_commit"]),
            candidate_tag=str(raw["candidate_tag"]),
            orphan_artifact_digests=orphans,
            schema_revision=str(raw["schema_revision"]),
            created_at_utc=str(raw["created_at_utc"]),
        )
        _validate_candidate(manifest.candidate_commit, manifest.candidate_tag)
        if not _DIGEST_PATTERN.fullmatch(manifest.database_sha256):
            raise ValueError("invalid database digest")
        timestamp = datetime.fromisoformat(manifest.created_at_utc.replace("Z", "+00:00"))
        if timestamp.tzinfo is None:
            raise ValueError("manifest timestamp must include a timezone")
        if any(not _DIGEST_PATTERN.fullmatch(digest) for digest in manifest.orphan_artifact_digests):
            raise ValueError("invalid orphan artifact digest")
        for relative, digest in manifest.artifact_files.items():
            if relative != _artifact_relative_path(digest):
                raise ValueError("artifact manifest path is not canonical")
        return manifest
    except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError) as error:
        raise BackupError("snapshot manifest is invalid") from error


def _validate_snapshot_tree(snapshot: Path) -> None:
    expected = {"manifest.json", "research.db", "artifacts"}
    if {entry.name for entry in snapshot.iterdir()} != expected:
        raise BackupError("snapshot contains unexpected or missing top-level entries")
    _regular_file(snapshot / "manifest.json", "snapshot manifest", single_link=True)
    _regular_file(snapshot / "research.db", "snapshot database", single_link=True)
    artifacts = _directory(snapshot / "artifacts", "snapshot artifacts")
    for root, directories, files in os.walk(artifacts, followlinks=False):
        root_path = Path(root)
        for name in directories:
            if (root_path / name).is_symlink():
                raise BackupError("snapshot tree contains a symlink")
        for name in files:
            _regular_file(root_path / name, "snapshot artifact", single_link=True)


def _snapshot_artifact_files(artifact_root: Path) -> dict[str, str]:
    files: dict[str, str] = {}
    for digest, path in _cas_files(artifact_root).items():
        actual_digest = _sha256(path)
        if actual_digest != digest:
            raise BackupError(f"snapshot artifact content does not match its digest: {digest}")
        files[_artifact_relative_path(digest)] = actual_digest
    return files


def _cas_files(artifact_root: Path) -> dict[str, Path]:
    cas_root = artifact_root / "sha256"
    if not cas_root.exists():
        return {}
    cas_root = _directory(cas_root, "CAS root")
    files: dict[str, Path] = {}
    for root, directories, names in os.walk(cas_root, followlinks=False):
        root_path = Path(root)
        for name in directories:
            if (root_path / name).is_symlink():
                raise BackupError("CAS tree contains a symlink")
        for name in names:
            path = _regular_file(root_path / name, "CAS object", single_link=True)
            relative = path.relative_to(cas_root)
            if len(relative.parts) != 2:
                raise BackupError("CAS object path is not canonical")
            prefix, digest = relative.parts
            if not _DIGEST_PATTERN.fullmatch(digest) or prefix != digest[:2]:
                raise BackupError("CAS object path is not canonical")
            files[digest] = path
    return files


def _validate_candidate(candidate_commit: str, candidate_tag: str) -> None:
    if not _COMMIT_PATTERN.fullmatch(candidate_commit):
        raise BackupError("candidate commit must be a lowercase 40-character SHA")
    if not _TAG_PATTERN.fullmatch(candidate_tag):
        raise BackupError("candidate tag must identify an immutable release candidate")


def _regular_file(path: Path, label: str, *, single_link: bool = False) -> Path:
    if path.is_symlink():
        raise BackupError(f"{label} must not be a symlink")
    try:
        resolved = path.resolve(strict=True)
    except OSError as error:
        raise BackupError(f"{label} is missing") from error
    if not resolved.is_file():
        raise BackupError(f"{label} must be a regular file")
    if single_link and resolved.stat().st_nlink != 1:
        raise BackupError(f"{label} must not be hard-linked")
    return resolved


def _directory(path: Path, label: str) -> Path:
    if path.is_symlink():
        raise BackupError(f"{label} must not be a symlink")
    try:
        resolved = path.resolve(strict=True)
    except OSError as error:
        raise BackupError(f"{label} is missing") from error
    if not resolved.is_dir():
        raise BackupError(f"{label} must be a directory")
    return resolved


def _new_path(path: Path, label: str) -> Path:
    absolute = Path(os.path.abspath(path))
    if absolute.exists() or absolute.is_symlink():
        raise BackupError(f"{label} already exists")
    absolute.parent.mkdir(parents=True, exist_ok=True)
    if any(parent.is_symlink() for parent in (absolute.parent, *absolute.parent.parents)):
        raise BackupError(f"{label} parent must not contain symlinks")
    return absolute


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _referenced_artifact_digests(database: Path) -> set[str]:
    try:
        with sqlite3.connect(f"file:{database}?mode=ro", uri=True) as connection:
            rows = connection.execute("SELECT artifact_id FROM artifacts").fetchall()
    except sqlite3.Error as error:
        raise BackupError("snapshot artifact table is unreadable") from error
    digests: set[str] = set()
    for (artifact_id,) in rows:
        if not isinstance(artifact_id, str) or not artifact_id.startswith("artifact://sha256/"):
            raise BackupError("database contains an invalid artifact ID")
        digest = artifact_id.removeprefix("artifact://sha256/")
        if not _DIGEST_PATTERN.fullmatch(digest):
            raise BackupError("database contains an invalid artifact hash")
        digests.add(digest)
    return digests


def _referenced_artifact_files(database: Path) -> set[str]:
    return {_artifact_relative_path(digest) for digest in _referenced_artifact_digests(database)}


def _artifact_relative_path(digest: str) -> str:
    if not _DIGEST_PATTERN.fullmatch(digest):
        raise ValueError("invalid artifact digest")
    return f"sha256/{digest[:2]}/{digest}"


def _schema_revision(database: Path) -> str:
    try:
        with sqlite3.connect(f"file:{database}?mode=ro", uri=True) as connection:
            row = connection.execute("SELECT version_num FROM alembic_version").fetchone()
    except sqlite3.Error as error:
        raise BackupError("snapshot schema version is unreadable") from error
    if row is None or not isinstance(row[0], str) or not row[0]:
        raise BackupError("snapshot schema version is missing")
    return row[0]
