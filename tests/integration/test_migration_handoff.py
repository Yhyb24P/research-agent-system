"""PX05-03/04: 0024->0025 handoff-resolution migration, the daemon's
schema-head gate, and backup/restore coverage of handoff data."""

from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import inspect, text

from researchd.backup.snapshot import (
    backup_snapshot,
    check_restored_snapshot,
    restore_snapshot,
)
from researchd.collaboration.handoff import HandoffResolutionService
from researchd.daemon.startup import EXPECTED_SCHEMA_REVISION, verify_migration_head
from researchd.storage.db import create_sqlite_engine, session_factory
from researchd.storage.models import HandoffProposalRecord
from tests.integration.test_handoff_safety import Fixture
from tests.integration.test_storage import ROOT

CANDIDATE_COMMIT = "0" * 40
CANDIDATE_TAG = "v0.0.0-rc.handoff-migration-test"


def _config(path: Path) -> Config:
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("sqlalchemy.url", f"sqlite:///{path}")
    return config


def _column_names(path: Path, table: str) -> set[str]:
    engine = create_sqlite_engine(path)
    try:
        return {column["name"] for column in inspect(engine).get_columns(table)}
    finally:
        engine.dispose()


def test_upgrade_0024_to_0025_adds_resolution_columns(tmp_path: Path) -> None:
    database = tmp_path / "handoff-migration.db"
    config = _config(database)
    command.upgrade(config, "0024")
    assert "resolution_entity_type" not in _column_names(database, "handoff_proposals")
    assert "resolution_entity_id" not in _column_names(database, "handoff_proposals")

    command.upgrade(config, "0025")
    columns = _column_names(database, "handoff_proposals")
    assert "resolution_entity_type" in columns
    assert "resolution_entity_id" in columns
    # The head is clean against the models (no autogenerate drift).
    command.check(config)


def test_daemon_schema_gate_accepts_only_0025(tmp_path: Path) -> None:
    assert EXPECTED_SCHEMA_REVISION == "0025"

    head_db = tmp_path / "head.db"
    command.upgrade(_config(head_db), "head")
    engine = create_sqlite_engine(head_db)
    verify_migration_head(engine)  # the current head is accepted
    engine.dispose()

    stale_db = tmp_path / "stale.db"
    command.upgrade(_config(stale_db), "0024")
    stale_engine = create_sqlite_engine(stale_db)
    with pytest.raises(RuntimeError, match="migration head does not match"):
        verify_migration_head(stale_engine)
    stale_engine.dispose()


def test_backup_restore_round_trip_preserves_handoff_data(tmp_path: Path) -> None:
    fixture = Fixture(tmp_path)
    proposal_id = fixture.proposal()
    fixture.terminalize_source()
    service = HandoffResolutionService(fixture.sessions, fixture.controller())
    resolved = service.accept(
        proposal_id, actor_type="HUMAN", actor_id="operator",
        reason="take over", target_agent_id="agent_b",
    )
    assert resolved.resolution_entity_type == "attempt"

    database = tmp_path / "handoff.db"
    artifact_root = tmp_path / "artifacts"
    artifact_root.mkdir()
    snapshot = tmp_path / "handoff-snapshot"
    manifest = backup_snapshot(
        database, artifact_root, snapshot,
        candidate_commit=CANDIDATE_COMMIT, candidate_tag=CANDIDATE_TAG,
    )
    assert manifest.schema_revision == "0025"

    restored_db = tmp_path / "restored.db"
    restored_artifacts = tmp_path / "restored-artifacts"
    restore_snapshot(
        snapshot, restored_db, restored_artifacts,
        expected_candidate_commit=CANDIDATE_COMMIT, expected_candidate_tag=CANDIDATE_TAG,
    )
    health = check_restored_snapshot(restored_db, restored_artifacts)
    assert health.healthy
    assert health.table_counts["handoff_proposals"] >= 1

    sessions = session_factory(create_sqlite_engine(restored_db))
    with sessions() as session:
        row = session.get(HandoffProposalRecord, proposal_id)
        assert row is not None
        assert row.status == "ACCEPTED"
        assert row.resolution_entity_type == "attempt"
        # The 0025 resolution columns survived the snapshot round trip.
        assert row.resolution_entity_id is not None
        columns = {column["name"] for column in inspect(session.get_bind()).get_columns("handoff_proposals")}
        assert "resolution_entity_type" in columns and "resolution_entity_id" in columns
        assert session.scalar(text("SELECT COUNT(*) FROM handoff_proposals")) == health.table_counts["handoff_proposals"]
