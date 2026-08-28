"""Add artifact derivation, approval, and policy-decision records."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from researchd.storage.types import UTCDateTime

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "artifact_derivations",
        sa.Column("derived_artifact_id", sa.String(96), sa.ForeignKey("artifacts.artifact_id"), primary_key=True),
        sa.Column("source_artifact_id", sa.String(96), sa.ForeignKey("artifacts.artifact_id"), primary_key=True),
        sa.Column("producer", sa.String(128), nullable=False), sa.Column("producer_version", sa.String(128), nullable=False),
        sa.Column("parameters", sa.JSON(), nullable=False), sa.Column("parameters_sha256", sa.String(64), nullable=False),
        sa.Column("transformation_sha256", sa.String(64), nullable=False), sa.Column("created_at", UTCDateTime(), nullable=False),
    )
    op.create_index("ix_artifact_derivations_source", "artifact_derivations", ["source_artifact_id"])
    op.create_table(
        "approval_requests", sa.Column("approval_id", sa.String(128), primary_key=True),
        sa.Column("operation_type", sa.String(128), nullable=False), sa.Column("canonical_parameters", sa.Text(), nullable=False),
        sa.Column("parameter_sha256", sa.String(64), nullable=False), sa.Column("requested_by", sa.String(128), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False), sa.Column("risk_level", sa.String(32), nullable=False),
        sa.Column("resource_scope", sa.JSON(), nullable=False), sa.Column("budget_delta", sa.JSON(), nullable=False),
        sa.Column("expires_at", UTCDateTime(), nullable=False), sa.Column("one_shot", sa.Boolean(), nullable=False),
        sa.Column("status", sa.String(32), nullable=False), sa.Column("created_at", UTCDateTime(), nullable=False),
    )
    op.create_index("ix_approval_requests_status_expires", "approval_requests", ["status", "expires_at"])
    op.create_table(
        "approval_grants", sa.Column("grant_id", sa.String(128), primary_key=True),
        sa.Column("approval_id", sa.String(128), sa.ForeignKey("approval_requests.approval_id"), nullable=False, unique=True),
        sa.Column("parameter_sha256", sa.String(64), nullable=False), sa.Column("granted_by", sa.String(128), nullable=False),
        sa.Column("expires_at", UTCDateTime(), nullable=False), sa.Column("one_shot", sa.Boolean(), nullable=False),
        sa.Column("used_at", UTCDateTime()), sa.Column("created_at", UTCDateTime(), nullable=False),
    )
    op.create_index("ix_approval_grants_hash_expires", "approval_grants", ["parameter_sha256", "expires_at"])
    op.create_table(
        "policy_decisions", sa.Column("policy_decision_id", sa.String(128), primary_key=True),
        sa.Column("run_id", sa.String(128), sa.ForeignKey("research_runs.run_id"), nullable=False),
        sa.Column("work_order_id", sa.String(128), sa.ForeignKey("work_orders.work_order_id")),
        sa.Column("outcome", sa.String(32), nullable=False), sa.Column("reason_codes", sa.JSON(), nullable=False),
        sa.Column("requested_capabilities", sa.JSON(), nullable=False), sa.Column("effective_capabilities", sa.JSON(), nullable=False),
        sa.Column("created_at", UTCDateTime(), nullable=False),
    )
    op.create_index("ix_policy_decisions_run_created", "policy_decisions", ["run_id", "created_at"])
    op.execute("""
        CREATE TRIGGER artifacts_classification_immutable
        BEFORE UPDATE OF classification ON artifacts
        WHEN NEW.classification <> OLD.classification
        BEGIN SELECT RAISE(ABORT, 'artifact classification is immutable'); END
    """)


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS artifacts_classification_immutable")
    op.drop_table("policy_decisions")
    op.drop_table("approval_grants")
    op.drop_table("approval_requests")
    op.drop_table("artifact_derivations")
