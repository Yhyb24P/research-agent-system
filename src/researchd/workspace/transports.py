"""Git/worktree and Archive transports for bounded workspace grants."""

import hashlib
import io
import os
from pathlib import Path
import re
import shutil
import subprocess
import tarfile
from typing import Protocol

from researchd.workspace.contracts import (
    ProvisionedWorkspace,
    ReconciliationPayload,
    WorkspaceAccessMode,
    WorkspaceFile,
    WorkspaceGrant,
    WorkspaceSnapshot,
    WorkspaceTransportKind,
)
from researchd.workspace.manifest import WorkspaceAdmissionError, selected, snapshot_workspace


class WorkspaceTransport(Protocol):
    kind: WorkspaceTransportKind

    def snapshot(self, grant: WorkspaceGrant, source_root: Path) -> WorkspaceSnapshot: ...
    def plan(self, grant: WorkspaceGrant, source_root: Path, snapshot: WorkspaceSnapshot) -> ProvisionedWorkspace: ...
    def provision(
        self,
        grant: WorkspaceGrant,
        source_root: Path,
        snapshot: WorkspaceSnapshot,
        planned: ProvisionedWorkspace,
    ) -> ProvisionedWorkspace: ...
    def reconcile(
        self,
        grant: WorkspaceGrant,
        provisioned: ProvisionedWorkspace,
        *,
        remote_result: Path | None = None,
    ) -> ReconciliationPayload: ...
    def cleanup(self, grant: WorkspaceGrant, provisioned: ProvisionedWorkspace) -> None: ...


def _safe_target(root: Path, identifier: str, suffix: str = "") -> Path:
    if re.fullmatch(r"[A-Za-z0-9_.-]+", identifier) is None:
        raise WorkspaceAdmissionError("workspace grant ID is unsafe for a transport handle")
    resolved_root = root.resolve()
    resolved_root.mkdir(parents=True, exist_ok=True)
    target = (resolved_root / f"{identifier}{suffix}").resolve()
    if target.parent != resolved_root:
        raise WorkspaceAdmissionError("transport target escaped its configured root")
    return target


def _git(root: Path, *arguments: str) -> str:
    result = subprocess.run(
        ("git", "-C", str(root), *arguments),
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=60,
    )
    return result.stdout.strip()


