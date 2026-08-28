import hashlib
import subprocess
from datetime import UTC, datetime
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy.orm import Session, sessionmaker

from researchd.storage.models import AttemptRecord, AttemptWorktreeRecord


class WorktreeError(RuntimeError):
    pass


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
        try:
            self._git(source, "worktree", "add", "--detach", str(target), commit)
        except Exception:
            if target.exists():
                raise WorktreeError("Git left a partial worktree; manual reconciliation required")
            raise
        environment_digest = hashlib.sha256(
            f"git={self._git(source, '--version').strip()}\ncommit={commit}\nbackend=bubblewrap-v1".encode()
        ).hexdigest()
        handle = WorktreeHandle(repository_id, commit, target, attempt_id, environment_digest, "bubblewrap-v1")
        if self.sessions is not None:
            with self.sessions.begin() as session:
                session.add(AttemptWorktreeRecord(
                    attempt_id=attempt_id, repository_id=repository_id, base_commit=commit,
                    worktree_path=str(target), environment_digest=environment_digest,
                    sandbox_backend="bubblewrap-v1", created_at=datetime.now(UTC),
                ))
        return handle

    def remove_clean(self, repository: Path, handle: WorktreeHandle) -> None:
        status = self._git(handle.path, "status", "--porcelain")
        if status:
            raise WorktreeError("refusing to remove a dirty attempt worktree")
        self._git(repository.resolve(strict=True), "worktree", "remove", str(handle.path))

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
