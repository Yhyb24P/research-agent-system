from collections.abc import Sequence

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from researchd.artifacts.store import ContentAddressedArtifactStore
from researchd.context.cloud_bundle import CloudArtifactItem, CloudContextBundle, CloudObservationItem, CloudVerificationItem
from researchd.context.redaction import DeterministicRedactor
from researchd.domain.enums import DataClassification
from researchd.domain.base import DomainModel
from researchd.storage.models import AgentInvocationRecord, ArtifactDerivationRecord, ArtifactRecord, AttemptRecord, ObservationRecord, ResearchRunRecord, VerificationResultRecord, WorkOrderRecord


class EgressDenied(PermissionError):
    pass


class ContextRecordMissing(LookupError):
    pass


class CloudContextSelection(DomainModel):
    run_id: str
    work_order_id: str | None = None
    invocation_id: str | None = None
    artifact_ids: tuple[str, ...] = ()
    observation_ids: tuple[str, ...] = ()
    verification_id: str | None = None


class ContextBuilder:
    """Build cloud context only from authoritative database identifiers."""

    cloud_allowed = frozenset({DataClassification.PUBLIC, DataClassification.CLOUD_SAFE})
    text_mime_types = frozenset({"text/plain", "application/json", "text/x-diff", "text/markdown"})

    def __init__(
        self, sessions: sessionmaker[Session], store: ContentAddressedArtifactStore,
        redactor: DeterministicRedactor, *, max_artifact_bytes: int = 256_000,
    ) -> None:
        self.sessions = sessions
        self.store = store
        self.redactor = redactor
        self.max_artifact_bytes = max_artifact_bytes

    def build(
        self, *, run_id: str, work_order_id: str | None = None,
        artifact_ids: Sequence[str] = (), observation_ids: Sequence[str] = (),
        verification_id: str | None = None,
        allowed_classifications: frozenset[DataClassification] | None = None,
    ) -> CloudContextBundle:
        allowed = allowed_classifications or self.cloud_allowed
        with self.sessions() as session:
            run = session.get(ResearchRunRecord, run_id)
            order = session.get(WorkOrderRecord, work_order_id) if work_order_id is not None else None
            if run is None or (work_order_id is not None and (order is None or order.run_id != run_id)):
                raise ContextRecordMissing("run/work order authoritative record missing or mismatched")
            selected: dict[str, ArtifactRecord] = {}
            for artifact_id in artifact_ids:
                artifact = session.get(ArtifactRecord, artifact_id)
                if artifact is None:
                    raise ContextRecordMissing(f"artifact not registered: {artifact_id}")
                try:
                    classification = DataClassification(artifact.classification)
                except ValueError as error:
                    raise EgressDenied("unknown artifact classification") from error
                if classification in allowed:
                    selected[artifact.artifact_id] = artifact
                elif classification is DataClassification.PROJECT_PRIVATE:
                    derivatives = self._cloud_safe_derivatives(session, artifact.artifact_id)
                    if not derivatives:
                        raise EgressDenied("PROJECT_PRIVATE artifact has no CLOUD_SAFE derivation")
                    selected.update((item.artifact_id, item) for item in derivatives)
                else:
                    raise EgressDenied(f"{classification.value} artifact is not eligible for cloud egress")

            verification_record = session.get(VerificationResultRecord, verification_id) if verification_id is not None else None
            if verification_id is not None and verification_record is None:
                raise ContextRecordMissing("verification record not found")
            required_observation_ids = set(observation_ids)
            if verification_record is not None:
                for evaluation in verification_record.criteria_json:
                    refs = evaluation.get("observation_refs", [])
                    if not isinstance(refs, list) or any(not isinstance(ref, str) for ref in refs):
                        raise EgressDenied("verification observation references are malformed")
                    required_observation_ids.update(refs)
            observation_records = [self._cloud_observation(session, run_id, work_order_id, identifier, allowed) for identifier in sorted(required_observation_ids)]
            verification_item = None
            if verification_record is not None:
                self._assert_verification_scope(session, verification_record, run_id, work_order_id)
                classification = self._allowed_classification(verification_record.classification, "verification", allowed)
                verification_item = CloudVerificationItem(
                    verification_id=verification_record.verification_id, overall=verification_record.overall,
                    criteria=tuple(self.redactor.redact_json(verification_record.criteria_json)),
                    acceptance_sha256=verification_record.acceptance_sha256,
                    verifier_version=verification_record.verifier_version,
                    classification=classification.value,
                )
            items = tuple(self._build_item(record) for record in sorted(selected.values(), key=lambda item: item.artifact_id))
            return CloudContextBundle(
                run_id=run.run_id, work_order_id=order.work_order_id if order is not None else None,
                goal=self.redactor.redact(run.objective),
                objective=self.redactor.redact(order.objective) if order is not None else None,
                selected_artifacts=items,
                observations=tuple(observation_records), verification=verification_item,
            )

    def build_selection(self, selection: CloudContextSelection) -> CloudContextBundle:
        if selection.invocation_id is not None:
            with self.sessions() as session:
                invocation = session.get(AgentInvocationRecord, selection.invocation_id)
                if (
                    invocation is None or invocation.run_id != selection.run_id
                    or (selection.work_order_id is not None and invocation.work_order_id != selection.work_order_id)
                ):
                    raise EgressDenied("cloud interaction invocation is outside the requested authoritative scope")
        return self.build(
            run_id=selection.run_id, work_order_id=selection.work_order_id,
            artifact_ids=selection.artifact_ids, observation_ids=selection.observation_ids,
            verification_id=selection.verification_id,
        )

    def _cloud_observation(self, session: Session, run_id: str, work_order_id: str | None, observation_id: str, allowed: frozenset[DataClassification]) -> CloudObservationItem:
        observation = session.get(ObservationRecord, observation_id)
        if observation is None:
            raise ContextRecordMissing(f"observation not found: {observation_id}")
        attempt = session.get(AttemptRecord, observation.attempt_id)
        order = session.get(WorkOrderRecord, attempt.work_order_id) if attempt is not None else None
        if attempt is None or order is None or order.run_id != run_id or (work_order_id is not None and order.work_order_id != work_order_id):
            raise EgressDenied("observation is outside the requested authoritative scope")
        classification = self._allowed_classification(observation.classification, "observation", allowed)
        if not observation.source_artifact_ids or observation.source_step_ids or observation.source_job_ids:
            raise EgressDenied("cloud-safe observation must derive only from explicit cloud-safe artifacts")
        for artifact_id in observation.source_artifact_ids:
            source = session.get(ArtifactRecord, artifact_id)
            if source is None:
                raise EgressDenied("observation source artifact is missing")
            self._allowed_classification(source.classification, "observation source", allowed)
        return CloudObservationItem(
            observation_id=observation.observation_id, name=self.redactor.redact(observation.name),
            value=self.redactor.redact_json(observation.value_json),
            source_artifact_ids=tuple(observation.source_artifact_ids),
            producer_id=observation.producer_id, producer_version=observation.producer_version,
            classification=classification.value,
        )

    def _assert_verification_scope(
        self, session: Session, verification: VerificationResultRecord,
        run_id: str, work_order_id: str | None,
    ) -> None:
        order = session.get(WorkOrderRecord, verification.work_order_id)
        if order is None or order.run_id != run_id or (work_order_id is not None and order.work_order_id != work_order_id):
            raise EgressDenied("verification is outside the requested authoritative scope")

    def _allowed_classification(self, value: str, object_type: str, allowed: frozenset[DataClassification] | None = None) -> DataClassification:
        try:
            classification = DataClassification(value)
        except ValueError as error:
            raise EgressDenied(f"unknown {object_type} classification") from error
        if classification not in (allowed or self.cloud_allowed):
            raise EgressDenied(f"{object_type} classification is not eligible for cloud")
        return classification

    def _cloud_safe_derivatives(self, session: Session, source_id: str) -> list[ArtifactRecord]:
        query = (
            select(ArtifactRecord)
            .join(ArtifactDerivationRecord, ArtifactDerivationRecord.derived_artifact_id == ArtifactRecord.artifact_id)
            .where(
                ArtifactDerivationRecord.source_artifact_id == source_id,
                ArtifactRecord.classification == DataClassification.CLOUD_SAFE.value,
            )
            .order_by(ArtifactRecord.artifact_id)
        )
        return list(session.scalars(query).all())

    def _build_item(self, record: ArtifactRecord) -> CloudArtifactItem:
        if record.mime_type not in self.text_mime_types:
            raise EgressDenied(f"artifact MIME type is not eligible for inline cloud context: {record.mime_type}")
        content = self.store.read(record.artifact_id)
        if len(content) > self.max_artifact_bytes:
            raise EgressDenied("artifact exceeds cloud context byte limit")
        try:
            text = content.decode("utf-8")
        except UnicodeDecodeError as error:
            raise EgressDenied("artifact is not valid UTF-8 text") from error
        return CloudArtifactItem(
            artifact_id=record.artifact_id, sha256=record.sha256,
            mime_type=record.mime_type, artifact_type=record.artifact_type,
            classification=record.classification, content=self.redactor.redact(text),
        )
