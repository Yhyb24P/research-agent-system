"""Add a durable monotonic cursor to the authoritative audit stream."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision = "0016"
down_revision = "0015"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "audit_stream_clock",
        sa.Column("singleton", sa.Integer(), primary_key=True),
        sa.Column("next_seq", sa.Integer(), nullable=False),
        sa.CheckConstraint("singleton = 1", name="ck_audit_stream_clock_singleton"),
        sa.CheckConstraint("next_seq > 0", name="ck_audit_stream_clock_next_seq"),
    )
    with op.batch_alter_table("audit_events") as batch:
        batch.add_column(sa.Column("audit_seq", sa.Integer(), nullable=True))

    connection = op.get_bind()
    event_ids = connection.execute(
        sa.text("SELECT event_id FROM audit_events ORDER BY timestamp, event_id")
    ).scalars().all()
    for sequence, event_id in enumerate(event_ids, start=1):
        connection.execute(
            sa.text("UPDATE audit_events SET audit_seq = :sequence WHERE event_id = :event_id"),
            {"sequence": sequence, "event_id": event_id},
        )
    connection.execute(
        sa.text("INSERT INTO audit_stream_clock (singleton, next_seq) VALUES (1, :next_seq)"),
        {"next_seq": len(event_ids) + 1},
    )
    op.create_index("ix_audit_events_run_seq", "audit_events", ["run_id", "audit_seq"])
    op.create_index("ux_audit_events_audit_seq", "audit_events", ["audit_seq"], unique=True)
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


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS audit_events_assign_seq")
    op.drop_index("ux_audit_events_audit_seq", table_name="audit_events")
    op.drop_index("ix_audit_events_run_seq", table_name="audit_events")
    with op.batch_alter_table("audit_events") as batch:
        batch.drop_column("audit_seq")
    op.drop_table("audit_stream_clock")
