"""Operator reconciliation for interrupted daemon command receipts.

An ``ACCEPTED`` receipt whose outcome was lost to a crash blocks the startup
barrier, and therefore every readiness-gated mutation. The only way out is a
narrow, authenticated recovery channel that converges the receipt to an
existing terminal state. The outcome is never an operator assertion: a
command-specific observer first observes the authoritative state, and the
operator may only abandon an undetermined outcome (``OPERATOR_ABANDONED``).
"""

import hashlib
import json
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, Protocol, cast
from uuid import uuid4

from sqlalchemy import select, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.orm import Session, sessionmaker

from researchd.backup.snapshot import BackupError, read_snapshot_manifest
from researchd.daemon.contracts import DaemonCommandResolveCommand, DaemonCommandResult
from researchd.domain.base import DomainModel
from researchd.domain.enums import ApprovalStatus, ResearchRunState, WorkOrderState
from researchd.runtime_sessions.contracts import SupervisorState
from researchd.storage.models import (
    ApprovalRequestRecord,
    AuditEventRecord,
    CollaborationMessageRecord,
    DaemonCommandRecord,
    ResearchRunRecord,
    RuntimeSessionRecord,
    WorkOrderRecord,
    WorkspaceRecord,
)


class ObservedOutcome(DomainModel):
    """Authoritative observation of a command family's durable effect."""

    status: Literal["COMPLETED", "REJECTED", "UNDETERMINED"]
    resource: dict[str, object] | None = None
    reason_code: str | None = None


class CommandOutcomeObserver(Protocol):
    """Observe the authoritative state one command family left behind."""

    def observe(self, resource_ref: Mapping[str, str]) -> ObservedOutcome: ...


def _ref(resource_ref: Mapping[str, str], *keys: str) -> tuple[str, ...] | None:
    values = tuple(resource_ref.get(key, "") for key in keys)
    if not all(values):
        return None
    return values


class RunCancelObserver:
    """The cancel effect is durable once the run reached CANCELLED."""

    def __init__(self, sessions: sessionmaker[Session]) -> None:
        self.sessions = sessions

    def observe(self, resource_ref: Mapping[str, str]) -> ObservedOutcome:
        ref = _ref(resource_ref, "run_id")
        if ref is None:
            return ObservedOutcome(status="UNDETERMINED", reason_code="resource_ref_invalid")
        run_id = ref[0]
        with self.sessions() as session:
            run = session.get(ResearchRunRecord, run_id)
            if run is None:
                return ObservedOutcome(status="REJECTED", reason_code="target_missing")
            resource: dict[str, object] = {
                "run_id": run.run_id,
                "state": run.state,
                "cancellation_requested": run.cancellation_requested,
            }
        if run.state == ResearchRunState.CANCELLED.value:
            return ObservedOutcome(status="COMPLETED", resource=resource)
        if run.cancellation_requested:
            return ObservedOutcome(
                status="UNDETERMINED",
                resource=resource,
                reason_code="cancellation_partial",
            )
        return ObservedOutcome(status="REJECTED", resource=resource, reason_code="effect_absent")


class WorkOrderApproveObserver:
    """The approve effect is durable once the granted grant advanced the order."""

    def __init__(self, sessions: sessionmaker[Session]) -> None:
        self.sessions = sessions

    def observe(self, resource_ref: Mapping[str, str]) -> ObservedOutcome:
        ref = _ref(resource_ref, "work_order_id", "grant_id")
        if ref is None:
            return ObservedOutcome(status="UNDETERMINED", reason_code="resource_ref_invalid")
        work_order_id, grant_id = ref
        with self.sessions() as session:
            order = session.get(WorkOrderRecord, work_order_id)
            if order is None:
                return ObservedOutcome(status="REJECTED", reason_code="target_missing")
            resource: dict[str, object] = {
                "work_order_id": order.work_order_id,
                "state": order.state,
                "approval_grant_id": order.approval_grant_id,
            }
        if order.state == WorkOrderState.WAITING_APPROVAL.value:
            return ObservedOutcome(status="REJECTED", resource=resource, reason_code="effect_absent")
        if order.approval_grant_id == grant_id:
            return ObservedOutcome(status="COMPLETED", resource=resource)
        return ObservedOutcome(
            status="UNDETERMINED",
            resource=resource,
            reason_code="conflicting_grant",
        )


