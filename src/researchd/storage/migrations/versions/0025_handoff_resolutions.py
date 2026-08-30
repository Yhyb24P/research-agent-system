"""Persist controller-owned handoff resolution results."""

from alembic import op
import sqlalchemy as sa

revision = "0025"
down_revision = "0024"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("handoff_proposals", sa.Column("resolution_entity_type", sa.String(32)))
    op.add_column("handoff_proposals", sa.Column("resolution_entity_id", sa.String(128)))


def downgrade() -> None:
    op.drop_column("handoff_proposals", "resolution_entity_id")
    op.drop_column("handoff_proposals", "resolution_entity_type")
