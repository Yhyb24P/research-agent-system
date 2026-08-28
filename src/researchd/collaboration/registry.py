from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from researchd.collaboration.contracts import AgentProfile, AgentRuntime, DiscoveredAgentDescriptor
from researchd.domain.enums import AgentTrustZone
from researchd.domain.ids import AgentId
from researchd.storage.models import AgentRecord, AgentRuntimeRecord


class AgentRegistryService:
    """Trusted registry; discovery descriptors never become enabled profiles."""

    def __init__(self, sessions: sessionmaker[Session]) -> None:
        self.sessions = sessions

    def register_profile(self, profile: AgentProfile) -> None:
        now = datetime.now(UTC)
        record = AgentRecord(
            agent_id=str(profile.agent_id), display_name=profile.display_name,
            roles_json=list(profile.roles), skills_json=list(profile.skills),
            trust_zone=profile.trust_zone.value, constraints_json=list(profile.constraints),
            labels_json=dict(profile.labels), max_parallel_delegations=profile.max_parallel_delegations,
            enabled=profile.enabled, profile_version=profile.profile_version,
            version=1, created_at=now, updated_at=now,
        )
        try:
            with self.sessions.begin() as session:
                session.add(record)
                session.flush()
        except IntegrityError as error:
            raise ValueError(f"agent profile already exists: {profile.agent_id}") from error

    def update_profile(self, profile: AgentProfile) -> None:
        """Replace a trusted profile and advance its immutable assignment version."""
        now = datetime.now(UTC)
        with self.sessions.begin() as session:
            row = session.get(AgentRecord, str(profile.agent_id))
            if row is None:
                raise ValueError(f"agent profile does not exist: {profile.agent_id}")
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

    def enable(self, agent_id: str) -> None:
        self._set_agent_enabled(agent_id, True)

    def disable(self, agent_id: str) -> None:
        self._set_agent_enabled(agent_id, False)

    def _set_agent_enabled(self, agent_id: str, enabled: bool) -> None:
        with self.sessions.begin() as session:
            row = session.get(AgentRecord, agent_id)
            if row is None:
                raise ValueError(f"agent profile does not exist: {agent_id}")
            row.enabled = enabled
            row.version += 1
            row.updated_at = datetime.now(UTC)

    def get_agent(self, agent_id: str) -> AgentProfile:
        with self.sessions() as session:
            row = session.get(AgentRecord, agent_id)
            if row is None:
                raise ValueError(f"agent profile does not exist: {agent_id}")
            return self._profile_from_record(row)

    def list_agents(self) -> tuple[AgentProfile, ...]:
        with self.sessions() as session:
            rows = session.scalars(select(AgentRecord).order_by(AgentRecord.agent_id)).all()
            return tuple(self._profile_from_record(row) for row in rows)

    @staticmethod
    def _profile_from_record(row: AgentRecord) -> AgentProfile:
        return AgentProfile(agent_id=AgentId(row.agent_id), display_name=row.display_name, roles=tuple(row.roles_json), skills=tuple(row.skills_json), trust_zone=AgentTrustZone(row.trust_zone), constraints=tuple(row.constraints_json), labels=dict(row.labels_json), max_parallel_delegations=row.max_parallel_delegations, enabled=row.enabled, profile_version=row.profile_version)

    def set_runtime_enabled(self, runtime_id: str, enabled: bool) -> None:
        with self.sessions.begin() as session:
            row = session.get(AgentRuntimeRecord, runtime_id)
            if row is None:
                raise ValueError(f"agent runtime does not exist: {runtime_id}")
            row.enabled = enabled
            row.version += 1
            row.updated_at = datetime.now(UTC)

    def register_runtime(self, runtime: AgentRuntime) -> None:
        now = datetime.now(UTC)
        with self.sessions.begin() as session:
            if session.get(AgentRecord, str(runtime.agent_id)) is None:
                raise ValueError(f"agent profile does not exist: {runtime.agent_id}")
            if session.get(AgentRuntimeRecord, str(runtime.runtime_id)) is not None:
                raise ValueError(f"agent runtime already exists: {runtime.runtime_id}")
            session.add(AgentRuntimeRecord(
                runtime_id=str(runtime.runtime_id), agent_id=str(runtime.agent_id),
                adapter_kind=runtime.adapter_kind.value, runtime_name=runtime.runtime_name,
                endpoint_ref=runtime.endpoint_ref, framework=runtime.framework,
                model_provider=runtime.model_provider, model_name=runtime.model_name,
                protocols_json=list(runtime.protocols), metadata_json=dict(runtime.metadata),
                enabled=True, version=1, created_at=now, updated_at=now,
            ))

    def discovered_descriptor(self, descriptor: DiscoveredAgentDescriptor) -> DiscoveredAgentDescriptor:
        return descriptor

    def heartbeat(self, runtime_id: str, *, lease_seconds: int = 30) -> None:
        if lease_seconds <= 0:
            raise ValueError("lease_seconds must be positive")
        now = datetime.now(UTC)
        with self.sessions.begin() as session:
            runtime = session.get(AgentRuntimeRecord, runtime_id)
            if runtime is None or not runtime.enabled:
                raise ValueError(f"agent runtime is unavailable: {runtime_id}")
            runtime.last_heartbeat_at = now
            runtime.lease_expires_at = now + timedelta(seconds=lease_seconds)
            runtime.updated_at = now
            runtime.version += 1

    def eligible(self, *, role: str | None = None, skill: str | None = None) -> tuple[str, ...]:
        with self.sessions() as session:
            rows = session.scalars(select(AgentRecord).where(AgentRecord.enabled.is_(True)).order_by(AgentRecord.agent_id)).all()
            return tuple(row.agent_id for row in rows if (role is None or role in row.roles_json) and (skill is None or skill in row.skills_json))

    def runtime_healthy(self, runtime_id: str, *, now: datetime | None = None) -> bool:
        """Return whether a runtime and its owning profile are currently usable."""
        reference = now or datetime.now(UTC)
        with self.sessions() as session:
            row = session.scalar(
                select(AgentRuntimeRecord)
                .join(AgentRecord, AgentRecord.agent_id == AgentRuntimeRecord.agent_id)
                .where(AgentRuntimeRecord.runtime_id == runtime_id)
            )
            owner = session.get(AgentRecord, row.agent_id) if row is not None else None
            return bool(
                row is not None
                and row.enabled
                and owner is not None
                and owner.enabled
                and row.lease_expires_at is not None
                and row.lease_expires_at > reference
            )
