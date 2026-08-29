"""Deterministic bounded workspace inventory with path-escape rejection."""

import fnmatch
import hashlib
import json
from pathlib import Path, PurePosixPath

from researchd.workspace.contracts import WorkspaceFile, WorkspaceLimits, WorkspaceSnapshot


class WorkspaceAdmissionError(ValueError):
    pass


def normalize_policy_path(value: str) -> str:
    path = PurePosixPath(value.replace("\\", "/"))
    if path.is_absolute() or ".." in path.parts:
        raise WorkspaceAdmissionError("workspace policy paths must be relative and cannot traverse parents")
    normalized = path.as_posix().removeprefix("./")
    return normalized if normalized not in {"", "."} else "."


def selected(path: str, allowed_paths: tuple[str, ...], excluded_paths: tuple[str, ...]) -> bool:
    allowed = tuple(normalize_policy_path(item) for item in allowed_paths)
    excluded = tuple(normalize_policy_path(item) for item in excluded_paths)
    admitted = not allowed or any(
        item == "." or path == item or path.startswith(f"{item}/") or fnmatch.fnmatch(path, item)
        for item in allowed
    )
    denied = any(
        path == item or path.startswith(f"{item}/") or fnmatch.fnmatch(path, item)
        for item in excluded
    )
    return admitted and not denied and path != ".git" and not path.startswith(".git/")


def snapshot_workspace(
    root: Path,
    *,
    allowed_paths: tuple[str, ...],
    excluded_paths: tuple[str, ...],
    limits: WorkspaceLimits,
    source_revision: str | None = None,
) -> WorkspaceSnapshot:
    resolved = root.resolve(strict=True)
    if not resolved.is_dir():
        raise WorkspaceAdmissionError("workspace root must be a directory")
    files: list[WorkspaceFile] = []
    total_bytes = 0
    for candidate in sorted(resolved.rglob("*"), key=lambda item: item.as_posix()):
        relative = candidate.relative_to(resolved).as_posix()
        if not selected(relative, allowed_paths, excluded_paths):
            continue
        if candidate.is_symlink():
            raise WorkspaceAdmissionError(f"workspace contains an admitted symbolic link: {relative}")
        if candidate.is_dir():
            continue
        if not candidate.is_file():
            raise WorkspaceAdmissionError(f"workspace contains an unsupported file type: {relative}")
        data = candidate.read_bytes()
        size = len(data)
        if size > limits.max_single_file_bytes:
            raise WorkspaceAdmissionError(f"workspace file exceeds the single-file limit: {relative}")
        total_bytes += size
        if total_bytes > limits.max_total_bytes:
            raise WorkspaceAdmissionError("workspace exceeds the total-byte limit")
        if len(files) + 1 > limits.max_file_count:
            raise WorkspaceAdmissionError("workspace exceeds the file-count limit")
        files.append(WorkspaceFile(path=relative, size=size, sha256=hashlib.sha256(data).hexdigest()))
    manifest_json = json.dumps(
        [item.model_dump(mode="json") for item in files],
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return WorkspaceSnapshot(
        source_revision=source_revision,
        manifest_sha256=hashlib.sha256(manifest_json).hexdigest(),
        files=tuple(files),
        total_bytes=total_bytes,
        file_count=len(files),
    )
