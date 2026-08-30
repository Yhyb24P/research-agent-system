"""Concrete composition root for the trusted local daemon."""

from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import re

from pydantic import ConfigDict, Field, field_validator, model_validator

from researchd.api.control import LocalControlAPI
from researchd.artifacts import ArtifactService, ContentAddressedArtifactStore
from researchd.backup.commands import BackupCommandService
from researchd.collaboration.delegation import DelegationService
from researchd.collaboration.action_broker import AgentActionBroker
from researchd.collaboration.gateway import CollaborationGateway
from researchd.collaboration.heterogeneous import (
    HttpAgentClient,
    HttpxAgentClient,
    ManagedProcessAgentAdapter,
)
from researchd.collaboration.invocation import InvocationService
from researchd.collaboration.registry import AgentRegistryService
from researchd.collaboration.runtime import AgentAdapterCatalog
from researchd.collaboration.selector import AgentSelector
from researchd.daemon.dispatcher import DaemonCommandDispatcher
from researchd.daemon.command_service import DurableDaemonCommandService
from researchd.daemon.reconciliation import (
    DaemonCommandResolutionService,
    build_builtin_observers,
)
from researchd.daemon.runtime import ResearchDaemon
from researchd.daemon.startup import build_startup_barrier
from researchd.domain.base import DomainModel
from researchd.domain.enums import AgentAdapterKind
from researchd.executor.jobs import JobManager, LocalDurableJobBackend
from researchd.executor.capability_broker import CapabilityBroker
from researchd.executor.contracts import CommandLimits
from researchd.executor.sandbox import BubblewrapBackend
from researchd.executor.worktree import WorktreeManager
from researchd.orchestrator.engine import ResearchOrchestrator
from researchd.policy.approval import ApprovalService
from researchd.policy.engine import DeterministicPolicyEngine, RecordingPolicyEngine
from researchd.runtime_sessions.service import RuntimeSessionService
from researchd.runtime_sessions.launch_profiles import RuntimeLaunchProfileService
from researchd.runtime_sessions.managed_start import ManagedAgentStartService
from researchd.storage.db import create_sqlite_engine, session_factory
from researchd.supervisor.runtime import RuntimeSupervisor
from researchd.verifier.driver import LocalVerificationDriver
from researchd.workspace.service import WorkspaceDelegationService
from researchd.workspace.transports import ArchiveWorkspaceTransport, GitWorktreeTransport


_CONFIGURATION_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")


class JobCommandConfig(DomainModel):
    argv: tuple[str, ...] = Field(min_length=1, max_length=128)

    @model_validator(mode="after")
    def validate_argv(self) -> "JobCommandConfig":
        if not Path(self.argv[0]).is_absolute():
            raise ValueError("job executable must be an absolute path")
        if any(not item or "\x00" in item for item in self.argv):
            raise ValueError("job argv contains an invalid argument")
        return self


