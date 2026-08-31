"""Persist trusted one-to-one AgentRuntime launch profiles."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from researchd.storage.types import UTCDateTime


revision = "0022"
down_revision = "0021"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "runtime_sessions",
        sa.Column("launch_profile_sha256", sa.String(64)),
    )
    op.create_table(
        "runtime_launch_profiles",
        sa.Column(
            "runtime_id",
            sa.String(128),
            sa.ForeignKey("agent_runtimes.runtime_id"),
            primary_key=True,
        ),
        sa.Column("launch_mode", sa.String(32), nullable=False),
        sa.Column("configuration_json", sa.JSON(), nullable=False),
        sa.Column("spec_sha256", sa.String(64), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("created_at", UTCDateTime(), nullable=False),
        sa.Column("updated_at", UTCDateTime(), nullable=False),
        sa.CheckConstraint(
            "launch_mode IN ('PROCESS', 'REMOTE_HTTP')",
            name="ck_runtime_launch_profiles_mode",
        ),
    )
    op.create_index(
        "ix_runtime_launch_profiles_enabled",
        "runtime_launch_profiles",
        ["enabled"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_runtime_launch_profiles_enabled",
        table_name="runtime_launch_profiles",
    )
    op.drop_table("runtime_launch_profiles")
    op.drop_column("runtime_sessions", "launch_profile_sha256")
