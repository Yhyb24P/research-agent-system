"""Add observations, claims, and independent verification results."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from researchd.storage.types import UTCDateTime

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("""
        CREATE TRIGGER artifacts_metadata_immutable
        BEFORE UPDATE ON artifacts
        WHEN NEW.artifact_id IS NOT OLD.artifact_id
          OR NEW.sha256 IS NOT OLD.sha256
          OR NEW.size IS NOT OLD.size
          OR NEW.mime_type IS NOT OLD.mime_type
          OR NEW.artifact_type IS NOT OLD.artifact_type
          OR NEW.classification IS NOT OLD.classification
          OR NEW.producer_type IS NOT OLD.producer_type
          OR NEW.producer_id IS NOT OLD.producer_id
          OR NEW.attempt_id IS NOT OLD.attempt_id
          OR NEW.relative_source_path IS NOT OLD.relative_source_path
          OR NEW.created_at IS NOT OLD.created_at
        BEGIN SELECT RAISE(ABORT, 'artifact metadata is immutable'); END
    """)
    op.create_table(
        "observations", sa.Column("observation_id", sa.String(128), primary_key=True),
        sa.Column("attempt_id", sa.String(128), sa.ForeignKey("attempts.attempt_id"), nullable=False),
        sa.Column("name", sa.String(256), nullable=False), sa.Column("value", sa.JSON(), nullable=False),
        sa.Column("source_artifact_ids", sa.JSON(), nullable=False), sa.Column("source_step_ids", sa.JSON(), nullable=False),
        sa.Column("source_job_ids", sa.JSON(), nullable=False), sa.Column("producer_type", sa.String(64), nullable=False),
        sa.Column("producer_id", sa.String(128), nullable=False), sa.Column("producer_version", sa.String(64), nullable=False),
        sa.Column("created_at", UTCDateTime(), nullable=False),
        sa.CheckConstraint("json_array_length(source_artifact_ids) + json_array_length(source_step_ids) + json_array_length(source_job_ids) > 0", name="ck_observations_has_source"),
    )
    op.create_index("ix_observations_attempt_name", "observations", ["attempt_id", "name"])
    op.create_table(
        "claims", sa.Column("claim_id", sa.String(128), primary_key=True),
        sa.Column("attempt_id", sa.String(128), sa.ForeignKey("attempts.attempt_id"), nullable=False),
        sa.Column("statement", sa.Text(), nullable=False), sa.Column("supporting_refs", sa.JSON(), nullable=False),
        sa.Column("producer_type", sa.String(64), nullable=False), sa.Column("producer_id", sa.String(128), nullable=False),
        sa.Column("created_at", UTCDateTime(), nullable=False),
    )
    op.create_index("ix_claims_attempt_id", "claims", ["attempt_id"])
    op.create_table(
        "verification_results", sa.Column("verification_id", sa.String(128), primary_key=True),
        sa.Column("attempt_id", sa.String(128), sa.ForeignKey("attempts.attempt_id"), nullable=False),
        sa.Column("work_order_id", sa.String(128), sa.ForeignKey("work_orders.work_order_id"), nullable=False),
        sa.Column("overall", sa.String(32), nullable=False), sa.Column("criteria", sa.JSON(), nullable=False),
        sa.Column("acceptance_sha256", sa.String(64), nullable=False), sa.Column("verifier_version", sa.String(64), nullable=False),
        sa.Column("valid", sa.Boolean(), nullable=False), sa.Column("created_at", UTCDateTime(), nullable=False),
    )
    op.create_index("ix_verification_results_work_order_created", "verification_results", ["work_order_id", "created_at"])
    op.create_index("ix_verification_results_attempt_created", "verification_results", ["attempt_id", "created_at"])


def downgrade() -> None:
    op.drop_table("verification_results")
    op.drop_table("claims")
    op.drop_table("observations")
    op.execute("DROP TRIGGER IF EXISTS artifacts_metadata_immutable")
