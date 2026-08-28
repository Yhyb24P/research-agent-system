"""Add durable executor dispatch and capability-step idempotency records."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from researchd.storage.types import UTCDateTime

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "attempt_worktrees", sa.Column("attempt_id", sa.String(128), sa.ForeignKey("attempts.attempt_id"), primary_key=True),
        sa.Column("repository_id", sa.String(128), nullable=False), sa.Column("base_commit", sa.String(64), nullable=False),
        sa.Column("worktree_path", sa.Text(), nullable=False), sa.Column("environment_digest", sa.String(64), nullable=False),
        sa.Column("sandbox_backend", sa.String(64), nullable=False), sa.Column("created_at", UTCDateTime(), nullable=False),
    )
    op.create_table(
        "execution_steps", sa.Column("step_id", sa.String(128), primary_key=True),
        sa.Column("attempt_id", sa.String(128), sa.ForeignKey("attempts.attempt_id"), nullable=False),
        sa.Column("capability", sa.String(128), nullable=False), sa.Column("parameters_sha256", sa.String(64), nullable=False),
        sa.Column("status", sa.String(32), nullable=False), sa.Column("result_json", sa.JSON()),
        sa.Column("created_at", UTCDateTime(), nullable=False), sa.Column("updated_at", UTCDateTime(), nullable=False),
    )
    op.create_index("ix_execution_steps_attempt_status", "execution_steps", ["attempt_id", "status"])
    op.create_table(
        "executor_dispatches", sa.Column("attempt_id", sa.String(128), sa.ForeignKey("attempts.attempt_id"), primary_key=True),
        sa.Column("status", sa.String(32), nullable=False), sa.Column("result_json", sa.JSON()),
        sa.Column("created_at", UTCDateTime(), nullable=False), sa.Column("updated_at", UTCDateTime(), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("attempt_worktrees")
    op.drop_table("executor_dispatches")
    op.drop_table("execution_steps")
