"""Persist generic daemon command idempotency receipts."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from researchd.storage.types import UTCDateTime


revision = "0021"
down_revision = "0020"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "daemon_commands",
        sa.Column("command_id", sa.String(128), primary_key=True),
        sa.Column("command_type", sa.String(128), nullable=False),
        sa.Column("command_version", sa.Integer(), nullable=False),
        sa.Column("request_sha256", sa.String(64), nullable=False),
        sa.Column("actor_type", sa.String(16), nullable=False),
        sa.Column("actor_id", sa.String(128), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("result_json", sa.JSON()),
        sa.Column("reason_code", sa.String(128)),
        sa.Column("created_at", UTCDateTime(), nullable=False),
        sa.Column("updated_at", UTCDateTime(), nullable=False),
        sa.CheckConstraint(
            "actor_type IN ('HUMAN', 'SYSTEM')",
            name="ck_daemon_commands_actor",
        ),
        sa.CheckConstraint(
            "status IN ('ACCEPTED', 'COMPLETED', 'REJECTED')",
            name="ck_daemon_commands_status",
        ),
    )
    op.create_index(
        "ix_daemon_commands_status",
        "daemon_commands",
        ["status", "created_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_daemon_commands_status", table_name="daemon_commands")
    op.drop_table("daemon_commands")
