"""Persist collaboration delegations and structured agent invocations."""
from collections.abc import Sequence
import sqlalchemy as sa
from alembic import op
from researchd.storage.types import UTCDateTime

revision = "0010"
down_revision = "0009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "delegations",
        sa.Column("delegation_id", sa.String(128), primary_key=True),
        sa.Column("run_id", sa.String(128), sa.ForeignKey("research_runs.run_id"), nullable=False),
        sa.Column("work_order_id", sa.String(128), sa.ForeignKey("work_orders.work_order_id")),
        sa.Column("purpose", sa.String(32), nullable=False),
        sa.Column("required_roles_json", sa.JSON(), nullable=False),
        sa.Column("required_skills_json", sa.JSON(), nullable=False),
        sa.Column("required_trust_zones_json", sa.JSON(), nullable=False),
        sa.Column("assigned_agent_id", sa.String(128), sa.ForeignKey("agents.agent_id")),
        sa.Column("assigned_runtime_id", sa.String(128), sa.ForeignKey("agent_runtimes.runtime_id")),
        sa.Column("agent_profile_version", sa.Integer()),
        sa.Column("agent_snapshot_json", sa.JSON()),
        sa.Column("assignment_sha256", sa.String(64)),
        sa.Column("state", sa.String(32), nullable=False),
        sa.Column("idempotency_key", sa.String(256), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("created_at", UTCDateTime(), nullable=False),
        sa.Column("updated_at", UTCDateTime(), nullable=False),
        sa.Column("completed_at", UTCDateTime()),
    )
    op.create_index("ix_delegations_run_state", "delegations", ["run_id", "state"])
    op.create_index("ix_delegations_idempotency", "delegations", ["idempotency_key"], unique=True)
    op.create_table(
        "agent_invocations",
        sa.Column("invocation_id", sa.String(128), primary_key=True),
        sa.Column("delegation_id", sa.String(128), sa.ForeignKey("delegations.delegation_id"), nullable=False),
        sa.Column("run_id", sa.String(128), sa.ForeignKey("research_runs.run_id"), nullable=False),
        sa.Column("work_order_id", sa.String(128), sa.ForeignKey("work_orders.work_order_id")),
        sa.Column("attempt_id", sa.String(128), sa.ForeignKey("attempts.attempt_id")),
        sa.Column("agent_id", sa.String(128), sa.ForeignKey("agents.agent_id"), nullable=False),
        sa.Column("runtime_id", sa.String(128), sa.ForeignKey("agent_runtimes.runtime_id"), nullable=False),
        sa.Column("purpose", sa.String(32), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("input_sha256", sa.String(64), nullable=False),
        sa.Column("output_type", sa.String(128)),
        sa.Column("output_json", sa.JSON()),
        sa.Column("reason_code", sa.String(128)),
        sa.Column("created_at", UTCDateTime(), nullable=False),
        sa.Column("completed_at", UTCDateTime()),
    )
    op.create_index("ix_agent_invocations_delegation", "agent_invocations", ["delegation_id"])
    op.create_index("ix_agent_invocations_status", "agent_invocations", ["status"])
    with op.batch_alter_table("attempts") as batch:
        batch.add_column(sa.Column("delegation_id", sa.String(128), sa.ForeignKey("delegations.delegation_id", name="fk_attempts_delegation")))
    with op.batch_alter_table("agent_interactions") as batch:
        batch.add_column(sa.Column("invocation_id", sa.String(128), sa.ForeignKey("agent_invocations.invocation_id", name="fk_interactions_invocation")))


def downgrade() -> None:
    with op.batch_alter_table("agent_interactions") as batch:
        batch.drop_constraint("fk_interactions_invocation", type_="foreignkey")
        batch.drop_column("invocation_id")
    with op.batch_alter_table("attempts") as batch:
        batch.drop_constraint("fk_attempts_delegation", type_="foreignkey")
        batch.drop_column("delegation_id")
    op.drop_index("ix_agent_invocations_status", table_name="agent_invocations")
    op.drop_index("ix_agent_invocations_delegation", table_name="agent_invocations")
    op.drop_table("agent_invocations")
    op.drop_index("ix_delegations_idempotency", table_name="delegations")
    op.drop_index("ix_delegations_run_state", table_name="delegations")
    op.drop_table("delegations")
