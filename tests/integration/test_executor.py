import asyncio
import os
import subprocess
import threading
import time
from datetime import UTC, datetime
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy import select

from researchd.artifacts.provenance import ArtifactService
from researchd.artifacts.store import ContentAddressedArtifactStore
from researchd.domain.enums import AttemptState, Capability, JobState, NetworkMode, ResearchRunState, WorkOrderState
from researchd.executor.capability_broker import CapabilityBroker
from researchd.executor.contracts import (
    CapabilityRequest,
    CommandLimits,
    GrantedWorkOrder,
    JobHandle,
    JobResources,
    JobSpec,
    LocalAgentRequest,
    LocalAgentResponse,
    SandboxMount,
    SandboxSpec,
)
from researchd.executor.jobs import JobManager, LocalDurableJobBackend
from researchd.executor.gpu import GpuAdmissionController, GpuAdmissionError
from researchd.executor.sandbox import BubblewrapBackend
from researchd.executor.worker import LocalExecutorWorker
from researchd.executor.worktree import WorktreeError, WorktreeManager
from researchd.models.base import LocalModelUnavailable
from researchd.models.vllm import VLLMLocalModel
from researchd.storage.db import create_sqlite_engine, session_factory
from researchd.storage.models import AttemptRecord, AttemptWorktreeRecord, AuditEventRecord, JobRecord, ResearchRunRecord, WorkspaceRecord, WorkOrderRecord
from researchd.storage.repositories import JobRepository

ROOT = Path(__file__).parents[2]
RUNTIME = ROOT / ".venv"


def migrate(path: Path) -> None:
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("sqlalchemy.url", f"sqlite:///{path}")
    command.upgrade(config, "head")
    command.check(config)


def seed_database(path: Path, attempt_id: str = "att_exec") -> sessionmaker[Session]:
    migrate(path)
    sessions = session_factory(create_sqlite_engine(path))
    now = datetime.now(UTC)
    with sessions.begin() as session:
        session.add(WorkspaceRecord(workspace_id="ws_exec", name="executor", version=1, created_at=now, updated_at=now))
        session.flush()
        session.add(ResearchRunRecord(run_id="run_exec", workspace_id="ws_exec", objective="fix fixture", state=ResearchRunState.ACTIVE.value, version=1, created_at=now, updated_at=now))
        session.flush()
        session.add(WorkOrderRecord(work_order_id="wo_exec", run_id="run_exec", parent_work_order_id=None, objective="fix add", state=WorkOrderState.EXECUTING.value, idempotency_key="executor-idempotency-0001", contract={}, version=1, created_at=now, updated_at=now))
        session.flush()
        session.add(AttemptRecord(attempt_id=attempt_id, work_order_id="wo_exec", state=AttemptState.RUNNING.value, terminal_at=None, version=1, created_at=now, updated_at=now))
    return sessions


def git(repository: Path, *arguments: str) -> str:
    result = subprocess.run(["/usr/bin/git", "-C", str(repository), *arguments], check=True, stdout=subprocess.PIPE, text=True)
    return result.stdout


def fixture_repository(path: Path) -> Path:
    path.mkdir()
    git(path, "init", "-q")
    git(path, "config", "user.email", "fixture@example.invalid")
    git(path, "config", "user.name", "Fixture")
    (path / "calc.py").write_text("def add(a, b):\n    return a - b\n")
    (path / "test_calc.py").write_text("from calc import add\n\ndef test_add():\n    assert add(2, 3) == 5\n")
    git(path, "add", ".")
    git(path, "commit", "-qm", "fixture")
    return path


def sandbox_for(path: Path, attempt_id: str = "att_exec") -> SandboxSpec:
    return SandboxSpec(
        attempt_id=attempt_id, workspace=str(path), network=NetworkMode.NONE,
        mounts=(SandboxMount(source=str(RUNTIME), target="/runtime", read_only=True),),
    )


def limits() -> CommandLimits:
    return CommandLimits(wall_seconds=10, cpu_seconds=8, memory_mb=768, file_size_mb=16, output_bytes=128_000)


class FixingFakeLocalModel:
    def __init__(self) -> None:
        self.calls = 0

    async def complete(self, request: LocalAgentRequest) -> LocalAgentResponse:
        self.calls += 1
        if not request.prior_results:
            return LocalAgentResponse(actions=(
                CapabilityRequest(request_id="step_write_fix", capability=Capability.WORKSPACE_WRITE, parameters={"path": "calc.py", "content": "def add(a, b):\n    return a + b\n"}),
                CapabilityRequest(request_id="step_run_pytest", capability=Capability.TEST_RUN, parameters={"target": "test_calc.py"}),
            ))
        return LocalAgentResponse(final_claim="The isolated fixture test now passes.")


