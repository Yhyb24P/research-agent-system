"""Persist provider-neutral Agent invocation failure categories."""

from alembic import op
import sqlalchemy as sa

revision = "0027"
down_revision = "0026"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "agent_invocations",
        sa.Column("failure_category", sa.String(64)),
    )


def downgrade() -> None:
    op.drop_column("agent_invocations", "failure_category")
