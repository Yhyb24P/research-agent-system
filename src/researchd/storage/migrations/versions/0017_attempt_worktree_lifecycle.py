"""Persist the attempt worktree lifecycle before host-side effects."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from researchd.storage.types import UTCDateTime


revision = "0017"
down_revision = "0016"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("attempt_worktrees") as batch:
        batch.add_column(sa.Column("state", sa.String(32), nullable=False, server_default="ACTIVE"))
        batch.add_column(sa.Column("updated_at", UTCDateTime(), nullable=True))
    op.execute("UPDATE attempt_worktrees SET updated_at = created_at WHERE updated_at IS NULL")
    with op.batch_alter_table("attempt_worktrees") as batch:
        batch.alter_column("state", server_default=None)
        batch.alter_column("updated_at", nullable=False)
    op.create_index("ix_attempt_worktrees_state", "attempt_worktrees", ["state"])


def downgrade() -> None:
    op.drop_index("ix_attempt_worktrees_state", table_name="attempt_worktrees")
    with op.batch_alter_table("attempt_worktrees") as batch:
        batch.drop_column("updated_at")
        batch.drop_column("state")
