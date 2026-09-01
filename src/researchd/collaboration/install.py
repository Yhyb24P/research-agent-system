"""Transactional installation of versioned AgentDefinitions.

The install service stages every validation before touching the database,
then applies the profile, runtimes, and launch profiles in one
transaction, so a failure or crash can never leave an agent with a
registered profile but missing launch profiles. Validation and digest
rules are reused from the existing registry and launch-catalog services;
no parallel registry or profile model is introduced.
"""

from datetime import UTC, datetime

from pydantic import PositiveInt
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from researchd.collaboration.agent_definitions import AgentDefinition
from researchd.collaboration.contracts import AgentProfile, AgentRuntime
from researchd.collaboration.registry import AgentRegistryService
from researchd.domain.base import DomainModel
from researchd.domain.enums import AgentAdapterKind
from researchd.domain.ids import AgentId
from researchd.runtime_sessions.contracts import (
    LaunchMode,
    ProcessLaunchConfiguration,
    RemoteHttpLaunchConfiguration,
    RuntimeLaunchProfile,
)
from researchd.runtime_sessions.launch_profiles import RuntimeLaunchProfileService
from researchd.storage.models import (
    AgentRecord,
    AgentRuntimeRecord,
    RuntimeLaunchProfileRecord,
)


class AgentInstallation(DomainModel):
    """Receipt for an applied AgentDefinition."""

    agent_id: AgentId
    definition_version: PositiveInt
    definition_sha256: str
    runtimes: tuple[str, ...] = ()
    launch_profile_runtimes: tuple[str, ...] = ()


class AgentRemoval(DomainModel):
    """Receipt for disabling an installed Agent while preserving history."""

    agent_id: AgentId
    disabled_runtimes: tuple[str, ...] = ()


