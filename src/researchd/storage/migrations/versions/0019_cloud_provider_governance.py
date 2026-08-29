"""Bind each cloud interaction to a non-secret provider configuration snapshot."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from researchd.storage.types import UTCDateTime

revision = "0019"
down_revision = "0018"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "cloud_interaction_governance",
        sa.Column(
            "interaction_id", sa.String(128),
            sa.ForeignKey("agent_interactions.interaction_id"), primary_key=True,
        ),
        sa.Column("provider_configuration_id", sa.String(128), nullable=False),
        sa.Column("provider_configuration_sha256", sa.String(64), nullable=False),
        sa.Column("provider_configuration_json", sa.JSON(), nullable=False),
        sa.Column("created_at", UTCDateTime(), nullable=False),
    )
    op.create_index(
        "ix_cloud_governance_configuration",
        "cloud_interaction_governance",
        ["provider_configuration_id", "created_at"],
    )
    op.execute("""
        CREATE TRIGGER cloud_interaction_governance_immutable
        BEFORE UPDATE ON cloud_interaction_governance
        BEGIN SELECT RAISE(ABORT, 'provider configuration snapshot is immutable'); END
    """)


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS cloud_interaction_governance_immutable")
    op.drop_index("ix_cloud_governance_configuration", table_name="cloud_interaction_governance")
    op.drop_table("cloud_interaction_governance")
