import hashlib
import subprocess
from datetime import UTC, datetime
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from researchd.storage.models import AttemptRecord, AttemptWorktreeRecord


class WorktreeError(RuntimeError):
    pass


class WorktreeState:
    PROVISIONING = "PROVISIONING"
    ACTIVE = "ACTIVE"
    REMOVING = "REMOVING"
    RECOVERING = "RECOVERING"
    CLEANED = "CLEANED"
    CLEANUP_FAILED = "CLEANUP_FAILED"


@dataclass(frozen=True)
class WorktreeHandle:
    repository_id: str
    base_commit: str
    path: Path
    attempt_id: str
    environment_digest: str
    sandbox_backend: str


class WorktreeManager:
    """Trusted typed Git operations; no agent-provided host shell strings."""

    def __init__(self, root: Path, sessions: sessionmaker[Session] | None = None) -> None:
        self.root = root.resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.sessions = sessions

    def create(self, repository: Path, *, repository_id: str, attempt_id: str, base_commit: str = "HEAD") -> WorktreeHandle:
        source = repository.resolve(strict=True)
        if not (source / ".git").exists():
            raise WorktreeError("source is not a Git repository")
        if not attempt_id or Path(attempt_id).name != attempt_id:
            raise WorktreeError("attempt_id must be one path segment")
        target = self.root / attempt_id
        if target.exists():
            raise WorktreeError("attempt worktree already exists; dirty worktrees are never reused")
        if self.sessions is not None:
            with self.sessions() as session:
                if session.get(AttemptRecord, attempt_id) is None:
                    raise WorktreeError("attempt must be persisted before worktree creation")
                if session.get(AttemptWorktreeRecord, attempt_id) is not None:
                    raise WorktreeError("attempt already has a persisted worktree; reuse is forbidden")
        commit = self._git(source, "rev-parse", "--verify", f"{base_commit}^{{commit}}").strip()
        environment_digest = hashlib.sha256(
            f"git={self._git(source, '--version').strip()}\ncommit={commit}\nbackend=bubblewrap-v1".encode()
        ).hexdigest()
        handle = WorktreeHandle(repository_id, commit, target, attempt_id, environment_digest, "bubblewrap-v1")
        if self.sessions is not None:
            now = datetime.now(UTC)
            with self.sessions.begin() as session:
                session.add(AttemptWorktreeRecord(
                    attempt_id=attempt_id, repository_id=repository_id, base_commit=commit,
                    worktree_path=str(target), environment_digest=environment_digest,
                    sandbox_backend="bubblewrap-v1", state=WorktreeState.PROVISIONING,
                    created_at=now, updated_at=now,
                ))
        try:
            self._add_worktree(source, target, commit)
        except Exception:
            if self.sessions is not None:
                self._cleanup_after_failed_create(source, attempt_id, target)
            if target.exists():
                raise WorktreeError("Git left a partial worktree; recovery failed")
            raise
        if self.sessions is not None:
            self._set_state(attempt_id, WorktreeState.ACTIVE)
        return handle

    def remove_clean(self, repository: Path, handle: WorktreeHandle) -> None:
        source = repository.resolve(strict=True)
        self._validate_handle_path(handle)
        status = self._git(handle.path, "status", "--porcelain")
        if status:
            raise WorktreeError("refusing to remove a dirty attempt worktree")
        if self.sessions is not None:
            with self.sessions() as session:
                row = session.get(AttemptWorktreeRecord, handle.attempt_id)
                if row is None or row.state != WorktreeState.ACTIVE:
                    raise WorktreeError("attempt worktree is not active")
            self._set_state(handle.attempt_id, WorktreeState.REMOVING)
        try:
            self._remove_worktree(source, handle.path)
        except Exception:
            if self.sessions is not None:
                self._set_state(handle.attempt_id, WorktreeState.CLEANUP_FAILED)
            raise
        if self.sessions is not None:
            self._set_state(handle.attempt_id, WorktreeState.CLEANED)

    def recover_incomplete(self, repositories: Mapping[str, Path]) -> tuple[str, ...]:
        """Clean worktrees interrupted before creation or removal became terminal."""
        if self.sessions is None:
            raise WorktreeError("durable sessions are required for worktree recovery")
        recoverable = {
            WorktreeState.PROVISIONING,
            WorktreeState.REMOVING,
            WorktreeState.RECOVERING,
            WorktreeState.CLEANUP_FAILED,
        }
        with self.sessions() as session:
            identifiers = tuple(session.scalars(
                select(AttemptWorktreeRecord.attempt_id)
                .where(AttemptWorktreeRecord.state.in_(recoverable))
                .order_by(AttemptWorktreeRecord.created_at, AttemptWorktreeRecord.attempt_id)
            ))
        recovered: list[str] = []
        for attempt_id in identifiers:
            with self.sessions.begin() as session:
                row = session.get(AttemptWorktreeRecord, attempt_id)
                if row is None or row.state not in recoverable:
                    continue
                repository_id = row.repository_id
                target = Path(row.worktree_path)
                expected = self.root / row.attempt_id
                if target != expected or not target.is_relative_to(self.root):
                    row.state = WorktreeState.CLEANUP_FAILED
                    row.updated_at = datetime.now(UTC)
                    continue
                row.state = WorktreeState.RECOVERING
                row.updated_at = datetime.now(UTC)
            source = repositories.get(repository_id)
            if source is None:
                self._set_state(attempt_id, WorktreeState.CLEANUP_FAILED)
                continue
            try:
                self._remove_worktree(source.resolve(strict=True), target)
            except Exception:
                self._set_state(attempt_id, WorktreeState.CLEANUP_FAILED)
                continue
            self._set_state(attempt_id, WorktreeState.CLEANED)
            recovered.append(attempt_id)
        return tuple(recovered)

    def _cleanup_after_failed_create(self, source: Path, attempt_id: str, target: Path) -> None:
        try:
            self._remove_worktree(source, target)
        except Exception:
            self._set_state(attempt_id, WorktreeState.CLEANUP_FAILED)
        else:
            self._set_state(attempt_id, WorktreeState.CLEANED)

    def _validate_handle_path(self, handle: WorktreeHandle) -> None:
        if handle.path != self.root / handle.attempt_id or not handle.path.is_relative_to(self.root):
            raise WorktreeError("attempt worktree path is outside its managed slot")

    def _set_state(self, attempt_id: str, state: str) -> None:
        if self.sessions is None:
            return
        with self.sessions.begin() as session:
            row = session.get(AttemptWorktreeRecord, attempt_id)
            if row is None:
                raise WorktreeError("attempt worktree lifecycle record is missing")
            row.state = state
            row.updated_at = datetime.now(UTC)

    def _add_worktree(self, source: Path, target: Path, commit: str) -> None:
        self._git(source, "worktree", "add", "--detach", str(target), commit)

    def _remove_worktree(self, source: Path, target: Path) -> None:
        if target.exists():
            self._git(source, "worktree", "remove", "--force", str(target))
        self._git(source, "worktree", "prune")

    @staticmethod
    def _git(repository: Path, *arguments: str) -> str:
        result = subprocess.run(
            ["/usr/bin/git", "-C", str(repository), *arguments],
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            check=False, text=True, timeout=30,
        )
        if result.returncode != 0:
            raise WorktreeError(result.stderr.strip() or "Git operation failed")
        return result.stdout