class UnavailableLocalModel:
    async def complete(self, request: LocalAgentRequest) -> LocalAgentResponse:
        del request
        raise LocalModelUnavailable("fixture local model outage")


def test_fake_work_order_modifies_only_isolated_worktree_and_runs_pytest(tmp_path: Path) -> None:
    repository = fixture_repository(tmp_path / "repo")
    sessions = seed_database(tmp_path / "executor.db")
    handle = WorktreeManager(tmp_path / "worktrees", sessions).create(repository, repository_id="repo-fixture", attempt_id="att_exec")
    artifacts = ArtifactService(ContentAddressedArtifactStore(tmp_path / "artifacts"), sessions)
    broker = CapabilityBroker(BubblewrapBackend(), artifacts, sessions, command_limits=limits())
    model = FixingFakeLocalModel()
    worker = LocalExecutorWorker(model, broker, sessions)
    work_order = GrantedWorkOrder(
        attempt_id="att_exec", objective="fix add and run the focused test",
        granted_capabilities=frozenset({Capability.WORKSPACE_WRITE, Capability.TEST_RUN}),
        sandbox=sandbox_for(handle.path),
    )
    result = asyncio.run(worker.execute(work_order))
    assert result.status == "execution_complete"
    assert [item.status for item in result.capability_results] == ["ok", "ok"]
    assert "a + b" in (handle.path / "calc.py").read_text()
    assert "a - b" in (repository / "calc.py").read_text()
    with sessions() as session:
        persisted = session.get(AttemptWorktreeRecord, "att_exec")
        assert persisted is not None
        assert persisted.base_commit == handle.base_commit and persisted.environment_digest == handle.environment_digest
    repeated = asyncio.run(worker.execute(work_order))
    assert repeated == result
    assert model.calls == 2


def test_local_model_outage_has_no_cloud_fallback(tmp_path: Path) -> None:
    repository = fixture_repository(tmp_path / "repo")
    handle = WorktreeManager(tmp_path / "worktrees").create(repository, repository_id="repo", attempt_id="att_outage")
    sessions = seed_database(tmp_path / "outage.db", attempt_id="att_outage")
    artifacts = ArtifactService(ContentAddressedArtifactStore(tmp_path / "artifacts"), sessions)
    worker = LocalExecutorWorker(UnavailableLocalModel(), CapabilityBroker(BubblewrapBackend(), artifacts, sessions, command_limits=limits()), sessions)
    result = asyncio.run(worker.execute(GrantedWorkOrder(
        attempt_id="att_outage", objective="fixture", granted_capabilities=frozenset(),
        sandbox=sandbox_for(handle.path, "att_outage"),
    )))
    assert result.status == "model_unavailable"
    assert "local model outage" in result.errors[0]


def test_dirty_worktree_is_never_reused_between_attempts(tmp_path: Path) -> None:
    repository = fixture_repository(tmp_path / "repo")
    manager = WorktreeManager(tmp_path / "worktrees")
    first = manager.create(repository, repository_id="repo", attempt_id="att_one")
    (first.path / "calc.py").write_text("dirty attempt one")
    second = manager.create(repository, repository_id="repo", attempt_id="att_two")
    assert "a - b" in (second.path / "calc.py").read_text()
    assert first.path != second.path
    with pytest.raises(WorktreeError, match="never reused"):
        manager.create(repository, repository_id="repo", attempt_id="att_one")