class HumanDecisionObserver:
    """A HUMAN_REQUIRED pause resolved by a human decision leaves an audit event."""

    def __init__(self, sessions: sessionmaker[Session]) -> None:
        self.sessions = sessions

    def observe(self, resource_ref: Mapping[str, str]) -> ObservedOutcome:
        ref = _ref(resource_ref, "work_order_id")
        if ref is None:
            return ObservedOutcome(status="UNDETERMINED", reason_code="resource_ref_invalid")
        work_order_id = ref[0]
        with self.sessions() as session:
            order = session.get(WorkOrderRecord, work_order_id)
            if order is None:
                return ObservedOutcome(status="REJECTED", reason_code="target_missing")
            resolved_by = session.scalar(select(AuditEventRecord.event_type).where(
                AuditEventRecord.entity_type == "work_order",
                AuditEventRecord.entity_id == work_order_id,
                AuditEventRecord.event_type.in_(("HUMAN_ABORTED", "HUMAN_REVISION_REQUESTED")),
            ).limit(1))
            resource: dict[str, object] = {
                "work_order_id": order.work_order_id,
                "state": order.state,
                "resolved_by": resolved_by,
            }
        if resolved_by is not None:
            return ObservedOutcome(status="COMPLETED", resource=resource)
        if order.state == WorkOrderState.HUMAN_REQUIRED.value:
            return ObservedOutcome(status="REJECTED", resource=resource, reason_code="effect_absent")
        return ObservedOutcome(
            status="UNDETERMINED",
            resource=resource,
            reason_code="outcome_unverifiable",
        )


class RuntimeSessionStartObserver:
    """A started session is durable once the supervisor settled it with an identity."""

    def __init__(self, sessions: sessionmaker[Session]) -> None:
        self.sessions = sessions

    def observe(self, resource_ref: Mapping[str, str]) -> ObservedOutcome:
        ref = _ref(resource_ref, "runtime_session_id")
        if ref is None:
            return ObservedOutcome(status="UNDETERMINED", reason_code="resource_ref_invalid")
        with self.sessions() as session:
            row = session.get(RuntimeSessionRecord, ref[0])
            if row is None:
                return ObservedOutcome(status="REJECTED", reason_code="target_missing")
            resource: dict[str, object] = {
                "runtime_session_id": row.runtime_session_id,
                "supervisor_state": row.supervisor_state,
                "exit_reason": row.exit_reason,
            }
        state = row.supervisor_state
        if state in {SupervisorState.HEALTHY.value, SupervisorState.STOPPED.value}:
            return ObservedOutcome(status="COMPLETED", resource=resource)
        if state == SupervisorState.LOST.value:
            return ObservedOutcome(status="REJECTED", resource=resource, reason_code="effect_absent")
        return ObservedOutcome(
            status="UNDETERMINED",
            resource=resource,
            reason_code="session_unsettled",
        )


class RuntimeSessionStopObserver:
    """The stop effect is durable once the external instance is no longer present."""

    def __init__(self, sessions: sessionmaker[Session]) -> None:
        self.sessions = sessions

    def observe(self, resource_ref: Mapping[str, str]) -> ObservedOutcome:
        ref = _ref(resource_ref, "runtime_session_id")
        if ref is None:
            return ObservedOutcome(status="UNDETERMINED", reason_code="resource_ref_invalid")
        with self.sessions() as session:
            row = session.get(RuntimeSessionRecord, ref[0])
            if row is None:
                return ObservedOutcome(status="REJECTED", reason_code="target_missing")
            resource: dict[str, object] = {
                "runtime_session_id": row.runtime_session_id,
                "supervisor_state": row.supervisor_state,
                "exit_reason": row.exit_reason,
            }
        state = row.supervisor_state
        if state in {SupervisorState.STOPPED.value, SupervisorState.LOST.value}:
            return ObservedOutcome(status="COMPLETED", resource=resource)
        if state in {SupervisorState.HEALTHY.value, SupervisorState.DEGRADED.value}:
            return ObservedOutcome(status="REJECTED", resource=resource, reason_code="effect_absent")
        return ObservedOutcome(
            status="UNDETERMINED",
            resource=resource,
            reason_code="session_unsettled",
        )


