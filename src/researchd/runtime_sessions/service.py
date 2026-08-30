"""Transactional RuntimeSession intent, observation, and audit persistence."""

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from researchd.collaboration.registry import AgentRegistryService
from researchd.domain.enums import AgentAdapterKind
from researchd.domain.ids import AgentRuntimeId, RuntimeSessionId
from researchd.runtime_sessions.contracts import (
    CommandStatus,
    CommandType,
    LaunchMode,
    ReattachState,
    RuntimeSession,
    RuntimeSessionAttachCommand,
    RuntimeSessionStartCommand,
    RuntimeSessionStopCommand,
    SupervisorState,
)
from researchd.storage.models import (
    AuditEventRecord,
    RuntimeSessionCommandRecord,
    RuntimeSessionRecord,
)


class RuntimeSessionConflict(RuntimeError):
    """Raised for a duplicate active runtime or reused command identity."""


class RuntimeSessionConcurrencyConflict(RuntimeError):
    """Raised when a command targets a stale RuntimeSession version."""


class RuntimeSessionTransitionError(RuntimeError):
    """Raised when a requested supervisor state transition is invalid."""


@dataclass(frozen=True)
class SessionIntent:
    session: RuntimeSession
    created: bool


_ACTIVE_STATES = (
    SupervisorState.STARTING.value,
    SupervisorState.HEALTHY.value,
    SupervisorState.DEGRADED.value,
    SupervisorState.STOPPING.value,
    SupervisorState.RECONCILIATION_REQUIRED.value,
)

_TRANSITIONS: dict[SupervisorState, frozenset[SupervisorState]] = {
    SupervisorState.STARTING: frozenset({
        SupervisorState.HEALTHY,
        SupervisorState.DEGRADED,
        SupervisorState.STOPPING,
        SupervisorState.LOST,
        SupervisorState.RECONCILIATION_REQUIRED,
    }),
    SupervisorState.HEALTHY: frozenset({
        SupervisorState.DEGRADED,
        SupervisorState.STOPPING,
        SupervisorState.LOST,
        SupervisorState.RECONCILIATION_REQUIRED,
    }),
    SupervisorState.DEGRADED: frozenset({
        SupervisorState.HEALTHY,
        SupervisorState.STOPPING,
        SupervisorState.LOST,
        SupervisorState.RECONCILIATION_REQUIRED,
    }),
    SupervisorState.STOPPING: frozenset({
        SupervisorState.STOPPED,
        SupervisorState.LOST,
        SupervisorState.RECONCILIATION_REQUIRED,
    }),
    SupervisorState.RECONCILIATION_REQUIRED: frozenset({
        SupervisorState.HEALTHY,
        SupervisorState.DEGRADED,
        SupervisorState.STOPPING,
        SupervisorState.STOPPED,
        SupervisorState.LOST,
    }),
    SupervisorState.STOPPED: frozenset(),
    SupervisorState.LOST: frozenset(),
}


