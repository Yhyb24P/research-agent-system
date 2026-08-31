from datetime import UTC, datetime, timedelta
from uuid import uuid4

from sqlalchemy import or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from researchd.collaboration.contracts import (
    AgentProfile,
    AgentRuntime,
    AgentRuntimeLease,
    DiscoveredAgentDescriptor,
)
from researchd.domain.enums import AgentAdapterKind, AgentTrustZone
from researchd.domain.ids import AgentId, AgentRuntimeId
from researchd.storage.models import (
    AgentRecord,
    AgentRuntimeLeaseEventRecord,
    AgentRuntimeRecord,
)


class RuntimeLeaseConflict(RuntimeError):
    pass


class RuntimeLeaseInvalid(RuntimeError):
    pass


class AgentRegistryService:
    """Trusted registry; discovery descriptors never become enabled profiles."""

    _RESERVED_SYSTEM_ROLES = frozenset({"verifier", "policy", "orchestrator", "job-manager"})

    def __init__(self, sessions: sessionmaker[Session]) -> None:
        self.sessions = sessions

    def register_profile(self, profile: AgentProfile) -> None:
        self._validate_profile(profile)
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
        self._validate_profile(profile)
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

    def update_runtime(self, runtime: AgentRuntime) -> None:
        """Update implementation details without changing runtime/agent identity."""
        now = datetime.now(UTC)
        with self.sessions.begin() as session:
            row = session.get(AgentRuntimeRecord, str(runtime.runtime_id))
            if row is None:
                raise ValueError(f"agent runtime does not exist: {runtime.runtime_id}")
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
            row.version += 1
            row.updated_at = now

    def get_runtime(self, runtime_id: str) -> AgentRuntime:
        with self.sessions() as session:
            row = session.get(AgentRuntimeRecord, runtime_id)
            if row is None:
                raise ValueError(f"agent runtime does not exist: {runtime_id}")
            return self._runtime_from_record(row)

    def require_enabled_runtime(self, runtime_id: str) -> AgentRuntime:
        """Resolve a launchable runtime without treating a lease as process health."""
        with self.sessions() as session:
            row = session.scalar(
                select(AgentRuntimeRecord)
                .join(AgentRecord, AgentRecord.agent_id == AgentRuntimeRecord.agent_id)
                .where(
                    AgentRuntimeRecord.runtime_id == runtime_id,
                    AgentRuntimeRecord.enabled.is_(True),
                    AgentRecord.enabled.is_(True),
                )
            )
            if row is None:
                raise ValueError(f"agent runtime is unavailable: {runtime_id}")
            return self._runtime_from_record(row)

    def list_runtimes(self, agent_id: str | None = None) -> tuple[AgentRuntime, ...]:
        with self.sessions() as session:
            query = select(AgentRuntimeRecord).order_by(AgentRuntimeRecord.runtime_id)
            if agent_id is not None:
                query = query.where(AgentRuntimeRecord.agent_id == agent_id)
            return tuple(self._runtime_from_record(row) for row in session.scalars(query).all())

    def list_enabled_runtimes(self, agent_id: str) -> tuple[AgentRuntime, ...]:
        """Enabled runtimes owned by an enabled agent, ordered by runtime_id."""
        with self.sessions() as session:
            rows = session.scalars(
                select(AgentRuntimeRecord)
                .join(AgentRecord, AgentRecord.agent_id == AgentRuntimeRecord.agent_id)
                .where(
                    AgentRuntimeRecord.agent_id == agent_id,
                    AgentRuntimeRecord.enabled.is_(True),
                    AgentRecord.enabled.is_(True),
                )
                .order_by(AgentRuntimeRecord.runtime_id)
            ).all()
            return tuple(self._runtime_from_record(row) for row in rows)

    @staticmethod
    def _runtime_from_record(row: AgentRuntimeRecord) -> AgentRuntime:
        return AgentRuntime(runtime_id=AgentRuntimeId(row.runtime_id), agent_id=AgentId(row.agent_id), adapter_kind=AgentAdapterKind(row.adapter_kind), runtime_name=row.runtime_name, endpoint_ref=row.endpoint_ref, framework=row.framework, model_provider=row.model_provider, model_name=row.model_name, protocols=tuple(row.protocols_json), metadata=dict(row.metadata_json))

    def discovered_descriptor(self, descriptor: DiscoveredAgentDescriptor) -> DiscoveredAgentDescriptor:
        return descriptor

    @classmethod
    def _validate_profile(cls, profile: AgentProfile) -> None:
        reserved = cls._RESERVED_SYSTEM_ROLES.intersection(profile.roles)
        if reserved:
            raise ValueError(f"reserved trusted role cannot be registered as Agent: {sorted(reserved)[0]}")

    def acquire_runtime(
        self,
        runtime_id: str,
        *,
        owner_id: str,
        lease_seconds: int = 30,
        now: datetime | None = None,
    ) -> AgentRuntimeLease:
        if lease_seconds <= 0:
            raise ValueError("lease_seconds must be positive")
        if not owner_id or len(owner_id) > 128:
            raise ValueError("runtime lease owner_id is invalid")
        reference = now or datetime.now(UTC)
        lease_id = f"runtime_lease_{uuid4().hex}"
        expires_at = reference + timedelta(seconds=lease_seconds)
        with self.sessions.begin() as session:
            runtime = session.scalar(
                select(AgentRuntimeRecord)
                .join(AgentRecord, AgentRecord.agent_id == AgentRuntimeRecord.agent_id)
                .where(
                    AgentRuntimeRecord.runtime_id == runtime_id,
                    AgentRuntimeRecord.enabled.is_(True),
                    AgentRecord.enabled.is_(True),
                )
            )
            if runtime is None:
                raise ValueError(f"agent runtime is unavailable: {runtime_id}")
            if (
                runtime.runtime_lease_id is not None
                and runtime.lease_owner_id == owner_id
                and runtime.lease_expires_at is not None
                and runtime.lease_expires_at > reference
            ):
                lease_id = runtime.runtime_lease_id
                acquired_at = runtime.lease_acquired_at or reference
                runtime.last_heartbeat_at = reference
                runtime.lease_expires_at = expires_at
                runtime.updated_at = reference
                runtime.version += 1
                session.add(self._lease_event(runtime_id, lease_id, owner_id, "RENEWED", reference))
                conflict = False
            else:
                acquired_at = reference
                acquired = int(getattr(session.execute(
                    update(AgentRuntimeRecord)
                    .where(
                        AgentRuntimeRecord.runtime_id == runtime_id,
                        or_(
                            AgentRuntimeRecord.runtime_lease_id.is_(None),
                            AgentRuntimeRecord.lease_expires_at.is_(None),
                            AgentRuntimeRecord.lease_expires_at <= reference,
                        ),
                    )
                    .values(
                        runtime_lease_id=lease_id,
                        lease_owner_id=owner_id,
                        lease_acquired_at=reference,
                        last_heartbeat_at=reference,
                        lease_expires_at=expires_at,
                        updated_at=reference,
                        version=AgentRuntimeRecord.version + 1,
                    )
                ), "rowcount", 0))
                conflict = acquired != 1
                if not conflict:
                    session.add(self._lease_event(
                        runtime_id, lease_id, owner_id, "ACQUIRED", reference
                    ))
            if conflict:
                session.add(self._lease_event(
                    runtime_id, runtime.runtime_lease_id, owner_id, "CONFLICT", reference
                ))
        if conflict:
            raise RuntimeLeaseConflict(f"agent runtime lease is already held: {runtime_id}")
        return AgentRuntimeLease(
            lease_id=lease_id,
            runtime_id=AgentRuntimeId(runtime_id),
            owner_id=owner_id,
            acquired_at=acquired_at,
            expires_at=expires_at,
        )

    def renew_runtime(
        self,
        lease: AgentRuntimeLease,
        *,
        lease_seconds: int = 30,
        now: datetime | None = None,
    ) -> AgentRuntimeLease:
        if lease_seconds <= 0:
            raise ValueError("lease_seconds must be positive")
        reference = now or datetime.now(UTC)
        expires_at = reference + timedelta(seconds=lease_seconds)
        with self.sessions.begin() as session:
            renewed = int(getattr(session.execute(
                update(AgentRuntimeRecord)
                .where(
                    AgentRuntimeRecord.runtime_id == str(lease.runtime_id),
                    AgentRuntimeRecord.runtime_lease_id == lease.lease_id,
                    AgentRuntimeRecord.lease_owner_id == lease.owner_id,
                    AgentRuntimeRecord.lease_expires_at > reference,
                )
                .values(
                    last_heartbeat_at=reference,
                    lease_expires_at=expires_at,
                    updated_at=reference,
                    version=AgentRuntimeRecord.version + 1,
                )
            ), "rowcount", 0))
            if renewed != 1:
                raise RuntimeLeaseInvalid("runtime lease is missing, expired, or out of scope")
            session.add(self._lease_event(
                str(lease.runtime_id), lease.lease_id, lease.owner_id, "RENEWED", reference
            ))
        return lease.model_copy(update={"expires_at": expires_at})

    def release_runtime(
        self,
        lease: AgentRuntimeLease,
        *,
        now: datetime | None = None,
    ) -> None:
        reference = now or datetime.now(UTC)
        with self.sessions.begin() as session:
            released = int(getattr(session.execute(
                update(AgentRuntimeRecord)
                .where(
                    AgentRuntimeRecord.runtime_id == str(lease.runtime_id),
                    AgentRuntimeRecord.runtime_lease_id == lease.lease_id,
                    AgentRuntimeRecord.lease_owner_id == lease.owner_id,
                )
                .values(
                    runtime_lease_id=None,
                    lease_owner_id=None,
                    lease_acquired_at=None,
                    lease_expires_at=None,
                    updated_at=reference,
                    version=AgentRuntimeRecord.version + 1,
                )
            ), "rowcount", 0))
            if released != 1:
                raise RuntimeLeaseInvalid("runtime lease is missing or out of scope")
            session.add(self._lease_event(
                str(lease.runtime_id), lease.lease_id, lease.owner_id, "RELEASED", reference
            ))

    @staticmethod
    def _lease_event(
        runtime_id: str,
        lease_id: str | None,
        owner_id: str,
        event_type: str,
        observed_at: datetime,
    ) -> AgentRuntimeLeaseEventRecord:
        return AgentRuntimeLeaseEventRecord(
            event_id=f"runtime_lease_event_{uuid4().hex}",
            runtime_id=runtime_id,
            lease_id=lease_id,
            owner_id=owner_id,
            event_type=event_type,
            observed_at=observed_at,
        )

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
