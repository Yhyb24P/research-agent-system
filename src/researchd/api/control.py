"""In-process local control API used by a loopback HTTP/CLI adapter."""

from collections.abc import Sequence
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from researchd.collaboration.contracts import CollaborationMessage
from researchd.collaboration.messages import CollaborationMessageService
from researchd.domain.enums import AttemptState, WorkOrderState
from researchd.orchestrator.engine import ResearchOrchestrator
from researchd.storage.models import AgentRecord, AgentRuntimeRecord, ArtifactRecord, ApprovalRequestRecord, AuditEventRecord, ClaimRecord, CollaborationMessageRecord, DaemonCommandRecord, DelegationRecord, AgentInvocationRecord, AttemptRecord, HandoffProposalRecord, ObservationRecord, PlanRecord, ResearchRunRecord, ReviewDecisionRecord, RunArtifactAttachmentRecord, RuntimeSessionRecord, VerificationResultRecord, WorkOrderRecord, WorkspaceGrantRecord, WorkspaceReconciliationRecord, WorkspaceTransportRecord, WorkspaceRecord
from researchd.workspace.creation import WorkspaceCreationService


class LocalControlAPI:
    """Controller facade; it exposes structured status, never agent conversation or raw files."""

    def __init__(self, sessions: sessionmaker[Session], orchestrator: ResearchOrchestrator | None = None) -> None:
        self.sessions = sessions
        self.orchestrator = orchestrator

    def run_status(self, run_id: str) -> dict[str, Any]:
        with self.sessions() as session:
            run = session.get(ResearchRunRecord, run_id)
            if run is None:
                raise LookupError(run_id)
            orders = session.scalars(select(WorkOrderRecord).where(WorkOrderRecord.run_id == run_id).order_by(WorkOrderRecord.created_at)).all()
            attempts = session.scalars(select(AttemptRecord).join(WorkOrderRecord).where(
                WorkOrderRecord.run_id == run_id,
                AttemptRecord.state.not_in((AttemptState.SUCCEEDED.value, AttemptState.FAILED.value, AttemptState.CANCELLED.value)),
            )).all()
            return {
                "run_id": run.run_id,
                "state": run.state,
                "work_orders": [{"work_order_id": item.work_order_id, "state": item.state} for item in orders],
                "pending_approval_ids": [item.approval_id for item in orders if item.approval_id and item.state == WorkOrderState.WAITING_APPROVAL.value],
                "active_attempt_ids": [item.attempt_id for item in attempts],
                "iterations_used": run.iterations_used,
                "max_iterations": run.max_iterations,
                "agent_turns_used": run.agent_turns_used,
                "max_agent_turns": run.max_agent_turns,
                "cancellation_requested": run.cancellation_requested,
            }

    def runs(self) -> list[dict[str, Any]]:
        with self.sessions() as session:
            identifiers = session.scalars(select(ResearchRunRecord.run_id).order_by(ResearchRunRecord.created_at, ResearchRunRecord.run_id)).all()
        return [self.run_status(run_id) for run_id in identifiers]

    def workspaces(self) -> list[dict[str, Any]]:
        with self.sessions() as session:
            rows = session.scalars(
                select(WorkspaceRecord).order_by(WorkspaceRecord.created_at, WorkspaceRecord.workspace_id)
            ).all()
            return [
                {"workspace_id": item.workspace_id, "name": item.name, "version": item.version}
                for item in rows
            ]

    def work_order_status(self, work_order_id: str) -> dict[str, Any]:
        with self.sessions() as session:
            order = session.get(WorkOrderRecord, work_order_id)
            if order is None:
                raise LookupError(work_order_id)
            return {
                "work_order_id": order.work_order_id, "run_id": order.run_id,
                "state": order.state, "parent_work_order_id": order.parent_work_order_id,
                "revision_reason": order.revision_reason,
            }

    def events(
        self,
        run_id: str,
        *,
        after_stream_offset: int | None = None,
    ) -> list[dict[str, Any]]:
        """Read the authoritative stream in database-assigned order."""
        with self.sessions() as session:
            query = select(AuditEventRecord).where(AuditEventRecord.run_id == run_id)
            if after_stream_offset is not None:
                query = query.where(AuditEventRecord.audit_seq > after_stream_offset)
            records: Sequence[AuditEventRecord] = session.scalars(
                query.order_by(AuditEventRecord.audit_seq),
            ).all()
            payload = [{
                "event_id": event.event_id, "event_type": event.event_type,
                "stream_offset": event.audit_seq,
                "entity_type": event.entity_type, "entity_id": event.entity_id,
                "actor_type": event.actor_type, "actor_id": event.actor_id,
                "timestamp": event.timestamp.isoformat(), "correlation_id": event.correlation_id,
                "metadata": event.metadata_json,
            } for event in records]
            return payload

    def system_events(
        self,
        *,
        after_stream_offset: int | None = None,
    ) -> list[dict[str, Any]]:
        """Read global SYSTEM/HUMAN events from the same monotonic stream."""
        with self.sessions() as session:
            query = select(AuditEventRecord).where(AuditEventRecord.run_id.is_(None))
            if after_stream_offset is not None:
                query = query.where(AuditEventRecord.audit_seq > after_stream_offset)
            records: Sequence[AuditEventRecord] = session.scalars(
                query.order_by(AuditEventRecord.audit_seq),
            ).all()
            return [{
                "event_id": event.event_id,
                "event_type": event.event_type,
                "stream_offset": event.audit_seq,
                "entity_type": event.entity_type,
                "entity_id": event.entity_id,
                "actor_type": event.actor_type,
                "actor_id": event.actor_id,
                "timestamp": event.timestamp.isoformat(),
                "correlation_id": event.correlation_id,
                "metadata": event.metadata_json,
            } for event in records]

    def runtime_sessions(self, runtime_id: str | None = None) -> list[dict[str, Any]]:
        """Project durable supervisor state without exposing launch secrets."""
        with self.sessions() as session:
            query = select(RuntimeSessionRecord).order_by(
                RuntimeSessionRecord.created_at,
                RuntimeSessionRecord.runtime_session_id,
            )
            if runtime_id is not None:
                query = query.where(RuntimeSessionRecord.runtime_id == runtime_id)
            rows = session.scalars(query).all()
            return [{
                "runtime_session_id": row.runtime_session_id,
                "runtime_id": row.runtime_id,
                "launch_mode": row.launch_mode,
                "supervisor_state": row.supervisor_state,
                "external_identity": row.external_identity_json,
                "started_at": row.started_at.isoformat() if row.started_at else None,
                "last_health_at": row.last_health_at.isoformat() if row.last_health_at else None,
                "stopped_at": row.stopped_at.isoformat() if row.stopped_at else None,
                "exit_reason": row.exit_reason,
                "reattach_state": row.reattach_state,
                "version": row.version,
            } for row in rows]

    def daemon_commands(self, status: str | None = None) -> list[dict[str, Any]]:
        """Project receipts without request payloads or command arguments."""
        with self.sessions() as session:
            query = select(DaemonCommandRecord).order_by(
                DaemonCommandRecord.created_at,
                DaemonCommandRecord.command_id,
            )
            if status is not None:
                if status not in {"ACCEPTED", "COMPLETED", "REJECTED"}:
                    raise ValueError("invalid daemon command status")
                query = query.where(DaemonCommandRecord.status == status)
            rows = session.scalars(query).all()
            return [{
                "command_id": row.command_id,
                "command_type": row.command_type,
                "command_version": row.command_version,
                "actor_type": row.actor_type,
                "actor_id": row.actor_id,
                "status": row.status,
                "reason_code": row.reason_code,
                "created_at": row.created_at.isoformat(),
                "updated_at": row.updated_at.isoformat(),
            } for row in rows]

    def stream_snapshot(self, run_id: str) -> dict[str, Any]:
        """Return the current read model used for an initial AG-UI snapshot."""

        status = self.run_status(run_id)
        return {
            "run": status,
            "agents": self.agents(),
            "delegations": self.delegations(run_id),
            "approvals": self.approvals(run_id),
            "artifacts": self.artifacts(run_id),
            "workspace_grants": self.workspace_grants(run_id),
            "handoffs": self.handoffs(run_id),
        }

    def collaboration_message(self, message_id: str) -> dict[str, Any]:
        """Resolve an audited message for presentation without widening authority."""

        with self.sessions() as session:
            row = session.get(CollaborationMessageRecord, message_id)
            if row is None:
                raise LookupError(message_id)
            return self._message_payload(row)

    def collaboration_messages(self, run_id: str) -> list[dict[str, Any]]:
        """List the native message read model in durable creation order."""
        with self.sessions() as session:
            if session.get(ResearchRunRecord, run_id) is None:
                raise LookupError(run_id)
            rows = session.scalars(select(CollaborationMessageRecord).where(
                CollaborationMessageRecord.run_id == run_id,
            ).order_by(
                CollaborationMessageRecord.created_at,
                CollaborationMessageRecord.message_id,
            )).all()
            return [self._message_payload(row) for row in rows]

    def handoffs(self, run_id: str | None = None) -> list[dict[str, Any]]:
        """Project handoff proposals and their controller-owned decisions."""
        with self.sessions() as session:
            query = select(HandoffProposalRecord).order_by(
                HandoffProposalRecord.created_at, HandoffProposalRecord.proposal_id,
            )
            if run_id is not None:
                if session.get(ResearchRunRecord, run_id) is None:
                    raise LookupError(run_id)
                query = query.where(HandoffProposalRecord.run_id == run_id)
            return [self._handoff_payload(row) for row in session.scalars(query).all()]

    @staticmethod
    def _handoff_payload(row: HandoffProposalRecord) -> dict[str, Any]:
        return {
            "proposal_id": row.proposal_id, "run_id": row.run_id,
            "work_order_id": row.work_order_id,
            "source_delegation_id": row.source_delegation_id,
            "source_invocation_id": row.source_invocation_id,
            "source_agent_id": row.source_agent_id,
            "proposed_target_agent_id": row.proposed_target_agent_id,
            "requested_mode": row.requested_mode, "status": row.status,
            "decision_pending": (
                row.status == "PROPOSED" and row.decision_actor_type is not None
            ),
            "reason": row.reason,
            "continuation_objective": row.continuation_objective,
            "artifact_ids": row.artifact_ids_json,
            "observation_ids": row.observation_ids_json,
            "decision_actor_type": row.decision_actor_type,
            "decision_actor_id": row.decision_actor_id,
            "decision_reason": row.decision_reason,
            "resolution_entity_type": row.resolution_entity_type,
            "resolution_entity_id": row.resolution_entity_id,
            "created_at": row.created_at.isoformat(),
            "decided_at": row.decided_at.isoformat() if row.decided_at else None,
        }

    @staticmethod
    def _message_payload(row: CollaborationMessageRecord) -> dict[str, Any]:
        protected = row.classification in {"LOCAL_ONLY", "SECRET"}
        return {
                "message_id": row.message_id,
                "run_id": row.run_id,
                "work_order_id": row.work_order_id,
                "delegation_id": row.delegation_id,
                "invocation_id": row.invocation_id,
                "reply_to_message_id": row.reply_to_message_id,
                "sender_actor_type": row.sender_actor_type,
                "sender_actor_id": row.sender_actor_id,
                "recipient_agent_id": row.recipient_agent_id,
                "purpose": row.purpose,
                "body": None if protected else row.body,
                "body_redacted": protected,
                "classification": row.classification,
                "created_at": row.created_at.isoformat(),
            }

    def agents(self) -> list[dict[str, Any]]:
        with self.sessions() as session:
            rows = session.scalars(select(AgentRecord).order_by(AgentRecord.agent_id)).all()
            return [self._agent_payload(session, row) for row in rows]

    def agent(self, agent_id: str) -> dict[str, Any]:
        with self.sessions() as session:
            row = session.get(AgentRecord, agent_id)
            if row is None:
                raise LookupError(agent_id)
            return self._agent_payload(session, row)

    def agent_console(
        self, agent_id: str, *, run_id: str | None = None,
    ) -> dict[str, Any]:
        """One Agent's projection-only collaboration console.

        This is deliberately a read model. It owns neither a terminal nor any
        control-plane state, so closing a client window cannot stop an Agent.
        """
        with self.sessions() as session:
            agent = session.get(AgentRecord, agent_id)
            if agent is None:
                raise LookupError(agent_id)
            if run_id is not None and session.get(ResearchRunRecord, run_id) is None:
                raise LookupError(run_id)
            delegations_query = select(DelegationRecord).where(
                DelegationRecord.assigned_agent_id == agent_id,
            ).order_by(DelegationRecord.created_at, DelegationRecord.delegation_id)
            invocations_query = select(AgentInvocationRecord).where(
                AgentInvocationRecord.agent_id == agent_id,
            ).order_by(AgentInvocationRecord.created_at, AgentInvocationRecord.invocation_id)
            messages_query = select(CollaborationMessageRecord).where(
                (CollaborationMessageRecord.sender_actor_id == agent_id)
                | (CollaborationMessageRecord.recipient_agent_id == agent_id),
            ).order_by(CollaborationMessageRecord.created_at, CollaborationMessageRecord.message_id)
            handoffs_query = select(HandoffProposalRecord).where(
                (HandoffProposalRecord.source_agent_id == agent_id)
                | (HandoffProposalRecord.proposed_target_agent_id == agent_id),
            ).order_by(HandoffProposalRecord.created_at, HandoffProposalRecord.proposal_id)
            if run_id is not None:
                delegations_query = delegations_query.where(DelegationRecord.run_id == run_id)
                invocations_query = invocations_query.where(AgentInvocationRecord.run_id == run_id)
                messages_query = messages_query.where(CollaborationMessageRecord.run_id == run_id)
                handoffs_query = handoffs_query.where(HandoffProposalRecord.run_id == run_id)
            delegations = session.scalars(delegations_query).all()
            invocations = session.scalars(invocations_query).all()
            messages = session.scalars(messages_query).all()
            handoffs = session.scalars(handoffs_query).all()
            artifacts_query = select(ArtifactRecord).join(AttemptRecord).join(
                DelegationRecord,
                AttemptRecord.delegation_id == DelegationRecord.delegation_id,
            ).where(DelegationRecord.assigned_agent_id == agent_id).order_by(
                ArtifactRecord.created_at, ArtifactRecord.artifact_id,
            )
            if run_id is not None:
                artifacts_query = artifacts_query.where(DelegationRecord.run_id == run_id)
            artifacts = session.scalars(artifacts_query).all()
            attachments_query = (
                select(ArtifactRecord)
                .join(
                    RunArtifactAttachmentRecord,
                    RunArtifactAttachmentRecord.artifact_id == ArtifactRecord.artifact_id,
                )
                .where(
                    (RunArtifactAttachmentRecord.recipient_agent_id.is_(None))
                    | (RunArtifactAttachmentRecord.recipient_agent_id == agent_id)
                )
                .order_by(
                    RunArtifactAttachmentRecord.created_at,
                    RunArtifactAttachmentRecord.attachment_id,
                )
            )
            if run_id is not None:
                attachments_query = attachments_query.where(
                    RunArtifactAttachmentRecord.run_id == run_id,
                )
            attached = session.scalars(attachments_query).all()
            artifacts = list({item.artifact_id: item for item in (*artifacts, *attached)}.values())
            runtimes = session.scalars(select(AgentRuntimeRecord).where(
                AgentRuntimeRecord.agent_id == agent_id,
            ).order_by(AgentRuntimeRecord.runtime_id)).all()
            runtime_ids = [item.runtime_id for item in runtimes]
            sessions = session.scalars(select(RuntimeSessionRecord).where(
                RuntimeSessionRecord.runtime_id.in_(runtime_ids),
            ).order_by(RuntimeSessionRecord.created_at, RuntimeSessionRecord.runtime_session_id)).all() if runtime_ids else []
            return {
                "agent": self._agent_payload(session, agent),
                "messages": [self._message_payload(item) for item in messages],
                "delegations": [self._delegation_payload(item) for item in delegations],
                "invocations": [{
                    "invocation_id": item.invocation_id, "run_id": item.run_id,
                    "work_order_id": item.work_order_id, "attempt_id": item.attempt_id,
                    "delegation_id": item.delegation_id, "runtime_id": item.runtime_id,
                    "purpose": item.purpose, "status": item.status,
                    "failure_category": item.failure_category,
                    "reason_code": item.reason_code,
                    "created_at": item.created_at.isoformat(),
                    "completed_at": item.completed_at.isoformat() if item.completed_at else None,
                } for item in invocations],
                "artifacts": [{
                    "artifact_id": item.artifact_id, "attempt_id": item.attempt_id,
                    "artifact_type": item.artifact_type,
                    "classification": item.classification,
                    "created_at": item.created_at.isoformat(),
                } for item in artifacts],
                "handoffs": [self._handoff_payload(item) for item in handoffs],
                "runtime_sessions": [{
                    "runtime_session_id": item.runtime_session_id,
                    "runtime_id": item.runtime_id,
                    "supervisor_state": item.supervisor_state,
                    "last_health_at": item.last_health_at.isoformat() if item.last_health_at else None,
                    "exit_reason": item.exit_reason,
                } for item in sessions],
            }

    def _agent_payload(self, session: Session, row: AgentRecord) -> dict[str, Any]:
        runtimes = session.scalars(select(AgentRuntimeRecord).where(AgentRuntimeRecord.agent_id == row.agent_id).order_by(AgentRuntimeRecord.runtime_id)).all()
        return {"agent_id": row.agent_id, "display_name": row.display_name, "roles": row.roles_json, "skills": row.skills_json, "trust_zone": row.trust_zone, "enabled": row.enabled, "profile_version": row.profile_version, "runtimes": [{"runtime_id": item.runtime_id, "adapter_kind": item.adapter_kind, "runtime_name": item.runtime_name, "framework": item.framework, "model_provider": item.model_provider, "model_name": item.model_name, "protocols": item.protocols_json, "enabled": item.enabled, "lease_expires_at": item.lease_expires_at.isoformat() if item.lease_expires_at else None} for item in runtimes]}

    def delegations(self, run_id: str | None = None) -> list[dict[str, Any]]:
        with self.sessions() as session:
            query = select(DelegationRecord).order_by(DelegationRecord.created_at, DelegationRecord.delegation_id)
            if run_id is not None:
                query = query.where(DelegationRecord.run_id == run_id)
            return [self._delegation_payload(item) for item in session.scalars(query).all()]

    def delegation(self, delegation_id: str) -> dict[str, Any]:
        with self.sessions() as session:
            row = session.get(DelegationRecord, delegation_id)
            if row is None:
                raise LookupError(delegation_id)
            payload = self._delegation_payload(row)
            invocations = session.scalars(select(AgentInvocationRecord).where(AgentInvocationRecord.delegation_id == delegation_id).order_by(AgentInvocationRecord.created_at)).all()
            payload["invocations"] = [{"invocation_id": item.invocation_id, "status": item.status, "agent_id": item.agent_id, "runtime_id": item.runtime_id, "purpose": item.purpose, "failure_category": item.failure_category, "reason_code": item.reason_code} for item in invocations]
            return payload

    def _delegation_payload(self, row: DelegationRecord) -> dict[str, Any]:
        return {"delegation_id": row.delegation_id, "run_id": row.run_id, "work_order_id": row.work_order_id, "purpose": row.purpose, "state": row.state, "assigned_agent_id": row.assigned_agent_id, "assigned_runtime_id": row.assigned_runtime_id, "agent_profile_version": row.agent_profile_version, "assignment_sha256": row.assignment_sha256, "created_at": row.created_at.isoformat()}

    def approvals(self, run_id: str | None = None) -> list[dict[str, Any]]:
        with self.sessions() as session:
            query = select(ApprovalRequestRecord).order_by(ApprovalRequestRecord.created_at, ApprovalRequestRecord.approval_id)
            if run_id is not None:
                query = query.where(ApprovalRequestRecord.run_id == run_id)
            return [{"approval_id": row.approval_id, "run_id": row.run_id, "work_order_id": row.work_order_id, "status": row.status, "requester_actor_type": row.requester_actor_type, "requester_actor_id": row.requester_actor_id, "operation_type": row.operation_type, "created_at": row.created_at.isoformat()} for row in session.scalars(query).all()]

    def workspace_grants(self, run_id: str | None = None) -> list[dict[str, Any]]:
        with self.sessions() as session:
            query = select(WorkspaceGrantRecord).join(DelegationRecord).order_by(
                WorkspaceGrantRecord.created_at, WorkspaceGrantRecord.workspace_grant_id
            )
            if run_id is not None:
                query = query.where(DelegationRecord.run_id == run_id)
            rows = session.scalars(query).all()
            payload: list[dict[str, Any]] = []
            for row in rows:
                transport = session.scalar(select(WorkspaceTransportRecord).where(
                    WorkspaceTransportRecord.workspace_grant_id == row.workspace_grant_id
                ).order_by(WorkspaceTransportRecord.created_at.desc()).limit(1))
                reconciliation = session.scalar(select(WorkspaceReconciliationRecord).where(
                    WorkspaceReconciliationRecord.workspace_grant_id == row.workspace_grant_id
                ).order_by(WorkspaceReconciliationRecord.created_at.desc()).limit(1))
                payload.append({
                    "workspace_grant_id": row.workspace_grant_id,
                    "delegation_id": row.delegation_id,
                    "source_workspace_id": row.source_workspace_id,
                    "source_revision": row.source_revision,
                    "source_manifest_sha256": row.source_manifest_sha256,
                    "access_mode": row.access_mode,
                    "allowed_paths": row.allowed_paths,
                    "excluded_paths": row.excluded_paths,
                    "classification_ceiling": row.classification_ceiling,
                    "transport_kind": row.transport_kind,
                    "remote_workspace_handle": transport.remote_workspace_handle if transport else None,
                    "lease_expires_at": row.lease_expires_at.isoformat() if row.lease_expires_at else None,
                    "state": row.state,
                    "cleanup_state": row.cleanup_state,
                    "result_artifact_id": reconciliation.result_artifact_id if reconciliation else None,
                    "created_at": row.created_at.isoformat(),
                })
            return payload

    def artifacts(self, run_id: str) -> list[dict[str, Any]]:
        with self.sessions() as session:
            if session.get(ResearchRunRecord, run_id) is None:
                raise LookupError(run_id)
            attempt_rows = session.execute(select(ArtifactRecord).join(AttemptRecord, AttemptRecord.attempt_id == ArtifactRecord.attempt_id).join(WorkOrderRecord, WorkOrderRecord.work_order_id == AttemptRecord.work_order_id).where(WorkOrderRecord.run_id == run_id).order_by(ArtifactRecord.created_at, ArtifactRecord.artifact_id)).scalars().all()
            attachment_rows = session.execute(
                select(RunArtifactAttachmentRecord, ArtifactRecord)
                .join(
                    ArtifactRecord,
                    ArtifactRecord.artifact_id
                    == RunArtifactAttachmentRecord.artifact_id,
                )
                .where(RunArtifactAttachmentRecord.run_id == run_id)
                .order_by(
                    RunArtifactAttachmentRecord.created_at,
                    RunArtifactAttachmentRecord.attachment_id,
                )
            ).all()
            payload = [{"artifact_id": row.artifact_id, "sha256": row.sha256, "artifact_type": row.artifact_type, "classification": row.classification, "mime_type": row.mime_type, "attempt_id": row.attempt_id, "attachment_id": None, "source_name": row.relative_source_path, "recipient_agent_id": None, "created_at": row.created_at.isoformat()} for row in attempt_rows]
            payload.extend({"artifact_id": artifact.artifact_id, "sha256": artifact.sha256, "artifact_type": artifact.artifact_type, "classification": artifact.classification, "mime_type": artifact.mime_type, "attempt_id": None, "attachment_id": attachment.attachment_id, "source_name": attachment.source_name, "recipient_agent_id": attachment.recipient_agent_id, "created_at": attachment.created_at.isoformat()} for attachment, artifact in attachment_rows)
            return payload

    def timeline(self, run_id: str) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = [{**event, "kind": "event"} for event in self.events(run_id)]
        for delegation in self.delegations(run_id):
            items.append({"kind": "delegation", "entity_id": delegation["delegation_id"], "timestamp": delegation["created_at"], **delegation})
        for approval in self.approvals(run_id):
            items.append({"kind": "approval", "entity_id": approval["approval_id"], "timestamp": approval["created_at"], **approval})
        for artifact in self.artifacts(run_id):
            items.append({"kind": "artifact", "entity_id": artifact["artifact_id"], "timestamp": artifact["created_at"], **artifact})
        for grant in self.workspace_grants(run_id):
            items.append({"kind": "workspace_grant", "entity_id": grant["workspace_grant_id"], "timestamp": grant["created_at"], **grant})
        with self.sessions() as session:
            run = session.get(ResearchRunRecord, run_id)
            if run is None:
                raise LookupError(run_id)
            plans = session.scalars(select(PlanRecord).where(PlanRecord.run_id == run_id).order_by(PlanRecord.created_at, PlanRecord.plan_id)).all()
            orders = session.scalars(select(WorkOrderRecord).where(WorkOrderRecord.run_id == run_id).order_by(WorkOrderRecord.created_at, WorkOrderRecord.work_order_id)).all()
            attempts = session.scalars(select(AttemptRecord).join(WorkOrderRecord).where(WorkOrderRecord.run_id == run_id).order_by(AttemptRecord.created_at, AttemptRecord.attempt_id)).all()
            invocations = session.scalars(select(AgentInvocationRecord).where(AgentInvocationRecord.run_id == run_id).order_by(AgentInvocationRecord.created_at, AgentInvocationRecord.invocation_id)).all()
            observations = session.scalars(select(ObservationRecord).join(AttemptRecord).join(WorkOrderRecord).where(WorkOrderRecord.run_id == run_id).order_by(ObservationRecord.created_at, ObservationRecord.observation_id)).all()
            claims = session.scalars(select(ClaimRecord).join(AttemptRecord).join(WorkOrderRecord).where(WorkOrderRecord.run_id == run_id).order_by(ClaimRecord.created_at, ClaimRecord.claim_id)).all()
            verifications = session.scalars(select(VerificationResultRecord).join(WorkOrderRecord).where(WorkOrderRecord.run_id == run_id).order_by(VerificationResultRecord.created_at, VerificationResultRecord.verification_id)).all()
            reviews = session.scalars(select(ReviewDecisionRecord).where(ReviewDecisionRecord.run_id == run_id).order_by(ReviewDecisionRecord.created_at, ReviewDecisionRecord.review_id)).all()
            messages = session.scalars(select(CollaborationMessageRecord).where(CollaborationMessageRecord.run_id == run_id).order_by(CollaborationMessageRecord.created_at, CollaborationMessageRecord.message_id)).all()
            items.append({"kind": "goal", "entity_id": run.run_id, "timestamp": run.created_at.isoformat(), "run_id": run.run_id, "objective": run.objective})
            items.extend({"kind": "plan", "entity_id": item.plan_id, "timestamp": item.created_at.isoformat(), "plan_id": item.plan_id, "run_id": item.run_id} for item in plans)
            items.extend({"kind": "work_order", "entity_id": item.work_order_id, "timestamp": item.created_at.isoformat(), "work_order_id": item.work_order_id, "state": item.state, "objective": item.objective} for item in orders)
            items.extend({"kind": "attempt", "entity_id": item.attempt_id, "timestamp": item.created_at.isoformat(), "attempt_id": item.attempt_id, "work_order_id": item.work_order_id, "delegation_id": item.delegation_id, "state": item.state} for item in attempts)
            items.extend({"kind": "invocation", "entity_id": item.invocation_id, "timestamp": item.created_at.isoformat(), "invocation_id": item.invocation_id, "delegation_id": item.delegation_id, "workspace_grant_id": item.workspace_grant_id, "purpose": item.purpose, "agent_id": item.agent_id, "runtime_id": item.runtime_id, "status": item.status, "failure_category": item.failure_category, "reason_code": item.reason_code} for item in invocations)
            items.extend({"kind": "observation", "entity_id": item.observation_id, "timestamp": item.created_at.isoformat(), "attempt_id": item.attempt_id, "name": item.name, "producer_type": item.producer_type, "producer_id": item.producer_id, "classification": item.classification} for item in observations)
            items.extend({"kind": "claim", "entity_id": item.claim_id, "timestamp": item.created_at.isoformat(), "attempt_id": item.attempt_id, "producer_type": item.producer_type, "producer_id": item.producer_id} for item in claims)
            items.extend({"kind": "verification", "entity_id": item.verification_id, "timestamp": item.created_at.isoformat(), "attempt_id": item.attempt_id, "work_order_id": item.work_order_id, "overall": item.overall, "verifier_version": item.verifier_version, "valid": item.valid} for item in verifications)
            items.extend({"kind": "review", "entity_id": item.review_id, "timestamp": item.created_at.isoformat(), "work_order_id": item.work_order_id, "attempt_id": item.attempt_id, "decision": item.decision} for item in reviews)
            items.extend({"kind": "directive" if item.purpose == "DIRECTIVE" else "message", "entity_id": item.message_id, "timestamp": item.created_at.isoformat(), "work_order_id": item.work_order_id, "delegation_id": item.delegation_id, "invocation_id": item.invocation_id, "reply_to_message_id": item.reply_to_message_id, "purpose": item.purpose, "sender_actor_type": item.sender_actor_type, "sender_actor_id": item.sender_actor_id, "recipient_agent_id": item.recipient_agent_id, "classification": item.classification} for item in messages)
        return sorted(items, key=lambda item: (item["timestamp"], item["kind"], item["entity_id"]))

    async def cancel_run(self, run_id: str) -> dict[str, Any]:
        if self.orchestrator is None:
            raise RuntimeError("controller is required for state-changing commands")
        await self.orchestrator.cancel(run_id)
        return self.run_status(run_id)

    def resume_run(self, run_id: str) -> dict[str, Any]:
        if self.orchestrator is None:
            raise RuntimeError("controller is required for state-changing commands")
        self.orchestrator.resume_external(run_id)
        return self.run_status(run_id)

    def retry_work_order(
        self,
        work_order_id: str,
        *,
        target_agent_id: str | None = None,
    ) -> dict[str, Any]:
        if self.orchestrator is None:
            raise RuntimeError("controller is required for state-changing commands")
        attempt_id = self.orchestrator.retry_attempt(
            work_order_id,
            preferred_agent_id=target_agent_id,
        )
        resource = self.work_order_status(work_order_id)
        resource["attempt_id"] = attempt_id
        return resource

    async def approve(self, work_order_id: str, grant_id: str) -> dict[str, Any]:
        if self.orchestrator is None:
            raise RuntimeError("controller is required for state-changing commands")
        await self.orchestrator.approve(work_order_id, grant_id)
        return self.work_order_status(work_order_id)

    async def approve_request(self, approval_id: str, *, granted_by: str) -> dict[str, Any]:
        """Resolve HUMAN intent through the pending request, never a grant ID."""
        if self.orchestrator is None:
            raise RuntimeError("approval controller is required for approval commands")
        work_order_id, _ = self.orchestrator.approve_request(
            approval_id, granted_by=granted_by,
        )
        resource = self.work_order_status(work_order_id)
        resource["approval_id"] = approval_id
        return resource

    def resolve_human(self, work_order_id: str, *, action: str, objective: str | None = None) -> dict[str, Any]:
        if self.orchestrator is None:
            raise RuntimeError("controller is required for state-changing commands")
        self.orchestrator.resolve_human(work_order_id, action=action, objective=objective)
        return self.work_order_status(work_order_id)

    def create_workspace(self, workspace_id: str, name: str) -> dict[str, Any]:
        record = WorkspaceCreationService(self.sessions).create(workspace_id, name)
        return {"workspace_id": record.workspace_id, "name": record.name, "version": record.version}

    def create_research_task(
        self,
        workspace_id: str,
        objective: str,
        *,
        run_id: str | None = None,
    ) -> dict[str, Any]:
        if self.orchestrator is None:
            raise RuntimeError("controller is required for state-changing commands")
        created = self.orchestrator.create_run(workspace_id=workspace_id, objective=objective, run_id=run_id)
        return {"run_id": created, "workspace_id": workspace_id, "state": "NEW"}

    def reject(
        self,
        work_order_id: str,
        approval_id: str,
        *,
        actor_type: str,
        actor_id: str,
    ) -> dict[str, Any]:
        if self.orchestrator is None:
            raise RuntimeError("controller is required for state-changing commands")
        self.orchestrator.reject(work_order_id, approval_id, actor_type=actor_type, actor_id=actor_id)
        return self.work_order_status(work_order_id)

    def send_collaboration_message(self, message: CollaborationMessage) -> dict[str, Any]:
        CollaborationMessageService(self.sessions).append(message)
        return {"message_id": str(message.message_id), "run_id": message.run_id, "purpose": message.purpose}


__all__ = ["LocalControlAPI"]
