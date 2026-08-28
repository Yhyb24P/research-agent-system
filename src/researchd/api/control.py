"""In-process local control API used by a loopback HTTP/CLI adapter."""

from collections.abc import Sequence
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from researchd.orchestrator.engine import ResearchOrchestrator, RunSnapshot
from researchd.storage.models import AuditEventRecord, ResearchRunRecord, WorkOrderRecord


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

    def events(self, run_id: str) -> list[dict[str, Any]]:
        with self.sessions() as session:
            records: Sequence[AuditEventRecord] = session.scalars(
                select(AuditEventRecord).where(AuditEventRecord.run_id == run_id).order_by(AuditEventRecord.timestamp, AuditEventRecord.event_id),
            ).all()
            return [{
                "event_id": event.event_id, "event_type": event.event_type,
                "entity_type": event.entity_type, "entity_id": event.entity_id,
                "timestamp": event.timestamp.isoformat(), "correlation_id": event.correlation_id,
                "metadata": event.metadata_json,
            } for event in records]

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