class WorkspaceCreateObserver:
    """A created workspace is durable once the authoritative record exists."""

    def __init__(self, sessions: sessionmaker[Session]) -> None:
        self.sessions = sessions

    def observe(self, resource_ref: Mapping[str, str]) -> ObservedOutcome:
        ref = _ref(resource_ref, "workspace_id")
        if ref is None:
            return ObservedOutcome(status="UNDETERMINED", reason_code="resource_ref_invalid")
        with self.sessions() as session:
            workspace = session.get(WorkspaceRecord, ref[0])
            if workspace is None:
                return ObservedOutcome(status="REJECTED", reason_code="target_missing")
            resource: dict[str, object] = {
                "workspace_id": workspace.workspace_id,
                "name": workspace.name,
            }
        return ObservedOutcome(status="COMPLETED", resource=resource)


class ResearchTaskCreateObserver:
    """A created research task is durable once the run record exists."""

    def __init__(self, sessions: sessionmaker[Session]) -> None:
        self.sessions = sessions

    def observe(self, resource_ref: Mapping[str, str]) -> ObservedOutcome:
        ref = _ref(resource_ref, "run_id")
        if ref is None:
            return ObservedOutcome(status="UNDETERMINED", reason_code="resource_ref_invalid")
        with self.sessions() as session:
            run = session.get(ResearchRunRecord, ref[0])
            if run is None:
                return ObservedOutcome(status="REJECTED", reason_code="target_missing")
            resource: dict[str, object] = {
                "run_id": run.run_id,
                "state": run.state,
            }
        return ObservedOutcome(status="COMPLETED", resource=resource)


class WorkOrderRejectObserver:
    """A rejected approval is durable once the request converged to REJECTED."""

    def __init__(self, sessions: sessionmaker[Session]) -> None:
        self.sessions = sessions

    def observe(self, resource_ref: Mapping[str, str]) -> ObservedOutcome:
        ref = _ref(resource_ref, "work_order_id", "approval_id")
        if ref is None:
            return ObservedOutcome(status="UNDETERMINED", reason_code="resource_ref_invalid")
        work_order_id, approval_id = ref
        with self.sessions() as session:
            request = session.get(ApprovalRequestRecord, approval_id)
            if request is None:
                return ObservedOutcome(status="REJECTED", reason_code="target_missing")
            order = session.get(WorkOrderRecord, work_order_id)
            resource: dict[str, object] = {
                "work_order_id": work_order_id,
                "approval_id": approval_id,
                "approval_status": request.status,
                "work_order_state": order.state if order is not None else None,
            }
        if request.status == ApprovalStatus.REJECTED.value:
            if order is not None and order.state == WorkOrderState.FAILED.value:
                return ObservedOutcome(status="COMPLETED", resource=resource)
            return ObservedOutcome(
                status="UNDETERMINED",
                resource=resource,
                reason_code="rejection_partial",
            )
        if request.status == ApprovalStatus.PENDING.value:
            return ObservedOutcome(status="REJECTED", resource=resource, reason_code="effect_absent")
        return ObservedOutcome(
            status="UNDETERMINED",
            resource=resource,
            reason_code="conflicting_approval",
        )


