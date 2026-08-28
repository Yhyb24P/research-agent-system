"""Store protocol adapter mappings without changing core workflow identities."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0007"
down_revision: str | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("agent_interactions", sa.Column("attempt_id", sa.String(128)))
    op.add_column("agent_interactions", sa.Column("remote_agent_id", sa.String(256)))
    op.add_column("agent_interactions", sa.Column("a2a_context_id", sa.String(256)))
    op.add_column("agent_interactions", sa.Column("a2a_task_id", sa.String(256)))
    op.create_index("ix_agent_interactions_a2a_task", "agent_interactions", ["a2a_task_id"])


def downgrade() -> None:
    op.drop_index("ix_agent_interactions_a2a_task", table_name="agent_interactions")
    op.drop_column("agent_interactions", "a2a_task_id")
    op.drop_column("agent_interactions", "a2a_context_id")
    op.drop_column("agent_interactions", "remote_agent_id")
    op.drop_column("agent_interactions", "attempt_id")
