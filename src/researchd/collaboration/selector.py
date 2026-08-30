from datetime import UTC, datetime
from pydantic import Field
from sqlalchemy import exists, func, select
from sqlalchemy.orm import Session, sessionmaker
from researchd.collaboration.contracts import AgentProfile
from researchd.domain.enums import AgentAdapterKind, AgentTrustZone, DelegationState
from researchd.domain.ids import AgentId, AgentRuntimeId
from researchd.storage.models import (
    AgentRecord,
    AgentRuntimeRecord,
    DelegationRecord,
    RuntimeLaunchProfileRecord,
    RuntimeSessionRecord,
)
from researchd.domain.base import DomainModel


class AgentSelection(DomainModel):
    agent_id: AgentId
    runtime_id: AgentRuntimeId
    priority: int = Field(ge=0)


class AgentSelector:
    """Deterministic trusted selector; no model/LLM scheduling is involved."""
    def __init__(
        self,
        sessions: sessionmaker[Session],
        *,
        require_supervised_session: bool = False,
        allowed_adapter_kinds: frozenset[AgentAdapterKind] = frozenset(),
        required_launch_mode: str | None = None,
    ) -> None:
        self.sessions = sessions
        self.require_supervised_session = require_supervised_session
        self.allowed_adapter_kinds = allowed_adapter_kinds
        self.required_launch_mode = required_launch_mode

    def select(self, *, required_roles: tuple[str, ...] = (), required_skills: tuple[str, ...] = (), required_trust_zones: tuple[AgentTrustZone, ...] = (), preferred_agent_id: AgentId | None = None, now: datetime | None = None) -> AgentSelection | None:
        reference = now or datetime.now(UTC)
        with self.sessions() as session:
            query = select(AgentRecord, AgentRuntimeRecord).join(
                AgentRuntimeRecord,
                AgentRuntimeRecord.agent_id == AgentRecord.agent_id,
            ).where(
                AgentRecord.enabled.is_(True),
                AgentRuntimeRecord.enabled.is_(True),
                AgentRuntimeRecord.runtime_lease_id.is_not(None),
                AgentRuntimeRecord.lease_expires_at > reference,
            )
            if preferred_agent_id is not None:
                query = query.where(AgentRecord.agent_id == str(preferred_agent_id))
            if self.allowed_adapter_kinds:
                query = query.where(AgentRuntimeRecord.adapter_kind.in_(
                    tuple(kind.value for kind in self.allowed_adapter_kinds)
                ))
            if self.require_supervised_session:
                query = query.where(exists().where(
                    RuntimeSessionRecord.runtime_id == AgentRuntimeRecord.runtime_id,
                    RuntimeSessionRecord.supervisor_state == "HEALTHY",
                    RuntimeLaunchProfileRecord.runtime_id == AgentRuntimeRecord.runtime_id,
                    RuntimeLaunchProfileRecord.enabled.is_(True),
                    RuntimeSessionRecord.launch_profile_sha256
                    == RuntimeLaunchProfileRecord.spec_sha256,
                ))
                if self.required_launch_mode is not None:
                    query = query.where(exists().where(
                        RuntimeLaunchProfileRecord.runtime_id
                        == AgentRuntimeRecord.runtime_id,
                        RuntimeLaunchProfileRecord.launch_mode
                        == self.required_launch_mode,
                    ))
            rows = session.execute(query.order_by(
                AgentRecord.agent_id,
                AgentRuntimeRecord.runtime_id,
            )).all()
            candidates: list[AgentSelection] = []
            for agent, runtime in rows:
                if any(role not in agent.roles_json for role in required_roles) or any(skill not in agent.skills_json for skill in required_skills):
                    continue
                if required_trust_zones and agent.trust_zone not in {zone.value for zone in required_trust_zones}:
                    continue
                active = session.scalar(select(func.count(DelegationRecord.delegation_id)).where(DelegationRecord.assigned_agent_id == agent.agent_id, DelegationRecord.state.in_((DelegationState.ASSIGNED.value, DelegationState.RUNNING.value)))) or 0
                if active >= agent.max_parallel_delegations:
                    continue
                try:
                    priority = int(agent.labels_json.get("priority", "0"))
                except (TypeError, ValueError):
                    priority = 0
                candidates.append(AgentSelection(agent_id=AgentId(agent.agent_id), runtime_id=AgentRuntimeId(runtime.runtime_id), priority=max(0, priority)))
            return min(candidates, key=lambda candidate: (-candidate.priority, str(candidate.agent_id), str(candidate.runtime_id))) if candidates else None
