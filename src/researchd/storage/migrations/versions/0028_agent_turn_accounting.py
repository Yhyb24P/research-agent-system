"""Rename generic controller accounting to provider-neutral Agent turns."""

from alembic import op

revision = "0028"
down_revision = "0027"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # SQLite's native rename preserves the table and its inbound foreign keys;
    # batch recreation cannot drop research_runs while child rows exist.
    op.execute("ALTER TABLE research_runs RENAME COLUMN max_cloud_calls TO max_agent_turns")
    op.execute("ALTER TABLE research_runs RENAME COLUMN cloud_calls_used TO agent_turns_used")


def downgrade() -> None:
    op.execute("ALTER TABLE research_runs RENAME COLUMN max_agent_turns TO max_cloud_calls")
    op.execute("ALTER TABLE research_runs RENAME COLUMN agent_turns_used TO cloud_calls_used")
