"""Ordered fail-closed recovery barrier executed before daemon mutations."""

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Protocol

from sqlalchemy import Engine, select, text
from sqlalchemy.orm import Session, sessionmaker

from researchd.backup import check_restored_snapshot
from researchd.collaboration.invocation import InvocationService
from researchd.domain.enums import InvocationStatus, JobState
from researchd.executor.jobs import JobManager
from researchd.executor.worktree import WorktreeManager
from researchd.storage.models import (
    AgentInvocationRecord,
    AttemptWorktreeRecord,
    JobRecord,
    RuntimeSessionRecord,
    WorkspaceGrantRecord,
)
from researchd.runtime_sessions.contracts import SupervisorState
from researchd.executor.worktree import WorktreeState
from researchd.workspace.contracts import CleanupState, WorkspaceGrantState
from researchd.supervisor.runtime import RuntimeSupervisor
from researchd.workspace.service import WorkspaceDelegationService


EXPECTED_SCHEMA_REVISION = "0028"


class StartupPhase(StrEnum):
    MIGRATION_CHECK = "MIGRATION_CHECK"
    STORAGE_SANITY = "STORAGE_SANITY"
    WORKSPACE_RECOVERY = "WORKSPACE_RECOVERY"
    WORKTREE_RECOVERY = "WORKTREE_RECOVERY"
    RUNTIME_RECONCILIATION = "RUNTIME_RECONCILIATION"
    JOB_RECONCILIATION = "JOB_RECONCILIATION"
    INVOCATION_RECONCILIATION = "INVOCATION_RECONCILIATION"
    AUDIT_STREAM_HEALTH = "AUDIT_STREAM_HEALTH"


class StartupPhaseStatus(StrEnum):
    PASS = "PASS"
    FAIL = "FAIL"
    SKIPPED = "SKIPPED"


@dataclass(frozen=True)
class StartupPhaseReport:
    phase: StartupPhase
    status: StartupPhaseStatus
    affected_count: int = 0
    error_type: str | None = None


@dataclass(frozen=True)
class StartupReport:
    ready: bool
    phases: tuple[StartupPhaseReport, ...]

    def as_dict(self) -> dict[str, object]:
        return {
            "ready": self.ready,
            "phases": [
                {
                    "phase": item.phase.value,
                    "status": item.status.value,
                    "affected_count": item.affected_count,
                    "error_type": item.error_type,
                }
                for item in self.phases
            ],
        }


class StartupAction(Protocol):
    def __call__(self) -> object: ...


class StartupBarrier:
    """Run every recovery phase in frozen order and stop on first failure."""

    ORDER = tuple(StartupPhase)

    def __init__(self, actions: Mapping[StartupPhase, StartupAction]) -> None:
        missing = set(self.ORDER) - set(actions)
        extra = set(actions) - set(self.ORDER)
        if missing or extra:
            raise ValueError("startup barrier requires exactly one action per phase")
        self.actions = dict(actions)

    def run(self) -> StartupReport:
        reports: list[StartupPhaseReport] = []
        failed = False
        for phase in self.ORDER:
            if failed:
                reports.append(StartupPhaseReport(phase, StartupPhaseStatus.SKIPPED))
                continue
            try:
                result = self.actions[phase]()
                if result is False:
                    raise RuntimeError("startup phase returned false")
            except Exception as error:
                reports.append(StartupPhaseReport(
                    phase,
                    StartupPhaseStatus.FAIL,
                    error_type=type(error).__name__,
                ))
                failed = True
            else:
                reports.append(StartupPhaseReport(
                    phase,
                    StartupPhaseStatus.PASS,
                    affected_count=_affected_count(result),
                ))
        return StartupReport(ready=not failed, phases=tuple(reports))


def build_startup_barrier(
    *,
    engine: Engine,
    sessions: sessionmaker[Session],
    database: Path,
    artifact_root: Path,
    workspace: WorkspaceDelegationService,
    worktrees: WorktreeManager,
    repositories: Mapping[str, Path],
    supervisor: RuntimeSupervisor,
    jobs: JobManager,
    invocations: InvocationService,
) -> StartupBarrier:
    """Compose the frozen LP01 startup order from existing trusted services."""
    return StartupBarrier({
        StartupPhase.MIGRATION_CHECK: lambda: verify_migration_head(engine),
        StartupPhase.STORAGE_SANITY: lambda: verify_storage_sanity(
            database,
            artifact_root,
        ),
        StartupPhase.WORKSPACE_RECOVERY: lambda: recover_workspaces_safely(
            sessions, workspace,
        ),
        StartupPhase.WORKTREE_RECOVERY: lambda: recover_worktrees_safely(
            sessions, worktrees, repositories,
        ),
        StartupPhase.RUNTIME_RECONCILIATION: lambda: reconcile_runtimes_safely(
            sessions, supervisor,
        ),
        StartupPhase.JOB_RECONCILIATION: lambda: reconcile_jobs_safely(
            sessions, jobs,
        ),
        StartupPhase.INVOCATION_RECONCILIATION: lambda: recover_invocations(
            sessions,
            invocations,
        ),
        StartupPhase.AUDIT_STREAM_HEALTH: lambda: verify_audit_stream(engine),
    })


def verify_migration_head(
    engine: Engine,
    expected_revision: str = EXPECTED_SCHEMA_REVISION,
) -> None:
    with engine.connect() as connection:
        revisions = tuple(connection.scalars(text("SELECT version_num FROM alembic_version")))
    if revisions != (expected_revision,):
        raise RuntimeError("database migration head does not match researchd")