class GitWorktreeTransport:
    kind = WorkspaceTransportKind.GIT_WORKTREE

    def __init__(self, transport_root: Path) -> None:
        self.transport_root = transport_root

    def snapshot(self, grant: WorkspaceGrant, source_root: Path) -> WorkspaceSnapshot:
        revision = _git(source_root, "rev-parse", "HEAD")
        if grant.source_revision is not None and revision != grant.source_revision:
            raise WorkspaceAdmissionError("Git workspace revision does not match the grant")
        return snapshot_workspace(
            source_root,
            allowed_paths=grant.allowed_paths,
            excluded_paths=grant.excluded_paths,
            limits=grant.limits,
            source_revision=revision,
        )

    def plan(
        self, grant: WorkspaceGrant, source_root: Path, snapshot: WorkspaceSnapshot
    ) -> ProvisionedWorkspace:
        if snapshot.source_revision is None:
            raise WorkspaceAdmissionError("Git workspace snapshot requires a source revision")
        target = _safe_target(self.transport_root, grant.workspace_grant_id)
        return ProvisionedWorkspace(
            transport_handle={
                "source_root": str(source_root.resolve()),
                "worktree_root": str(target),
                "base_revision": snapshot.source_revision,
            },
            remote_workspace_handle=str(target),
        )

    def provision(
        self,
        grant: WorkspaceGrant,
        source_root: Path,
        snapshot: WorkspaceSnapshot,
        planned: ProvisionedWorkspace,
    ) -> ProvisionedWorkspace:
        expected = self.plan(grant, source_root, snapshot)
        if planned != expected:
            raise WorkspaceAdmissionError("Git transport plan changed before provisioning")
        target = Path(planned.transport_handle["worktree_root"])
        if target.exists():
            raise WorkspaceAdmissionError("Git transport target already exists")
        # Linked worktrees contain an absolute pointer to the source repository's
        # Git metadata.  That pointer is unusable inside the executor sandbox and
        # mounting the source metadata would leak authority into the delegation.
        # A no-hardlink clone keeps the delegated repository self-contained.
        _git(
            source_root,
            "clone",
            "--no-hardlinks",
            "--no-checkout",
            str(source_root.resolve()),
            str(target),
        )
        patterns = list(grant.allowed_paths or (".",))
        patterns.extend(f"!{item}" for item in grant.excluded_paths)
        if patterns != ["."]:
            _git(target, "sparse-checkout", "set", "--no-cone", "--", *patterns)
        _git(target, "checkout", "--detach", planned.transport_handle["base_revision"])
        if grant.access_mode is WorkspaceAccessMode.READ_ONLY:
            self._set_read_only(target, read_only=True)
        return planned

    def reconcile(
        self,
        grant: WorkspaceGrant,
        provisioned: ProvisionedWorkspace,
        *,
        remote_result: Path | None = None,
    ) -> ReconciliationPayload:
        del remote_result
        target = Path(provisioned.transport_handle["worktree_root"])
        base_revision = provisioned.transport_handle["base_revision"]
        changed = self._changed_paths(target, base_revision)
        outside = [
            path for path in changed
            if not selected(path, grant.allowed_paths, grant.excluded_paths)
        ]
        if outside:
            raise WorkspaceAdmissionError(
                f"Git reconciliation contains changes outside the grant: {outside[0]}"
            )
        result_snapshot = snapshot_workspace(
            target,
            allowed_paths=grant.allowed_paths,
            excluded_paths=grant.excluded_paths,
            limits=grant.limits,
            source_revision=_git(target, "rev-parse", "HEAD"),
        )
        stage_paths = grant.allowed_paths or (".",)
        _git(target, "add", "-A", "--", *stage_paths)
        patch = subprocess.run(
            ("git", "-C", str(target), "diff", "--binary", "--cached", base_revision),
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=60,
        ).stdout
        return ReconciliationPayload(
            payload=patch,
            mime_type="text/x-diff",
            artifact_type="workspace-git-diff",
            result_snapshot=result_snapshot,
            summary=f"Git reconciliation captured {len(patch)} patch bytes",
        )

    @staticmethod
    def _changed_paths(root: Path, base_revision: str) -> tuple[str, ...]:
        tracked = subprocess.run(
            ("git", "-C", str(root), "diff", "--name-only", "-z", base_revision),
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=60,
        ).stdout
        untracked = subprocess.run(
            ("git", "-C", str(root), "ls-files", "--others", "--exclude-standard", "-z"),
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=60,
        ).stdout
        try:
            return tuple(
                item.decode("utf-8")
                for item in (*tracked.split(b"\0"), *untracked.split(b"\0"))
                if item
            )
        except UnicodeDecodeError as error:
            raise WorkspaceAdmissionError("Git reconciliation paths must be UTF-8") from error

    def cleanup(self, grant: WorkspaceGrant, provisioned: ProvisionedWorkspace) -> None:
        target = Path(provisioned.transport_handle["worktree_root"])
        if grant.access_mode is WorkspaceAccessMode.READ_ONLY and target.exists():
            self._set_read_only(target, read_only=False)
        if target.exists():
            shutil.rmtree(target)

    @staticmethod
    def _set_read_only(root: Path, *, read_only: bool) -> None:
        directory_mode, file_mode = ((0o500, 0o400) if read_only else (0o700, 0o600))
        for path in sorted(root.rglob("*"), reverse=True):
            if path.is_symlink():
                continue
            os.chmod(path, directory_mode if path.is_dir() else file_mode)
        os.chmod(root, directory_mode)


