"""Concrete ``VerificationDriver`` adapter over the trusted verifier domain.

The executor cannot self-verify: the outcome is computed by the
``VerifierEngine`` from hash-verified, attempt-scoped CAS artifacts and
trusted execution-step records. Executor-reported claims are recorded as
claims and never become verifier inputs, and a result that references
artifacts the store does not know about is refused.
"""

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker
from pydantic import TypeAdapter

from researchd.artifacts.store import ContentAddressedArtifactStore
from researchd.domain.criteria import (
    AcceptanceCriterion,
    MetricCriterion,
    ReproCriterion,
)
from researchd.domain.verification import VerificationResult
from researchd.executor.contracts import ExecutorResult
from researchd.storage.models import ArtifactRecord, AttemptRecord, WorkOrderRecord
from researchd.verifier.contracts import VerificationInputs
from researchd.verifier.engine import VerifierEngine
from researchd.verifier.producers import TrustedObservationProducers, VerificationRefused

_CRITERIA_ADAPTER = TypeAdapter(tuple[AcceptanceCriterion, ...])


class LocalVerificationDriver:
    """Bridge the orchestrator's ``VerificationDriver`` protocol to the engine."""

    def __init__(
        self,
        sessions: sessionmaker[Session],
        store: ContentAddressedArtifactStore,
    ) -> None:
        self.sessions = sessions
        self.engine = VerifierEngine(sessions, TrustedObservationProducers(store))

    def verify(
        self,
        work_order: WorkOrderRecord,
        attempt: AttemptRecord,
        result: ExecutorResult,
    ) -> VerificationResult:
        criteria = self._criteria_from_contract(work_order)
        inputs = self._inputs_for(attempt, result, criteria)
        return self.engine.verify(
            work_order_id=work_order.work_order_id,
            attempt_id=attempt.attempt_id,
            criteria=criteria,
            inputs=inputs,
        )

    @staticmethod
    def _criteria_from_contract(work_order: WorkOrderRecord) -> tuple[AcceptanceCriterion, ...]:
        stored = work_order.contract.get("acceptance")
        if not stored:
            # Zero criteria would auto-pass with no evidence; refuse instead.
            raise VerificationRefused("work order contract has no acceptance criteria")
        return _CRITERIA_ADAPTER.validate_python(stored)

    def _inputs_for(
        self,
        attempt: AttemptRecord,
        result: ExecutorResult,
        criteria: tuple[AcceptanceCriterion, ...],
    ) -> VerificationInputs:
        with self.sessions() as session:
            records = {
                record.artifact_id: record
                for record in session.scalars(
                    select(ArtifactRecord).where(
                        ArtifactRecord.attempt_id == attempt.attempt_id
                    )
                ).all()
            }
        for capability in result.capability_results:
            declared = capability.output_artifact_id
            if declared is not None and declared not in records:
                raise VerificationRefused(
                    f"result references unknown artifact {declared}"
                )
        metric_ids = sorted(
            artifact_id
            for artifact_id, record in records.items()
            if record.artifact_type == "metrics"
        )
        repro_ids = sorted(
            artifact_id
            for artifact_id, record in records.items()
            if record.artifact_type == "reproducibility"
        )
        metric_artifacts: dict[str, str] = {}
        repro_artifacts: dict[str, tuple[str, ...]] = {}
        for criterion in criteria:
            if isinstance(criterion, MetricCriterion):
                if not metric_ids:
                    raise VerificationRefused(
                        f"metric criterion {criterion.criterion_id} has no metrics artifact"
                    )
                if len(metric_ids) != 1:
                    raise VerificationRefused(
                        f"metric criterion {criterion.criterion_id} has ambiguous metrics artifacts"
                    )
                metric_artifacts[criterion.metric] = metric_ids[0]
            elif isinstance(criterion, ReproCriterion):
                if not repro_ids:
                    raise VerificationRefused(
                        f"reproducibility criterion {criterion.criterion_id} "
                        "has no reproducibility artifacts"
                    )
                repro_artifacts[criterion.criterion_id] = tuple(repro_ids)
        return VerificationInputs(
            metric_artifacts=metric_artifacts,
            reproducibility_artifacts=repro_artifacts,
        )


__all__ = ["LocalVerificationDriver"]
