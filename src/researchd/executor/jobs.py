import json
import os
import signal
import subprocess
import sys
import time
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol
from uuid import uuid4

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from researchd.domain.enums import JobState, NetworkMode
from researchd.executor.contracts import JobHandle, JobSpec
from researchd.executor.gpu import GpuAdmissionController, GpuAdmissionError
from researchd.storage.models import AttemptRecord, AuditEventRecord, JobRecord, WorkOrderRecord
from researchd.storage.repositories import JobRepository


class JobBackend(Protocol):
    def submit(self, spec: JobSpec) -> JobHandle: ...
    def find_by_operation(self, operation_id: str) -> JobHandle | None: ...
    def get(self, native_handle: str) -> JobHandle | None: ...
    def cancel(self, native_handle: str) -> JobHandle: ...


class LocalDurableJobBackend:
    """Operation-ID keyed detached jobs with durable runner status files."""

    def __init__(self, root: Path, commands: Mapping[str, Sequence[str]]) -> None:
        self.root = root.resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.commands = {name: tuple(argv) for name, argv in commands.items()}

    def submit(self, spec: JobSpec) -> JobHandle:
        if spec.network is not NetworkMode.NONE:
            raise ValueError("local durable backend currently supports network=none only")
        configured = self.commands.get(spec.job_type)
        if configured is None:
            raise ValueError("unknown typed job_type")
        directory = self._directory(spec.operation_id)
        try:
            directory.mkdir(mode=0o700)
        except FileExistsError:
            existing = self.find_by_operation(spec.operation_id)
            return existing or JobHandle(native_handle=spec.operation_id, state=JobState.LOST.value)
        status_path = directory / "status.json"
        argv = self._sandbox_argv(spec, configured)
        internal = directory / "runner.json"
        internal.write_text(json.dumps({"argv": argv, "status_path": str(status_path)}, sort_keys=True, separators=(",", ":")))
        runner = subprocess.Popen(
            [sys.executable, "-m", "researchd.executor.job_runner", str(internal)],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            start_new_session=True, close_fds=True,
            env={"PATH": "/usr/bin", "HOME": "/nonexistent", "TMPDIR": "/tmp"},
        )
        (directory / "runner.pid").write_text(json.dumps({
            "pid": runner.pid,
            "start_time": self._process_start_time(runner.pid),
        }, sort_keys=True))
        return JobHandle(native_handle=spec.operation_id, state=JobState.SUBMITTED.value)

    def find_by_operation(self, operation_id: str) -> JobHandle | None:
        directory = self._directory(operation_id)
        if not directory.exists():
            return None
        return self.get(operation_id)

    def get(self, native_handle: str) -> JobHandle | None:
        directory = self._directory(native_handle)
        status = directory / "status.json"
        if status.exists():
            try:
                state = str(json.loads(status.read_text())["state"])
            except (OSError, ValueError, KeyError, TypeError):
                return JobHandle(native_handle=native_handle, state=JobState.LOST.value)
            return JobHandle(native_handle=native_handle, state=state)
        pid_path = directory / "runner.pid"
        if not pid_path.exists():
            return JobHandle(native_handle=native_handle, state=JobState.LOST.value)
        try:
            identity = json.loads(pid_path.read_text())
            pid = int(identity["pid"])
            if self._process_start_time(pid) != str(identity["start_time"]):
                return JobHandle(native_handle=native_handle, state=JobState.LOST.value)
            os.kill(pid, 0)
            return JobHandle(native_handle=native_handle, state=JobState.SUBMITTED.value)
        except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError):
            return JobHandle(native_handle=native_handle, state=JobState.LOST.value)

    def cancel(self, native_handle: str) -> JobHandle:
        directory = self._directory(native_handle)
        pid_path = directory / "runner.pid"
        if not pid_path.exists():
            return JobHandle(native_handle=native_handle, state=JobState.LOST.value)
        pid: int | None = None
        try:
            identity = json.loads(pid_path.read_text())
            pid = int(identity["pid"])
            if self._process_start_time(pid) != str(identity["start_time"]):
                return JobHandle(native_handle=native_handle, state=JobState.LOST.value)
            os.killpg(pid, signal.SIGTERM)
        except (ProcessLookupError, ValueError, KeyError, TypeError, json.JSONDecodeError):
            pass
        deadline = time.monotonic() + 2
        while pid is not None and time.monotonic() < deadline:
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                break
            time.sleep(0.02)
        payload = {"state": JobState.CANCELLED.value, "updated_at": datetime.now(UTC).isoformat()}
        temporary = directory / "status.tmp"
        temporary.write_text(json.dumps(payload, sort_keys=True, separators=(",", ":")))
        os.replace(temporary, directory / "status.json")
        return JobHandle(native_handle=native_handle, state=JobState.CANCELLED.value)

    def _sandbox_argv(self, spec: JobSpec, command: Sequence[str]) -> list[str]:
        if spec.resources.gpu_count:
            raise ValueError("local durable backend does not promise GPU isolation")
        available_cpus = sorted(os.sched_getaffinity(0))
        if spec.resources.cpu_count > len(available_cpus):
            raise ValueError("requested CPU count exceeds backend affinity")
        cpu_list = ",".join(str(cpu) for cpu in available_cpus[:spec.resources.cpu_count])
        workspace = Path(spec.workspace).resolve(strict=True)
        argv = [
            "/usr/bin/taskset", "--cpu-list", cpu_list,
            "/usr/bin/prlimit", f"--as={spec.resources.memory_mb * 1024 * 1024}", "--",
            "/usr/bin/bwrap", "--unshare-user", "--unshare-pid", "--unshare-net",
            "--die-with-parent", "--new-session", "--clearenv",
            "--setenv", "PATH", "/usr/bin", "--setenv", "HOME", "/nonexistent", "--setenv", "TMPDIR", "/tmp",
            "--ro-bind", "/usr", "/usr", "--ro-bind", "/lib", "/lib",
        ]
        if Path("/lib64").exists():
            argv += ["--ro-bind", "/lib64", "/lib64"]
        argv += ["--proc", "/proc", "--dev", "/dev", "--tmpfs", "/tmp", "--bind", str(workspace), "/workspace", "--chdir", "/workspace", "--", *command]
        return argv

    @staticmethod
    def _process_start_time(pid: int) -> str:
        return Path(f"/proc/{pid}/stat").read_text().split()[21]

    def _directory(self, operation_id: str) -> Path:
        if len(operation_id) < 8 or any(character not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-" for character in operation_id):
            raise ValueError("operation_id contains invalid path characters")
        return self.root / operation_id


class JobManager:
    def __init__(self, sessions: sessionmaker[Session], backend: JobBackend, gpu_admission: GpuAdmissionController | None = None) -> None:
        self.sessions = sessions
        self.backend = backend
        self.gpu_admission = gpu_admission

    def submit(self, spec: JobSpec) -> JobRecord:
        now = datetime.now(UTC)
        job_id = f"job_{uuid4().hex}"
        try:
            with self.sessions.begin() as session:
                session.add(JobRecord(
                    job_id=job_id, attempt_id=spec.attempt_id, operation_id=spec.operation_id,
                    state=JobState.CREATED.value, backend=spec.backend, native_handle=None,
                    version=1, created_at=now, updated_at=now,
                ))
                self._append_event(session, spec.attempt_id, job_id, "JOB_SUBMISSION_RESERVED", {"operation_id": spec.operation_id})
        except IntegrityError:
            with self.sessions() as session:
                existing = JobRepository(session).get_by_operation_id(spec.operation_id)
                if existing is None:
                    raise
                return existing
        if spec.resources.gpu_count and self.gpu_admission is None:
            self._mark_failed(job_id, "GPU_ADMISSION_REQUIRED")
            raise GpuAdmissionError("GPU admission controller is required for GPU jobs")
        try:
            submitted_spec = spec
            if self.gpu_admission is not None:
                leases = self.gpu_admission.acquire(job_id, spec.resources.gpu_count)
                submitted_spec = spec.model_copy(update={"gpu_device_ids": tuple(item.device_id for item in leases)})
            handle = self.backend.submit(submitted_spec)
        except Exception:
            if self.gpu_admission is not None:
                self.gpu_admission.release(job_id)
            self._mark_failed(job_id, "GPU_OR_BACKEND_SUBMIT_FAILED")
            raise
        with self.sessions.begin() as session:
            record = session.get(JobRecord, job_id)
            assert record is not None
            record.native_handle = handle.native_handle
            record.state = handle.state
            record.version += 1
            record.updated_at = datetime.now(UTC)
            self._append_event(session, record.attempt_id, job_id, "JOB_SUBMITTED", {"operation_id": record.operation_id, "native_handle": handle.native_handle})
        return record

    def reconcile(self) -> list[JobRecord]:
        reconciled: list[JobRecord] = []
        if self.gpu_admission is not None:
            self.gpu_admission.reconcile()
        with self.sessions() as session:
            identifiers = [record.job_id for record in JobRepository(session).active()]
        for job_id in identifiers:
            release = False
            with self.sessions.begin() as session:
                record = session.get(JobRecord, job_id)
                assert record is not None
                handle = self.backend.get(record.native_handle) if record.native_handle else self.backend.find_by_operation(record.operation_id)
                record.state = JobState.LOST.value if handle is None else handle.state
                if handle is not None:
                    record.native_handle = handle.native_handle
                # LOST is deliberately not released: the native scheduler may
                # still be running the job and its GPU lease must be held until
                # an operator reconciles that uncertainty.
                release = record.state in {JobState.SUCCEEDED.value, JobState.FAILED.value, JobState.CANCELLED.value}
                record.version += 1
                record.updated_at = datetime.now(UTC)
                self._append_event(session, record.attempt_id, job_id, "JOB_STATUS_CHANGED", {"state": record.state})
                reconciled.append(record)
            if release and self.gpu_admission is not None:
                self.gpu_admission.release(job_id)
        return reconciled

    def cancel(self, job_id: str) -> JobRecord:
        with self.sessions.begin() as session:
            record = session.get(JobRecord, job_id)
            if record is None or record.native_handle is None:
                raise LookupError(job_id)
            record.state = JobState.CANCEL_REQUESTED.value
            record.version += 1
            record.updated_at = datetime.now(UTC)
            handle_value = record.native_handle
            self._append_event(session, record.attempt_id, job_id, "JOB_CANCEL_REQUESTED", {})
        handle = self.backend.cancel(handle_value)
        if self.gpu_admission is not None and handle.state in {JobState.CANCELLED.value, JobState.FAILED.value}:
            self.gpu_admission.release(job_id)
        with self.sessions.begin() as session:
            record = session.get(JobRecord, job_id)
            assert record is not None
            record.state = handle.state
            record.version += 1
            record.updated_at = datetime.now(UTC)
            self._append_event(session, record.attempt_id, job_id, "JOB_STATUS_CHANGED", {"state": record.state})
            return record

    def release_lost(self, job_id: str) -> JobRecord:
        """Explicitly release a LOST job's resources after operator review."""
        with self.sessions.begin() as session:
            record = session.get(JobRecord, job_id)
            if record is None:
                raise LookupError(job_id)
            if record.state != JobState.LOST.value:
                raise ValueError("only LOST jobs require explicit resource resolution")
            self._append_event(session, record.attempt_id, job_id, "JOB_LOST_RESOLUTION_REQUESTED", {"action": "release_resources"})
        if self.gpu_admission is not None:
            self.gpu_admission.release(job_id)
        with self.sessions() as session:
            record = session.get(JobRecord, job_id)
            assert record is not None
            return record

    def _mark_failed(self, job_id: str, reason: str) -> None:
        with self.sessions.begin() as session:
            record = session.get(JobRecord, job_id)
            if record is None:
                return
            record.state = JobState.FAILED.value
            record.version += 1
            record.updated_at = datetime.now(UTC)
            self._append_event(session, record.attempt_id, job_id, "JOB_SUBMISSION_FAILED", {"reason": reason})

    @staticmethod
    def _append_event(session: Session, attempt_id: str, job_id: str, event_type: str, metadata: dict[str, str]) -> None:
        run_id = (
            session.query(WorkOrderRecord.run_id)
            .join(AttemptRecord, AttemptRecord.work_order_id == WorkOrderRecord.work_order_id)
            .filter(AttemptRecord.attempt_id == attempt_id)
            .scalar()
        )
        if run_id is None:
            raise LookupError(f"attempt has no owning run: {attempt_id}")
        session.add(AuditEventRecord(
            event_id=f"evt_{uuid4().hex}", event_type=event_type, run_id=run_id,
            entity_type="job", entity_id=job_id, actor_type="controller",
            actor_id="job-manager", timestamp=datetime.now(UTC), correlation_id=attempt_id,
            causation_id=None, metadata_json=metadata,
        ))