def verify_storage_sanity(
    database: Path,
    artifact_root: Path,
    expected_revision: str = EXPECTED_SCHEMA_REVISION,
) -> None:
    report = check_restored_snapshot(database, artifact_root)
    if not report.healthy or report.schema_revision != expected_revision:
        raise RuntimeError("authoritative database/CAS state is not healthy")


def recover_invocations(
    sessions: sessionmaker[Session],
    invocations: InvocationService,
) -> tuple[str, ...]:
    with sessions() as session:
        run_ids = tuple(session.scalars(
            select(AgentInvocationRecord.run_id)
            .where(AgentInvocationRecord.status == InvocationStatus.RUNNING.value)
            .distinct()
            .order_by(AgentInvocationRecord.run_id)
        ))
    recovered: list[str] = []
    for run_id in run_ids:
        recovered.extend(invocations.recover_run(run_id))
    with sessions() as session:
        unresolved = session.scalar(select(AgentInvocationRecord.invocation_id).where(
            AgentInvocationRecord.status == InvocationStatus.RUNNING.value,
        ).limit(1))
    if unresolved is not None:
        raise RuntimeError("invocation recovery requires operator reconciliation")
    return tuple(recovered)


def recover_workspaces_safely(
    sessions: sessionmaker[Session],
    workspace: WorkspaceDelegationService,
) -> tuple[str, ...]:
    recovered = workspace.recover_incomplete()
    with sessions() as session:
        unresolved = session.scalar(select(WorkspaceGrantRecord.workspace_grant_id).where(
            (WorkspaceGrantRecord.state == WorkspaceGrantState.RECOVERING.value)
            | (WorkspaceGrantRecord.cleanup_state == CleanupState.FAILED.value),
        ).limit(1))
    if unresolved is not None:
        raise RuntimeError("workspace recovery left an unsafe grant")
    return recovered


def recover_worktrees_safely(
    sessions: sessionmaker[Session],
    worktrees: WorktreeManager,
    repositories: Mapping[str, Path],
) -> tuple[str, ...]:
    recovered = worktrees.recover_incomplete(repositories)
    with sessions() as session:
        unresolved = session.scalar(select(AttemptWorktreeRecord.attempt_id).where(
            AttemptWorktreeRecord.state == WorktreeState.CLEANUP_FAILED,
        ).limit(1))
    if unresolved is not None:
        raise RuntimeError("worktree recovery left an unsafe worktree")
    return recovered


def reconcile_runtimes_safely(
    sessions: sessionmaker[Session],
    supervisor: RuntimeSupervisor,
) -> tuple[object, ...]:
    reconciled = supervisor.reconcile_sessions()
    with sessions() as session:
        unresolved = session.scalar(select(RuntimeSessionRecord.runtime_session_id).where(
            RuntimeSessionRecord.supervisor_state
            == SupervisorState.RECONCILIATION_REQUIRED.value,
        ).limit(1))
    if unresolved is not None:
        raise RuntimeError("runtime recovery requires operator reconciliation")
    return reconciled


def reconcile_jobs_safely(
    sessions: sessionmaker[Session],
    jobs: JobManager,
) -> tuple[object, ...]:
    reconciled = tuple(jobs.reconcile())
    with sessions() as session:
        unresolved = session.scalar(select(JobRecord.job_id).where(
            JobRecord.state == JobState.LOST.value,
        ).limit(1))
    if unresolved is not None:
        raise RuntimeError("job recovery requires operator reconciliation")
    return reconciled


def verify_audit_stream(engine: Engine) -> None:
    """Verify the database-owned cursor has no holes, duplicates, or drift."""
    with engine.connect() as connection:
        if connection.scalar(text("PRAGMA quick_check")) != "ok":
            raise RuntimeError("database quick check failed")
        if connection.execute(text("PRAGMA foreign_key_check")).first() is not None:
            raise RuntimeError("database foreign-key check failed")
        event_count = int(connection.scalar(text("SELECT COUNT(*) FROM audit_events")) or 0)
        sequenced_count = int(connection.scalar(text(
            "SELECT COUNT(DISTINCT audit_seq) FROM audit_events WHERE audit_seq IS NOT NULL"
        )) or 0)
        maximum = int(connection.scalar(text("SELECT COALESCE(MAX(audit_seq), 0) FROM audit_events")) or 0)
        next_sequence = int(connection.scalar(text(
            "SELECT next_seq FROM audit_stream_clock WHERE singleton = 1"
        )) or 0)
        trigger_count = int(connection.scalar(text(
            "SELECT COUNT(*) FROM sqlite_master "
            "WHERE type = 'trigger' AND name = 'audit_events_assign_seq'"
        )) or 0)
        unresolved_commands = int(connection.scalar(text(
            "SELECT COUNT(*) FROM daemon_commands WHERE status = 'ACCEPTED'"
        )) or 0)
    if sequenced_count != event_count or maximum != event_count:
        raise RuntimeError("audit stream sequence is not contiguous")
    if next_sequence != maximum + 1 or trigger_count != 1:
        raise RuntimeError("audit stream allocator is unhealthy")
    if unresolved_commands:
        raise RuntimeError("daemon command outcome requires operator reconciliation")


def _affected_count(result: object) -> int:
    if result is None or isinstance(result, bool):
        return 0
    if isinstance(result, (tuple, list, set, frozenset, dict)):
        return len(result)
    return 1
