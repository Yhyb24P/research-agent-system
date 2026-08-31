"""Persist supervised runtime sessions and global audit events."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from researchd.storage.types import UTCDateTime


revision = "0020"
down_revision = "0019"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_ACTIVE_STATES = (
    "'STARTING', 'HEALTHY', 'DEGRADED', 'STOPPING', "
    "'RECONCILIATION_REQUIRED'"
)


def _create_audit_sequence_trigger() -> None:
    op.execute(
        """
        CREATE TRIGGER audit_events_assign_seq
        AFTER INSERT ON audit_events
        WHEN NEW.audit_seq IS NULL
        BEGIN
            UPDATE audit_events
               SET audit_seq = (SELECT next_seq FROM audit_stream_clock WHERE singleton = 1)
             WHERE event_id = NEW.event_id;
            UPDATE audit_stream_clock SET next_seq = next_seq + 1 WHERE singleton = 1;
        END
        """
    )


def upgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS audit_events_assign_seq")
    with op.batch_alter_table("audit_events", recreate="always") as batch:
        batch.alter_column(
            "run_id",
            existing_type=sa.String(128),
            nullable=True,
        )
    _create_audit_sequence_trigger()

    op.create_table(
        "runtime_sessions",
        sa.Column("runtime_session_id", sa.String(128), primary_key=True),
        sa.Column(
            "runtime_id",
            sa.String(128),
            sa.ForeignKey("agent_runtimes.runtime_id"),
            nullable=False,
        ),
        sa.Column("launch_mode", sa.String(32), nullable=False),
        sa.Column("supervisor_state", sa.String(32), nullable=False),
        sa.Column("launch_spec_json", sa.JSON(), nullable=False),
        sa.Column("external_identity_json", sa.JSON()),
        sa.Column("started_at", UTCDateTime()),
        sa.Column("last_health_at", UTCDateTime()),
        sa.Column("stopped_at", UTCDateTime()),
        sa.Column("exit_reason", sa.String(256)),
        sa.Column("reattach_state", sa.String(32), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("created_at", UTCDateTime(), nullable=False),
        sa.Column("updated_at", UTCDateTime(), nullable=False),
        sa.CheckConstraint(
            "launch_mode IN ('PROCESS', 'REMOTE_HTTP', 'CLOUD', 'A2A')",
            name="ck_runtime_sessions_launch_mode",
        ),
        sa.CheckConstraint(
            "supervisor_state IN ('STARTING', 'HEALTHY', 'DEGRADED', "
            "'STOPPING', 'STOPPED', 'LOST', 'RECONCILIATION_REQUIRED')",
            name="ck_runtime_sessions_state",
        ),
        sa.CheckConstraint(
            "reattach_state IN ('PENDING', 'ATTACHED', 'NOT_APPLICABLE', "
            "'DETACHED', 'FAILED')",
            name="ck_runtime_sessions_reattach_state",
        ),
    )
    op.create_index(
        "ix_runtime_sessions_runtime",
        "runtime_sessions",
        ["runtime_id", "created_at"],
    )
    op.create_index(
        "ix_runtime_sessions_state",
        "runtime_sessions",
        ["supervisor_state", "updated_at"],
    )
    op.create_index(
        "ux_runtime_sessions_one_active_runtime",
        "runtime_sessions",
        ["runtime_id"],
        unique=True,
        sqlite_where=sa.text(f"supervisor_state IN ({_ACTIVE_STATES})"),
    )
    op.create_table(
        "runtime_session_commands",
        sa.Column("command_id", sa.String(128), primary_key=True),
        sa.Column(
            "runtime_session_id",
            sa.String(128),
            sa.ForeignKey("runtime_sessions.runtime_session_id"),
            nullable=False,
        ),
        sa.Column("command_type", sa.String(16), nullable=False),
        sa.Column("request_sha256", sa.String(64), nullable=False),
        sa.Column("actor_type", sa.String(16), nullable=False),
        sa.Column("actor_id", sa.String(128), nullable=False),
        sa.Column("expected_version", sa.Integer()),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("result_state", sa.String(32)),
        sa.Column("failure_reason", sa.String(128)),
        sa.Column("created_at", UTCDateTime(), nullable=False),
        sa.Column("updated_at", UTCDateTime(), nullable=False),
        sa.CheckConstraint(
            "command_type IN ('START', 'ATTACH', 'STOP')",
            name="ck_runtime_session_commands_type",
        ),
        sa.CheckConstraint(
            "actor_type IN ('HUMAN', 'SYSTEM')",
            name="ck_runtime_session_commands_actor",
        ),
        sa.CheckConstraint(
            "status IN ('ACCEPTED', 'COMPLETED', 'FAILED')",
            name="ck_runtime_session_commands_status",
        ),
    )
    op.create_index(
        "ix_runtime_session_commands_session",
        "runtime_session_commands",
        ["runtime_session_id", "created_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_runtime_session_commands_session",
        table_name="runtime_session_commands",
    )
    op.drop_table("runtime_session_commands")
    op.drop_index(
        "ux_runtime_sessions_one_active_runtime",
        table_name="runtime_sessions",
    )
    op.drop_index("ix_runtime_sessions_state", table_name="runtime_sessions")
    op.drop_index("ix_runtime_sessions_runtime", table_name="runtime_sessions")
    op.drop_table("runtime_sessions")
    op.execute("DROP TRIGGER IF EXISTS audit_events_assign_seq")
    with op.batch_alter_table("audit_events", recreate="always") as batch:
        batch.alter_column(
            "run_id",
            existing_type=sa.String(128),
            nullable=False,
        )
    _create_audit_sequence_trigger()
