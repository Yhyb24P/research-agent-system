"""Persist exclusive runtime leases and external invocation lifecycle."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from researchd.storage.types import UTCDateTime


revision = "0018"
down_revision = "0017"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("agent_runtimes") as batch:
        batch.add_column(sa.Column("runtime_lease_id", sa.String(128)))
        batch.add_column(sa.Column("lease_owner_id", sa.String(128)))
        batch.add_column(sa.Column("lease_acquired_at", UTCDateTime()))
    op.create_table(
        "agent_runtime_lease_events",
        sa.Column("event_id", sa.String(128), primary_key=True),
        sa.Column("runtime_id", sa.String(128), sa.ForeignKey("agent_runtimes.runtime_id"), nullable=False),
        sa.Column("lease_id", sa.String(128)),
        sa.Column("owner_id", sa.String(128), nullable=False),
        sa.Column("event_type", sa.String(32), nullable=False),
        sa.Column("observed_at", UTCDateTime(), nullable=False),
    )
    op.create_index("ix_runtime_lease_events_runtime", "agent_runtime_lease_events", ["runtime_id", "observed_at"])
    op.create_index("ix_runtime_lease_events_type", "agent_runtime_lease_events", ["event_type"])
    with op.batch_alter_table("agent_invocations") as batch:
        batch.add_column(sa.Column("runtime_lease_id", sa.String(128)))
        batch.add_column(sa.Column("external_invocation_id", sa.String(256)))
        batch.add_column(sa.Column("dispatched_at", UTCDateTime()))
        batch.add_column(sa.Column("external_started_at", UTCDateTime()))
        batch.add_column(sa.Column("reconciliation_requested_at", UTCDateTime()))
        batch.add_column(sa.Column("last_reconciled_at", UTCDateTime()))
        batch.add_column(sa.Column("cancel_requested_at", UTCDateTime()))
        batch.add_column(sa.Column("deadline_at", UTCDateTime()))
    op.create_index(
        "ux_agent_invocations_runtime_external",
        "agent_invocations",
        ["runtime_id", "external_invocation_id"],
        unique=True,
    )


def downgrade() -> None:
    op.drop_index("ux_agent_invocations_runtime_external", table_name="agent_invocations")
    with op.batch_alter_table("agent_invocations") as batch:
        batch.drop_column("deadline_at")
        batch.drop_column("cancel_requested_at")
        batch.drop_column("last_reconciled_at")
        batch.drop_column("reconciliation_requested_at")
        batch.drop_column("external_started_at")
        batch.drop_column("dispatched_at")
        batch.drop_column("external_invocation_id")
        batch.drop_column("runtime_lease_id")
    op.drop_index("ix_runtime_lease_events_type", table_name="agent_runtime_lease_events")
    op.drop_index("ix_runtime_lease_events_runtime", table_name="agent_runtime_lease_events")
    op.drop_table("agent_runtime_lease_events")
    with op.batch_alter_table("agent_runtimes") as batch:
        batch.drop_column("lease_acquired_at")
        batch.drop_column("lease_owner_id")
        batch.drop_column("runtime_lease_id")
