import os
import hashlib
import stat
from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session, sessionmaker

from researchd.artifacts.provenance import ArtifactService
from researchd.domain.enums import Capability, DataClassification
from researchd.executor.contracts import (
    CapabilityRequest,
    CapabilityResult,
    CommandLimits,
    CommandSpec,
    SandboxSpec,
)
from researchd.executor.sandbox import SandboxBackend
from researchd.artifacts.provenance import canonical_json
from researchd.storage.models import ExecutionStepRecord
from datetime import UTC, datetime


class CapabilityDenied(PermissionError):
    pass


class CapabilityBroker:
    def __init__(
        self, backend: SandboxBackend, artifact_service: ArtifactService, sessions: sessionmaker[Session],
        *, command_limits: CommandLimits, inline_output_bytes: int = 4096,
    ) -> None:
        self.backend = backend
        self.artifact_service = artifact_service
        self.sessions = sessions
        self.command_limits = command_limits
        self.inline_output_bytes = inline_output_bytes

    def execute(
        self, request: CapabilityRequest, *, granted: frozenset[Capability], sandbox: SandboxSpec,
    ) -> CapabilityResult:
        if request.capability not in granted:
            return CapabilityResult(request_id=request.request_id, status="denied", reason_code="CAPABILITY_NOT_GRANTED")
        cached = self._reserve_or_reuse(request, sandbox.attempt_id)
        if cached is not None:
            return cached
        try:
            if request.capability is Capability.WORKSPACE_WRITE:
                result = self._write(request, sandbox)
                self._complete(request.request_id, result)
                return result
            if request.capability is Capability.WORKSPACE_READ:
                result = self._read(request, sandbox)
                self._complete(request.request_id, result)
                return result
            command = self._command_for(request)
            command_result = self.backend.run(sandbox, command)
            combined = command_result.stdout + (b"\n" if command_result.stdout and command_result.stderr else b"") + command_result.stderr
            artifact_id: str | None = None
            if len(combined) > self.inline_output_bytes or command_result.output_limit_exceeded:
                artifact = self.artifact_service.register(
                    combined, mime_type="text/plain", artifact_type="run_log",
                    classification=DataClassification.LOCAL_ONLY, producer_type="tool",
                    producer_id="capability-broker", attempt_id=sandbox.attempt_id,
                )
                artifact_id = artifact.artifact_id
            inline = self._bounded_text(combined)
            if command_result.timed_out:
                capability_result = CapabilityResult(request_id=request.request_id, status="failed", exit_code=command_result.exit_code, output=inline, output_artifact_id=artifact_id, reason_code="EXECUTION_TIMEOUT")
                self._complete(request.request_id, capability_result)
                return capability_result
            if command_result.output_limit_exceeded:
                capability_result = CapabilityResult(request_id=request.request_id, status="failed", exit_code=command_result.exit_code, output=inline, output_artifact_id=artifact_id, reason_code="EXECUTION_OUTPUT_LIMIT")
                self._complete(request.request_id, capability_result)
                return capability_result
            capability_result = CapabilityResult(request_id=request.request_id, status="ok" if command_result.exit_code == 0 else "failed", exit_code=command_result.exit_code, output=inline, output_artifact_id=artifact_id, reason_code=None if command_result.exit_code == 0 else "EXECUTION_NONZERO_EXIT")
            self._complete(request.request_id, capability_result)
            return capability_result
        except (CapabilityDenied, ValueError) as error:
            result = CapabilityResult(request_id=request.request_id, status="denied", reason_code="CAPABILITY_PARAMETERS_DENIED", output=str(error))
            self._complete(request.request_id, result)
            return result

    def _reserve_or_reuse(self, request: CapabilityRequest, attempt_id: str) -> CapabilityResult | None:
        digest = hashlib.sha256(canonical_json(request.parameters).encode()).hexdigest()
        now = datetime.now(UTC)
        with self.sessions.begin() as session:
            existing = session.get(ExecutionStepRecord, request.request_id)
            if existing is not None:
                if existing.attempt_id != attempt_id or existing.capability != request.capability.value or existing.parameters_sha256 != digest:
                    return CapabilityResult(request_id=request.request_id, status="denied", reason_code="IDEMPOTENCY_KEY_MISMATCH")
                if existing.status == "COMPLETED" and existing.result_json is not None:
                    return CapabilityResult.model_validate(existing.result_json)
                return CapabilityResult(request_id=request.request_id, status="failed", reason_code="DUPLICATE_OPERATION_IN_PROGRESS")
            session.add(ExecutionStepRecord(
                step_id=request.request_id, attempt_id=attempt_id, capability=request.capability.value,
                parameters_sha256=digest, status="IN_PROGRESS", result_json=None,
                created_at=now, updated_at=now,
            ))
        return None

    def _complete(self, request_id: str, result: CapabilityResult) -> None:
        with self.sessions.begin() as session:
            record = session.get(ExecutionStepRecord, request_id)
            if record is None:
                raise RuntimeError("capability operation reservation disappeared")
            record.status = "COMPLETED"
            record.result_json = result.model_dump(mode="json")
            record.updated_at = datetime.now(UTC)

    def _command_for(self, request: CapabilityRequest) -> CommandSpec:
        parameters = request.parameters
        if request.capability is Capability.TEST_RUN:
            target = self._required_string(parameters, "target")
            if target.startswith("-") or "\x00" in target:
                raise CapabilityDenied("invalid pytest target")
            return CommandSpec(argv=("/runtime/bin/python", "-m", "pytest", "-q", target), limits=self.command_limits)
        if request.capability is Capability.PYTHON_RUN:
            script = self._relative_path(self._required_string(parameters, "script"))
            arguments = self._string_list(parameters.get("arguments", []), allow_empty=True)
            return CommandSpec(argv=("/usr/bin/python3", str(PureSandboxPath(script)), *arguments), limits=self.command_limits)
        if request.capability is Capability.SANDBOX_SHELL:
            argv = tuple(self._string_list(parameters.get("argv")))
            return CommandSpec(argv=argv, limits=self.command_limits)
        if request.capability is Capability.GIT_STATUS:
            return CommandSpec(argv=("/usr/bin/git", "status", "--short"), limits=self.command_limits)
        if request.capability is Capability.GIT_DIFF:
            return CommandSpec(argv=("/usr/bin/git", "diff", "--no-ext-diff"), limits=self.command_limits)
        raise CapabilityDenied(f"capability has no broker operation: {request.capability.value}")

    def _write(self, request: CapabilityRequest, sandbox: SandboxSpec) -> CapabilityResult:
        relative = self._relative_path(self._required_string(request.parameters, "path"))
        content = self._required_string(request.parameters, "content").encode()
        if len(content) > self.command_limits.file_size_mb * 1024 * 1024:
            raise CapabilityDenied("write exceeds file-size limit")
        parent_fd: int | None = None
        try:
            parent_fd, leaf = self._open_parent_fd(sandbox, relative, create=True)
            descriptor = os.open(
                leaf, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW | os.O_CLOEXEC,
                0o600, dir_fd=parent_fd,
            )
        except OSError as error:
            raise CapabilityDenied("workspace write target is not a safe regular path") from error
        finally:
            if parent_fd is not None:
                os.close(parent_fd)
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            os.close(descriptor)
            raise CapabilityDenied("workspace write target is not a regular file")
        with os.fdopen(descriptor, "wb", closefd=True) as stream:
            stream.write(content)
        return CapabilityResult(request_id=request.request_id, status="ok", output=f"wrote {len(content)} bytes")

    def _read(self, request: CapabilityRequest, sandbox: SandboxSpec) -> CapabilityResult:
        relative = self._relative_path(self._required_string(request.parameters, "path"))
        parent_fd: int | None = None
        try:
            parent_fd, leaf = self._open_parent_fd(sandbox, relative, create=False)
            descriptor = os.open(leaf, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=parent_fd)
        except OSError as error:
            raise CapabilityDenied("workspace read target is not a safe regular path") from error
        finally:
            if parent_fd is not None:
                os.close(parent_fd)
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            os.close(descriptor)
            raise CapabilityDenied("workspace read target is not a regular file")
        with os.fdopen(descriptor, "rb", closefd=True) as stream:
            data = stream.read(self.command_limits.output_bytes + 1)
        if len(data) > self.command_limits.output_bytes:
            raise CapabilityDenied("read exceeds output limit")
        return CapabilityResult(request_id=request.request_id, status="ok", output=self._bounded_text(data))

    def _open_parent_fd(self, sandbox: SandboxSpec, relative: Path, *, create: bool) -> tuple[int, str]:
        """Open each parent component beneath an anchored workspace directory.

        Directory FDs and ``O_NOFOLLOW`` remove the resolve-then-open TOCTOU
        window: replacing a parent with a symlink after validation cannot move
        the operation outside the already-open workspace root.
        """
        root = Path(sandbox.workspace).resolve(strict=True)
        if not root.is_dir():
            raise CapabilityDenied("workspace root is not a directory")
        flags = os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW
        parent_fd = os.open(root, flags)
        try:
            for component in relative.parts[:-1]:
                try:
                    child_fd = os.open(component, flags, dir_fd=parent_fd)
                except FileNotFoundError:
                    if not create:
                        raise
                    os.mkdir(component, mode=0o700, dir_fd=parent_fd)
                    child_fd = os.open(component, flags, dir_fd=parent_fd)
                os.close(parent_fd)
                parent_fd = child_fd
            return parent_fd, relative.parts[-1]
        except BaseException:
            os.close(parent_fd)
            raise

    @staticmethod
    def _relative_path(value: str) -> Path:
        path = Path(value)
        if not path.parts or path.is_absolute() or ".." in path.parts or value.startswith("~"):
            raise CapabilityDenied("path must be relative and traversal-free")
        return path

    @staticmethod
    def _required_string(parameters: dict[str, Any], name: str) -> str:
        value = parameters.get(name)
        if not isinstance(value, str) or not value:
            raise CapabilityDenied(f"{name} must be a nonempty string")
        return value

    @staticmethod
    def _string_list(value: Any, *, allow_empty: bool = False) -> list[str]:
        if not isinstance(value, list) or (not value and not allow_empty) or any(not isinstance(item, str) or not item or "\x00" in item for item in value):
            raise CapabilityDenied("arguments must be a nonempty list of strings")
        return value

    def _bounded_text(self, data: bytes) -> str:
        if len(data) <= self.inline_output_bytes:
            return data.decode("utf-8", errors="replace")
        half = self.inline_output_bytes // 2
        return (data[:half] + b"\n...[truncated; see artifact]...\n" + data[-half:]).decode("utf-8", errors="replace")


class PureSandboxPath:
    """Render a validated repository-relative path inside the sandbox."""

    def __init__(self, relative: Path) -> None:
        self.relative = relative

    def __str__(self) -> str:
        return f"/workspace/{self.relative.as_posix()}"
