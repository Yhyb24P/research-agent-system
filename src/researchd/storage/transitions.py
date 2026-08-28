from collections.abc import Mapping
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, TypeVar, cast
from uuid import uuid4

from sqlalchemy import select, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from researchd.domain.enums import AttemptState, ResearchRunState, WorkOrderState
from researchd.domain.transitions import ATTEMPT_TRANSITIONS, RUN_TRANSITIONS, WORK_ORDER_TRANSITIONS, InvalidTransition, require_transition
from researchd.domain.criteria import acceptance_fingerprint, normalized_acceptance
from researchd.storage.models import AttemptRecord, AuditEventRecord, ResearchRunRecord, VerificationResultRecord, WorkOrderRecord

StateT = TypeVar("StateT", bound=StrEnum)


class EntityNotFound(LookupError):
    pass


class ConcurrencyConflict(RuntimeError):
    pass


class TransitionPreconditionFailed(RuntimeError):
    pass


class TransactionalTransitionService:
    def __init__(self, sessions: sessionmaker[Session]) -> None:
        self.sessions = sessions

    def transition_run(
        self, run_id: str, expected_version: int, target: ResearchRunState, *,
        event_type: str, actor_type: str, actor_id: str, correlation_id: str,
        causation_id: str | None = None, metadata: Mapping[str, Any] | None = None,
    ) -> int:
        return self._transition(ResearchRunRecord, "run_id", run_id, expected_version, target, RUN_TRANSITIONS, run_id, "research_run", event_type, actor_type, actor_id, correlation_id, causation_id, metadata)

    def transition_work_order(
        self, work_order_id: str, expected_version: int, target: WorkOrderState, *,
        event_type: str, actor_type: str, actor_id: str, correlation_id: str,
        causation_id: str | None = None, metadata: Mapping[str, Any] | None = None,
    ) -> int:
        with self.sessions() as lookup:
            run_id = lookup.query(WorkOrderRecord.run_id).filter(WorkOrderRecord.work_order_id == work_order_id).scalar()
        if run_id is None:
            raise EntityNotFound(work_order_id)
        return self._transition(WorkOrderRecord, "work_order_id", work_order_id, expected_version, target, WORK_ORDER_TRANSITIONS, run_id, "work_order", event_type, actor_type, actor_id, correlation_id, causation_id, metadata)

    def transition_attempt(
        self, attempt_id: str, expected_version: int, target: AttemptState, *,
        event_type: str, actor_type: str, actor_id: str, correlation_id: str,
        causation_id: str | None = None, metadata: Mapping[str, Any] | None = None,
    ) -> int:
        with self.sessions() as lookup:
            run_id = lookup.query(WorkOrderRecord.run_id).join(AttemptRecord, AttemptRecord.work_order_id == WorkOrderRecord.work_order_id).filter(AttemptRecord.attempt_id == attempt_id).scalar()
        if run_id is None:
            raise EntityNotFound(attempt_id)
        extra: dict[str, Any] = {}
        if target in (AttemptState.SUCCEEDED, AttemptState.FAILED, AttemptState.CANCELLED):
            extra["terminal_at"] = datetime.now(UTC)
        return self._transition(AttemptRecord, "attempt_id", attempt_id, expected_version, target, ATTEMPT_TRANSITIONS, run_id, "attempt", event_type, actor_type, actor_id, correlation_id, causation_id, metadata, extra)

    def _transition(
        self,
        model: type[ResearchRunRecord] | type[WorkOrderRecord] | type[AttemptRecord],
        id_name: str,
        entity_id: str,
        expected_version: int,
        target: StateT,
        table: dict[StateT, frozenset[StateT]],
        run_id: str,
        entity_type: str,
        event_type: str,
        actor_type: str,
        actor_id: str,
        correlation_id: str,
        causation_id: str | None,
        metadata: Mapping[str, Any] | None,
        extra_values: Mapping[str, Any] | None = None,
    ) -> int:
        with self.sessions.begin() as session:
            model_any = cast(Any, model)
            row = session.get(model, entity_id)
            if row is None:
                raise EntityNotFound(entity_id)
            if cast(Any, row).version != expected_version:
                raise ConcurrencyConflict(f"{entity_type} {entity_id} expected version {expected_version}")
            current = type(target)(cast(Any, row).state)
            require_transition(current, target, table)
            if model is WorkOrderRecord and target is WorkOrderState.REVIEW_READY:
                self._require_review_ready_verification(session, cast(WorkOrderRecord, row))
            now = datetime.now(UTC)
            values: dict[str, Any] = {"state": target.value, "version": expected_version + 1, "updated_at": now}
            values.update(extra_values or {})
            identifier = getattr(model_any, id_name)
            result = cast(CursorResult[Any], session.execute(
                update(model_any)
                .where(identifier == entity_id, model_any.version == expected_version, model_any.state == current.value)
                .values(**values)
            ))
            if result.rowcount != 1:
                raise ConcurrencyConflict(f"{entity_type} {entity_id} expected version {expected_version}")
            session.add(AuditEventRecord(
                event_id=f"evt_{uuid4().hex}", event_type=event_type, run_id=run_id,
                entity_type=entity_type, entity_id=entity_id, actor_type=actor_type,
                actor_id=actor_id, timestamp=now, correlation_id=correlation_id,
                causation_id=causation_id, metadata_json=dict(metadata or {}),
            ))
            try:
                session.flush()
            except IntegrityError:
                raise
            return expected_version + 1

    @staticmethod
    def _require_review_ready_verification(session: Session, order: WorkOrderRecord) -> None:
        latest_attempt = session.scalar(
            select(AttemptRecord)
            .where(AttemptRecord.work_order_id == order.work_order_id)
            .order_by(AttemptRecord.created_at.desc(), AttemptRecord.attempt_id.desc())
            .limit(1)
        )
        if latest_attempt is None:
            raise TransitionPreconditionFailed("REVIEW_READY requires an Attempt")
        result = session.scalar(
            select(VerificationResultRecord)
            .where(
                VerificationResultRecord.work_order_id == order.work_order_id,
                VerificationResultRecord.attempt_id == latest_attempt.attempt_id,
            )
            .order_by(VerificationResultRecord.created_at.desc(), VerificationResultRecord.verification_id.desc())
            .limit(1)
        )
        acceptance = order.contract.get("acceptance")
        structurally_valid = False
        if result is not None and acceptance is not None:
            expected = normalized_acceptance(acceptance)
            expected_by_id = {item["criterion_id"]: item for item in expected}
            actual_by_id = {
                item.get("criterion_id"): item
                for item in result.criteria_json
                if isinstance(item, dict) and isinstance(item.get("criterion_id"), str)
            }
            structurally_valid = (
                len(expected_by_id) == len(expected)
                and len(actual_by_id) == len(result.criteria_json)
                and set(actual_by_id) == set(expected_by_id)
                and all(
                    actual_by_id[identifier].get("severity") == criterion.get("severity", "hard")
                    and (
                        criterion.get("severity", "hard") != "hard"
                        or actual_by_id[identifier].get("result") == "pass"
                    )
                    for identifier, criterion in expected_by_id.items()
                )
            )
        if (
            result is None
            or not result.valid
            or result.overall != "pass"
            or acceptance is None
            or result.acceptance_sha256 != acceptance_fingerprint(acceptance)
            or not structurally_valid
        ):
            raise TransitionPreconditionFailed("REVIEW_READY requires a valid hard-pass result for the latest Attempt and frozen acceptance contract")
