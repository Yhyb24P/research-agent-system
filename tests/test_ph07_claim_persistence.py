"""PH07 re-verification (6066aba): executor claims persist with execution results.

Covers the F-NEW-1 closure in 6066aba "fix: persist executor claims with
execution results":

- ``reported_claims`` persist atomically with the execution result for
  success, failure, and empty-claim outcomes;
- re-reading or retrying a stored result never writes duplicate claims;
- a claim-write failure rolls the whole transaction back, so the
  execution result is never persisted on its own.

Post-hoc test: no source changes, no commits.
"""

from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from researchd.collaboration.delegation import DelegationService
from researchd.collaboration.gateway import CollaborationGateway
from researchd.executor.contracts import ExecutorResult
from researchd.orchestrator.engine import OrchestrationError, ResearchOrchestrator
from researchd.policy.engine import DeterministicPolicyEngine, RecordingPolicyEngine
from researchd.storage.db import create_sqlite_engine, session_factory
from researchd.storage.models import (
    AttemptRecord,
    ClaimRecord,
    ExecutorDispatchRecord,
    ResearchRunRecord,
    WorkOrderRecord,
    WorkspaceRecord,
)
from researchd.verifier.engine import ClaimRecorder
from tests.integration.test_storage import assert_migration_matches_models, migrate

WORKSPACE_ID = "ws_ph07_claims"
RUN_ID = "run_ph07_claims"
ORDER_ID = "wo_ph07_claims"
ATTEMPT_ID = "att_ph07_claims"


def _result(status: str = "execution_complete", claims: tuple[str, ...] = ("fixed the NaN",)) -> ExecutorResult:
    return ExecutorResult(
        attempt_id=ATTEMPT_ID,
        status=status,  # type: ignore[arg-type]
        capability_results=(),
        reported_claims=claims,
        errors=(),
    )


class ClaimEnv:
    def __init__(self, tmp_path: Path) -> None:
        db_path = tmp_path / "claims.db"
        migrate(db_path)
        assert_migration_matches_models(db_path)
        self.sessions: sessionmaker[Session] = session_factory(create_sqlite_engine(db_path))
        now = datetime.now(UTC)
        with self.sessions.begin() as session:
            # Explicit flushes: without ORM relationships the unit-of-work
            # flush order is not guaranteed to be parent-first.
            session.add(WorkspaceRecord(
                workspace_id=WORKSPACE_ID, name="PH07 claims",
                created_at=now, updated_at=now, version=1,
            ))
            session.flush()
            session.add(ResearchRunRecord(
                run_id=RUN_ID, workspace_id=WORKSPACE_ID,
                objective="claim persistence", state="ACTIVE",
                created_at=now, updated_at=now, version=1,
            ))
            session.flush()
            session.add(WorkOrderRecord(
                work_order_id=ORDER_ID, run_id=RUN_ID, objective="execute",
                state="DISPATCHED", idempotency_key=f"{ORDER_ID}-orchestration",
                contract={}, created_at=now, updated_at=now, version=1,
            ))
            session.flush()
            session.add(AttemptRecord(
                attempt_id=ATTEMPT_ID, work_order_id=ORDER_ID, state="RUNNING",
                created_at=now, updated_at=now, version=1,
            ))
        self.orchestrator = ResearchOrchestrator(
            self.sessions,
            collaboration=CollaborationGateway(
                None, None,
                delegations=DelegationService(self.sessions),
            ),
            policy=RecordingPolicyEngine(DeterministicPolicyEngine(), self.sessions),
        )

    def dispatch_row(self) -> ExecutorDispatchRecord | None:
        with self.sessions() as session:
            row = session.get(ExecutorDispatchRecord, ATTEMPT_ID)
            if row is not None:
                session.expunge(row)
            return row

    def claim_statements(self) -> list[str]:
        with self.sessions() as session:
            rows = session.scalars(select(ClaimRecord).where(
                ClaimRecord.attempt_id == ATTEMPT_ID,
            )).all()
            for row in rows:
                session.expunge(row)
            return [row.statement for row in rows]


@pytest.fixture
def env(tmp_path: Path) -> ClaimEnv:
    return ClaimEnv(tmp_path)


# ----------------------------------------------------------------------
# 1. Persistence semantics across outcomes.
# ----------------------------------------------------------------------

def test_success_result_persists_claims(env: ClaimEnv) -> None:
    env.orchestrator._store_execution_result(ATTEMPT_ID, _result())
    row = env.dispatch_row()
    assert row is not None
    assert row.status == "COMPLETED"
    assert row.result_json is not None
    assert env.claim_statements() == ["fixed the NaN"]


def test_failed_result_still_persists_claims(env: ClaimEnv) -> None:
    result = _result(status="failed", claims=("partial fix applied",))
    env.orchestrator._store_execution_result(ATTEMPT_ID, result)
    row = env.dispatch_row()
    assert row is not None
    assert row.status == "COMPLETED"
    assert row.result_json is not None
    assert env.claim_statements() == ["partial fix applied"]


