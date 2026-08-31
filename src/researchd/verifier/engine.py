from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from sqlalchemy.orm import Session, sessionmaker

from researchd.domain.criteria import (
    AcceptanceCriterion,
    ArtifactCriterion,
    CommandCriterion,
    MetricCriterion,
    ReproCriterion,
    acceptance_fingerprint,
)
from researchd.domain.enums import CriterionResult, DataClassification, VerificationOverall
from researchd.domain.verification import CriterionEvaluation, VerificationResult
from researchd.domain.ids import AttemptId, ObservationId, VerificationId
from researchd.storage.models import (
    AttemptRecord,
    AuditEventRecord,
    ClaimRecord,
    ObservationRecord,
    VerificationResultRecord,
    WorkOrderRecord,
)
from researchd.verifier.contracts import ObservationDraft, VerificationInputs
from researchd.verifier.producers import TrustedObservationProducers, VerificationRefused


class ClaimRecorder:
    """Persist interpretations separately; claims never become verifier inputs."""

    def __init__(self, sessions: sessionmaker[Session]) -> None:
        self.sessions = sessions

    def record_executor_claims(self, attempt_id: str, claims: tuple[str, ...], supporting_refs: tuple[str, ...] = ()) -> list[str]:
        with self.sessions.begin() as session:
            return self.record_executor_claims_in_session(
                session, attempt_id, claims, supporting_refs,
            )

    @staticmethod
    def record_executor_claims_in_session(
        session: Session,
        attempt_id: str,
        claims: tuple[str, ...],
        supporting_refs: tuple[str, ...] = (),
    ) -> list[str]:
        """Add non-authoritative executor statements to an existing transaction."""
        if session.get(AttemptRecord, attempt_id) is None:
            raise LookupError(attempt_id)
        now = datetime.now(UTC)
        identifiers: list[str] = []
        for statement in claims:
            identifier = f"claim_{uuid4().hex}"
            session.add(ClaimRecord(
                claim_id=identifier, attempt_id=attempt_id, statement=statement,
                supporting_refs=list(supporting_refs), producer_type="executor",
                producer_id="local-executor", created_at=now,
            ))
            identifiers.append(identifier)
        return identifiers


class VerifierEngine:
    version = "verifier-v1"

    def __init__(self, sessions: sessionmaker[Session], producers: TrustedObservationProducers) -> None:
        self.sessions = sessions
        self.producers = producers

    def verify(
        self, *, work_order_id: str, attempt_id: str,
        criteria: tuple[AcceptanceCriterion, ...], inputs: VerificationInputs,
    ) -> VerificationResult:
        fingerprint = acceptance_fingerprint(criteria)
        observations: list[ObservationDraft] = []
        evaluations: list[CriterionEvaluation] = []
        with self.sessions() as session:
            attempt = session.get(AttemptRecord, attempt_id)
            order = session.get(WorkOrderRecord, work_order_id)
            if attempt is None or order is None or attempt.work_order_id != work_order_id:
                raise VerificationRefused("attempt/work order provenance is missing or mismatched")
            stored_acceptance = order.contract.get("acceptance")
            if stored_acceptance is None or acceptance_fingerprint(stored_acceptance) != fingerprint:
                raise VerificationRefused("criteria do not match the dispatched WorkOrder contract")
            for criterion in criteria:
                passed, reason, produced = self._evaluate(session, attempt_id, criterion, inputs)
                observations.extend(produced)
                evaluations.append(CriterionEvaluation(
                    criterion_id=criterion.criterion_id,
                    result=CriterionResult.PASS if passed else CriterionResult.FAIL,
                    observation_refs=tuple(ObservationId(item.observation_id) for item in produced),
                    severity=criterion.severity, reason_code=reason,
                ))
            run_id = order.run_id
        hard_pass = all(item.result is CriterionResult.PASS for item in evaluations if item.severity == "hard")
        classification_rank = {
            DataClassification.PUBLIC: 0,
            DataClassification.CLOUD_SAFE: 1,
            DataClassification.PROJECT_PRIVATE: 2,
            DataClassification.LOCAL_ONLY: 3,
            DataClassification.SECRET: 4,
        }
        result_classification = max(
            (item.classification for item in observations),
            key=classification_rank.__getitem__,
            default=DataClassification.LOCAL_ONLY,
        )
        result = VerificationResult(
            verification_id=VerificationId(f"ver_{uuid4().hex}"), attempt_id=AttemptId(attempt_id),
            overall=VerificationOverall.PASS if hard_pass else VerificationOverall.FAIL,
            criteria=tuple(evaluations), acceptance_sha256=fingerprint,
            verifier_version=self.version, valid=True,
            classification=result_classification,
        )
        self._persist(run_id, work_order_id, result, observations)
        return result

    def _evaluate(
        self, session: Session, attempt_id: str, criterion: AcceptanceCriterion,
        inputs: VerificationInputs,
    ) -> tuple[bool, str, list[ObservationDraft]]:
        if isinstance(criterion, CommandCriterion):
            return self.producers.command(session, attempt_id, criterion)
        if isinstance(criterion, MetricCriterion):
            source = inputs.metric_artifacts.get(criterion.metric)
            if source is None:
                raise VerificationRefused("metric criterion source artifact is missing")
            passed, reason, observation = self.producers.metric(session, attempt_id, criterion, source)
            return passed, reason, [observation]
        if isinstance(criterion, ArtifactCriterion):
            return self.producers.artifact(session, attempt_id, criterion)
        if isinstance(criterion, ReproCriterion):
            return self.producers.reproducibility(
                session, attempt_id, criterion,
                inputs.reproducibility_artifacts.get(criterion.criterion_id, ()),
            )
        raise VerificationRefused("unsupported acceptance criterion")

    def _persist(
        self, run_id: str, work_order_id: str, result: VerificationResult,
        observations: list[ObservationDraft],
    ) -> None:
        now = datetime.now(UTC)
        with self.sessions.begin() as session:
            for item in observations:
                session.add(ObservationRecord(
                    observation_id=item.observation_id, attempt_id=str(result.attempt_id),
                    name=item.name, value_json=item.value,
                    source_artifact_ids=list(item.source_artifact_ids),
                    source_step_ids=list(item.source_step_ids), source_job_ids=list(item.source_job_ids),
                    producer_type=item.producer_type, producer_id=item.producer_id,
                    producer_version=item.producer_version, created_at=now,
                    classification=item.classification.value,
                ))
            session.add(VerificationResultRecord(
                verification_id=str(result.verification_id), attempt_id=str(result.attempt_id),
                work_order_id=work_order_id, overall=result.overall.value,
                criteria_json=[item.model_dump(mode="json") for item in result.criteria],
                acceptance_sha256=result.acceptance_sha256, verifier_version=result.verifier_version,
                valid=result.valid, created_at=now,
                classification=result.classification.value,
            ))
            session.add(AuditEventRecord(
                event_id=f"evt_{uuid4().hex}", event_type="VERIFICATION_COMPLETED",
                run_id=run_id, entity_type="verification", entity_id=str(result.verification_id),
                actor_type="verifier", actor_id=self.version, timestamp=now,
                correlation_id=str(result.attempt_id), causation_id=None,
                metadata_json={"overall": result.overall.value, "acceptance_sha256": result.acceptance_sha256},
            ))
