"""Persist Agent profiles and runtimes for the collaboration plane."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from researchd.storage.types import UTCDateTime

revision: str = "0009"
down_revision: str | None = "0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "agents",
        sa.Column("agent_id", sa.String(128), primary_key=True),
        sa.Column("display_name", sa.String(256), nullable=False),
        sa.Column("roles_json", sa.JSON(), nullable=False),
        sa.Column("skills_json", sa.JSON(), nullable=False),
        sa.Column("trust_zone", sa.String(32), nullable=False),
        sa.Column("constraints_json", sa.JSON(), nullable=False),
        sa.Column("labels_json", sa.JSON(), nullable=False),
        sa.Column("max_parallel_delegations", sa.Integer(), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("profile_version", sa.Integer(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("created_at", UTCDateTime(), nullable=False),
        sa.Column("updated_at", UTCDateTime(), nullable=False),
    )
    op.create_index("ix_agents_enabled", "agents", ["enabled"])
    op.create_table(
        "agent_runtimes",
        sa.Column("runtime_id", sa.String(128), primary_key=True),
        sa.Column("agent_id", sa.String(128), sa.ForeignKey("agents.agent_id"), nullable=False),
        sa.Column("adapter_kind", sa.String(32), nullable=False),
        sa.Column("runtime_name", sa.String(256), nullable=False),
        sa.Column("endpoint_ref", sa.String(512)),
        sa.Column("framework", sa.String(128)),
        sa.Column("model_provider", sa.String(128)),
        sa.Column("model_name", sa.String(256)),
        sa.Column("protocols_json", sa.JSON(), nullable=False),
        sa.Column("metadata", sa.JSON(), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("last_heartbeat_at", UTCDateTime()),
        sa.Column("lease_expires_at", UTCDateTime()),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("created_at", UTCDateTime(), nullable=False),
        sa.Column("updated_at", UTCDateTime(), nullable=False),
    )
    op.create_index("ix_agent_runtimes_agent", "agent_runtimes", ["agent_id"])
    op.create_index("ix_agent_runtimes_lease", "agent_runtimes", ["lease_expires_at"])


def downgrade() -> None:
    op.drop_index("ix_agent_runtimes_lease", table_name="agent_runtimes")
    op.drop_index("ix_agent_runtimes_agent", table_name="agent_runtimes")
    op.drop_table("agent_runtimes")
    op.drop_index("ix_agents_enabled", table_name="agents")
    op.drop_table("agents")
