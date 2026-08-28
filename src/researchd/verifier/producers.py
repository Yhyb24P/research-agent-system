import json
import math
import xml.etree.ElementTree as ET
from decimal import Decimal
from typing import Any
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session

from researchd.artifacts.store import ArtifactCorruptionError, ContentAddressedArtifactStore
from researchd.domain.criteria import ArtifactCriterion, CommandCriterion, MetricCriterion, ReproCriterion
from researchd.domain.enums import DataClassification
from researchd.executor.contracts import CapabilityResult
from researchd.storage.models import ArtifactRecord, ExecutionStepRecord
from researchd.verifier.contracts import ObservationDraft


class VerificationRefused(RuntimeError):
    pass


def observation_id() -> str:
    return f"obs_{uuid4().hex}"


class TrustedObservationProducers:
    version = "1.0"

    def __init__(self, store: ContentAddressedArtifactStore, *, max_evidence_bytes: int = 16 * 1024 * 1024) -> None:
        self.store = store
        self.max_evidence_bytes = max_evidence_bytes

    def command(self, session: Session, attempt_id: str, criterion: CommandCriterion) -> tuple[bool, str, list[ObservationDraft]]:
        step = session.get(ExecutionStepRecord, criterion.command_id)
        if step is None or step.attempt_id != attempt_id or step.status != "COMPLETED" or step.result_json is None:
            raise VerificationRefused("command execution source is missing or incomplete")
        result = CapabilityResult.model_validate(step.result_json)
        observations = [ObservationDraft(
            observation_id=observation_id(), name=f"command:{criterion.command_id}:exit_code",
            value=result.exit_code, source_step_ids=(criterion.command_id,),
            producer_type="verifier", producer_id="command-observer", producer_version=self.version,
            classification=DataClassification.LOCAL_ONLY,
        )]
        passed = result.exit_code == criterion.expected_exit_code
        reason = "VERIFICATION_COMMAND_PASSED" if passed else "VERIFICATION_COMMAND_FAILED"
        if criterion.junit_artifact_id is not None:
            junit_passed, junit_observation = self.junit(session, attempt_id, criterion.junit_artifact_id, criterion.criterion_id)
            observations.append(junit_observation)
            if not junit_passed:
                passed = False
                reason = "VERIFICATION_JUNIT_FAILED"
        return passed, reason, observations

    def junit(self, session: Session, attempt_id: str, artifact_id: str, criterion_id: str) -> tuple[bool, ObservationDraft]:
        artifact, data = self._artifact(session, attempt_id, artifact_id, expected_type="junit")
        if b"<!DOCTYPE" in data.upper():
            raise VerificationRefused("JUnit with a DOCTYPE is not accepted")
        try:
            root = ET.fromstring(data)
        except ET.ParseError as error:
            raise VerificationRefused("JUnit artifact is malformed") from error
        suites = [root] if root.tag == "testsuite" else list(root.findall(".//testsuite"))
        if not suites:
            raise VerificationRefused("JUnit artifact contains no testsuite")
        try:
            totals = {key: sum(int(suite.attrib.get(key, "0")) for suite in suites) for key in ("tests", "failures", "errors", "skipped")}
        except ValueError as error:
            raise VerificationRefused("JUnit counters must be integers") from error
        passed = totals["tests"] > 0 and totals["failures"] == 0 and totals["errors"] == 0
        return passed, ObservationDraft(
            observation_id=observation_id(), name=f"junit:{criterion_id}", value=totals,
            source_artifact_ids=(artifact.artifact_id,), producer_type="verifier",
            producer_id="junit-observer", producer_version=self.version,
            classification=self._classification(artifact),
        )

    def metric(self, session: Session, attempt_id: str, criterion: MetricCriterion, artifact_id: str) -> tuple[bool, str, ObservationDraft]:
        artifact, data = self._artifact(session, attempt_id, artifact_id, expected_type="metrics")
        payload = self._strict_json(data)
        if not isinstance(payload, dict) or criterion.metric not in payload:
            raise VerificationRefused("metric artifact does not contain the required metric")
        value = payload[criterion.metric]
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(float(value)):
            raise VerificationRefused("metric value must be a finite number")
        left = Decimal(str(value))
        right = Decimal(str(criterion.threshold))
        operators = {
            "==": left == right, "!=": left != right, ">": left > right,
            ">=": left >= right, "<": left < right, "<=": left <= right,
        }
        passed = operators[criterion.operator]
        return passed, "VERIFICATION_METRIC_PASSED" if passed else "VERIFICATION_METRIC_FAILED", ObservationDraft(
            observation_id=observation_id(), name=criterion.metric, value=value,
            source_artifact_ids=(artifact.artifact_id,), producer_type="verifier",
            producer_id="metric-observer", producer_version=self.version,
            classification=self._classification(artifact),
        )

    def artifact(self, session: Session, attempt_id: str, criterion: ArtifactCriterion) -> tuple[bool, str, list[ObservationDraft]]:
        records = list(session.scalars(select(ArtifactRecord).where(
            ArtifactRecord.attempt_id == attempt_id,
            ArtifactRecord.artifact_type == criterion.artifact_type,
        )).all())
        for record in records:
            self._verify_bytes(record)
        observations: list[ObservationDraft] = []
        if records:
            observations.append(ObservationDraft(
                observation_id=observation_id(), name=f"artifact_count:{criterion.artifact_type}",
                value=len(records), source_artifact_ids=tuple(record.artifact_id for record in records),
                producer_type="verifier", producer_id="artifact-observer", producer_version=self.version,
                classification=self._most_restrictive(records),
            ))
        passed = len(records) >= criterion.min_count
        return passed, "VERIFICATION_ARTIFACT_PASSED" if passed else "VERIFICATION_ARTIFACT_FAILED", observations

    def reproducibility(self, session: Session, attempt_id: str, criterion: ReproCriterion, artifact_ids: tuple[str, ...]) -> tuple[bool, str, list[ObservationDraft]]:
        if len(set(artifact_ids)) != len(artifact_ids):
            raise VerificationRefused("reproducibility sources must be distinct artifacts")
        observations: list[ObservationDraft] = []
        run_ids: set[str] = set()
        successes = 0
        for artifact_id in artifact_ids:
            artifact, data = self._artifact(session, attempt_id, artifact_id, expected_type="reproducibility")
            payload = self._strict_json(data)
            if not isinstance(payload, dict) or not isinstance(payload.get("run_id"), str) or not isinstance(payload.get("success"), bool):
                raise VerificationRefused("reproducibility artifact must contain run_id and boolean success")
            run_id = payload["run_id"]
            if run_id in run_ids:
                raise VerificationRefused("reproducibility run IDs must be independent")
            run_ids.add(run_id)
            success = payload["success"]
            successes += int(success)
            observations.append(ObservationDraft(
                observation_id=observation_id(), name=f"reproducibility:{criterion.criterion_id}:{run_id}",
                value=success, source_artifact_ids=(artifact.artifact_id,), producer_type="verifier",
                producer_id="reproducibility-observer", producer_version=self.version,
                classification=self._classification(artifact),
            ))
        passed = len(run_ids) >= criterion.runs and successes >= criterion.required_successes
        return passed, "VERIFICATION_REPRO_PASSED" if passed else "VERIFICATION_REPRO_FAILED", observations

    def _artifact(self, session: Session, attempt_id: str, artifact_id: str, *, expected_type: str) -> tuple[ArtifactRecord, bytes]:
        artifact = session.get(ArtifactRecord, artifact_id)
        if artifact is None or artifact.attempt_id != attempt_id:
            raise VerificationRefused("verification source artifact is missing or has wrong provenance")
        if artifact.artifact_type != expected_type:
            raise VerificationRefused("verification source artifact has unexpected type")
        return artifact, self._verify_bytes(artifact)

    def _verify_bytes(self, artifact: ArtifactRecord) -> bytes:
        if artifact.artifact_id.removeprefix("artifact://sha256/") != artifact.sha256:
            raise VerificationRefused("artifact ID and SHA256 metadata disagree")
        if artifact.size > self.max_evidence_bytes:
            raise VerificationRefused("verification artifact exceeds evidence byte limit")
        try:
            data = self.store.read(artifact.artifact_id)
        except (FileNotFoundError, ArtifactCorruptionError, ValueError) as error:
            raise VerificationRefused("artifact bytes are missing or fail hash verification") from error
        if len(data) != artifact.size:
            raise VerificationRefused("artifact size metadata does not match bytes")
        return data

    @staticmethod
    def _classification(artifact: ArtifactRecord) -> DataClassification:
        try:
            return DataClassification(artifact.classification)
        except ValueError as error:
            raise VerificationRefused("artifact classification is unknown") from error

    @classmethod
    def _most_restrictive(cls, artifacts: list[ArtifactRecord]) -> DataClassification:
        rank = {
            DataClassification.PUBLIC: 0,
            DataClassification.CLOUD_SAFE: 1,
            DataClassification.PROJECT_PRIVATE: 2,
            DataClassification.LOCAL_ONLY: 3,
            DataClassification.SECRET: 4,
        }
        return max((cls._classification(item) for item in artifacts), key=rank.__getitem__)

    @staticmethod
    def _strict_json(data: bytes) -> Any:
        try:
            return json.loads(data, parse_constant=lambda value: (_ for _ in ()).throw(ValueError(value)))
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as error:
            raise VerificationRefused("verification JSON is malformed or non-finite") from error
