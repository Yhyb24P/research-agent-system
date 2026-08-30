"""Persist non-authoritative handoff proposals."""

from alembic import op
import sqlalchemy as sa

revision = "0024"
down_revision = "0023"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "handoff_proposals",
        sa.Column("proposal_id", sa.String(128), primary_key=True),
        sa.Column("action_id", sa.String(128), nullable=False),
        sa.Column("run_id", sa.String(128), sa.ForeignKey("research_runs.run_id"), nullable=False),
        sa.Column("work_order_id", sa.String(128), sa.ForeignKey("work_orders.work_order_id"), nullable=False),
        sa.Column("source_delegation_id", sa.String(128), sa.ForeignKey("delegations.delegation_id"), nullable=False),
        sa.Column("source_invocation_id", sa.String(128), sa.ForeignKey("agent_invocations.invocation_id"), nullable=False),
        sa.Column("source_agent_id", sa.String(128), sa.ForeignKey("agents.agent_id"), nullable=False),
        sa.Column("proposed_target_agent_id", sa.String(128), sa.ForeignKey("agents.agent_id")),
        sa.Column("requested_mode", sa.String(16), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("continuation_objective", sa.Text()),
        sa.Column("artifact_ids_json", sa.JSON(), nullable=False),
        sa.Column("observation_ids_json", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("decided_at", sa.DateTime(timezone=True)),
        sa.Column("decision_actor_type", sa.String(32)),
        sa.Column("decision_actor_id", sa.String(128)),
        sa.Column("decision_reason", sa.Text()),
        sa.CheckConstraint("requested_mode IN ('CONTINUE','REVISE')", name="ck_handoff_mode"),
        sa.CheckConstraint("status IN ('PROPOSED','ACCEPTED','REJECTED','SUPERSEDED')", name="ck_handoff_status"),
    )
    op.create_index("ix_handoff_run_created", "handoff_proposals", ["run_id", "created_at"])
    op.create_index("ix_handoff_status_created", "handoff_proposals", ["status", "created_at"])


def downgrade() -> None:
    op.drop_index("ix_handoff_status_created", table_name="handoff_proposals")
    op.drop_index("ix_handoff_run_created", table_name="handoff_proposals")
    op.drop_table("handoff_proposals")