class AgentInstallService:
    """Install or update an AgentDefinition atomically against the trusted registry."""

    def __init__(
        self,
        sessions: sessionmaker[Session],
        registry: AgentRegistryService,
        launch_profiles: RuntimeLaunchProfileService,
    ) -> None:
        self.sessions = sessions
        self.registry = registry
        self.launch_profiles = launch_profiles

    def install(self, definition: AgentDefinition) -> AgentInstallation:
        self._validate_staged(definition)
        now = datetime.now(UTC)
        with self.sessions.begin() as session:
            self._apply_profile(session, definition.profile, now)
            for runtime in definition.runtimes:
                self._apply_runtime(session, runtime, now)
            for launch_profile in definition.launch_profiles:
                self._apply_launch_profile(session, launch_profile, now)
        return AgentInstallation(
            agent_id=definition.profile.agent_id,
            definition_version=definition.definition_version,
            definition_sha256=definition.definition_sha256(),
            runtimes=tuple(str(runtime.runtime_id) for runtime in definition.runtimes),
            launch_profile_runtimes=tuple(
                str(launch_profile.runtime_id) for launch_profile in definition.launch_profiles
            ),
        )

    def disable(self, agent_id: AgentId) -> AgentRemoval:
        """Disable an Agent, its runtimes and launch profiles atomically."""
        now = datetime.now(UTC)
        with self.sessions.begin() as session:
            agent = session.get(AgentRecord, str(agent_id))
            if agent is None:
                raise ValueError(f"agent does not exist: {agent_id}")
            runtimes = list(session.scalars(select(AgentRuntimeRecord).where(
                AgentRuntimeRecord.agent_id == str(agent_id)
            )))
            runtime_ids = tuple(sorted(row.runtime_id for row in runtimes))
            agent.enabled = False
            agent.version += 1
            agent.updated_at = now
            for runtime in runtimes:
                runtime.enabled = False
                runtime.version += 1
                runtime.updated_at = now
                launch = session.get(RuntimeLaunchProfileRecord, runtime.runtime_id)
                if launch is not None:
                    launch.enabled = False
                    launch.version += 1
                    launch.updated_at = now
        return AgentRemoval(agent_id=agent_id, disabled_runtimes=runtime_ids)

    def _validate_staged(self, definition: AgentDefinition) -> None:
        AgentRegistryService._validate_profile(definition.profile)
        runtimes = {str(runtime.runtime_id): runtime for runtime in definition.runtimes}
        for launch_profile in definition.launch_profiles:
            runtime = runtimes.get(str(launch_profile.runtime_id))
            if runtime is None:
                raise ValueError(
                    f"launch profile targets undeclared runtime {launch_profile.runtime_id}",
                )
            self._validate_launch_profile(runtime, launch_profile)

    @staticmethod
    def _validate_launch_profile(
        runtime: AgentRuntime,
        launch_profile: RuntimeLaunchProfile,
    ) -> None:
        if launch_profile.launch_mode is LaunchMode.PROCESS:
            if runtime.adapter_kind is not AgentAdapterKind.PROCESS:
                raise ValueError("PROCESS launch profile requires a PROCESS AgentRuntime")
            ProcessLaunchConfiguration.model_validate(launch_profile.configuration)
        elif launch_profile.launch_mode is LaunchMode.REMOTE_HTTP:
            if runtime.adapter_kind is not AgentAdapterKind.HTTP or runtime.endpoint_ref is None:
                raise ValueError("REMOTE_HTTP launch profile requires an HTTP AgentRuntime endpoint")
            RemoteHttpLaunchConfiguration.model_validate(launch_profile.configuration)
        else:
            raise ValueError(f"launch mode {launch_profile.launch_mode.value} is not installable")
        expected = RuntimeLaunchProfileService._digest(
            launch_profile.launch_mode, launch_profile.configuration,
        )
        if launch_profile.spec_sha256 != expected:
            raise ValueError("launch profile digest does not match its configuration")

    @staticmethod
    def _apply_profile(session: Session, profile: AgentProfile, now: datetime) -> None:
        row = session.get(AgentRecord, str(profile.agent_id))
        if row is None:
            session.add(AgentRecord(
                agent_id=str(profile.agent_id), display_name=profile.display_name,
                roles_json=list(profile.roles), skills_json=list(profile.skills),
                trust_zone=profile.trust_zone.value, constraints_json=list(profile.constraints),
                labels_json=dict(profile.labels),
                max_parallel_delegations=profile.max_parallel_delegations,
                enabled=profile.enabled, profile_version=profile.profile_version,
                version=1, created_at=now, updated_at=now,
            ))
            return
        row.display_name = profile.display_name
        row.roles_json = list(profile.roles)
        row.skills_json = list(profile.skills)
        row.trust_zone = profile.trust_zone.value
        row.constraints_json = list(profile.constraints)
        row.labels_json = dict(profile.labels)
        row.max_parallel_delegations = profile.max_parallel_delegations
        row.enabled = profile.enabled
        row.profile_version = max(row.profile_version + 1, profile.profile_version)
        row.version += 1
        row.updated_at = now

    @staticmethod
    def _apply_runtime(session: Session, runtime: AgentRuntime, now: datetime) -> None:
        row = session.get(AgentRuntimeRecord, str(runtime.runtime_id))
        if row is None:
            session.add(AgentRuntimeRecord(
                runtime_id=str(runtime.runtime_id), agent_id=str(runtime.agent_id),
                adapter_kind=runtime.adapter_kind.value, runtime_name=runtime.runtime_name,
                endpoint_ref=runtime.endpoint_ref, framework=runtime.framework,
                model_provider=runtime.model_provider, model_name=runtime.model_name,
                protocols_json=list(runtime.protocols), metadata_json=dict(runtime.metadata),
                enabled=True, version=1, created_at=now, updated_at=now,
            ))
            return
        if row.agent_id != str(runtime.agent_id):
            raise ValueError("runtime owner cannot be changed")
        row.adapter_kind = runtime.adapter_kind.value
        row.runtime_name = runtime.runtime_name
        row.endpoint_ref = runtime.endpoint_ref
        row.framework = runtime.framework
        row.model_provider = runtime.model_provider
        row.model_name = runtime.model_name
        row.protocols_json = list(runtime.protocols)
        row.metadata_json = dict(runtime.metadata)
        # Presence in an accepted AgentDefinition means the runtime is active.
        # Reinstallation is the explicit product operation that reverses a
        # prior disable while preserving the durable identity and history.
        row.enabled = True
        row.version += 1
        row.updated_at = now

    @staticmethod
    def _apply_launch_profile(
        session: Session,
        launch_profile: RuntimeLaunchProfile,
        now: datetime,
    ) -> None:
        digest = RuntimeLaunchProfileService._digest(
            launch_profile.launch_mode, launch_profile.configuration,
        )
        row = session.get(RuntimeLaunchProfileRecord, str(launch_profile.runtime_id))
        if row is None:
            session.add(RuntimeLaunchProfileRecord(
                runtime_id=str(launch_profile.runtime_id),
                launch_mode=launch_profile.launch_mode.value,
                configuration_json=dict(launch_profile.configuration),
                spec_sha256=digest,
                enabled=launch_profile.enabled,
                version=1, created_at=now, updated_at=now,
            ))
            return
        row.launch_mode = launch_profile.launch_mode.value
        row.configuration_json = dict(launch_profile.configuration)
        row.spec_sha256 = digest
        row.enabled = launch_profile.enabled
        row.version += 1
        row.updated_at = now


__all__ = ["AgentInstallation", "AgentInstallService", "AgentRemoval"]
