"""Concrete composition root for the trusted local daemon."""

from dataclasses import dataclass
from pathlib import Path

from pydantic import ConfigDict, Field

from researchd.api.control import LocalControlAPI
from researchd.artifacts import ArtifactService, ContentAddressedArtifactStore
from researchd.collaboration.invocation import InvocationService
from researchd.collaboration.registry import AgentRegistryService
from researchd.daemon.dispatcher import DaemonCommandDispatcher
from researchd.daemon.runtime import ResearchDaemon
from researchd.daemon.startup import build_startup_barrier
from researchd.domain.base import DomainModel
from researchd.executor.jobs import JobManager, LocalDurableJobBackend
from researchd.executor.worktree import WorktreeManager
from researchd.runtime_sessions.service import RuntimeSessionService
from researchd.storage.db import create_sqlite_engine, session_factory
from researchd.supervisor.runtime import RuntimeSupervisor
from researchd.workspace.service import WorkspaceDelegationService
from researchd.workspace.transports import ArchiveWorkspaceTransport, GitWorktreeTransport


class DaemonConfig(DomainModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    database: Path
    artifact_root: Path
    state_root: Path
    repositories: dict[str, Path] = Field(default_factory=dict)
    host: str = "127.0.0.1"
    port: int = Field(default=8788, ge=0, le=65_535)


@dataclass(frozen=True)
class DaemonApplication:
    config: DaemonConfig
    daemon: ResearchDaemon
    api: LocalControlAPI


def compose_daemon(config: DaemonConfig) -> DaemonApplication:
    """Wire existing authorities once; no service is reimplemented here."""
    database = config.database.resolve()
    artifacts_path = config.artifact_root.resolve()
    state = config.state_root.resolve()
    artifacts_path.mkdir(parents=True, exist_ok=True)
    state.mkdir(parents=True, exist_ok=True)

    engine = create_sqlite_engine(database)
    sessions = session_factory(engine)
    artifact_service = ArtifactService(ContentAddressedArtifactStore(artifacts_path), sessions)
    workspace = WorkspaceDelegationService(
        sessions,
        artifact_service,
        (
            GitWorktreeTransport(state / "workspace-git"),
            ArchiveWorkspaceTransport(state / "workspace-archive"),
        ),
    )
    worktrees = WorktreeManager(state / "worktrees", sessions)
    jobs = JobManager(sessions, LocalDurableJobBackend(state / "jobs", {}))
    invocations = InvocationService(sessions)
    registry = AgentRegistryService(sessions)
    supervisor = RuntimeSupervisor(RuntimeSessionService(sessions, registry))
    barrier = build_startup_barrier(
        engine=engine,
        sessions=sessions,
        database=database,
        artifact_root=artifacts_path,
        workspace=workspace,
        worktrees=worktrees,
        repositories={key: value.resolve() for key, value in config.repositories.items()},
        supervisor=supervisor,
        jobs=jobs,
        invocations=invocations,
    )
    daemon = ResearchDaemon(barrier, DaemonCommandDispatcher(supervisor))
    return DaemonApplication(config=config, daemon=daemon, api=LocalControlAPI(sessions))


__all__ = ["DaemonApplication", "DaemonConfig", "compose_daemon"]
