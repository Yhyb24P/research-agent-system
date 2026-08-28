from researchd.domain.base import DomainModel
from researchd.domain.enums import CriterionResult, DataClassification, VerificationOverall
from researchd.domain.ids import AttemptId, ObservationId, VerificationId


class CriterionEvaluation(DomainModel):
    criterion_id: str
    result: CriterionResult
    observation_refs: tuple[ObservationId, ...]
    severity: str = "hard"
    reason_code: str


class VerificationResult(DomainModel):
    verification_id: VerificationId
    attempt_id: AttemptId
    overall: VerificationOverall
    criteria: tuple[CriterionEvaluation, ...]
    acceptance_sha256: str
    verifier_version: str
    valid: bool = True
    classification: DataClassification
