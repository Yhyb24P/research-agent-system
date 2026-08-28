"""Persist append-only collaboration messages without control effects."""
from collections.abc import Sequence
import sqlalchemy as sa
from alembic import op
from researchd.storage.types import UTCDateTime

revision = "0012"
down_revision = "0011"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

def upgrade() -> None:
    op.create_table("collaboration_messages", sa.Column("message_id", sa.String(128), primary_key=True), sa.Column("run_id", sa.String(128), sa.ForeignKey("research_runs.run_id"), nullable=False), sa.Column("sender_actor_type", sa.String(32), nullable=False), sa.Column("sender_actor_id", sa.String(128), nullable=False), sa.Column("recipient_agent_id", sa.String(128), sa.ForeignKey("agents.agent_id")), sa.Column("purpose", sa.String(64), nullable=False), sa.Column("body", sa.Text(), nullable=False), sa.Column("metadata", sa.JSON(), nullable=False), sa.Column("created_at", UTCDateTime(), nullable=False))
    op.create_index("ix_collaboration_messages_run_created", "collaboration_messages", ["run_id", "created_at"])

def downgrade() -> None:
    op.drop_index("ix_collaboration_messages_run_created", table_name="collaboration_messages")
    op.drop_table("collaboration_messages")
