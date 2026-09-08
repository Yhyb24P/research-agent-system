"""Daemon-owned wake driver for the existing trusted orchestrator.

The driver has no workflow authority: durable controller rows remain the source
of truth and each advancement delegates to ``ResearchOrchestrator.advance``.
Its in-memory wake set is only a latency optimization; startup reconstructs
work from durable runnable Run state.
"""

import asyncio
import logging
import threading
from typing import Protocol

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from researchd.domain.enums import ResearchRunState
from researchd.storage.models import ResearchRunRecord


_LOGGER = logging.getLogger(__name__)
_RUNNABLE_STATES = frozenset({
    ResearchRunState.NEW.value,
    ResearchRunState.PLANNING.value,
    ResearchRunState.ACTIVE.value,
    ResearchRunState.REVIEWING.value,
})


class OrchestrationTarget(Protocol):
    """Public controller surface used by the driver."""

    async def advance(self, run_id: str) -> bool: ...


class OrchestrationDriver:
    """Bounded daemon service that wakes durable runnable ResearchRuns.

    Workers may advance different Runs concurrently while per-Run ownership
    guarantees at most one active loop for each Run. No runnable queue is durable:
    a daemon restart scans ``research_runs`` again after the recovery barrier.
    """

    def __init__(
        self,
        orchestrator: OrchestrationTarget,
        sessions: sessionmaker[Session],
        *,
        max_steps_per_wake: int = 100,
        max_workers: int = 4,
    ) -> None:
        if max_steps_per_wake < 1:
            raise ValueError("max_steps_per_wake must be positive")
        if max_workers < 1 or max_workers > 32:
            raise ValueError("max_workers must be between 1 and 32")
        self.orchestrator = orchestrator
        self.sessions = sessions
        self.max_steps_per_wake = max_steps_per_wake
        self.max_workers = max_workers
        self._condition = threading.Condition()
        self._pending: set[str] = set()
        self._active: set[str] = set()
        self._rewake: set[str] = set()
        self._stopped = True
        self._threads: list[threading.Thread] = []
        self._last_error: str | None = None
        self._last_error_run_id: str | None = None

    def start(self) -> None:
        """Start after the daemon startup barrier and discover durable work."""
        with self._condition:
            if any(thread.is_alive() for thread in self._threads):
                return
            self._stopped = False
            self._threads = [threading.Thread(
                target=self._run,
                name=f"researchd-orchestration-driver-{index + 1}",
                daemon=True,
            ) for index in range(self.max_workers)]
            for thread in self._threads:
                thread.start()
        self.discover_runnable()

    def stop(self) -> None:
        with self._condition:
            self._stopped = True
            self._pending.clear()
            self._rewake.clear()
            self._condition.notify_all()
            threads = tuple(self._threads)
        for thread in threads:
            if thread is not threading.current_thread():
                thread.join(timeout=5)

    def wake(self, run_id: str) -> None:
        """Request a future advancement after a durable controller write."""
        with self._condition:
            if self._stopped:
                return
            if run_id in self._active:
                self._rewake.add(run_id)
                return
            self._pending.add(run_id)
            self._condition.notify()

    def wake_if_runnable(self, run_id: str) -> bool:
        """Wake only after the committed Run state is authoritatively runnable."""
        with self.sessions() as session:
            state = session.scalar(select(ResearchRunRecord.state).where(
                ResearchRunRecord.run_id == run_id,
            ))
        if state not in _RUNNABLE_STATES:
            return False
        self.wake(run_id)
        return True

    def discover_runnable(self) -> None:
        """Rebuild pending work from durable controller state after restart."""
        try:
            with self.sessions() as session:
                run_ids = session.scalars(
                    select(ResearchRunRecord.run_id).where(
                        ResearchRunRecord.state.in_(_RUNNABLE_STATES)
                    )
                ).all()
        except Exception as error:
            self._record_error(None, error)
            return
        for run_id in run_ids:
            self.wake(run_id)

    def health(self) -> dict[str, object]:
        with self._condition:
            running_workers = sum(thread.is_alive() for thread in self._threads)
            return {
                "running": bool(running_workers and not self._stopped),
                "worker_count": running_workers,
                "pending_run_count": len(self._pending),
                "active_run_count": len(self._active),
                "last_error": self._last_error,
                "last_error_run_id": self._last_error_run_id,
            }

    def _run(self) -> None:
        while True:
            with self._condition:
                while not self._stopped and not self._pending:
                    self._condition.wait()
                if self._stopped:
                    return
                run_id = self._pending.pop()
                self._active.add(run_id)
            try:
                self._drive(run_id)
            finally:
                with self._condition:
                    self._active.discard(run_id)
                    if run_id in self._rewake and not self._stopped:
                        self._rewake.discard(run_id)
                        self._pending.add(run_id)
                        self._condition.notify()

    def _drive(self, run_id: str) -> None:
        try:
            for _ in range(self.max_steps_per_wake):
                progressed: bool = asyncio.run(self.orchestrator.advance(run_id))
                if not progressed:
                    return
        except Exception as error:
            self._record_error(run_id, error)
            return
        self._record_error(
            run_id,
            RuntimeError("orchestration driver step budget exhausted"),
        )

    def _record_error(self, run_id: str | None, error: Exception) -> None:
        message = f"{type(error).__name__}: {error}"
        with self._condition:
            self._last_error = message
            self._last_error_run_id = run_id
        _LOGGER.exception("orchestration driver failure for run %s", run_id, exc_info=error)


__all__ = ["OrchestrationDriver", "OrchestrationTarget"]