def test_capability_broker_blocks_traversal_symlink_and_reuses_step_result(tmp_path: Path) -> None:
    repository = fixture_repository(tmp_path / "repo")
    handle = WorktreeManager(tmp_path / "worktrees").create(repository, repository_id="repo", attempt_id="att_exec")
    outside = tmp_path / "outside-secret"
    outside.write_text("host secret")
    (handle.path / "escape-link").symlink_to(outside)
    sessions = seed_database(tmp_path / "broker.db")
    artifacts = ArtifactService(ContentAddressedArtifactStore(tmp_path / "artifacts"), sessions)
    broker = CapabilityBroker(BubblewrapBackend(), artifacts, sessions, command_limits=limits())
    granted = frozenset({Capability.WORKSPACE_READ, Capability.WORKSPACE_WRITE})
    traversal = broker.execute(
        CapabilityRequest(request_id="step_traversal", capability=Capability.WORKSPACE_READ, parameters={"path": "../../etc/passwd"}),
        granted=granted, sandbox=sandbox_for(handle.path),
    )
    symlink = broker.execute(
        CapabilityRequest(request_id="step_symlink", capability=Capability.WORKSPACE_WRITE, parameters={"path": "escape-link", "content": "overwrite"}),
        granted=granted, sandbox=sandbox_for(handle.path),
    )
    request = CapabilityRequest(request_id="step_idempotent", capability=Capability.WORKSPACE_WRITE, parameters={"path": "result.txt", "content": "once"})
    first = broker.execute(request, granted=granted, sandbox=sandbox_for(handle.path))
    second = broker.execute(request, granted=granted, sandbox=sandbox_for(handle.path))
    mismatch = broker.execute(
        CapabilityRequest(request_id="step_idempotent", capability=Capability.WORKSPACE_WRITE, parameters={"path": "result.txt", "content": "changed"}),
        granted=granted, sandbox=sandbox_for(handle.path),
    )
    assert traversal.status == "denied" and symlink.status == "denied"
    assert outside.read_text() == "host secret"
    assert first == second and (handle.path / "result.txt").read_text() == "once"
    assert mismatch.reason_code == "IDEMPOTENCY_KEY_MISMATCH"


def test_capability_broker_rejects_parent_symlink_and_racing_replacement(tmp_path: Path) -> None:
    repository = fixture_repository(tmp_path / "repo")
    handle = WorktreeManager(tmp_path / "worktrees").create(repository, repository_id="repo", attempt_id="att_exec")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "marker.txt").write_text("must remain untouched")
    (handle.path / "parent-link").symlink_to(outside, target_is_directory=True)
    sessions = seed_database(tmp_path / "broker-race.db")
    artifacts = ArtifactService(ContentAddressedArtifactStore(tmp_path / "artifacts"), sessions)
    broker = CapabilityBroker(BubblewrapBackend(), artifacts, sessions, command_limits=limits())
    granted = frozenset({Capability.WORKSPACE_WRITE})
    static = broker.execute(
        CapabilityRequest(
            request_id="step_parent_symlink", capability=Capability.WORKSPACE_WRITE,
            parameters={"path": "parent-link/escape.txt", "content": "must not escape"},
        ), granted=granted, sandbox=sandbox_for(handle.path),
    )
    assert static.status == "denied"

    race = handle.path / "race-link"
    stop = threading.Event()

    def replace_with_symlink() -> None:
        while not stop.is_set():
            try:
                race.unlink()
            except FileNotFoundError:
                pass
            except IsADirectoryError:
                # The broker may have securely created the directory.  Leave
                # a non-empty directory in place; the next operation remains
                # anchored by its directory fd.
                continue
            try:
                race.symlink_to(outside, target_is_directory=True)
            except FileExistsError:
                pass

    attacker = threading.Thread(target=replace_with_symlink)
    attacker.start()
    try:
        results = [broker.execute(
            CapabilityRequest(
                request_id=f"step_race_{index}", capability=Capability.WORKSPACE_WRITE,
                parameters={"path": "race-link/escape.txt", "content": "must not escape"},
            ), granted=granted, sandbox=sandbox_for(handle.path),
        ) for index in range(40)]
    finally:
        stop.set()
        attacker.join(timeout=2)
    assert all(result.status in {"denied", "ok"} for result in results)
    assert not (outside / "escape.txt").exists()
    assert (outside / "marker.txt").read_text() == "must remain untouched"


