"""Durable, non-authoritative handoff proposal contract and storage."""

from datetime import datetime
from typing import Literal

from pydantic import Field, model_validator
from sqlalchemy import select
from sqlalchemy.orm import Session

from researchd.domain.base import DomainModel
from researchd.domain.enums import HandoffMode, HandoffStatus
from researchd.domain.ids import AgentId
from researchd.storage.models import AgentInvocationRecord, AgentRecord, ArtifactRecord, AttemptRecord, AuditEventRecord, DelegationRecord, HandoffProposalRecord, ObservationRecord, WorkOrderRecord


class HandoffProposalAction(DomainModel):
    kind: Literal["handoff"] = "handoff"
    action_id: str = Field(min_length=1, max_length=128)
    proposed_target_agent_id: AgentId | None = None
    requested_mode: HandoffMode
    reason: str = Field(min_length=1, max_length=16_384)
    continuation_objective: str | None = Field(default=None, min_length=1, max_length=16_384)
    artifact_ids: tuple[str, ...] = ()
    observation_ids: tuple[str, ...] = ()

    @model_validator(mode="after")
    def revision_has_objective_and_unique_evidence(self) -> "HandoffProposalAction":
        if self.requested_mode is HandoffMode.REVISE and self.continuation_objective is None:
            raise ValueError("REVISE handoff requires a continuation objective")
        if len(set(self.artifact_ids)) != len(self.artifact_ids) or len(set(self.observation_ids)) != len(self.observation_ids):
            raise ValueError("handoff evidence references must be unique")
        return self


class HandoffProposal(DomainModel):
    proposal_id: str
    run_id: str
    work_order_id: str
    source_delegation_id: str
    source_invocation_id: str
    source_agent_id: str
    proposed_target_agent_id: str | None = None
    requested_mode: HandoffMode
    reason: str
    continuation_objective: str | None = None
    artifact_ids: tuple[str, ...] = ()
    observation_ids: tuple[str, ...] = ()
    status: HandoffStatus
    created_at: datetime


class HandoffProposalService:
    @classmethod
    def propose_in_session(
        cls,
        session: Session,
        *,
        proposal_id: str,
        invocation: AgentInvocationRecord,
        delegation: DelegationRecord,
        action: HandoffProposalAction,
        now: datetime,
    ) -> HandoffProposal:
        run_id = invocation.run_id
        work_order_id = invocation.work_order_id
        invocation_id = invocation.invocation_id
        agent_id = invocation.agent_id
        if not isinstance(work_order_id, str):
            raise ValueError("handoff requires a WorkOrder-scoped invocation")
        if action.proposed_target_agent_id is not None:
            target = session.get(AgentRecord, str(action.proposed_target_agent_id))
            if target is None or not target.enabled:
                raise ValueError("proposed target Agent is unavailable")
        cls._validate_evidence(session, run_id, action)
        existing = session.get(HandoffProposalRecord, proposal_id)
        if existing is not None:
            if (
                existing.source_invocation_id != invocation_id
                or existing.action_id != action.action_id
                or existing.proposed_target_agent_id != (str(action.proposed_target_agent_id) if action.proposed_target_agent_id else None)
                or existing.requested_mode != action.requested_mode.value
                or existing.reason != action.reason
                or existing.continuation_objective != action.continuation_objective
                or existing.artifact_ids_json != list(action.artifact_ids)
                or existing.observation_ids_json != list(action.observation_ids)
            ):
                raise ValueError("handoff proposal identity conflict")
            return cls._from_record(existing)
        row = HandoffProposalRecord(
            proposal_id=proposal_id, action_id=action.action_id, run_id=run_id,
            work_order_id=work_order_id, source_delegation_id=delegation.delegation_id,
            source_invocation_id=invocation_id, source_agent_id=agent_id,
            proposed_target_agent_id=str(action.proposed_target_agent_id) if action.proposed_target_agent_id else None,
            requested_mode=action.requested_mode.value, reason=action.reason,
            continuation_objective=action.continuation_objective,
            artifact_ids_json=list(action.artifact_ids), observation_ids_json=list(action.observation_ids),
            status=HandoffStatus.PROPOSED.value, created_at=now, decided_at=None,
            decision_actor_type=None, decision_actor_id=None, decision_reason=None,
        )
        session.add(row)
        session.add(AuditEventRecord(
            event_id=f"evt_handoff_{proposal_id}", event_type="HANDOFF_PROPOSED",
            run_id=run_id, entity_type="handoff_proposal", entity_id=proposal_id,
            actor_type="agent", actor_id=agent_id, timestamp=now,
            correlation_id=work_order_id, causation_id=invocation_id,
            metadata_json={"requested_mode": action.requested_mode.value},
        ))
        return cls._from_record(row)

    @staticmethod
    def _validate_evidence(session: Session, run_id: str, action: HandoffProposalAction) -> None:
        for artifact_id in action.artifact_ids:
            found = session.scalar(select(ArtifactRecord.artifact_id).join(AttemptRecord).join(WorkOrderRecord).where(
                ArtifactRecord.artifact_id == artifact_id, WorkOrderRecord.run_id == run_id,
            ))
            if found is None:
                raise ValueError("handoff artifact is outside its run")
        for observation_id in action.observation_ids:
            found = session.scalar(select(ObservationRecord.observation_id).join(AttemptRecord).join(WorkOrderRecord).where(
                ObservationRecord.observation_id == observation_id, WorkOrderRecord.run_id == run_id,
            ))
            if found is None:
                raise ValueError("handoff observation is outside its run")

    @staticmethod
    def _from_record(row: HandoffProposalRecord) -> HandoffProposal:
        return HandoffProposal(
            proposal_id=row.proposal_id, run_id=row.run_id, work_order_id=row.work_order_id,
            source_delegation_id=row.source_delegation_id, source_invocation_id=row.source_invocation_id,
            source_agent_id=row.source_agent_id, proposed_target_agent_id=row.proposed_target_agent_id,
            requested_mode=HandoffMode(row.requested_mode), reason=row.reason,
            continuation_objective=row.continuation_objective,
            artifact_ids=tuple(row.artifact_ids_json), observation_ids=tuple(row.observation_ids_json),
            status=HandoffStatus(row.status), created_at=row.created_at,
        )


__all__ = ["HandoffProposal", "HandoffProposalAction", "HandoffProposalService"]