class RuntimeSessionService:
    """Own durable runtime-instance state without becoming an Agent registry."""

    def __init__(
        self,
        sessions: sessionmaker[Session],
        registry: AgentRegistryService,
    ) -> None:
        self.sessions = sessions
        self.registry = registry

    def begin_start(self, command: RuntimeSessionStartCommand) -> SessionIntent:
        replay = self._replay(command, CommandType.START)
        if replay is not None:
            return replay
        runtime = self.registry.require_enabled_runtime(str(command.runtime_id))
        if runtime.adapter_kind is not AgentAdapterKind.PROCESS:
            raise ValueError("PROCESS launch requires a PROCESS AgentRuntime")
        return self._begin(
            command=command,
            command_type=CommandType.START,
            launch_mode=LaunchMode.PROCESS,
            launch_spec=command.launch_spec.model_dump(mode="json"),
        )

    def begin_attach(self, command: RuntimeSessionAttachCommand) -> SessionIntent:
        replay = self._replay(command, CommandType.ATTACH)
        if replay is not None:
            return replay
        runtime = self.registry.require_enabled_runtime(str(command.runtime_id))
        if runtime.adapter_kind is not AgentAdapterKind.HTTP:
            raise ValueError("REMOTE_HTTP attach requires an HTTP AgentRuntime")
        if runtime.endpoint_ref != command.launch_spec.endpoint:
            raise ValueError("attach endpoint must match the registered AgentRuntime")
        return self._begin(
            command=command,
            command_type=CommandType.ATTACH,
            launch_mode=LaunchMode.REMOTE_HTTP,
            launch_spec=command.launch_spec.model_dump(mode="json"),
        )

    def _begin(
        self,
        *,
        command: RuntimeSessionStartCommand | RuntimeSessionAttachCommand,
        command_type: CommandType,
        launch_mode: LaunchMode,
        launch_spec: dict[str, Any],
    ) -> SessionIntent:
        request_sha256 = command.request_sha256()
        now = datetime.now(UTC)
        try:
            with self.sessions.begin() as session:
                replay = session.get(RuntimeSessionCommandRecord, command.command_id)
                if replay is not None:
                    return SessionIntent(
                        self._validate_replay(session, replay, request_sha256, command_type),
                        False,
                    )
                if session.get(RuntimeSessionRecord, str(command.runtime_session_id)) is not None:
                    raise RuntimeSessionConflict("runtime_session_id already exists")
                record = RuntimeSessionRecord(
                    runtime_session_id=str(command.runtime_session_id),
                    runtime_id=str(command.runtime_id),
                    launch_mode=launch_mode.value,
                    supervisor_state=SupervisorState.STARTING.value,
                    launch_spec_json=launch_spec,
                    launch_profile_sha256=command.launch_profile_sha256,
                    external_identity_json=None,
                    started_at=None,
                    last_health_at=None,
                    stopped_at=None,
                    exit_reason=None,
                    reattach_state=ReattachState.PENDING.value,
                    version=1,
                    created_at=now,
                    updated_at=now,
                )
                # Flush the parent first. SQLAlchemy has no ORM relationship
                # between the aggregate and its receipt, while SQLite must
                # enforce the command receipt foreign key in this transaction.
                session.add(record)
                session.flush()
                receipt = RuntimeSessionCommandRecord(
                    command_id=command.command_id,
                    runtime_session_id=str(command.runtime_session_id),
                    command_type=command_type.value,
                    request_sha256=request_sha256,
                    actor_type=command.actor_type,
                    actor_id=command.actor_id,
                    expected_version=None,
                    status=CommandStatus.ACCEPTED.value,
                    result_state=SupervisorState.STARTING.value,
                    failure_reason=None,
                    created_at=now,
                    updated_at=now,
                )
                session.add(receipt)
                session.add(self._event(
                    event_type=f"RUNTIME_SESSION_{command_type.value}_REQUESTED",
                    session_id=record.runtime_session_id,
                    actor_type=command.actor_type,
                    actor_id=command.actor_id,
                    correlation_id=command.command_id,
                    now=now,
                    metadata={"runtime_id": record.runtime_id, "launch_mode": launch_mode.value},
                ))
                session.flush()
                return SessionIntent(self._from_record(record), True)
        except IntegrityError as error:
            with self.sessions() as session:
                replay = session.get(RuntimeSessionCommandRecord, command.command_id)
                if replay is not None:
                    return SessionIntent(
                        self._validate_replay(
                            session,
                            replay,
                            request_sha256,
                            command_type,
                        ),
                        False,
                    )
            raise RuntimeSessionConflict(
                f"an active RuntimeSession already exists for {command.runtime_id}"
            ) from error

    def begin_stop(self, command: RuntimeSessionStopCommand) -> SessionIntent:
        request_sha256 = command.request_sha256()
        now = datetime.now(UTC)
        with self.sessions.begin() as session:
            replay = session.get(RuntimeSessionCommandRecord, command.command_id)
            if replay is not None:
                return SessionIntent(
                    self._validate_replay(session, replay, request_sha256, CommandType.STOP),
                    False,
                )
            record = session.get(RuntimeSessionRecord, str(command.runtime_session_id))
            if record is None or record.runtime_id != str(command.runtime_id):
                raise LookupError(str(command.runtime_session_id))
            current = SupervisorState(record.supervisor_state)
            self._require_transition(current, SupervisorState.STOPPING)
            if record.version != command.expected_version:
                raise RuntimeSessionConcurrencyConflict(
                    f"RuntimeSession expected version {command.expected_version}"
                )
            updated = session.execute(
                update(RuntimeSessionRecord)
                .where(
                    RuntimeSessionRecord.runtime_session_id == record.runtime_session_id,
                    RuntimeSessionRecord.version == command.expected_version,
                    RuntimeSessionRecord.supervisor_state == current.value,
                )
                .values(
                    supervisor_state=SupervisorState.STOPPING.value,
                    version=command.expected_version + 1,
                    updated_at=now,
                )
            )
            if int(getattr(updated, "rowcount", 0)) != 1:
                raise RuntimeSessionConcurrencyConflict(
                    f"RuntimeSession expected version {command.expected_version}"
                )
            receipt = RuntimeSessionCommandRecord(
                command_id=command.command_id,
                runtime_session_id=record.runtime_session_id,
                command_type=CommandType.STOP.value,
                request_sha256=request_sha256,
                actor_type=command.actor_type,
                actor_id=command.actor_id,
                expected_version=command.expected_version,
                status=CommandStatus.ACCEPTED.value,
                result_state=SupervisorState.STOPPING.value,
                failure_reason=None,
                created_at=now,
                updated_at=now,
            )
            session.add(receipt)
            session.add(self._event(
                event_type="RUNTIME_SESSION_STOP_REQUESTED",
                session_id=record.runtime_session_id,
                actor_type=command.actor_type,
                actor_id=command.actor_id,
                correlation_id=command.command_id,
                now=now,
                metadata={"runtime_id": record.runtime_id},
            ))
        return SessionIntent(self.get(str(command.runtime_session_id)), True)

    def complete_command(
        self,
        command_id: str,
        *,
        expected_version: int,
        target: SupervisorState,
        external_identity: dict[str, object] | None = None,
        exit_reason: str | None = None,
        reattach_state: ReattachState,
        command_status: CommandStatus = CommandStatus.COMPLETED,
        failure_reason: str | None = None,
    ) -> RuntimeSession:
        now = datetime.now(UTC)
        with self.sessions.begin() as session:
            receipt = session.get(RuntimeSessionCommandRecord, command_id)
            if receipt is None:
                raise LookupError(command_id)
            record = session.get(RuntimeSessionRecord, receipt.runtime_session_id)
            if record is None:
                raise LookupError(receipt.runtime_session_id)
            current = SupervisorState(record.supervisor_state)
            self._require_transition(current, target)
            if record.version != expected_version:
                raise RuntimeSessionConcurrencyConflict(
                    f"RuntimeSession expected version {expected_version}"
                )
            values: dict[str, object] = {
                "supervisor_state": target.value,
                "external_identity_json": external_identity,
                "exit_reason": exit_reason,
                "reattach_state": reattach_state.value,
                "version": expected_version + 1,
                "updated_at": now,
            }
            if target is SupervisorState.HEALTHY:
                values["started_at"] = record.started_at or now
                values["last_health_at"] = now
            if target in {SupervisorState.STOPPED, SupervisorState.LOST}:
                values["stopped_at"] = now
            updated = session.execute(
                update(RuntimeSessionRecord)
                .where(
                    RuntimeSessionRecord.runtime_session_id == record.runtime_session_id,
                    RuntimeSessionRecord.version == expected_version,
                    RuntimeSessionRecord.supervisor_state == current.value,
                )
                .values(**values)
            )
            if int(getattr(updated, "rowcount", 0)) != 1:
                raise RuntimeSessionConcurrencyConflict(
                    f"RuntimeSession expected version {expected_version}"
                )
            receipt.status = command_status.value
            receipt.result_state = target.value
            receipt.failure_reason = failure_reason
            receipt.updated_at = now
            session.add(self._event(
                event_type=f"RUNTIME_SESSION_{target.value}",
                session_id=record.runtime_session_id,
                actor_type="SYSTEM",
                actor_id="runtime-supervisor",
                correlation_id=command_id,
                now=now,
                metadata={"runtime_id": record.runtime_id},
            ))
        return self.get(receipt.runtime_session_id)

    def reconcile(
        self,
        runtime_session_id: str,
        *,
        expected_version: int,
        target: SupervisorState,
        external_identity: dict[str, object] | None,
        reattach_state: ReattachState,
        exit_reason: str | None = None,
    ) -> RuntimeSession:
        now = datetime.now(UTC)
        with self.sessions.begin() as session:
            record = session.get(RuntimeSessionRecord, runtime_session_id)
            if record is None:
                raise LookupError(runtime_session_id)
            current = SupervisorState(record.supervisor_state)
            if target is current and target is SupervisorState.HEALTHY:
                allowed = True
            else:
                allowed = target in _TRANSITIONS[current]
            if not allowed:
                raise RuntimeSessionTransitionError(f"invalid transition {current} -> {target}")
            if record.version != expected_version:
                raise RuntimeSessionConcurrencyConflict(
                    f"RuntimeSession expected version {expected_version}"
                )
            values: dict[str, object] = {
                "supervisor_state": target.value,
                "external_identity_json": external_identity,
                "reattach_state": reattach_state.value,
                "exit_reason": exit_reason,
                "version": expected_version + 1,
                "updated_at": now,
            }
            if target is SupervisorState.HEALTHY:
                values["last_health_at"] = now
                values["started_at"] = record.started_at or now
            if target in {SupervisorState.STOPPED, SupervisorState.LOST}:
                values["stopped_at"] = now
            result = session.execute(
                update(RuntimeSessionRecord)
                .where(
                    RuntimeSessionRecord.runtime_session_id == runtime_session_id,
                    RuntimeSessionRecord.version == expected_version,
                )
                .values(**values)
            )
            if int(getattr(result, "rowcount", 0)) != 1:
                raise RuntimeSessionConcurrencyConflict(
                    f"RuntimeSession expected version {expected_version}"
                )
            session.add(self._event(
                event_type=f"RUNTIME_SESSION_RECONCILED_{target.value}",
                session_id=runtime_session_id,
                actor_type="SYSTEM",
                actor_id="runtime-supervisor",
                correlation_id=runtime_session_id,
                now=now,
                metadata={"previous_state": current.value},
            ))
        return self.get(runtime_session_id)

    def get(self, runtime_session_id: str) -> RuntimeSession:
        with self.sessions() as session:
            record = session.get(RuntimeSessionRecord, runtime_session_id)
            if record is None:
                raise LookupError(runtime_session_id)
            return self._from_record(record)

    def active(self) -> tuple[RuntimeSession, ...]:
        with self.sessions() as session:
            records = session.scalars(
                select(RuntimeSessionRecord)
                .where(RuntimeSessionRecord.supervisor_state.in_(_ACTIVE_STATES))
                .order_by(RuntimeSessionRecord.created_at, RuntimeSessionRecord.runtime_session_id)
            ).all()
            return tuple(self._from_record(record) for record in records)

    def list(self) -> tuple[RuntimeSession, ...]:
        with self.sessions() as session:
            records = session.scalars(
                select(RuntimeSessionRecord).order_by(
                    RuntimeSessionRecord.created_at,
                    RuntimeSessionRecord.runtime_session_id,
                )
            ).all()
            return tuple(self._from_record(record) for record in records)

    def _validate_replay(
        self,
        session: Session,
        receipt: RuntimeSessionCommandRecord,
        request_sha256: str,
        command_type: CommandType,
    ) -> RuntimeSession:
        if receipt.request_sha256 != request_sha256 or receipt.command_type != command_type.value:
            raise RuntimeSessionConflict("command_id was reused with a different request")
        record = session.get(RuntimeSessionRecord, receipt.runtime_session_id)
        if record is None:
            raise RuntimeSessionConflict("command receipt refers to a missing RuntimeSession")
        return self._from_record(record)

    def _replay(
        self,
        command: RuntimeSessionStartCommand | RuntimeSessionAttachCommand,
        command_type: CommandType,
    ) -> SessionIntent | None:
        with self.sessions() as session:
            receipt = session.get(RuntimeSessionCommandRecord, command.command_id)
            if receipt is None:
                return None
            return SessionIntent(
                self._validate_replay(
                    session,
                    receipt,
                    command.request_sha256(),
                    command_type,
                ),
                False,
            )

    @staticmethod
    def _require_transition(current: SupervisorState, target: SupervisorState) -> None:
        if target not in _TRANSITIONS[current]:
            raise RuntimeSessionTransitionError(f"invalid transition {current} -> {target}")

    @staticmethod
    def _event(
        *,
        event_type: str,
        session_id: str,
        actor_type: str,
        actor_id: str,
        correlation_id: str,
        now: datetime,
        metadata: dict[str, Any],
    ) -> AuditEventRecord:
        return AuditEventRecord(
            event_id=f"evt_{uuid4().hex}",
            event_type=event_type,
            run_id=None,
            entity_type="runtime_session",
            entity_id=session_id,
            actor_type=actor_type,
            actor_id=actor_id,
            timestamp=now,
            correlation_id=correlation_id,
            causation_id=None,
            metadata_json=metadata,
        )

    @staticmethod
    def _from_record(record: RuntimeSessionRecord) -> RuntimeSession:
        return RuntimeSession(
            runtime_session_id=RuntimeSessionId(record.runtime_session_id),
            runtime_id=AgentRuntimeId(record.runtime_id),
            launch_mode=LaunchMode(record.launch_mode),
            supervisor_state=SupervisorState(record.supervisor_state),
            launch_spec=dict(record.launch_spec_json),
            launch_profile_sha256=record.launch_profile_sha256,
            external_identity=(
                dict(record.external_identity_json)
                if record.external_identity_json is not None
                else None
            ),
            started_at=record.started_at,
            last_health_at=record.last_health_at,
            stopped_at=record.stopped_at,
            exit_reason=record.exit_reason,
            reattach_state=ReattachState(record.reattach_state),
            version=record.version,
            created_at=record.created_at,
            updated_at=record.updated_at,
        )
