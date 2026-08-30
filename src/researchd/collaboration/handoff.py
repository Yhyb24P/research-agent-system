"""Durable, non-authoritative handoff proposal contract and storage."""

from datetime import UTC, datetime
from typing import Literal, Protocol
import hashlib

from pydantic import Field, model_validator
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from researchd.domain.base import DomainModel
from researchd.domain.enums import AttemptState, DelegationPurpose, DelegationState, HandoffMode, HandoffStatus
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
    resolution_entity_type: str | None = None
    resolution_entity_id: str | None = None


class HandoffController(Protocol):
    def retry_attempt(self, work_order_id: str, *, preferred_agent_id: str | None = None, attempt_id: str | None = None, delegation_id: str | None = None) -> str: ...
    def create_handoff_revision(self, work_order_id: str, *, objective: str, reason: str, revision_work_order_id: str) -> str: ...


class HandoffResolutionService:
    """Resolve proposals without granting an Agent control-plane authority."""

    def __init__(self, sessions: sessionmaker[Session], controller: HandoffController) -> None:
        self.sessions = sessions
        self.controller = controller

    def accept(
        self, proposal_id: str, *, actor_type: str, actor_id: str,
        reason: str, target_agent_id: str | None = None,
    ) -> HandoffProposal:
        with self.sessions() as session:
            proposal = session.get(HandoffProposalRecord, proposal_id)
            if proposal is None:
                raise LookupError(proposal_id)
            if proposal.status != HandoffStatus.PROPOSED.value:
                if proposal.status == HandoffStatus.ACCEPTED.value:
                    if target_agent_id is not None:
                        self._require_matching_target(session, proposal, target_agent_id)
                    return HandoffProposalService._from_record(proposal)
                raise ValueError("handoff proposal already has a different decision")
            self._require_source_terminal(session, proposal)
            mode = HandoffMode(proposal.requested_mode)
            target = target_agent_id or proposal.proposed_target_agent_id
            objective = proposal.continuation_objective
        digest = hashlib.sha256(proposal_id.encode()).hexdigest()[:32]
        if mode is HandoffMode.CONTINUE:
            if target is None:
                raise ValueError("CONTINUE handoff requires a target Agent")
            entity_type = "attempt"
            entity_id = self.controller.retry_attempt(
                proposal.work_order_id, preferred_agent_id=target,
                attempt_id=f"att_handoff_{digest}", delegation_id=f"del_handoff_{digest}",
            )
        else:
            if target is not None:
                raise ValueError("REVISE handoff cannot select an execution target Agent")
            if objective is None:
                raise ValueError("REVISE handoff requires a continuation objective")
            entity_type = "work_order"
            entity_id = self.controller.create_handoff_revision(
                proposal.work_order_id, objective=objective, reason=proposal.reason,
                revision_work_order_id=f"wo_handoff_{digest}",
            )
        return self._decide(
            proposal_id, status=HandoffStatus.ACCEPTED, actor_type=actor_type,
            actor_id=actor_id, reason=reason, entity_type=entity_type, entity_id=entity_id,
        )

    def reject(self, proposal_id: str, *, actor_type: str, actor_id: str, reason: str) -> HandoffProposal:
        return self._decide(
            proposal_id, status=HandoffStatus.REJECTED, actor_type=actor_type,
            actor_id=actor_id, reason=reason, entity_type=None, entity_id=None,
        )

    def _decide(self, proposal_id: str, *, status: HandoffStatus, actor_type: str, actor_id: str, reason: str, entity_type: str | None, entity_id: str | None) -> HandoffProposal:
        if not actor_type or not actor_id or not reason:
            raise ValueError("handoff decision actor and reason are required")
        now = datetime.now(UTC)
        with self.sessions.begin() as session:
            row = session.get(HandoffProposalRecord, proposal_id)
            if row is None:
                raise LookupError(proposal_id)
            if row.status != HandoffStatus.PROPOSED.value:
                if row.status != status.value or row.resolution_entity_id != entity_id:
                    raise ValueError("handoff proposal already has a different decision")
                return HandoffProposalService._from_record(row)
            row.status, row.decided_at = status.value, now
            row.decision_actor_type, row.decision_actor_id = actor_type, actor_id
            row.decision_reason = reason
            row.resolution_entity_type, row.resolution_entity_id = entity_type, entity_id
            session.add(AuditEventRecord(
                event_id=f"evt_handoff_decision_{proposal_id}", event_type=f"HANDOFF_{status.value}",
                run_id=row.run_id, entity_type="handoff_proposal", entity_id=proposal_id,
                actor_type=actor_type, actor_id=actor_id, timestamp=now,
                correlation_id=row.work_order_id, causation_id=row.source_invocation_id,
                metadata_json={"resolution_entity_type": entity_type, "resolution_entity_id": entity_id},
            ))
            session.flush()
            return HandoffProposalService._from_record(row)

    @staticmethod
    def _require_matching_target(
        session: Session, proposal: HandoffProposalRecord, target_agent_id: str,
    ) -> None:
        """An explicit target on an already-accepted proposal must match the stored resolution."""
        if proposal.resolution_entity_type != "attempt" or proposal.resolution_entity_id is None:
            raise ValueError("handoff proposal already has a different decision")
        attempt = session.get(AttemptRecord, proposal.resolution_entity_id)
        delegation = session.get(DelegationRecord, attempt.delegation_id) if attempt is not None else None
        if delegation is None or delegation.assigned_agent_id != target_agent_id:
            raise ValueError("handoff proposal already has a different decision")

    @staticmethod
    def _require_source_terminal(session: Session, proposal: HandoffProposalRecord) -> None:
        delegation = session.get(DelegationRecord, proposal.source_delegation_id)
        attempt = session.scalar(select(AttemptRecord).where(
            AttemptRecord.delegation_id == proposal.source_delegation_id,
        ).order_by(AttemptRecord.created_at.desc()).limit(1))
        if delegation is None or delegation.state not in {
            DelegationState.COMPLETED.value, DelegationState.FAILED.value,
            DelegationState.CANCELLED.value,
        }:
            raise ValueError("handoff source Delegation is not terminal")
        if attempt is None or attempt.state not in {
            AttemptState.SUCCEEDED.value, AttemptState.FAILED.value,
            AttemptState.CANCELLED.value,
        }:
            raise ValueError("handoff source Attempt is not terminal")


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
        if invocation.attempt_id is None or delegation.purpose != DelegationPurpose.EXECUTE.value:
            raise ValueError("handoff requires an execution-scoped invocation")
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
            resolution_entity_type=row.resolution_entity_type,
            resolution_entity_id=row.resolution_entity_id,
        )


__all__ = ["HandoffController", "HandoffProposal", "HandoffProposalAction", "HandoffProposalService", "HandoffResolutionService"]
