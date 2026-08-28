import json
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from researchd.artifacts.hashing import sha256_bytes
from researchd.artifacts.store import ContentAddressedArtifactStore
from researchd.domain.enums import DataClassification
from researchd.storage.models import ArtifactDerivationRecord, ArtifactRecord


def canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


class ArtifactMetadataConflict(RuntimeError):
    pass


class DerivationError(ValueError):
    pass


class ArtifactService:
    def __init__(self, store: ContentAddressedArtifactStore, sessions: sessionmaker[Session]) -> None:
        self.store = store
        self.sessions = sessions

    def register(
        self, data: bytes, *, mime_type: str, artifact_type: str,
        classification: DataClassification, producer_type: str, producer_id: str,
        attempt_id: str | None = None, relative_source_path: str | None = None,
    ) -> ArtifactRecord:
        artifact_id, digest = self.store.put(data)
        with self.sessions.begin() as session:
            existing = session.get(ArtifactRecord, artifact_id)
            if existing is not None:
                if existing.classification != classification.value:
                    raise ArtifactMetadataConflict("same bytes already registered with a different immutable classification")
                return existing
            record = ArtifactRecord(
                artifact_id=artifact_id, sha256=digest, size=len(data), mime_type=mime_type,
                artifact_type=artifact_type, classification=classification.value,
                producer_type=producer_type, producer_id=producer_id, attempt_id=attempt_id,
                relative_source_path=relative_source_path, created_at=datetime.now(UTC),
            )
            session.add(record)
            session.flush()
            return record

    def derive(
        self, source_artifact_ids: Sequence[str], derived_bytes: bytes, *,
        mime_type: str, artifact_type: str, classification: DataClassification,
        producer: str, producer_version: str, parameters: Mapping[str, Any],
    ) -> ArtifactRecord:
        if not source_artifact_ids:
            raise DerivationError("at least one source artifact is required")
        if len(set(source_artifact_ids)) != len(source_artifact_ids):
            raise DerivationError("source artifact IDs must be unique")
        canonical_parameters = canonical_json(parameters)
        parameters_hash = sha256_bytes(canonical_parameters.encode())
        artifact_id, digest = self.store.put(derived_bytes)
        now = datetime.now(UTC)
        with self.sessions.begin() as session:
            sources = session.scalars(select(ArtifactRecord).where(ArtifactRecord.artifact_id.in_(source_artifact_ids))).all()
            if len(sources) != len(source_artifact_ids):
                raise DerivationError("every source artifact must be registered")
            source_classes = {DataClassification(source.classification) for source in sources}
            cloud_visible = {DataClassification.PUBLIC, DataClassification.CLOUD_SAFE}
            if source_classes - cloud_visible and classification is not DataClassification.CLOUD_SAFE:
                raise DerivationError("a private/raw source may become cloud-visible only as a CLOUD_SAFE derivation")
            derived = session.get(ArtifactRecord, artifact_id)
            if derived is None:
                derived = ArtifactRecord(
                    artifact_id=artifact_id, sha256=digest, size=len(derived_bytes),
                    mime_type=mime_type, artifact_type=artifact_type,
                    classification=classification.value, producer_type="tool",
                    producer_id=f"{producer}@{producer_version}", attempt_id=None,
                    relative_source_path=None, created_at=now,
                )
                session.add(derived)
                session.flush()
            elif derived.classification != classification.value:
                raise ArtifactMetadataConflict("same derived bytes already have a different immutable classification")
            transformation_payload = canonical_json({
                "derived_sha256": derived.sha256,
                "parameters_sha256": parameters_hash,
                "producer": producer,
                "producer_version": producer_version,
                "source_sha256": sorted(source.sha256 for source in sources),
            })
            transformation_hash = sha256_bytes(transformation_payload.encode())
            for source_id in sorted(source_artifact_ids):
                existing = session.get(ArtifactDerivationRecord, (derived.artifact_id, source_id))
                if existing is not None:
                    if existing.transformation_sha256 != transformation_hash:
                        raise ArtifactMetadataConflict("derived bytes already have conflicting provenance")
                    continue
                session.add(ArtifactDerivationRecord(
                    derived_artifact_id=derived.artifact_id, source_artifact_id=source_id,
                    producer=producer, producer_version=producer_version,
                    parameters_json=dict(parameters), parameters_sha256=parameters_hash,
                    transformation_sha256=transformation_hash, created_at=now,
                ))
            session.flush()
        return derived
