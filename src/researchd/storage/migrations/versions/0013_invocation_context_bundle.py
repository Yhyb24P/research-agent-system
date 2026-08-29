"""Persist target-agent context snapshots on canonical invocations."""
from collections.abc import Sequence
import sqlalchemy as sa
from alembic import op

revision = "0013"
down_revision = "0012"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("agent_invocations", sa.Column("context_bundle_sha256", sa.String(64), nullable=True))
    op.add_column("agent_invocations", sa.Column("context_bundle_json", sa.JSON(), nullable=True))


def downgrade() -> None:
    op.drop_column("agent_invocations", "context_bundle_json")
    op.drop_column("agent_invocations", "context_bundle_sha256")
