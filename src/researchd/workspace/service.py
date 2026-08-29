"""Authoritative WorkspaceGrant lifecycle and fail-closed lease enforcement."""

from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from researchd.artifacts.provenance import ArtifactService
from researchd.domain.enums import AgentTrustZone, DataClassification
from researchd.storage.models import (
    AgentRecord,
    AttemptRecord,
    AuditEventRecord,
    DelegationRecord,
    ResearchRunRecord,
    WorkspaceGrantRecord,
    WorkspaceReconciliationRecord,
    WorkspaceRecord,
    WorkspaceSnapshotRecord,
    WorkspaceTransportRecord,
)
from researchd.workspace.contracts import (
    CleanupState,
    ProvisionedWorkspace,
    ReconciliationMode,
    ReconciliationState,
    RenewalPolicy,
    WorkspaceAccessMode,
    WorkspaceGrant,
    WorkspaceGrantState,
    WorkspaceLimits,
    WorkspaceTransportKind,
)
from researchd.workspace.manifest import normalize_policy_path
from researchd.workspace.transports import WorkspaceTransport


class WorkspaceDelegationError(RuntimeError):
    pass


_TRUST_CLASSIFICATIONS = {
    AgentTrustZone.LOCAL_PRIVATE: frozenset({
        DataClassification.PUBLIC,
        DataClassification.CLOUD_SAFE,
        DataClassification.PROJECT_PRIVATE,
        DataClassification.LOCAL_ONLY,
    }),
    AgentTrustZone.REMOTE_PRIVATE: frozenset({
        DataClassification.PUBLIC,
        DataClassification.CLOUD_SAFE,
        DataClassification.PROJECT_PRIVATE,
    }),
    AgentTrustZone.EXTERNAL_CLOUD: frozenset({
        DataClassification.PUBLIC,
        DataClassification.CLOUD_SAFE,
    }),
    AgentTrustZone.EXTERNAL_UNTRUSTED: frozenset({DataClassification.PUBLIC}),
}


