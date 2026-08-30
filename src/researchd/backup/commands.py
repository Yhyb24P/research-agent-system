"""Daemon-command surface over the fail-closed snapshot functions.

The service binds the daemon's own database and artifact root; every method
either produces a durable snapshot or performs a read-only validation, so a
crash can never leave a half-written snapshot behind.
"""

from pathlib import Path
from typing import Any

from researchd.backup.snapshot import (
    backup_snapshot,
    plan_restore,
    verify_snapshot,
)


class BackupCommandService:
    """Create, verify and plan restores for the daemon's own state."""

    def __init__(self, database: Path, artifact_root: Path) -> None:
        self.database = database
        self.artifact_root = artifact_root

    def create_backup(
        self,
        destination: str,
        candidate_commit: str,
        candidate_tag: str,
    ) -> dict[str, Any]:
        manifest = backup_snapshot(
            self.database,
            self.artifact_root,
            Path(destination),
            candidate_commit=candidate_commit,
            candidate_tag=candidate_tag,
        )
        payload = manifest.as_dict()
        payload["destination"] = destination
        return payload

    def verify_backup(self, snapshot: str) -> dict[str, Any]:
        health = verify_snapshot(Path(snapshot))
        return {
            "snapshot": snapshot,
            "healthy": True,
            "schema_revision": health.schema_revision,
            "artifacts_verified": health.artifacts_verified,
            "table_counts": health.table_counts,
        }

    def plan_restore(
        self,
        snapshot: str,
        database_destination: str,
        artifact_destination: str,
        expected_candidate_commit: str,
        expected_candidate_tag: str,
    ) -> dict[str, Any]:
        return plan_restore(
            Path(snapshot),
            Path(database_destination),
            Path(artifact_destination),
            expected_candidate_commit=expected_candidate_commit,
            expected_candidate_tag=expected_candidate_tag,
        )


__all__ = ["BackupCommandService"]