def test_duplicate_job_operation_and_restart_reconcile_running_job(tmp_path: Path) -> None:
    repository = fixture_repository(tmp_path / "repo")
    handle = WorktreeManager(tmp_path / "worktrees").create(repository, repository_id="repo", attempt_id="att_exec")
    sessions = seed_database(tmp_path / "jobs.db")
    backend_root = tmp_path / "job-backend"
    commands = {"sleep_fixture": ("/usr/bin/python3", "-c", "import time; time.sleep(30)")}
    manager = JobManager(sessions, LocalDurableJobBackend(backend_root, commands))
    spec = JobSpec(
        job_type="sleep_fixture", attempt_id="att_exec", resources=JobResources(memory_mb=256),
        operation_id="op_restart_fixture", workspace=str(handle.path), network=NetworkMode.NONE,
    )
    first = manager.submit(spec)
    second = manager.submit(spec)
    assert first.job_id == second.job_id
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        status = LocalDurableJobBackend(backend_root, commands).get("op_restart_fixture")
        if status is not None and status.state == JobState.RUNNING.value:
            break
        time.sleep(0.05)
    restarted = JobManager(sessions, LocalDurableJobBackend(backend_root, commands))
    records = restarted.reconcile()
    assert len(records) == 1 and records[0].job_id == first.job_id
    assert records[0].state in {JobState.RUNNING.value, JobState.SUBMITTED.value}
    cancelled = restarted.cancel(first.job_id)
    assert cancelled.state == JobState.CANCELLED.value
    with sessions() as session:
        event_types = session.scalars(select(AuditEventRecord.event_type).where(AuditEventRecord.entity_id == first.job_id).order_by(AuditEventRecord.timestamp)).all()
        assert event_types == ["JOB_SUBMISSION_RESERVED", "JOB_SUBMITTED", "JOB_STATUS_CHANGED", "JOB_CANCEL_REQUESTED", "JOB_STATUS_CHANGED"]


def test_gpu_admission_is_exclusive_durable_and_releasable(tmp_path: Path) -> None:
    sessions = seed_database(tmp_path / "gpu-admission.db")
    now = datetime.now(UTC)
    with sessions.begin() as session:
        session.add_all([
            JobRecord(job_id="job_gpu_a", attempt_id="att_exec", operation_id="op-gpu-a", state=JobState.CREATED.value, backend="gpu", native_handle=None, version=1, created_at=now, updated_at=now),
            JobRecord(job_id="job_gpu_b", attempt_id="att_exec", operation_id="op-gpu-b", state=JobState.CREATED.value, backend="gpu", native_handle=None, version=1, created_at=now, updated_at=now),
        ])
    admission = GpuAdmissionController(sessions, ("gpu0",))
    first = admission.acquire("job_gpu_a", 1)
    assert [item.device_id for item in first] == ["gpu0"]
    with pytest.raises(GpuAdmissionError, match="insufficient"):
        admission.acquire("job_gpu_b", 1)
    # A newly constructed controller observes the persisted lease.
    assert [item.job_id for item in GpuAdmissionController(sessions, ("gpu0",)).active()] == ["job_gpu_a"]
    assert admission.release("job_gpu_a") == ("gpu0",)
    second = admission.acquire("job_gpu_b", 1)
    assert [item.device_id for item in second] == ["gpu0"]
    assert admission.release("job_gpu_b") == ("gpu0",)


def test_gpu_assignment_is_passed_to_backend_submission(tmp_path: Path) -> None:
    sessions = seed_database(tmp_path / "gpu-binding.db")
    captured: list[JobSpec] = []

    class GpuBackend:
        def submit(self, spec: JobSpec) -> JobHandle:
            captured.append(spec)
            return JobHandle(native_handle="native-gpu", state=JobState.SUBMITTED.value)

        def find_by_operation(self, operation_id: str) -> JobHandle | None:
            del operation_id
            return None

        def get(self, native_handle: str) -> JobHandle | None:
            del native_handle
            return None

        def cancel(self, native_handle: str) -> JobHandle:
            del native_handle
            return JobHandle(native_handle="native-gpu", state=JobState.CANCELLED.value)

    spec = JobSpec(
        job_type="gpu_fixture", attempt_id="att_exec",
        resources=JobResources(gpu_count=1, max_gpu_seconds=10, memory_mb=256),
        operation_id="op-gpu-binding", workspace=str(tmp_path), network=NetworkMode.NONE,
    )
    manager = JobManager(sessions, GpuBackend(), GpuAdmissionController(sessions, ("gpu0",)))
    record = manager.submit(spec)
    assert record.state == JobState.SUBMITTED.value
    assert len(captured) == 1
    assert captured[0].gpu_device_ids == ("gpu0",)
    assert spec.gpu_device_ids == ()
    manager.cancel(record.job_id)


