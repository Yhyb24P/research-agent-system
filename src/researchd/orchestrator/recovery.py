"""Restart coordinator for the durable orchestration controller."""

from researchd.orchestrator.engine import ResearchOrchestrator, RunSnapshot


class RecoveryCoordinator:
    """Reconciles persisted execution before normal controller advancement."""

    def __init__(self, orchestrator: ResearchOrchestrator) -> None:
        self.orchestrator = orchestrator

    def recover_run(self, run_id: str) -> RunSnapshot:
        return self.orchestrator.recover(run_id)


__all__ = ["RecoveryCoordinator"]
