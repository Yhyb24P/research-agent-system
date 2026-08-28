"""Read-only storage and backup freshness metrics for operational qualification."""

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path


class StorageMetricsError(RuntimeError):
    """Raised when the storage layout cannot be observed safely."""


@dataclass(frozen=True)
class StorageMetrics:
    database_size_bytes: int
    wal_size_bytes: int
    cas_size_bytes: int
    cas_file_count: int
    backup_manifest_present: bool
    backup_age_seconds: float | None

    def as_dict(self) -> dict[str, int | float | bool | None]:
        return {
            "database_size_bytes": self.database_size_bytes,
            "wal_size_bytes": self.wal_size_bytes,
            "cas_size_bytes": self.cas_size_bytes,
            "cas_file_count": self.cas_file_count,
            "backup_manifest_present": self.backup_manifest_present,
            "backup_age_seconds": self.backup_age_seconds,
        }

    def prometheus(self) -> str:
        lines = [
            f"research_storage_database_bytes {self.database_size_bytes}",
            f"research_storage_wal_bytes {self.wal_size_bytes}",
            f"research_storage_cas_bytes {self.cas_size_bytes}",
            f"research_storage_cas_files {self.cas_file_count}",
            f"research_backup_manifest_present {int(self.backup_manifest_present)}",
        ]
        if self.backup_age_seconds is not None:
            lines.append(f"research_backup_age_seconds {self.backup_age_seconds}")
        return "\n".join(lines) + "\n"


def collect_storage_metrics(
    database: Path, artifact_root: Path, backup_root: Path | None = None
) -> StorageMetrics:
    """Collect filesystem-only metrics without reading artifact contents."""
    database = database.resolve(strict=True)
    artifact_root = artifact_root.resolve(strict=True)
    if not database.is_file() or database.is_symlink():
        raise StorageMetricsError("database must be a regular file")
    if not artifact_root.is_dir() or artifact_root.is_symlink():
        raise StorageMetricsError("artifact root must be a directory")
    cas_root = artifact_root / "sha256"
    cas_bytes = 0
    cas_files = 0
    if cas_root.exists():
        if not cas_root.is_dir() or cas_root.is_symlink():
            raise StorageMetricsError("CAS root must be a directory")
        for path in cas_root.rglob("*"):
            if path.is_symlink():
                continue
            if path.is_file():
                cas_bytes += path.stat().st_size
                cas_files += 1
    backup_present = False
    backup_age: float | None = None
    if backup_root is not None:
        backup_root = backup_root.resolve(strict=True)
        if not backup_root.is_dir() or backup_root.is_symlink():
            raise StorageMetricsError("backup root must be a directory")
        manifest_path = backup_root / "manifest.json"
        if manifest_path.is_file() and not manifest_path.is_symlink():
            try:
                raw = json.loads(manifest_path.read_text(encoding="utf-8"))
                created = raw.get("created_at_utc")
                timestamp = datetime.fromisoformat(str(created).replace("Z", "+00:00"))
                if timestamp.tzinfo is None:
                    raise ValueError("backup timestamp is naive")
            except (OSError, TypeError, ValueError, json.JSONDecodeError) as error:
                raise StorageMetricsError("backup manifest timestamp is invalid") from error
            backup_present = True
            backup_age = max(0.0, (datetime.now(UTC) - timestamp.astimezone(UTC)).total_seconds())
    return StorageMetrics(
        database_size_bytes=database.stat().st_size,
        wal_size_bytes=_sidecar_size(database.with_name(database.name + "-wal")),
        cas_size_bytes=cas_bytes,
        cas_file_count=cas_files,
        backup_manifest_present=backup_present,
        backup_age_seconds=backup_age,
    )


def _sidecar_size(path: Path) -> int:
    if path.is_symlink():
        raise StorageMetricsError("SQLite WAL sidecar must not be a symlink")
    return path.stat().st_size if path.is_file() else 0
