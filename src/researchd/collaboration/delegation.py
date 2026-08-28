import hashlib
import json
from datetime import UTC, datetime
from sqlalchemy.exc import IntegrityError
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker
from researchd.collaboration.contracts import Delegation
from researchd.domain.enums import DelegationState
from researchd.storage.models import AgentRecord, AgentRuntimeRecord, DelegationRecord

class DelegationService:
    def __init__(self, sessions: sessionmaker[Session]) -> None:
        self.sessions = sessions

    def create(self, delegation: Delegation) -> None:
        now = datetime.now(UTC)
        record = DelegationRecord(delegation_id=str(delegation.delegation_id), run_id=delegation.run_id, work_order_id=delegation.work_order_id, purpose=delegation.purpose.value, required_roles_json=list(delegation.required_roles), required_skills_json=list(delegation.required_skills), required_trust_zones_json=[zone.value for zone in delegation.required_trust_zones], state=delegation.state.value, idempotency_key=delegation.idempotency_key, version=1, created_at=now, updated_at=now)
        try:
            with self.sessions.begin() as session:
                session.add(record)
                session.flush()
        except IntegrityError as error:
            raise ValueError("delegation id or idempotency key already exists") from error

    def assign(self, delegation_id: str, *, agent_id: str, runtime_id: str) -> str:
        now = datetime.now(UTC)
        with self.sessions.begin() as session:
            delegation = session.get(DelegationRecord, delegation_id)
            agent = session.get(AgentRecord, agent_id)
            runtime = session.get(AgentRuntimeRecord, runtime_id)
            if delegation is None or delegation.state != DelegationState.PENDING.value:
                raise ValueError("delegation is not pending")
            if agent is None or not agent.enabled or runtime is None or not runtime.enabled or runtime.agent_id != agent_id:
                raise ValueError("agent runtime is unavailable")
            snapshot = {"agent_id": agent.agent_id, "profile_version": agent.profile_version, "roles": agent.roles_json, "skills": agent.skills_json, "trust_zone": agent.trust_zone, "constraints": agent.constraints_json, "runtime_id": runtime.runtime_id}
            digest = hashlib.sha256(json.dumps(snapshot, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
            delegation.assigned_agent_id, delegation.assigned_runtime_id = agent_id, runtime_id
            delegation.agent_profile_version, delegation.agent_snapshot_json = agent.profile_version, snapshot
            delegation.assignment_sha256, delegation.state = digest, DelegationState.ASSIGNED.value
            delegation.updated_at, delegation.version = now, delegation.version + 1
            return digest
