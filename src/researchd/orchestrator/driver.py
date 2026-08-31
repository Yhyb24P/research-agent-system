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
    """Single daemon service that wakes durable runnable ResearchRuns.

    One worker serializes advancement, which guarantees at most one active
    advancement loop for a run in this daemon.  No runnable queue is durable:
    a daemon restart scans ``research_runs`` again after the recovery barrier.
    """

    def __init__(
        self,
        orchestrator: OrchestrationTarget,
        sessions: sessionmaker[Session],
        *,
        max_steps_per_wake: int = 100,
    ) -> None:
        if max_steps_per_wake < 1:
            raise ValueError("max_steps_per_wake must be positive")
        self.orchestrator = orchestrator
        self.sessions = sessions
        self.max_steps_per_wake = max_steps_per_wake
        self._condition = threading.Condition()
        self._pending: set[str] = set()
        self._stopped = True
        self._thread: threading.Thread | None = None
        self._last_error: str | None = None
        self._last_error_run_id: str | None = None

    def start(self) -> None:
        """Start after the daemon startup barrier and discover durable work."""
        with self._condition:
            if self._thread is not None and self._thread.is_alive():
                return
            self._stopped = False
            self._thread = threading.Thread(
                target=self._run,
                name="researchd-orchestration-driver",
                daemon=True,
            )
            self._thread.start()
        self.discover_runnable()

    def stop(self) -> None:
        with self._condition:
            self._stopped = True
            self._pending.clear()
            self._condition.notify_all()
            thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=5)

    def wake(self, run_id: str) -> None:
        """Request a future advancement after a durable controller write."""
        with self._condition:
            if self._stopped:
                return
            self._pending.add(run_id)
            self._condition.notify()

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
            thread = self._thread
            return {
                "running": bool(thread is not None and thread.is_alive() and not self._stopped),
                "pending_run_count": len(self._pending),
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
            self._drive(run_id)

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