class ArchiveWorkspaceTransport:
    kind = WorkspaceTransportKind.ARCHIVE

    def __init__(self, transport_root: Path) -> None:
        self.transport_root = transport_root

    def snapshot(self, grant: WorkspaceGrant, source_root: Path) -> WorkspaceSnapshot:
        return snapshot_workspace(
            source_root,
            allowed_paths=grant.allowed_paths,
            excluded_paths=grant.excluded_paths,
            limits=grant.limits,
            source_revision=grant.source_revision,
        )

    def plan(
        self, grant: WorkspaceGrant, source_root: Path, snapshot: WorkspaceSnapshot
    ) -> ProvisionedWorkspace:
        del source_root, snapshot
        target = _safe_target(self.transport_root, grant.workspace_grant_id, ".tar")
        return ProvisionedWorkspace(
            transport_handle={"archive_path": str(target)},
            remote_workspace_handle=str(target),
        )

    def provision(
        self,
        grant: WorkspaceGrant,
        source_root: Path,
        snapshot: WorkspaceSnapshot,
        planned: ProvisionedWorkspace,
    ) -> ProvisionedWorkspace:
        expected = self.plan(grant, source_root, snapshot)
        if planned != expected:
            raise WorkspaceAdmissionError("Archive transport plan changed before provisioning")
        target = Path(planned.transport_handle["archive_path"])
        if target.exists():
            raise WorkspaceAdmissionError("Archive transport target already exists")
        payload = self._create_archive(source_root.resolve(), snapshot)
        descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        return planned

    def reconcile(
        self,
        grant: WorkspaceGrant,
        provisioned: ProvisionedWorkspace,
        *,
        remote_result: Path | None = None,
    ) -> ReconciliationPayload:
        del provisioned
        if remote_result is None:
            raise WorkspaceAdmissionError("Archive reconciliation requires a returned archive")
        try:
            payload = remote_result.read_bytes()
        except OSError as error:
            raise WorkspaceAdmissionError("returned archive is unavailable") from error
        result_snapshot = self._inspect_archive(payload, grant)
        return ReconciliationPayload(
            payload=payload,
            mime_type="application/x-tar",
            artifact_type="workspace-archive-result",
            result_snapshot=result_snapshot,
            summary=f"Archive reconciliation captured {result_snapshot.file_count} files",
        )

    def cleanup(self, grant: WorkspaceGrant, provisioned: ProvisionedWorkspace) -> None:
        del grant
        Path(provisioned.transport_handle["archive_path"]).unlink(missing_ok=True)

    @staticmethod
    def _create_archive(root: Path, snapshot: WorkspaceSnapshot) -> bytes:
        output = io.BytesIO()
        with tarfile.open(fileobj=output, mode="w") as archive:
            for item in snapshot.files:
                data = (root / item.path).read_bytes()
                info = tarfile.TarInfo(item.path)
                info.size = len(data)
                info.mode = 0o644
                info.mtime = 0
                archive.addfile(info, io.BytesIO(data))
        return output.getvalue()

    @staticmethod
    def _inspect_archive(payload: bytes, grant: WorkspaceGrant) -> WorkspaceSnapshot:
        files: list[WorkspaceFile] = []
        total = 0
        try:
            with tarfile.open(fileobj=io.BytesIO(payload), mode="r:") as archive:
                for member in sorted(archive.getmembers(), key=lambda item: item.name):
                    path = member.name.replace("\\", "/")
                    if path.startswith("/") or ".." in Path(path).parts:
                        raise WorkspaceAdmissionError("returned archive contains a path traversal")
                    if not member.isfile():
                        raise WorkspaceAdmissionError("returned archive may contain only regular files")
                    if not selected(path, grant.allowed_paths, grant.excluded_paths):
                        raise WorkspaceAdmissionError("returned archive contains a path outside the grant")
                    if member.size > grant.limits.max_single_file_bytes:
                        raise WorkspaceAdmissionError("returned archive file exceeds the single-file limit")
                    total += member.size
                    if total > grant.limits.max_total_bytes:
                        raise WorkspaceAdmissionError("returned archive exceeds the total-byte limit")
                    if len(files) + 1 > grant.limits.max_file_count:
                        raise WorkspaceAdmissionError("returned archive exceeds the file-count limit")
                    stream = archive.extractfile(member)
                    if stream is None:
                        raise WorkspaceAdmissionError("returned archive member could not be read")
                    data = stream.read()
                    if len(data) != member.size:
                        raise WorkspaceAdmissionError("returned archive member size changed while reading")
                    files.append(WorkspaceFile(path=path, size=len(data), sha256=hashlib.sha256(data).hexdigest()))
        except tarfile.TarError as error:
            raise WorkspaceAdmissionError("returned archive is corrupt") from error
        import json

        canonical = json.dumps(
            [item.model_dump(mode="json") for item in files], sort_keys=True, separators=(",", ":")
        ).encode()
        return WorkspaceSnapshot(
            source_revision=None,
            manifest_sha256=hashlib.sha256(canonical).hexdigest(),
            files=tuple(files),
            total_bytes=total,
            file_count=len(files),
        )
