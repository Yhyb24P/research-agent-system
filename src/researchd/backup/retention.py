"""Deterministic snapshot retention planning and bounded rotation."""

import shutil
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from researchd.backup.snapshot import BackupError, read_snapshot_manifest


@dataclass(frozen=True)
class SnapshotRotationPlan:
    snapshot_root: Path
    retained: tuple[str, ...]
    delete: tuple[str, ...]


def plan_snapshot_rotation(
    snapshot_root: Path,
    *,
    retain_latest: int,
    protected: frozenset[str] = frozenset(),
) -> SnapshotRotationPlan:
    """Select old snapshots for deletion without changing the filesystem."""
    if retain_latest < 1:
        raise BackupError("retain_latest must be at least one")
    if snapshot_root.is_symlink():
        raise BackupError("snapshot root must not be a symlink")
    try:
        root = snapshot_root.resolve(strict=True)
    except OSError as error:
        raise BackupError("snapshot root is missing") from error
    if not root.is_dir():
        raise BackupError("snapshot root must be a directory")

    snapshots: list[tuple[datetime, str]] = []
    for path in root.iterdir():
        if path.is_symlink() or not path.is_dir():
            raise BackupError("snapshot root contains an unsafe entry")
        manifest = read_snapshot_manifest(path)
        created_at = datetime.fromisoformat(manifest.created_at_utc.replace("Z", "+00:00"))
        snapshots.append((created_at, path.name))
    snapshots.sort(reverse=True)
    latest = {name for _, name in snapshots[:retain_latest]}
    known = {name for _, name in snapshots}
    unknown_protected = protected - known
    if unknown_protected:
        raise BackupError(f"protected snapshot does not exist: {sorted(unknown_protected)[0]}")
    retained = tuple(sorted(latest | protected))
    delete = tuple(sorted(known - set(retained)))
    return SnapshotRotationPlan(root, retained, delete)


def apply_snapshot_rotation(plan: SnapshotRotationPlan) -> tuple[str, ...]:
    """Delete exactly the direct-child snapshots selected by a prior plan."""
    root = plan.snapshot_root.resolve(strict=True)
    if not root.is_dir() or root.is_symlink():
        raise BackupError("snapshot root is unsafe")
    overlap = set(plan.retained) & set(plan.delete)
    if overlap:
        raise BackupError("rotation plan retains and deletes the same snapshot")
    for name in plan.delete:
        if not name or name in {".", ".."} or Path(name).name != name:
            raise BackupError("rotation target must be a direct child name")
        target = root / name
        if target.is_symlink() or not target.is_dir() or target.parent != root:
            raise BackupError(f"rotation target is unsafe: {name}")
        read_snapshot_manifest(target)
    for name in plan.delete:
        shutil.rmtree(root / name)
    return plan.delete