class CollaborationMessageSendObserver:
    """A sent message is durable once the append-only record exists."""

    def __init__(self, sessions: sessionmaker[Session]) -> None:
        self.sessions = sessions

    def observe(self, resource_ref: Mapping[str, str]) -> ObservedOutcome:
        ref = _ref(resource_ref, "message_id")
        if ref is None:
            return ObservedOutcome(status="UNDETERMINED", reason_code="resource_ref_invalid")
        with self.sessions() as session:
            record = session.get(CollaborationMessageRecord, ref[0])
            if record is None:
                return ObservedOutcome(status="REJECTED", reason_code="target_missing")
            resource: dict[str, object] = {
                "message_id": record.message_id,
                "run_id": record.run_id,
                "purpose": record.purpose,
            }
        return ObservedOutcome(status="COMPLETED", resource=resource)


class BackupCreateObserver:
    """A created snapshot is durable once its tree is complete and valid."""

    def observe(self, resource_ref: Mapping[str, str]) -> ObservedOutcome:
        ref = _ref(resource_ref, "destination")
        if ref is None:
            return ObservedOutcome(status="UNDETERMINED", reason_code="resource_ref_invalid")
        try:
            manifest = read_snapshot_manifest(Path(ref[0]))
        except BackupError:
            # backup_snapshot is atomic: a crash leaves either a complete tree
            # or nothing. A damaged tree is undecidable, not a failed create.
            return ObservedOutcome(
                status="UNDETERMINED",
                reason_code="snapshot_invalid",
            )
        resource: dict[str, object] = {
            "destination": ref[0],
            "candidate_commit": manifest.candidate_commit,
            "candidate_tag": manifest.candidate_tag,
            "schema_revision": manifest.schema_revision,
        }
        return ObservedOutcome(status="COMPLETED", resource=resource)


class ReadOnlyEffectObserver:
    """Read-only commands leave no durable effect; only abandonment applies."""

    def observe(self, resource_ref: Mapping[str, str]) -> ObservedOutcome:
        del resource_ref
        return ObservedOutcome(
            status="UNDETERMINED",
            reason_code="read_only_no_persistent_effect",
        )


def build_builtin_observers(sessions: sessionmaker[Session]) -> dict[str, CommandOutcomeObserver]:
    """Bind the closed command families of the current dispatcher to observers."""
    start_observer = RuntimeSessionStartObserver(sessions)
    read_only = ReadOnlyEffectObserver()
    return {
        "RunCancelCommand": RunCancelObserver(sessions),
        "WorkOrderApproveCommand": WorkOrderApproveObserver(sessions),
        "HumanDecisionCommand": HumanDecisionObserver(sessions),
        "RuntimeSessionStartCommand": start_observer,
        "RuntimeSessionAttachCommand": start_observer,
        "RuntimeSessionStopCommand": RuntimeSessionStopObserver(sessions),
        "WorkspaceCreateCommand": WorkspaceCreateObserver(sessions),
        "ResearchTaskCreateCommand": ResearchTaskCreateObserver(sessions),
        "WorkOrderRejectCommand": WorkOrderRejectObserver(sessions),
        "CollaborationMessageSendCommand": CollaborationMessageSendObserver(sessions),
        "BackupCreateCommand": BackupCreateObserver(),
        "BackupVerifyCommand": read_only,
        "RestorePlanCommand": read_only,
    }


