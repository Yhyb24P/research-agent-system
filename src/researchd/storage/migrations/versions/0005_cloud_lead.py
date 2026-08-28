"""Classify evidence for egress and record cloud interactions."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from researchd.storage.types import UTCDateTime

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("observations", sa.Column("classification", sa.String(32), nullable=False, server_default="LOCAL_ONLY"))
    op.add_column("verification_results", sa.Column("classification", sa.String(32), nullable=False, server_default="LOCAL_ONLY"))
    op.execute("""
        CREATE TRIGGER observations_immutable
        BEFORE UPDATE ON observations
        BEGIN SELECT RAISE(ABORT, 'observation is immutable'); END
    """)
    op.execute("""
        CREATE TRIGGER verification_results_immutable
        BEFORE UPDATE ON verification_results
        BEGIN SELECT RAISE(ABORT, 'verification result is immutable'); END
    """)
    op.create_table(
        "agent_interactions", sa.Column("interaction_id", sa.String(128), primary_key=True),
        sa.Column("run_id", sa.String(128), sa.ForeignKey("research_runs.run_id"), nullable=False),
        sa.Column("work_order_id", sa.String(128), sa.ForeignKey("work_orders.work_order_id")),
        sa.Column("role", sa.String(32), nullable=False), sa.Column("purpose", sa.String(32), nullable=False),
        sa.Column("provider", sa.String(128), nullable=False), sa.Column("model", sa.String(128), nullable=False),
        sa.Column("bundle_sha256", sa.String(64), nullable=False), sa.Column("response_type", sa.String(128), nullable=False),
        sa.Column("response_json", sa.JSON()), sa.Column("status", sa.String(32), nullable=False),
        sa.Column("reason_code", sa.String(128)), sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("prompt_tokens", sa.Integer(), nullable=False), sa.Column("completion_tokens", sa.Integer(), nullable=False),
        sa.Column("total_tokens", sa.Integer(), nullable=False), sa.Column("cost_usd", sa.String(64), nullable=False),
        sa.Column("provider_request_id", sa.String(256)), sa.Column("created_at", UTCDateTime(), nullable=False),
        sa.Column("completed_at", UTCDateTime()),
    )
    op.create_index("ix_agent_interactions_run_created", "agent_interactions", ["run_id", "created_at"])
    op.create_index("ix_agent_interactions_status", "agent_interactions", ["status"])


def downgrade() -> None:
    op.drop_table("agent_interactions")
    op.execute("DROP TRIGGER IF EXISTS verification_results_immutable")
    op.execute("DROP TRIGGER IF EXISTS observations_immutable")
    op.drop_column("verification_results", "classification")
    op.drop_column("observations", "classification")
