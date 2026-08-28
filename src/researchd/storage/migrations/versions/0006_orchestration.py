"""Persist plans, reviews, run budgets, and cancellation intent."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from researchd.storage.types import UTCDateTime

revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("research_runs", sa.Column("max_iterations", sa.Integer(), nullable=False, server_default="8"))
    op.add_column("research_runs", sa.Column("max_cloud_calls", sa.Integer(), nullable=False, server_default="24"))
    op.add_column("research_runs", sa.Column("iterations_used", sa.Integer(), nullable=False, server_default="0"))
    op.add_column("research_runs", sa.Column("cloud_calls_used", sa.Integer(), nullable=False, server_default="0"))
    op.add_column("research_runs", sa.Column("cancellation_requested", sa.Boolean(), nullable=False, server_default=sa.false()))
    op.add_column("work_orders", sa.Column("revision_reason", sa.Text()))
    op.add_column("work_orders", sa.Column("approval_id", sa.String(128)))
    op.add_column("work_orders", sa.Column("approval_grant_id", sa.String(128)))
    op.create_table(
        "plans",
        sa.Column("plan_id", sa.String(128), primary_key=True),
        sa.Column("run_id", sa.String(128), sa.ForeignKey("research_runs.run_id"), nullable=False),
        sa.Column("proposal_json", sa.JSON(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("created_at", UTCDateTime(), nullable=False),
        sa.Column("updated_at", UTCDateTime(), nullable=False),
    )
    op.create_index("ix_plans_run_created", "plans", ["run_id", "created_at"])
    op.create_table(
        "review_decisions",
        sa.Column("review_id", sa.String(128), primary_key=True),
        sa.Column("run_id", sa.String(128), sa.ForeignKey("research_runs.run_id"), nullable=False),
        sa.Column("work_order_id", sa.String(128), sa.ForeignKey("work_orders.work_order_id"), nullable=False),
        sa.Column("attempt_id", sa.String(128), sa.ForeignKey("attempts.attempt_id")),
        sa.Column("interaction_id", sa.String(128), sa.ForeignKey("agent_interactions.interaction_id")),
        sa.Column("decision", sa.String(32), nullable=False),
        sa.Column("evidence_refs", sa.JSON(), nullable=False),
        sa.Column("deficiencies", sa.JSON(), nullable=False),
        sa.Column("rationale", sa.Text(), nullable=False),
        sa.Column("requested_next_objective", sa.Text()),
        sa.Column("requested_evidence", sa.JSON(), nullable=False),
        sa.Column("confidence", sa.Float()),
        sa.Column("created_at", UTCDateTime(), nullable=False),
    )
    op.create_index("ix_review_decisions_run_created", "review_decisions", ["run_id", "created_at"])
    op.create_index("ix_review_decisions_work_order_created", "review_decisions", ["work_order_id", "created_at"])


def downgrade() -> None:
    op.drop_table("review_decisions")
    op.drop_table("plans")
    op.drop_column("work_orders", "approval_id")
    op.drop_column("work_orders", "approval_grant_id")
    op.drop_column("work_orders", "revision_reason")
    op.drop_column("research_runs", "cancellation_requested")
    op.drop_column("research_runs", "cloud_calls_used")
    op.drop_column("research_runs", "iterations_used")
    op.drop_column("research_runs", "max_cloud_calls")
    op.drop_column("research_runs", "max_iterations")