class DaemonCommandResolutionService:
    """Converge an interrupted ACCEPTED receipt through observation, atomically.

    The target receipt, the operator's resolution receipt and every audit
    event are committed in one transaction, so a crash can never leave a
    resolution half-applied. A terminal target can only be replayed, never
    re-resolved with a different result.
    """

    def __init__(
        self,
        sessions: sessionmaker[Session],
        observers: Mapping[str, CommandOutcomeObserver],
    ) -> None:
        self.sessions = sessions
        self.observers = dict(observers)

    def resolve(self, command: DaemonCommandResolveCommand) -> DaemonCommandResult:
        with self.sessions() as session:
            existing = session.get(DaemonCommandRecord, command.command_id)
            if existing is not None:
                return self._replay(existing)
            target = session.get(DaemonCommandRecord, command.target_command_id)
            if target is None:
                return self._persist_rejection(command, "target_missing")
            if target.status != "ACCEPTED":
                return self._persist_rejection(command, "receipt_not_pending")
            observer = self.observers.get(target.command_type)
            if observer is None:
                return self._persist_rejection(command, "unsupported_command_family")
        observation = observer.observe(command.resource_ref)
        if observation.status == "UNDETERMINED" and not command.abandon:
            # Nothing is persisted: the operator investigates and retries.
            return self._undetermined(command, target, observation)
        target_status: str
        target_reason: str | None
        if observation.status == "UNDETERMINED":
            target_status, target_reason = "REJECTED", "OPERATOR_ABANDONED"
        else:
            target_status, target_reason = observation.status, observation.reason_code
        envelope = self._converged(command, target, target_status, target_reason, observation)
        now = datetime.now(UTC)
        with self.sessions.begin() as session:
            guarded = cast("CursorResult[Any]", session.execute(
                update(DaemonCommandRecord)
                .where(
                    DaemonCommandRecord.command_id == command.target_command_id,
                    DaemonCommandRecord.status == "ACCEPTED",
                )
                .values(
                    status=target_status,
                    result_json=self._target_result(target, target_status, target_reason, observation),
                    reason_code=target_reason,
                    updated_at=now,
                )
            ))
            if guarded.rowcount != 1:
                # A concurrent resolution converged the target first.
                return self._persist_rejection(command, "receipt_not_pending")
            session.add(self._receipt(command, envelope, now))
            session.add(self._target_event(target, command, target_status, target_reason, now))
            session.add(self._receipt_event(command, "DAEMON_COMMAND_COMPLETED", now))
        return envelope

    def _replay(self, existing: DaemonCommandRecord) -> DaemonCommandResult:
        if existing.status in {"COMPLETED", "REJECTED"}:
            if existing.result_json is None:
                raise RuntimeError("final daemon command receipt has no result")
            return DaemonCommandResult.model_validate(existing.result_json)
        return DaemonCommandResult(
            command_id=existing.command_id,
            command_type="DaemonCommandResolve",
            status="ACCEPTED",
            reason_code="COMMAND_OUTCOME_UNKNOWN",
        )

    def _persist_rejection(
        self,
        command: DaemonCommandResolveCommand,
        reason_code: str,
    ) -> DaemonCommandResult:
        result = DaemonCommandResult(
            command_id=command.command_id,
            command_type="DaemonCommandResolve",
            status="REJECTED",
            reason_code=reason_code,
        )
        now = datetime.now(UTC)
        with self.sessions.begin() as session:
            session.add(DaemonCommandRecord(
                command_id=command.command_id,
                command_type=type(command).__name__,
                command_version=command.command_version,
                request_sha256=self._request_sha256(command),
                actor_type=command.actor_type,
                actor_id=command.actor_id,
                status="REJECTED",
                result_json=result.model_dump(mode="json"),
                reason_code=reason_code,
                created_at=now,
                updated_at=now,
            ))
            session.add(self._receipt_event(command, "DAEMON_COMMAND_REJECTED", now))
        return result

    @staticmethod
    def _undetermined(
        command: DaemonCommandResolveCommand,
        target: DaemonCommandRecord,
        observation: ObservedOutcome,
    ) -> DaemonCommandResult:
        return DaemonCommandResult(
            command_id=command.command_id,
            command_type="DaemonCommandResolve",
            status="ACCEPTED",
            resource={
                "target_command_id": target.command_id,
                "target_command_type": target.command_type,
                "target_status": "UNDETERMINED",
                "target_reason_code": observation.reason_code,
                "observation_status": observation.status,
                "observation_reason_code": observation.reason_code,
                "resource_ref": dict(command.resource_ref),
                "observed_resource": observation.resource,
            },
        )

    @staticmethod
    def _converged(
        command: DaemonCommandResolveCommand,
        target: DaemonCommandRecord,
        target_status: str,
        target_reason: str | None,
        observation: ObservedOutcome,
    ) -> DaemonCommandResult:
        return DaemonCommandResult(
            command_id=command.command_id,
            command_type="DaemonCommandResolve",
            status="ACCEPTED",
            resource={
                "target_command_id": target.command_id,
                "target_command_type": target.command_type,
                "target_status": target_status,
                "target_reason_code": target_reason,
                "observation_status": observation.status,
                "observation_reason_code": observation.reason_code,
                "resource_ref": dict(command.resource_ref),
                "observed_resource": observation.resource,
            },
        )

    @staticmethod
    def _target_result(
        target: DaemonCommandRecord,
        target_status: str,
        target_reason: str | None,
        observation: ObservedOutcome,
    ) -> dict[str, object]:
        envelope = DaemonCommandResult(
            command_id=target.command_id,
            command_type=target.command_type.removesuffix("Command"),
            status="ACCEPTED" if target_status == "COMPLETED" else "REJECTED",
            resource=observation.resource,
            reason_code=target_reason,
        )
        return envelope.model_dump(mode="json")

    def _receipt(
        self,
        command: DaemonCommandResolveCommand,
        envelope: DaemonCommandResult,
        now: datetime,
    ) -> DaemonCommandRecord:
        return DaemonCommandRecord(
            command_id=command.command_id,
            command_type=type(command).__name__,
            command_version=command.command_version,
            request_sha256=self._request_sha256(command),
            actor_type=command.actor_type,
            actor_id=command.actor_id,
            status="COMPLETED",
            result_json=envelope.model_dump(mode="json"),
            reason_code=None,
            created_at=now,
            updated_at=now,
        )

    def _target_event(
        self,
        target: DaemonCommandRecord,
        command: DaemonCommandResolveCommand,
        target_status: str,
        target_reason: str | None,
        now: datetime,
    ) -> AuditEventRecord:
        return AuditEventRecord(
            event_id=f"evt_{uuid4().hex}",
            event_type="DAEMON_COMMAND_RESOLVED",
            run_id=None,
            entity_type="daemon_command",
            entity_id=target.command_id,
            actor_type=command.actor_type,
            actor_id=command.actor_id,
            timestamp=now,
            correlation_id=target.command_id,
            causation_id=None,
            metadata_json={
                "command_type": target.command_type,
                "target_status": target_status,
                "reason_code": target_reason,
                "resource_ref": dict(command.resource_ref),
            },
        )

    def _receipt_event(
        self,
        command: DaemonCommandResolveCommand,
        event_type: str,
        now: datetime,
    ) -> AuditEventRecord:
        return AuditEventRecord(
            event_id=f"evt_{uuid4().hex}",
            event_type=event_type,
            run_id=None,
            entity_type="daemon_command",
            entity_id=command.command_id,
            actor_type=command.actor_type,
            actor_id=command.actor_id,
            timestamp=now,
            correlation_id=command.command_id,
            causation_id=None,
            metadata_json={"command_type": type(command).__name__},
        )

    @staticmethod
    def _request_sha256(command: DaemonCommandResolveCommand) -> str:
        payload = json.dumps(
            command.model_dump(mode="json"),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode()
        return hashlib.sha256(payload).hexdigest()


__all__ = [
    "BackupCreateObserver",
    "CollaborationMessageSendObserver",
    "CommandOutcomeObserver",
    "DaemonCommandResolutionService",
    "HumanDecisionObserver",
    "ObservedOutcome",
    "ReadOnlyEffectObserver",
    "ResearchTaskCreateObserver",
    "RunCancelObserver",
    "RuntimeSessionStartObserver",
    "RuntimeSessionStopObserver",
    "WorkOrderApproveObserver",
    "WorkOrderRejectObserver",
    "WorkspaceCreateObserver",
    "build_builtin_observers",
]