class WorkspaceDelegationService:
    def __init__(
        self,
        sessions: sessionmaker[Session],
        artifacts: ArtifactService,
        transports: tuple[WorkspaceTransport, ...],
    ) -> None:
        self.sessions = sessions
        self.artifacts = artifacts
        self.transports = {transport.kind: transport for transport in transports}
        if len(self.transports) != len(transports):
            raise ValueError("workspace transport kinds must be unique")

    def create(self, grant: WorkspaceGrant) -> None:
        allowed = tuple(normalize_policy_path(item) for item in grant.allowed_paths)
        excluded = tuple(normalize_policy_path(item) for item in grant.excluded_paths)
        now = datetime.now(UTC)
        with self.sessions.begin() as session:
            delegation = session.get(DelegationRecord, grant.delegation_id)
            if delegation is None or delegation.assigned_agent_id is None:
                raise WorkspaceDelegationError("workspace grant requires an assigned Delegation")
            if delegation.state not in {"ASSIGNED", "RUNNING"}:
                raise WorkspaceDelegationError("workspace grant requires a live Delegation")
            run = session.get(ResearchRunRecord, delegation.run_id)
            workspace = session.get(WorkspaceRecord, grant.source_workspace_id)
            agent = session.get(AgentRecord, delegation.assigned_agent_id)
            if run is None or workspace is None or run.workspace_id != workspace.workspace_id:
                raise WorkspaceDelegationError("workspace grant is outside the Delegation run")
            if agent is None:
                raise WorkspaceDelegationError("assigned Agent disappeared")
            if grant.classification_ceiling not in _TRUST_CLASSIFICATIONS[AgentTrustZone(agent.trust_zone)]:
                raise WorkspaceDelegationError("classification ceiling exceeds the assigned Agent trust zone")
            if grant.transport_kind not in self.transports:
                raise WorkspaceDelegationError("workspace transport is not registered")
            if session.get(WorkspaceGrantRecord, grant.workspace_grant_id) is not None:
                raise WorkspaceDelegationError("workspace grant already exists")
            existing = session.scalar(select(WorkspaceGrantRecord).where(
                WorkspaceGrantRecord.delegation_id == grant.delegation_id
            ))
            if existing is not None:
                raise WorkspaceDelegationError("Delegation already has a workspace grant")
            session.add(WorkspaceGrantRecord(
                workspace_grant_id=grant.workspace_grant_id,
                delegation_id=grant.delegation_id,
                source_workspace_id=grant.source_workspace_id,
                source_revision=grant.source_revision,
                source_manifest_sha256=grant.source_manifest_sha256,
                access_mode=grant.access_mode.value,
                allowed_paths=list(allowed),
                excluded_paths=list(excluded),
                classification_ceiling=grant.classification_ceiling.value,
                max_total_bytes=grant.limits.max_total_bytes,
                max_file_count=grant.limits.max_file_count,
                max_single_file_bytes=grant.limits.max_single_file_bytes,
                lease_seconds=grant.lease_seconds,
                lease_started_at=None,
                lease_expires_at=None,
                renewal_policy=grant.renewal_policy.value,
                transport_kind=grant.transport_kind.value,
                reconciliation_mode=grant.reconciliation_mode.value,
                state=WorkspaceGrantState.PENDING.value,
                cleanup_state=CleanupState.PENDING.value,
                version=1,
                created_at=now,
                updated_at=now,
            ))
            self._event(
                session,
                run_id=delegation.run_id,
                event_type="WORKSPACE_GRANT_CREATED",
                grant_id=grant.workspace_grant_id,
                correlation_id=grant.delegation_id,
                metadata={"transport_kind": grant.transport_kind.value, "access_mode": grant.access_mode.value},
            )

    def provision(self, grant_id: str, source_root: Path) -> ProvisionedWorkspace:
        grant = self._contract(grant_id)
        transport = self.transports[grant.transport_kind]
        snapshot = transport.snapshot(grant, source_root)
        if grant.source_manifest_sha256 is not None and snapshot.manifest_sha256 != grant.source_manifest_sha256:
            raise WorkspaceDelegationError("workspace manifest does not match the grant")
        snapshot_id = f"wss_{uuid4().hex}"
        transport_id = f"wst_{uuid4().hex}"
        planned = transport.plan(grant, source_root, snapshot)
        now = datetime.now(UTC)
        with self.sessions.begin() as session:
            row = self._locked_grant(session, grant_id, WorkspaceGrantState.PENDING)
            row.source_revision = snapshot.source_revision
            row.source_manifest_sha256 = snapshot.manifest_sha256
            row.state = WorkspaceGrantState.PROVISIONING.value
            row.updated_at = now
            row.version += 1
            session.add(WorkspaceSnapshotRecord(
                workspace_snapshot_id=snapshot_id,
                workspace_grant_id=grant_id,
                source_revision=snapshot.source_revision,
                manifest_sha256=snapshot.manifest_sha256,
                manifest=[item.model_dump(mode="json") for item in snapshot.files],
                total_bytes=snapshot.total_bytes,
                file_count=snapshot.file_count,
                created_at=now,
            ))
            session.add(WorkspaceTransportRecord(
                workspace_transport_id=transport_id,
                workspace_grant_id=grant_id,
                transport_kind=grant.transport_kind.value,
                transport_handle=planned.transport_handle,
                remote_workspace_handle=planned.remote_workspace_handle,
                state="PROVISIONING",
                created_at=now,
                closed_at=None,
            ))
        try:
            provisioned = transport.provision(grant, source_root, snapshot, planned)
            if provisioned != planned:
                raise WorkspaceDelegationError("workspace transport returned a handle different from its durable plan")
        except Exception:
            try:
                transport.cleanup(grant, planned)
            except Exception:
                self._set_cleanup_state(grant_id, CleanupState.FAILED)
            else:
                self._set_cleanup_state(grant_id, CleanupState.CLEANED)
            self._fail(grant_id, "WORKSPACE_PROVISION_FAILED")
            raise
        started = datetime.now(UTC)
        expires = started + timedelta(seconds=grant.lease_seconds)
        with self.sessions.begin() as session:
            row = self._locked_grant(session, grant_id, WorkspaceGrantState.PROVISIONING)
            transport_row = session.get(WorkspaceTransportRecord, transport_id)
            assert transport_row is not None
            transport_row.transport_handle = provisioned.transport_handle
            transport_row.remote_workspace_handle = provisioned.remote_workspace_handle
            transport_row.state = "ACTIVE"
            row.lease_started_at = started
            row.lease_expires_at = expires
            row.state = WorkspaceGrantState.ACTIVE.value
            row.updated_at = started
            row.version += 1
            delegation = session.get(DelegationRecord, row.delegation_id)
            assert delegation is not None
            self._event(
                session,
                run_id=delegation.run_id,
                event_type="WORKSPACE_LEASE_STARTED",
                grant_id=grant_id,
                correlation_id=row.delegation_id,
                metadata={"expires_at": expires.isoformat(), "manifest_sha256": snapshot.manifest_sha256},
            )
        return provisioned

    def reconcile(self, grant_id: str, *, remote_result: Path | None = None) -> str:
        grant = self._contract(grant_id)
        transport = self.transports[grant.transport_kind]
        provisioned, transport_id, base_manifest = self._active_transport(grant_id)
        now = datetime.now(UTC)
        expired = False
        with self.sessions.begin() as session:
            row = self._locked_grant(session, grant_id, WorkspaceGrantState.ACTIVE)
            if row.lease_expires_at is None or row.lease_expires_at <= now:
                row.state = WorkspaceGrantState.EXPIRED.value
                row.updated_at = now
                row.version += 1
                expired = True
            else:
                row.state = WorkspaceGrantState.RECONCILING.value
                row.updated_at = now
                row.version += 1
        if expired:
            raise WorkspaceDelegationError("workspace lease expired before reconciliation")
        try:
            payload = transport.reconcile(grant, provisioned, remote_result=remote_result)
            with self.sessions() as session:
                attempt_id = session.scalar(select(AttemptRecord.attempt_id).where(
                    AttemptRecord.delegation_id == grant.delegation_id
                ).order_by(AttemptRecord.created_at.desc()).limit(1))
            artifact = self.artifacts.register(
                payload.payload,
                mime_type=payload.mime_type,
                artifact_type=payload.artifact_type,
                classification=grant.classification_ceiling,
                producer_type="workspace_transport",
                producer_id=transport_id,
                attempt_id=attempt_id,
            )
        except Exception:
            self._fail(grant_id, "WORKSPACE_RECONCILIATION_FAILED")
            raise
        completed = datetime.now(UTC)
        reconciliation_id = f"wsr_{uuid4().hex}"
        with self.sessions.begin() as session:
            row = self._locked_grant(session, grant_id, WorkspaceGrantState.RECONCILING)
            session.add(WorkspaceReconciliationRecord(
                workspace_reconciliation_id=reconciliation_id,
                workspace_grant_id=grant_id,
                workspace_transport_id=transport_id,
                base_manifest_sha256=base_manifest,
                result_manifest_sha256=payload.result_snapshot.manifest_sha256,
                result_artifact_id=artifact.artifact_id,
                state=ReconciliationState.COMPLETED.value,
                summary=payload.summary,
                created_at=now,
                completed_at=completed,
            ))
            row.state = WorkspaceGrantState.COMPLETED.value
            row.updated_at = completed
            row.version += 1
            delegation = session.get(DelegationRecord, row.delegation_id)
            assert delegation is not None
            self._event(
                session,
                run_id=delegation.run_id,
                event_type="WORKSPACE_RECONCILED",
                grant_id=grant_id,
                correlation_id=row.delegation_id,
                metadata={
                    "artifact_id": artifact.artifact_id,
                    "result_manifest_sha256": payload.result_snapshot.manifest_sha256,
                },
            )
        return artifact.artifact_id

    def cleanup(self, grant_id: str) -> None:
        grant = self._contract(grant_id)
        provisioned, transport_id, _ = self._latest_transport(grant_id)
        transport = self.transports[grant.transport_kind]
        try:
            transport.cleanup(grant, provisioned)
        except Exception:
            with self.sessions.begin() as session:
                row = session.get(WorkspaceGrantRecord, grant_id)
                assert row is not None
                row.cleanup_state = CleanupState.FAILED.value
                row.updated_at = datetime.now(UTC)
                row.version += 1
            raise
        now = datetime.now(UTC)
        with self.sessions.begin() as session:
            row = session.get(WorkspaceGrantRecord, grant_id)
            transport_row = session.get(WorkspaceTransportRecord, transport_id)
            assert row is not None and transport_row is not None
            row.cleanup_state = CleanupState.CLEANED.value
            row.updated_at = now
            row.version += 1
            transport_row.state = "CLOSED"
            transport_row.closed_at = now

    def renew(self, grant_id: str, *, additional_seconds: int) -> datetime:
        if additional_seconds <= 0 or additional_seconds > 86_400:
            raise ValueError("workspace lease renewal must be between 1 and 86400 seconds")
        now = datetime.now(UTC)
        with self.sessions.begin() as session:
            row = self._locked_grant(session, grant_id, WorkspaceGrantState.ACTIVE)
            if row.renewal_policy != RenewalPolicy.EXPLICIT.value:
                raise WorkspaceDelegationError("workspace lease does not permit renewal")
            if row.lease_expires_at is None or row.lease_expires_at <= now:
                raise WorkspaceDelegationError("expired workspace lease cannot be renewed")
            row.lease_expires_at += timedelta(seconds=additional_seconds)
            row.updated_at = now
            row.version += 1
            return row.lease_expires_at

    def expire_due(self, *, now: datetime | None = None) -> tuple[str, ...]:
        reference = now or datetime.now(UTC)
        expired: list[str] = []
        with self.sessions.begin() as session:
            rows = session.scalars(select(WorkspaceGrantRecord).where(
                WorkspaceGrantRecord.state == WorkspaceGrantState.ACTIVE.value,
                WorkspaceGrantRecord.lease_expires_at <= reference,
            )).all()
            for row in rows:
                row.state = WorkspaceGrantState.EXPIRED.value
                row.updated_at = reference
                row.version += 1
                expired.append(row.workspace_grant_id)
        return tuple(expired)

    def recover_incomplete(self) -> tuple[str, ...]:
        """Fail closed and clean durable transport handles left by a crash window."""

        with self.sessions() as session:
            rows = session.scalars(select(WorkspaceGrantRecord).where(
                WorkspaceGrantRecord.state.in_((
                    WorkspaceGrantState.PROVISIONING.value,
                    WorkspaceGrantState.RECONCILING.value,
                    WorkspaceGrantState.RECOVERING.value,
                ))
            ).order_by(WorkspaceGrantRecord.created_at, WorkspaceGrantRecord.workspace_grant_id)).all()
            pending = tuple((row.workspace_grant_id, row.state) for row in rows)
        recovered: list[str] = []
        for grant_id, interrupted_state in pending:
            with self.sessions.begin() as session:
                row = session.get(WorkspaceGrantRecord, grant_id)
                if row is None or row.state != interrupted_state:
                    continue
                row.state = WorkspaceGrantState.RECOVERING.value
                row.updated_at = datetime.now(UTC)
                row.version += 1
            grant = self._contract(grant_id)
            transport = self.transports[grant.transport_kind]
            provisioned, transport_id, _ = self._latest_transport(grant_id)
            cleanup_state = CleanupState.CLEANED
            try:
                transport.cleanup(grant, provisioned)
            except Exception:
                cleanup_state = CleanupState.FAILED
            now = datetime.now(UTC)
            with self.sessions.begin() as session:
                row = session.get(WorkspaceGrantRecord, grant_id)
                transport_row = session.get(WorkspaceTransportRecord, transport_id)
                if (
                    row is None
                    or transport_row is None
                    or row.state != WorkspaceGrantState.RECOVERING.value
                ):
                    continue
                row.state = WorkspaceGrantState.FAILED.value
                row.cleanup_state = cleanup_state.value
                row.updated_at = now
                row.version += 1
                transport_row.state = "CLOSED" if cleanup_state is CleanupState.CLEANED else "CLEANUP_FAILED"
                transport_row.closed_at = now if cleanup_state is CleanupState.CLEANED else None
                delegation = session.get(DelegationRecord, row.delegation_id)
                assert delegation is not None
                self._event(
                    session,
                    run_id=delegation.run_id,
                    event_type="WORKSPACE_CRASH_RECOVERED",
                    grant_id=grant_id,
                    correlation_id=row.delegation_id,
                    metadata={"interrupted_state": interrupted_state, "cleanup_state": cleanup_state.value},
                )
            recovered.append(grant_id)
        return tuple(recovered)

    def _set_cleanup_state(self, grant_id: str, state: CleanupState) -> None:
        with self.sessions.begin() as session:
            row = session.get(WorkspaceGrantRecord, grant_id)
            if row is not None:
                row.cleanup_state = state.value
                row.updated_at = datetime.now(UTC)
                row.version += 1

    def _contract(self, grant_id: str) -> WorkspaceGrant:
        with self.sessions() as session:
            row = session.get(WorkspaceGrantRecord, grant_id)
            if row is None:
                raise WorkspaceDelegationError("workspace grant does not exist")
            return WorkspaceGrant(
                workspace_grant_id=row.workspace_grant_id,
                delegation_id=row.delegation_id,
                source_workspace_id=row.source_workspace_id,
                source_revision=row.source_revision,
                source_manifest_sha256=row.source_manifest_sha256,
                access_mode=WorkspaceAccessMode(row.access_mode),
                allowed_paths=tuple(row.allowed_paths),
                excluded_paths=tuple(row.excluded_paths),
                classification_ceiling=DataClassification(row.classification_ceiling),
                limits=WorkspaceLimits(
                    max_total_bytes=row.max_total_bytes,
                    max_file_count=row.max_file_count,
                    max_single_file_bytes=row.max_single_file_bytes,
                ),
                lease_seconds=row.lease_seconds,
                renewal_policy=RenewalPolicy(row.renewal_policy),
                transport_kind=WorkspaceTransportKind(row.transport_kind),
                reconciliation_mode=ReconciliationMode(row.reconciliation_mode),
            )

    def _active_transport(self, grant_id: str) -> tuple[ProvisionedWorkspace, str, str]:
        with self.sessions() as session:
            row = session.get(WorkspaceGrantRecord, grant_id)
            if row is None or row.state != WorkspaceGrantState.ACTIVE.value:
                raise WorkspaceDelegationError("workspace grant is not active")
        return self._latest_transport(grant_id)

    def _latest_transport(self, grant_id: str) -> tuple[ProvisionedWorkspace, str, str]:
        with self.sessions() as session:
            transport = session.scalar(select(WorkspaceTransportRecord).where(
                WorkspaceTransportRecord.workspace_grant_id == grant_id,
            ).order_by(WorkspaceTransportRecord.created_at.desc()).limit(1))
            snapshot = session.scalar(select(WorkspaceSnapshotRecord).where(
                WorkspaceSnapshotRecord.workspace_grant_id == grant_id
            ).order_by(WorkspaceSnapshotRecord.created_at.desc()).limit(1))
            if transport is None or snapshot is None:
                raise WorkspaceDelegationError("workspace transport or snapshot is missing")
            return (
                ProvisionedWorkspace(
                    transport_handle=dict(transport.transport_handle),
                    remote_workspace_handle=transport.remote_workspace_handle,
                ),
                transport.workspace_transport_id,
                snapshot.manifest_sha256,
            )

    @staticmethod
    def _locked_grant(
        session: Session, grant_id: str, expected_state: WorkspaceGrantState
    ) -> WorkspaceGrantRecord:
        row = session.get(WorkspaceGrantRecord, grant_id)
        if row is None or row.state != expected_state.value:
            raise WorkspaceDelegationError(
                f"workspace grant must be {expected_state.value}"
            )
        return row

    def _fail(self, grant_id: str, event_type: str) -> None:
        now = datetime.now(UTC)
        with self.sessions.begin() as session:
            row = session.get(WorkspaceGrantRecord, grant_id)
            if row is None:
                return
            row.state = WorkspaceGrantState.FAILED.value
            row.updated_at = now
            row.version += 1
            delegation = session.get(DelegationRecord, row.delegation_id)
            if delegation is not None:
                self._event(
                    session,
                    run_id=delegation.run_id,
                    event_type=event_type,
                    grant_id=grant_id,
                    correlation_id=row.delegation_id,
                    metadata={},
                )

    @staticmethod
    def _event(
        session: Session,
        *,
        run_id: str,
        event_type: str,
        grant_id: str,
        correlation_id: str,
        metadata: dict[str, object],
    ) -> None:
        session.add(AuditEventRecord(
            event_id=f"evt_{uuid4().hex}",
            event_type=event_type,
            run_id=run_id,
            entity_type="workspace_grant",
            entity_id=grant_id,
            actor_type="controller",
            actor_id="workspace-delegation",
            timestamp=datetime.now(UTC),
            correlation_id=correlation_id,
            causation_id=None,
            metadata_json=metadata,
        ))
