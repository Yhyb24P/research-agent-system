from datetime import UTC, datetime, timedelta
from uuid import uuid4
from sqlalchemy.orm import Session, sessionmaker
from researchd.collaboration.contracts import AgentInvocationRequest, AgentInvocationResult, ExecuteInvocationInput
from researchd.domain.enums import InvocationStatus
from researchd.storage.models import (
    AgentInvocationRecord,
    AgentRuntimeRecord,
    AuditEventRecord,
    DelegationRecord,
    WorkspaceGrantRecord,
)


class StaleInvocationResult(ValueError):
    pass

class InvocationService:
    def __init__(self, sessions: sessionmaker[Session]) -> None:
        self.sessions = sessions

    def start(
        self,
        request: AgentInvocationRequest,
        *,
        timeout_seconds: float | None = None,
    ) -> None:
        if timeout_seconds is not None and timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        now = datetime.now(UTC)
        with self.sessions.begin() as session:
            existing = session.get(AgentInvocationRecord, str(request.invocation_id))
            if existing is not None:
                identity = (
                    existing.delegation_id,
                    existing.run_id,
                    existing.work_order_id,
                    existing.attempt_id,
                    existing.agent_id,
                    existing.runtime_id,
                    existing.purpose,
                    existing.input_sha256,
                )
                requested_identity = (
                    str(request.delegation_id),
                    request.run_id,
                    request.work_order_id,
                    request.attempt_id,
                    str(request.agent_id),
                    str(request.runtime_id),
                    request.purpose.value,
                    request.input_sha256,
                )
                if identity != requested_identity:
                    raise StaleInvocationResult(
                        "duplicate invocation identity changed its authoritative scope"
                    )
                return
            delegation = session.get(DelegationRecord, str(request.delegation_id))
            if delegation is None or delegation.assigned_agent_id != str(request.agent_id) or delegation.assigned_runtime_id != str(request.runtime_id):
                raise ValueError("invocation does not match assigned delegation")
            if delegation.run_id != request.run_id or delegation.work_order_id != request.work_order_id:
                raise ValueError("invocation scope does not match delegation")
            if delegation.state not in {"ASSIGNED", "RUNNING"}:
                raise ValueError("delegation is terminal")
            runtime = session.get(AgentRuntimeRecord, str(request.runtime_id))
            if (
                runtime is None
                or not runtime.enabled
                or runtime.runtime_lease_id is None
                or runtime.lease_expires_at is None
                or runtime.lease_expires_at <= now
            ):
                raise ValueError("invocation runtime lease is missing or expired")
            binding_id = (
                request.typed_input.work_order.workspace_grant.workspace_grant_id
                if isinstance(request.typed_input, ExecuteInvocationInput)
                and request.typed_input.work_order.workspace_grant is not None
                else None
            )
            if request.workspace_grant_id != binding_id:
                raise ValueError("invocation workspace binding does not match its typed input")
            if request.workspace_grant_id is not None:
                grant = session.get(WorkspaceGrantRecord, request.workspace_grant_id)
                if (
                    grant is None
                    or grant.delegation_id != str(request.delegation_id)
                    or grant.state != "ACTIVE"
                    or grant.lease_expires_at is None
                    or grant.lease_expires_at <= now
                ):
                    raise ValueError("invocation workspace grant is missing, expired, or out of scope")
            invocation = AgentInvocationRecord(invocation_id=str(request.invocation_id), delegation_id=str(request.delegation_id), run_id=request.run_id, work_order_id=request.work_order_id, attempt_id=request.attempt_id, workspace_grant_id=request.workspace_grant_id, agent_id=str(request.agent_id), runtime_id=str(request.runtime_id), runtime_lease_id=runtime.runtime_lease_id, purpose=request.purpose.value, status=InvocationStatus.RUNNING.value, input_sha256=request.input_sha256, context_bundle_sha256=request.context_bundle.bundle_sha256 if request.context_bundle else None, context_bundle_json=request.context_bundle.model_dump(mode="json") if request.context_bundle else None, deadline_at=now + timedelta(seconds=timeout_seconds) if timeout_seconds is not None else None, created_at=now)
            session.add(invocation)
            session.add(self._audit(invocation, "AGENT_INVOCATION_STARTED", now))
            delegation.state = "RUNNING"
            delegation.updated_at = now
            delegation.version += 1

    def mark_dispatched(
        self,
        invocation_id: str,
        *,
        external_invocation_id: str | None = None,
        now: datetime | None = None,
    ) -> None:
        reference = now or datetime.now(UTC)
        with self.sessions.begin() as session:
            row = session.get(AgentInvocationRecord, invocation_id)
            if row is None or row.status != InvocationStatus.RUNNING.value:
                raise StaleInvocationResult("invocation is not running")
            runtime = session.get(AgentRuntimeRecord, row.runtime_id)
            if (
                runtime is None
                or runtime.runtime_lease_id != row.runtime_lease_id
                or runtime.lease_expires_at is None
                or runtime.lease_expires_at <= reference
            ):
                raise ValueError("bound runtime lease is missing, replaced, or expired")
            if row.dispatched_at is None:
                row.dispatched_at = reference
                session.add(self._audit(row, "AGENT_INVOCATION_DISPATCHED", reference))
            if external_invocation_id is not None:
                self._bind_external(row, external_invocation_id, reference)
                session.add(self._audit(row, "AGENT_INVOCATION_EXTERNAL_BOUND", reference))

    def bind_external(
        self,
        invocation_id: str,
        external_invocation_id: str,
        *,
        now: datetime | None = None,
    ) -> None:
        reference = now or datetime.now(UTC)
        with self.sessions.begin() as session:
            row = session.get(AgentInvocationRecord, invocation_id)
            if row is None or row.status != InvocationStatus.RUNNING.value:
                raise StaleInvocationResult("invocation is not running")
            if row.dispatched_at is None:
                raise ValueError("invocation must be dispatched before external identity binding")
            self._bind_external(row, external_invocation_id, reference)
            session.add(self._audit(row, "AGENT_INVOCATION_EXTERNAL_BOUND", reference))

    @staticmethod
    def _bind_external(
        row: AgentInvocationRecord,
        external_invocation_id: str,
        reference: datetime,
    ) -> None:
        if not external_invocation_id or len(external_invocation_id) > 256:
            raise ValueError("external invocation identity is invalid")
        if row.external_invocation_id not in {None, external_invocation_id}:
            raise StaleInvocationResult("external invocation identity changed")
        row.external_invocation_id = external_invocation_id
        row.external_started_at = row.external_started_at or reference

    def request_cancel(
        self,
        invocation_id: str,
        *,
        now: datetime | None = None,
    ) -> bool:
        reference = now or datetime.now(UTC)
        with self.sessions.begin() as session:
            row = session.get(AgentInvocationRecord, invocation_id)
            if row is None or row.status != InvocationStatus.RUNNING.value:
                raise StaleInvocationResult("invocation is already terminal or missing")
            row.cancel_requested_at = row.cancel_requested_at or reference
            session.add(self._audit(row, "AGENT_INVOCATION_CANCEL_REQUESTED", reference))
            if row.dispatched_at is None:
                row.status = InvocationStatus.CANCELLED.value
                row.reason_code = "CANCELLED_BEFORE_DISPATCH"
                row.completed_at = reference
                delegation = session.get(DelegationRecord, row.delegation_id)
                if delegation is not None:
                    delegation.state = InvocationStatus.CANCELLED.value
                    delegation.updated_at = reference
                    delegation.completed_at = reference
                    delegation.version += 1
                session.add(self._audit(row, "AGENT_INVOCATION_CANCELLED", reference))
                return True
            return False

    def complete(
        self,
        result: AgentInvocationResult,
        *,
        external_invocation_id: str | None = None,
        now: datetime | None = None,
    ) -> None:
        reference = now or datetime.now(UTC)
        with self.sessions.begin() as session:
            row = session.get(AgentInvocationRecord, str(result.invocation_id))
            if row is None or row.status != InvocationStatus.RUNNING.value:
                raise StaleInvocationResult("invocation is not running")
            result_external_id = external_invocation_id or result.external_invocation_id
            if row.external_invocation_id is not None:
                if result_external_id != row.external_invocation_id:
                    raise StaleInvocationResult("result external invocation identity is stale")
            elif result_external_id is not None:
                raise StaleInvocationResult("result arrived before external identity binding")
            row.status, row.output_type, row.output_json = result.status.value, result.output_type, result.output
            row.reason_code, row.completed_at = result.reason_code, reference
            row.last_reconciled_at = reference
            session.add(self._audit(row, "AGENT_INVOCATION_RECONCILED", reference, {
                "status": result.status.value,
            }))
            delegation = session.get(DelegationRecord, row.delegation_id)
            if delegation is not None:
                delegation.state = "COMPLETED" if result.status is InvocationStatus.SUCCEEDED else result.status.value
                delegation.updated_at = reference
                delegation.completed_at = reference
                delegation.version += 1

    def expire_deadlines(self, *, now: datetime | None = None) -> tuple[str, ...]:
        reference = now or datetime.now(UTC)
        expired: list[str] = []
        with self.sessions.begin() as session:
            rows = session.query(AgentInvocationRecord).filter(
                AgentInvocationRecord.status == InvocationStatus.RUNNING.value,
                AgentInvocationRecord.deadline_at.is_not(None),
                AgentInvocationRecord.deadline_at <= reference,
            ).all()
            for row in rows:
                row.status = InvocationStatus.FAILED.value
                row.reason_code = "INVOCATION_TIMEOUT"
                row.completed_at = reference
                row.last_reconciled_at = reference
                session.add(self._audit(row, "AGENT_INVOCATION_TIMED_OUT", reference))
                delegation = session.get(DelegationRecord, row.delegation_id)
                if delegation is not None:
                    delegation.state = InvocationStatus.FAILED.value
                    delegation.updated_at = reference
                    delegation.completed_at = reference
                    delegation.version += 1
                expired.append(row.invocation_id)
        return tuple(expired)

    def recover_run(self, run_id: str) -> tuple[str, ...]:
        """Reconcile externally bound work and fail closed work not yet bound."""
        now = datetime.now(UTC)
        recovered: list[str] = []
        with self.sessions.begin() as session:
            rows = session.query(AgentInvocationRecord).filter(
                AgentInvocationRecord.run_id == run_id,
                AgentInvocationRecord.status == InvocationStatus.RUNNING.value,
            ).all()
            for row in rows:
                if row.external_invocation_id is not None:
                    row.reason_code = "RECONCILIATION_REQUIRED"
                    row.reconciliation_requested_at = row.reconciliation_requested_at or now
                    session.add(self._audit(
                        row, "AGENT_INVOCATION_RECONCILIATION_REQUIRED", now
                    ))
                    recovered.append(row.invocation_id)
                    continue
                row.status = InvocationStatus.FAILED.value
                row.reason_code = "CONTROLLER_RESTARTED_BEFORE_EXTERNAL_BIND"
                row.completed_at = now
                session.add(self._audit(
                    row, "AGENT_INVOCATION_RESTART_FAILED", now
                ))
                delegation = session.get(DelegationRecord, row.delegation_id)
                if delegation is not None and delegation.state == "RUNNING":
                    delegation.state = InvocationStatus.FAILED.value
                    delegation.completed_at = now
                    delegation.updated_at = now
                    delegation.version += 1
                recovered.append(row.invocation_id)
        return tuple(recovered)

    @staticmethod
    def _audit(
        row: AgentInvocationRecord,
        event_type: str,
        timestamp: datetime,
        metadata: dict[str, object] | None = None,
    ) -> AuditEventRecord:
        return AuditEventRecord(
            event_id=f"evt_{uuid4().hex}",
            event_type=event_type,
            run_id=row.run_id,
            entity_type="agent_invocation",
            entity_id=row.invocation_id,
            actor_type="controller",
            actor_id="invocation-service",
            timestamp=timestamp,
            correlation_id=row.attempt_id or row.invocation_id,
            causation_id=None,
            metadata_json=metadata or {},
        )