def test_empty_claims_write_no_claim_rows(env: ClaimEnv) -> None:
    env.orchestrator._store_execution_result(ATTEMPT_ID, _result(claims=()))
    row = env.dispatch_row()
    assert row is not None
    assert row.result_json is not None
    assert env.claim_statements() == []


# ----------------------------------------------------------------------
# 2. Duplicate reads / retries never duplicate claims.
# ----------------------------------------------------------------------

def test_repeat_store_does_not_duplicate_claims(env: ClaimEnv) -> None:
    env.orchestrator._store_execution_result(ATTEMPT_ID, _result())
    # A retry that re-stores the same result finds result_json already set
    # and must not mint a second set of claims.
    env.orchestrator._store_execution_result(ATTEMPT_ID, _result())
    assert env.claim_statements() == ["fixed the NaN"]

    # A retry carrying different claims must not overwrite or append.
    env.orchestrator._store_execution_result(ATTEMPT_ID, _result(claims=("late claim",)))
    assert env.claim_statements() == ["fixed the NaN"]

    # The resume path re-reads the stored result instead of re-executing.
    stored = env.orchestrator._stored_execution_result(ATTEMPT_ID)
    assert stored is not None
    assert stored.reported_claims == ("fixed the NaN",)


# ----------------------------------------------------------------------
# 3. Claim-write failures must not leave the result persisted alone.
# ----------------------------------------------------------------------

def test_claim_writer_failure_rolls_back_result(env: ClaimEnv, monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(*args: object, **kwargs: object) -> list[str]:
        raise RuntimeError("claim store exploded")

    monkeypatch.setattr(ClaimRecorder, "record_executor_claims_in_session", staticmethod(boom))
    with pytest.raises(RuntimeError, match="claim store exploded"):
        env.orchestrator._store_execution_result(ATTEMPT_ID, _result())
    assert env.dispatch_row() is None
    assert env.claim_statements() == []


def test_production_sequence_persists_claims(env: ClaimEnv) -> None:
    # Production ordering (heterogeneous.py managed EXECUTE turn): the
    # LocalExecutorWorker reserves the dispatch record (RUNNING, None) and
    # completes it with result_json before the orchestrator stores the
    # result. Claims must still be persisted exactly once.
    now = datetime.now(UTC)
    with env.sessions.begin() as session:
        session.add(ExecutorDispatchRecord(
            attempt_id=ATTEMPT_ID, status="COMPLETED",
            result_json=_result().model_dump(mode="json"),
            created_at=now, updated_at=now,
        ))
    env.orchestrator._store_execution_result(ATTEMPT_ID, _result())
    assert env.claim_statements() == ["fixed the NaN"]


def test_retry_with_different_claims_keeps_durable_claims(env: ClaimEnv) -> None:
    # The durable dispatch result is the authority: a retry carrying
    # different claims must neither append nor replace the persisted set.
    now = datetime.now(UTC)
    with env.sessions.begin() as session:
        session.add(ExecutorDispatchRecord(
            attempt_id=ATTEMPT_ID, status="COMPLETED",
            result_json=_result(claims=("durable claim",)).model_dump(mode="json"),
            created_at=now, updated_at=now,
        ))
    env.orchestrator._store_execution_result(ATTEMPT_ID, _result(claims=("durable claim",)))
    assert env.claim_statements() == ["durable claim"]
    env.orchestrator._store_execution_result(ATTEMPT_ID, _result(claims=("retry claim",)))
    assert env.claim_statements() == ["durable claim"]


def test_mismatched_claims_fail_closed(env: ClaimEnv) -> None:
    # Claim rows disagreeing with the durable dispatch result must fail
    # closed: no silent rewrite of the claim ledger.
    now = datetime.now(UTC)
    with env.sessions.begin() as session:
        session.add(ExecutorDispatchRecord(
            attempt_id=ATTEMPT_ID, status="COMPLETED",
            result_json=_result(claims=("durable claim",)).model_dump(mode="json"),
            created_at=now, updated_at=now,
        ))
        session.flush()
        session.add(ClaimRecord(
            claim_id="claim_foreign", attempt_id=ATTEMPT_ID, statement="foreign claim",
            supporting_refs=[], producer_type="executor", producer_id="local-executor",
            created_at=now,
        ))
    with pytest.raises(OrchestrationError, match="do not match"):
        env.orchestrator._store_execution_result(ATTEMPT_ID, _result(claims=("durable claim",)))
    assert env.claim_statements() == ["foreign claim"]


def test_missing_attempt_rolls_back_result(env: ClaimEnv) -> None:
    with env.sessions.begin() as session:
        session.delete(session.get(AttemptRecord, ATTEMPT_ID))
    # Either the claim writer's LookupError or the dispatch FK (surfaced by
    # autoflush) must abort the transaction: the result must not persist.
    with pytest.raises((LookupError, IntegrityError)):
        env.orchestrator._store_execution_result(ATTEMPT_ID, _result())
    assert env.dispatch_row() is None
    assert env.claim_statements() == []