def test_gpu_job_without_admission_controller_fails_closed(tmp_path: Path) -> None:
    sessions = seed_database(tmp_path / "gpu-required.db")
    backend = LocalDurableJobBackend(tmp_path / "job-backend", {"gpu_fixture": ("/usr/bin/true",)})
    manager = JobManager(sessions, backend)
    spec = JobSpec(
        job_type="gpu_fixture", attempt_id="att_exec", resources=JobResources(gpu_count=1, max_gpu_seconds=10, memory_mb=256),
        operation_id="op-gpu-required", workspace=str(tmp_path), network=NetworkMode.NONE,
    )
    with pytest.raises(GpuAdmissionError, match="required"):
        manager.submit(spec)
    with sessions() as session:
        record = JobRepository(session).get_by_operation_id(spec.operation_id)
        assert record is not None and record.state == JobState.FAILED.value


def test_lost_job_holds_gpu_lease_until_explicit_resolution(tmp_path: Path) -> None:
    sessions = seed_database(tmp_path / "gpu-lost.db")
    now = datetime.now(UTC)
    with sessions.begin() as session:
        session.add(JobRecord(
            job_id="job_gpu_lost", attempt_id="att_exec", operation_id="op-gpu-lost",
            state=JobState.SUBMITTED.value, backend="gpu", native_handle="native-lost",
            version=1, created_at=now, updated_at=now,
        ))
    admission = GpuAdmissionController(sessions, ("gpu0",))
    admission.acquire("job_gpu_lost", 1)

    class MissingBackend:
        def submit(self, spec: JobSpec) -> JobHandle:
            del spec
            raise AssertionError("submit is not used during reconciliation")

        def find_by_operation(self, operation_id: str) -> JobHandle | None:
            del operation_id
            return None

        def get(self, native_handle: str) -> JobHandle | None:
            del native_handle
            return None

        def cancel(self, native_handle: str) -> JobHandle:
            del native_handle
            raise AssertionError("cancel is not used")

    manager = JobManager(sessions, MissingBackend(), admission)
    records = manager.reconcile()
    assert records[0].state == JobState.LOST.value
    assert [item.device_id for item in admission.active("job_gpu_lost")] == ["gpu0"]
    manager.release_lost("job_gpu_lost")
    assert admission.active("job_gpu_lost") == ()


def test_gpu_reconcile_releases_stale_lease_for_known_terminal_job(tmp_path: Path) -> None:
    sessions = seed_database(tmp_path / "gpu-reconcile.db")
    now = datetime.now(UTC)
    with sessions.begin() as session:
        session.add(JobRecord(
            job_id="job_gpu_done", attempt_id="att_exec", operation_id="op-gpu-done",
            state=JobState.SUCCEEDED.value, backend="gpu", native_handle="native-done",
            version=1, created_at=now, updated_at=now,
        ))
    admission = GpuAdmissionController(sessions, ("gpu0",))
    admission.acquire("job_gpu_done", 1)
    assert admission.reconcile() == ("job_gpu_done",)
    assert admission.active("job_gpu_done") == ()


def test_job_crash_window_reconciles_side_effect_without_native_handle(tmp_path: Path) -> None:
    repository = fixture_repository(tmp_path / "repo")
    handle = WorktreeManager(tmp_path / "worktrees").create(repository, repository_id="repo", attempt_id="att_exec")
    sessions = seed_database(tmp_path / "crash-window.db")
    backend = LocalDurableJobBackend(
        tmp_path / "job-backend",
        {"sleep_fixture": ("/usr/bin/python3", "-c", "import time; time.sleep(30)")},
    )
    spec = JobSpec(
        job_type="sleep_fixture", attempt_id="att_exec", resources=JobResources(memory_mb=256),
        operation_id="op_crash_window", workspace=str(handle.path), network=NetworkMode.NONE,
    )
    now = datetime.now(UTC)
    with sessions.begin() as session:
        session.add(JobRecord(
            job_id="job_crash_window", attempt_id="att_exec", operation_id=spec.operation_id,
            state=JobState.CREATED.value, backend="local", native_handle=None,
            version=1, created_at=now, updated_at=now,
        ))
    backend.submit(spec)  # Side effect happened; simulate crash before DB handle update.
    records = JobManager(sessions, LocalDurableJobBackend(tmp_path / "job-backend", backend.commands)).reconcile()
    assert [(record.job_id, record.native_handle) for record in records] == [("job_crash_window", "op_crash_window")]
    JobManager(sessions, backend).cancel("job_crash_window")


def test_vllm_adapter_refuses_non_loopback_endpoint() -> None:
    with pytest.raises(ValueError, match="loopback"):
        VLLMLocalModel(base_url="https://api.example.com", model="not-local")
