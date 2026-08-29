"""In-process local control API used by a loopback HTTP/CLI adapter."""

from collections.abc import Sequence
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from researchd.orchestrator.engine import ResearchOrchestrator, RunSnapshot
from researchd.storage.models import AgentRecord, AgentRuntimeRecord, ArtifactRecord, ApprovalRequestRecord, AuditEventRecord, DelegationRecord, AgentInvocationRecord, AttemptRecord, PlanRecord, ResearchRunRecord, WorkOrderRecord


class LocalControlAPI:
    """Controller facade; it exposes structured status, never agent conversation or raw files."""

    def __init__(self, sessions: sessionmaker[Session], orchestrator: ResearchOrchestrator) -> None:
        self.sessions = sessions
        self.orchestrator = orchestrator

    def run_status(self, run_id: str) -> dict[str, Any]:
        snapshot = self.orchestrator.snapshot(run_id)
        with self.sessions() as session:
            run = session.get(ResearchRunRecord, run_id)
            if run is None:
                raise LookupError(run_id)
            return {
                "run_id": snapshot.run_id,
                "state": snapshot.state.value,
                "work_orders": [{"work_order_id": identifier, "state": state.value} for identifier, state in snapshot.work_orders],
                "pending_approval_ids": list(snapshot.pending_approval_ids),
                "active_attempt_ids": list(snapshot.active_attempt_ids),
                "iterations_used": run.iterations_used,
                "max_iterations": run.max_iterations,
                "cloud_calls_used": run.cloud_calls_used,
                "max_cloud_calls": run.max_cloud_calls,
                "cancellation_requested": run.cancellation_requested,
            }

    def runs(self) -> list[dict[str, Any]]:
        with self.sessions() as session:
            identifiers = session.scalars(select(ResearchRunRecord.run_id).order_by(ResearchRunRecord.created_at, ResearchRunRecord.run_id)).all()
        return [self.run_status(run_id) for run_id in identifiers]

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

    def events(self, run_id: str, *, after_event_id: str | None = None) -> list[dict[str, Any]]:
        with self.sessions() as session:
            records: Sequence[AuditEventRecord] = session.scalars(
                select(AuditEventRecord).where(AuditEventRecord.run_id == run_id).order_by(AuditEventRecord.timestamp, AuditEventRecord.event_id),
            ).all()
            payload = [{
                "event_id": event.event_id, "event_type": event.event_type,
                "entity_type": event.entity_type, "entity_id": event.entity_id,
                "timestamp": event.timestamp.isoformat(), "correlation_id": event.correlation_id,
                "metadata": event.metadata_json,
            } for event in records]
            if after_event_id is None:
                return payload
            for index, event in enumerate(payload):
                if event["event_id"] == after_event_id:
                    return payload[index + 1:]
            # An unknown cursor is treated as an initial read. This keeps clients
            # resilient when their cursor predates retention or a new run.
            return payload

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
            payload["invocations"] = [{"invocation_id": item.invocation_id, "status": item.status, "agent_id": item.agent_id, "runtime_id": item.runtime_id, "purpose": item.purpose} for item in invocations]
            return payload

    def _delegation_payload(self, row: DelegationRecord) -> dict[str, Any]:
        return {"delegation_id": row.delegation_id, "run_id": row.run_id, "work_order_id": row.work_order_id, "purpose": row.purpose, "state": row.state, "assigned_agent_id": row.assigned_agent_id, "assigned_runtime_id": row.assigned_runtime_id, "agent_profile_version": row.agent_profile_version, "assignment_sha256": row.assignment_sha256, "created_at": row.created_at.isoformat()}

    def approvals(self, run_id: str | None = None) -> list[dict[str, Any]]:
        with self.sessions() as session:
            query = select(ApprovalRequestRecord).order_by(ApprovalRequestRecord.created_at, ApprovalRequestRecord.approval_id)
            if run_id is not None:
                query = query.where(ApprovalRequestRecord.run_id == run_id)
            return [{"approval_id": row.approval_id, "run_id": row.run_id, "work_order_id": row.work_order_id, "status": row.status, "requester_actor_type": row.requester_actor_type, "requester_actor_id": row.requester_actor_id, "operation_type": row.operation_type, "created_at": row.created_at.isoformat()} for row in session.scalars(query).all()]

    def artifacts(self, run_id: str) -> list[dict[str, Any]]:
        with self.sessions() as session:
            rows = session.execute(select(ArtifactRecord).join(AttemptRecord, AttemptRecord.attempt_id == ArtifactRecord.attempt_id).join(WorkOrderRecord, WorkOrderRecord.work_order_id == AttemptRecord.work_order_id).where(WorkOrderRecord.run_id == run_id).order_by(ArtifactRecord.created_at, ArtifactRecord.artifact_id)).scalars().all()
            return [{"artifact_id": row.artifact_id, "sha256": row.sha256, "artifact_type": row.artifact_type, "classification": row.classification, "mime_type": row.mime_type, "attempt_id": row.attempt_id, "created_at": row.created_at.isoformat()} for row in rows]

    def timeline(self, run_id: str) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = [{**event, "kind": "event"} for event in self.events(run_id)]
        for delegation in self.delegations(run_id):
            items.append({"kind": "delegation", "entity_id": delegation["delegation_id"], "timestamp": delegation["created_at"], **delegation})
        for approval in self.approvals(run_id):
            items.append({"kind": "approval", "entity_id": approval["approval_id"], "timestamp": approval["created_at"], **approval})
        for artifact in self.artifacts(run_id):
            items.append({"kind": "artifact", "entity_id": artifact["artifact_id"], "timestamp": artifact["created_at"], **artifact})
        with self.sessions() as session:
            plans = session.scalars(select(PlanRecord).where(PlanRecord.run_id == run_id).order_by(PlanRecord.created_at, PlanRecord.plan_id)).all()
            orders = session.scalars(select(WorkOrderRecord).where(WorkOrderRecord.run_id == run_id).order_by(WorkOrderRecord.created_at, WorkOrderRecord.work_order_id)).all()
            attempts = session.scalars(select(AttemptRecord).join(WorkOrderRecord).where(WorkOrderRecord.run_id == run_id).order_by(AttemptRecord.created_at, AttemptRecord.attempt_id)).all()
            invocations = session.scalars(select(AgentInvocationRecord).where(AgentInvocationRecord.run_id == run_id).order_by(AgentInvocationRecord.created_at, AgentInvocationRecord.invocation_id)).all()
            items.extend({"kind": "plan", "entity_id": item.plan_id, "timestamp": item.created_at.isoformat(), "plan_id": item.plan_id, "run_id": item.run_id} for item in plans)
            items.extend({"kind": "work_order", "entity_id": item.work_order_id, "timestamp": item.created_at.isoformat(), "work_order_id": item.work_order_id, "state": item.state, "objective": item.objective} for item in orders)
            items.extend({"kind": "attempt", "entity_id": item.attempt_id, "timestamp": item.created_at.isoformat(), "attempt_id": item.attempt_id, "work_order_id": item.work_order_id, "delegation_id": item.delegation_id, "state": item.state} for item in attempts)
            items.extend({"kind": "invocation", "entity_id": item.invocation_id, "timestamp": item.created_at.isoformat(), "invocation_id": item.invocation_id, "delegation_id": item.delegation_id, "purpose": item.purpose, "agent_id": item.agent_id, "runtime_id": item.runtime_id, "status": item.status} for item in invocations)
        return sorted(items, key=lambda item: (item["timestamp"], item["kind"], item["entity_id"]))

    async def cancel_run(self, run_id: str) -> dict[str, Any]:
        await self.orchestrator.cancel(run_id)
        return self.run_status(run_id)

    async def approve(self, work_order_id: str, grant_id: str) -> dict[str, Any]:
        await self.orchestrator.approve(work_order_id, grant_id)
        return self.work_order_status(work_order_id)

    def resolve_human(self, work_order_id: str, *, action: str, objective: str | None = None) -> dict[str, Any]:
        self.orchestrator.resolve_human(work_order_id, action=action, objective=objective)
        return self.work_order_status(work_order_id)


__all__ = ["LocalControlAPI"]