class DaemonConfig(DomainModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    database: Path
    artifact_root: Path
    state_root: Path
    repositories: dict[str, Path] = Field(default_factory=dict)
    job_commands: dict[str, JobCommandConfig] = Field(default_factory=dict)
    executor_command_limits: CommandLimits = Field(default_factory=lambda: CommandLimits(
        wall_seconds=300,
        cpu_seconds=300,
        memory_mb=2048,
        file_size_mb=128,
        output_bytes=1_000_000,
    ))
    host: str = "127.0.0.1"
    port: int = Field(default=8788, ge=0, le=65_535)

    @field_validator("database", "artifact_root", "state_root")
    @classmethod
    def core_paths_are_absolute(cls, value: Path) -> Path:
        if not value.is_absolute():
            raise ValueError("daemon paths must be absolute")
        return value

    @field_validator("repositories")
    @classmethod
    def validate_repositories(cls, value: dict[str, Path]) -> dict[str, Path]:
        for repository_id, path in value.items():
            if _CONFIGURATION_ID.fullmatch(repository_id) is None:
                raise ValueError("repository ID is invalid")
            if not path.is_absolute():
                raise ValueError("repository paths must be absolute")
        return value

    @field_validator("job_commands")
    @classmethod
    def validate_job_types(
        cls,
        value: dict[str, JobCommandConfig],
    ) -> dict[str, JobCommandConfig]:
        if any(_CONFIGURATION_ID.fullmatch(job_type) is None for job_type in value):
            raise ValueError("job type is invalid")
        return value

    @field_validator("host")
    @classmethod
    def host_is_loopback(cls, value: str) -> str:
        if value not in {"127.0.0.1", "localhost", "::1"}:
            raise ValueError("researchd host must be loopback")
        return value

    def sha256(self) -> str:
        payload = json.dumps(
            self.model_dump(mode="json"),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
        return hashlib.sha256(payload).hexdigest()

    def inspection(self) -> dict[str, object]:
        """Return an operator projection without exposing command arguments."""
        return {
            "config_sha256": self.sha256(),
            "database": str(self.database),
            "artifact_root": str(self.artifact_root),
            "state_root": str(self.state_root),
            "repositories": {
                key: str(value) for key, value in sorted(self.repositories.items())
            },
            "job_commands": {
                key: {
                    "executable": value.argv[0],
                    "argument_count": len(value.argv) - 1,
                }
                for key, value in sorted(self.job_commands.items())
            },
            "executor_command_limits": self.executor_command_limits.model_dump(mode="json"),
            "host": self.host,
            "port": self.port,
        }


@dataclass(frozen=True)
class DaemonApplication:
    config: DaemonConfig
    daemon: ResearchDaemon
    api: LocalControlAPI
    managed_start: ManagedAgentStartService
    resolution: DaemonCommandResolutionService


def compose_daemon(
    config: DaemonConfig,
    *,
    agent_client: HttpAgentClient | None = None,
) -> DaemonApplication:
    """Wire existing authorities once; no service is reimplemented here."""
    database = config.database.resolve()
    artifacts_path = config.artifact_root.resolve()
    state = config.state_root.resolve()
    artifacts_path.mkdir(parents=True, exist_ok=True)
    state.mkdir(parents=True, exist_ok=True)

    engine = create_sqlite_engine(database)
    sessions = session_factory(engine)
    store = ContentAddressedArtifactStore(artifacts_path)
    artifact_service = ArtifactService(store, sessions)
    workspace = WorkspaceDelegationService(
        sessions,
        artifact_service,
        (
            GitWorktreeTransport(state / "workspace-git"),
            ArchiveWorkspaceTransport(state / "workspace-archive"),
        ),
    )
    worktrees = WorktreeManager(state / "worktrees", sessions)
    repositories = {key: value.resolve(strict=True) for key, value in config.repositories.items()}
    for repository_id, repository in repositories.items():
        if not (repository / ".git").exists():
            raise ValueError(f"configured repository is not a Git repository: {repository_id}")
    commands = {key: value.argv for key, value in config.job_commands.items()}
    jobs = JobManager(sessions, LocalDurableJobBackend(state / "jobs", commands))
    invocations = InvocationService(sessions)
    registry = AgentRegistryService(sessions)
    launch_profiles = RuntimeLaunchProfileService(sessions, registry)
    managed_start = ManagedAgentStartService(registry, launch_profiles)
    supervisor = RuntimeSupervisor(RuntimeSessionService(sessions, registry))
    barrier = build_startup_barrier(
        engine=engine,
        sessions=sessions,
        database=database,
        artifact_root=artifacts_path,
        workspace=workspace,
        worktrees=worktrees,
        repositories=repositories,
        supervisor=supervisor,
        jobs=jobs,
        invocations=invocations,
    )
    # The real trusted controller is composed here from existing authorities
    # only. Capabilities default to empty (fail-closed grants); budget and
    # iteration limits use the orchestrator constructor defaults. The
    # verification driver is the concrete LocalVerificationDriver over the
    # trusted verifier domain (immutable attempt artifacts, executor claims
    # never fed to the verifier). The adapter catalog registers only the
    # managed PROCESS executor adapter: the launch profile and live session
    # are resolved per runtime, never from request payloads or hardcoded Agent
    # identities; other adapter kinds stay unregistered so catalog resolution
    # fails closed for them. The adapter targets the already-supervised Agent
    # service and never launches the profile argv again.
    verifier_driver = LocalVerificationDriver(sessions, store)
    capability_broker = CapabilityBroker(
        BubblewrapBackend(),
        artifact_service,
        sessions,
        command_limits=config.executor_command_limits,
        inline_output_bytes=0,
    )
    action_broker = AgentActionBroker(sessions)
    catalog = AgentAdapterCatalog(sessions)
    catalog.register(
        AgentAdapterKind.PROCESS,
        ManagedProcessAgentAdapter(
            sessions,
            launch_profiles,
            agent_client or HttpxAgentClient(),
            capability_broker,
            action_broker,
        ),
    )
    orchestrator = ResearchOrchestrator(
        sessions,
        collaboration=CollaborationGateway(
            delegations=DelegationService(sessions),
            invocations=invocations,
            selector=AgentSelector(
                sessions,
                require_supervised_session=True,
                allowed_adapter_kinds=frozenset({AgentAdapterKind.PROCESS}),
                required_launch_mode="PROCESS",
            ),
            catalog=catalog,
        ),
        policy=RecordingPolicyEngine(DeterministicPolicyEngine(), sessions),
        verifier=verifier_driver,
        approvals=ApprovalService(sessions),
        jobs=jobs,
    )
    api = LocalControlAPI(sessions, orchestrator)
    dispatcher = DaemonCommandDispatcher(
        supervisor,
        api,
        backups=BackupCommandService(database, artifacts_path),
        managed_start=managed_start,
    )
    daemon = ResearchDaemon(barrier, DurableDaemonCommandService(sessions, dispatcher))
    resolution = DaemonCommandResolutionService(sessions, build_builtin_observers(sessions))
    return DaemonApplication(
        config=config,
        daemon=daemon,
        api=api,
        managed_start=managed_start,
        resolution=resolution,
    )


__all__ = ["DaemonApplication", "DaemonConfig", "JobCommandConfig", "compose_daemon"]
