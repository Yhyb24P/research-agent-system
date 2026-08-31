"""PH01: regression contracts for the daemon-owned orchestration driver.

The driver is a latency optimization over durable controller state: startup
re-scans runnable Run states, one worker serializes advancement, and failures
are recorded on the health projection instead of looping forever.
"""

import threading
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

from researchd.domain.enums import ResearchRunState
from researchd.orchestrator.driver import OrchestrationDriver, OrchestrationTarget
from researchd.storage.db import create_sqlite_engine, session_factory
from researchd.storage.models import Base, ResearchRunRecord, WorkspaceRecord
from sqlalchemy.orm import Session, sessionmaker

_RUNNABLE = (
    ResearchRunState.NEW,
    ResearchRunState.PLANNING,
    ResearchRunState.ACTIVE,
    ResearchRunState.REVIEWING,
)
_PARKED = (ResearchRunState.WAITING_HUMAN, ResearchRunState.FAILED)


class _RecordingOrchestrator:
    """Advances nothing; records calls and the peak concurrent advancement."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.done = threading.Event()
        self.calls: list[str] = []
        self.active = 0
        self.max_concurrent = 0

    async def advance(self, run_id: str) -> bool:
        with self.lock:
            self.calls.append(run_id)
            self.active += 1
            self.max_concurrent = max(self.max_concurrent, self.active)
            settled = len(self.calls)
        try:
            return False
        finally:
            with self.lock:
                self.active -= 1
                if settled == 4:
                    self.done.set()


class _ThrowingOrchestrator:
    def __init__(self) -> None:
        self.calls = 0
        self.failed = threading.Event()

    async def advance(self, run_id: str) -> bool:
        self.calls += 1
        self.failed.set()
        raise ValueError("injected driver failure")


class _AlwaysProgressingOrchestrator:
    def __init__(self, steps: int) -> None:
        self.limit = steps
        self.calls = 0
        self.settled = threading.Event()

    async def advance(self, run_id: str) -> bool:
        self.calls += 1
        if self.calls >= self.limit:
            self.settled.set()
        return True


def _seeded_sessions(tmp_path: Path, *, runnable: bool) -> sessionmaker[Session]:
    engine = create_sqlite_engine(tmp_path / "driver.db")
    Base.metadata.create_all(engine)
    sessions = session_factory(engine)
    now = datetime.now(UTC)
    # The workspace must commit in its own transaction: without ORM
    # relationships the flush order is table-name ordered and the FK-enabled
    # SQLite engine rejects child-first inserts.
    with sessions.begin() as session:
        session.add(WorkspaceRecord(
            workspace_id="ws_driver", name="Driver", version=1,
            created_at=now, updated_at=now,
        ))
    states = (*_RUNNABLE, *_PARKED) if runnable else _PARKED
    with sessions.begin() as session:
        for state in states:
            session.add(ResearchRunRecord(
                run_id=f"run_{state.value.lower()}",
                workspace_id="ws_driver",
                objective=f"drive {state.value}",
                state=state.value,
                version=1,
                created_at=now,
                updated_at=now,
            ))
    return sessions


def test_driver_restart_recovery_scans_durable_runnable_states(tmp_path: Path) -> None:
    """A restarted driver wakes exactly the durable runnable runs, never parked ones."""
    sessions = _seeded_sessions(tmp_path, runnable=True)
    orchestrator = _RecordingOrchestrator()
    driver = OrchestrationDriver(cast(OrchestrationTarget, orchestrator), sessions)
    driver.start()
    try:
        assert orchestrator.done.wait(timeout=5)
        assert sorted(orchestrator.calls) == sorted(
            f"run_{state.value.lower()}" for state in _RUNNABLE
        )
        for state in _PARKED:
            assert f"run_{state.value.lower()}" not in orchestrator.calls
        health = driver.health()
        assert health["pending_run_count"] == 0
        assert health["last_error"] is None
    finally:
        driver.stop()


def test_driver_serializes_advancement_to_a_single_worker(tmp_path: Path) -> None:
    sessions = _seeded_sessions(tmp_path, runnable=True)
    orchestrator = _RecordingOrchestrator()
    driver = OrchestrationDriver(cast(OrchestrationTarget, orchestrator), sessions)
    driver.start()
    try:
        assert orchestrator.done.wait(timeout=5)
        assert orchestrator.max_concurrent == 1
    finally:
        driver.stop()


def test_duplicate_wake_while_advancing_is_serialized_not_parallel(
    tmp_path: Path,
) -> None:
    """A wake issued while a run is mid-advance coalesces into one follow-up.

    The database holds only parked runs, so startup discovery queues nothing
    and every advancement below is driven exclusively by explicit wakes.
    """
    sessions = _seeded_sessions(tmp_path, runnable=False)

    class _BlockingOrchestrator:
        def __init__(self) -> None:
            self.condition = threading.Condition()
            self.release = threading.Event()
            self.calls: list[str] = []

        async def advance(self, run_id: str) -> bool:
            with self.condition:
                self.calls.append(run_id)
                self.condition.notify_all()
            self.release.wait(timeout=5)
            return False

        def wait_calls(self, count: int, timeout: float = 5) -> bool:
            with self.condition:
                return self.condition.wait_for(
                    lambda: len(self.calls) >= count, timeout=timeout,
                )

    orchestrator = _BlockingOrchestrator()
    driver = OrchestrationDriver(cast(OrchestrationTarget, orchestrator), sessions)
    driver.start()
    try:
        driver.wake("run_new")
        assert orchestrator.wait_calls(1)
        # Coalesce: a duplicate wake while the run is mid-advance queues at most
        # one follow-up advancement.
        driver.wake("run_new")
        orchestrator.release.set()
        assert orchestrator.wait_calls(2)
        driver.wake("run_planning")
        orchestrator.release.set()
        assert orchestrator.wait_calls(3)
        driver.stop()
        assert orchestrator.calls == ["run_new", "run_new", "run_planning"]
    finally:
        orchestrator.release.set()
        driver.stop()


def _wait_last_error(driver: OrchestrationDriver, needle: str | None = None) -> dict[str, object]:
    """Poll the health projection until the error record lands (it is written
    after the failing advance returns to the worker loop)."""
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        health = driver.health()
        if health["last_error"] is not None and (needle is None or needle in str(health["last_error"])):
            return health
        time.sleep(0.01)
    raise AssertionError(f"driver health never recorded {needle!r}")


def test_driver_records_exceptions_with_bounded_retry(tmp_path: Path) -> None:
    sessions = _seeded_sessions(tmp_path, runnable=False)
    throwing = _ThrowingOrchestrator()
    driver = OrchestrationDriver(cast(OrchestrationTarget, throwing), sessions)
    driver.start()
    try:
        driver.wake("run_new")
        assert throwing.failed.wait(timeout=5)
        health = _wait_last_error(driver, "ValueError")
        assert health["last_error_run_id"] == "run_new"
        assert throwing.calls == 1
    finally:
        driver.stop()

    progressing = _AlwaysProgressingOrchestrator(steps=3)
    budgeted = OrchestrationDriver(
        cast(OrchestrationTarget, progressing), sessions, max_steps_per_wake=3,
    )
    budgeted.start()
    try:
        budgeted.wake("run_active")
        assert progressing.settled.wait(timeout=5)
        assert progressing.calls == 3
        health = _wait_last_error(budgeted, "step budget exhausted")
        assert health["last_error_run_id"] == "run_active"
    finally:
        budgeted.stop()


def test_wake_after_stop_is_a_noop(tmp_path: Path) -> None:
    sessions = _seeded_sessions(tmp_path, runnable=False)
    orchestrator = _RecordingOrchestrator()
    driver = OrchestrationDriver(cast(OrchestrationTarget, orchestrator), sessions)
    driver.start()
    driver.stop()
    driver.wake("run_new")
    assert driver.health()["pending_run_count"] == 0
    assert orchestrator.calls == []
