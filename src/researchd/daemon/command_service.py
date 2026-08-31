"""Durable idempotency boundary around every daemon command dispatcher."""

import hashlib
import inspect
import json
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from uuid import uuid4

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from researchd.daemon.contracts import DaemonCommandResult
from researchd.domain.base import DomainModel
from researchd.storage.models import AuditEventRecord, DaemonCommandRecord


class DaemonCommandConflict(RuntimeError):
    pass


class DurableDaemonCommandService:
    """Reserve identity before dispatch and replay only known final results."""

    def __init__(
        self,
        sessions: sessionmaker[Session],
        dispatcher: Callable[[DomainModel], object | Awaitable[object]],
    ) -> None:
        self.sessions = sessions
        self.dispatcher = dispatcher

    def __call__(self, command: DomainModel) -> Awaitable[DaemonCommandResult]:
        return self.execute(command)

    async def execute(self, command: DomainModel) -> DaemonCommandResult:
        identity = self._identity(command)
        replay = self._reserve(command, *identity)
        if replay is not None:
            return replay
        command_id, command_type, _, _, _ = identity
        try:
            dispatched = self.dispatcher(command)
            if inspect.isawaitable(dispatched):
                dispatched = await dispatched
            if not isinstance(dispatched, DaemonCommandResult):
                raise TypeError("daemon dispatcher must return DaemonCommandResult")
        except Exception as error:
            result = DaemonCommandResult(
                command_id=command_id,
                command_type=command_type.removesuffix("Command"),
                status="REJECTED",
                reason_code=type(error).__name__[:128],
            )
            self._finish(command_id, result, status="REJECTED")
            return result
        self._finish(command_id, dispatched, status="COMPLETED")
        return dispatched

    def _reserve(
        self,
        command: DomainModel,
        command_id: str,
        command_type: str,
        command_version: int,
        actor_type: str,
        actor_id: str,
    ) -> DaemonCommandResult | None:
        digest = self._request_sha256(command)
        now = datetime.now(UTC)
        try:
            with self.sessions.begin() as session:
                existing = session.get(DaemonCommandRecord, command_id)
                if existing is not None:
                    return self._replay_existing(
                        existing, command_type, command_version, digest, actor_type, actor_id,
                    )
                session.add(DaemonCommandRecord(
                    command_id=command_id,
                    command_type=command_type,
                    command_version=command_version,
                    request_sha256=digest,
                    actor_type=actor_type,
                    actor_id=actor_id,
                    status="ACCEPTED",
                    result_json=None,
                    reason_code=None,
                    created_at=now,
                    updated_at=now,
                ))
                session.add(self._event(
                    command_id,
                    command_type,
                    actor_type,
                    actor_id,
                    "DAEMON_COMMAND_ACCEPTED",
                    now,
                ))
        except IntegrityError:
            with self.sessions() as session:
                existing = session.get(DaemonCommandRecord, command_id)
                if existing is None:
                    raise
                return self._replay_existing(
                    existing, command_type, command_version, digest, actor_type, actor_id,
                )
        return None

    @staticmethod
    def _replay_existing(
        existing: DaemonCommandRecord,
        command_type: str,
        command_version: int,
        digest: str,
        actor_type: str,
        actor_id: str,
    ) -> DaemonCommandResult:
        if (
            existing.command_type != command_type
            or existing.command_version != command_version
            or existing.request_sha256 != digest
            or existing.actor_type != actor_type
            or existing.actor_id != actor_id
        ):
            raise DaemonCommandConflict("command identity was reused with a different request")
        if existing.status in {"COMPLETED", "REJECTED"}:
            if existing.result_json is None:
                raise RuntimeError("final daemon command receipt has no result")
            return DaemonCommandResult.model_validate(existing.result_json)
        return DaemonCommandResult(
            command_id=existing.command_id,
            command_type=command_type.removesuffix("Command"),
            status="ACCEPTED",
            reason_code="COMMAND_OUTCOME_UNKNOWN",
        )

    def _finish(
        self,
        command_id: str,
        result: DaemonCommandResult,
        *,
        status: str,
    ) -> None:
        now = datetime.now(UTC)
        with self.sessions.begin() as session:
            record = session.get(DaemonCommandRecord, command_id)
            if record is None or record.status != "ACCEPTED":
                raise DaemonCommandConflict("daemon command receipt is not pending")
            record.status = status
            record.result_json = result.model_dump(mode="json")
            record.reason_code = result.reason_code
            record.updated_at = now
            session.add(self._event(
                command_id,
                record.command_type,
                "SYSTEM",
                "researchd-command-service",
                f"DAEMON_COMMAND_{status}",
                now,
            ))

    @staticmethod
    def _identity(command: DomainModel) -> tuple[str, str, int, str, str]:
        command_id = getattr(command, "command_id", None)
        command_version = getattr(command, "command_version", None)
        actor_type = getattr(command, "actor_type", None)
        actor_id = getattr(command, "actor_id", None)
        if (
            not isinstance(command_id, str)
            or not isinstance(command_version, int)
            or actor_type not in {"HUMAN", "SYSTEM"}
            or not isinstance(actor_id, str)
        ):
            raise TypeError("daemon command lacks required boundary identity")
        return command_id, type(command).__name__, command_version, actor_type, actor_id

    @staticmethod
    def _request_sha256(command: DomainModel) -> str:
        payload = json.dumps(
            command.model_dump(mode="json"),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode()
        return hashlib.sha256(payload).hexdigest()

    @staticmethod
    def _event(
        command_id: str,
        command_type: str,
        actor_type: str,
        actor_id: str,
        event_type: str,
        now: datetime,
    ) -> AuditEventRecord:
        return AuditEventRecord(
            event_id=f"evt_{uuid4().hex}",
            event_type=event_type,
            run_id=None,
            entity_type="daemon_command",
            entity_id=command_id,
            actor_type=actor_type,
            actor_id=actor_id,
            timestamp=now,
            correlation_id=command_id,
            causation_id=None,
            metadata_json={"command_type": command_type},
        )


__all__ = ["DaemonCommandConflict", "DurableDaemonCommandService"]
