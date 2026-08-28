"""Create authoritative workspace, execution, artifact, and event records."""

from collections.abc import Sequence
from typing import Any

import sqlalchemy as sa
from alembic import op

from researchd.storage.types import UTCDateTime

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def versioned_columns() -> list[Any]:
    return [
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("created_at", UTCDateTime(), nullable=False),
        sa.Column("updated_at", UTCDateTime(), nullable=False),
    ]


def upgrade() -> None:
    op.create_table("workspaces", sa.Column("workspace_id", sa.String(128), primary_key=True), sa.Column("name", sa.String(256), nullable=False), *versioned_columns())
    op.create_table(
        "research_runs", sa.Column("run_id", sa.String(128), primary_key=True),
        sa.Column("workspace_id", sa.String(128), sa.ForeignKey("workspaces.workspace_id"), nullable=False),
        sa.Column("objective", sa.Text(), nullable=False), sa.Column("state", sa.String(32), nullable=False), *versioned_columns(),
    )
    op.create_index("ix_research_runs_workspace_id", "research_runs", ["workspace_id"])
    op.create_index("ix_research_runs_state", "research_runs", ["state"])
    op.create_table(
        "work_orders", sa.Column("work_order_id", sa.String(128), primary_key=True),
        sa.Column("run_id", sa.String(128), sa.ForeignKey("research_runs.run_id"), nullable=False),
        sa.Column("parent_work_order_id", sa.String(128), sa.ForeignKey("work_orders.work_order_id")),
        sa.Column("objective", sa.Text(), nullable=False), sa.Column("state", sa.String(40), nullable=False),
        sa.Column("idempotency_key", sa.String(256), nullable=False), sa.Column("contract", sa.JSON(), nullable=False),
        *versioned_columns(), sa.UniqueConstraint("idempotency_key", name="uq_work_orders_idempotency_key"),
    )
    op.create_index("ix_work_orders_state", "work_orders", ["state"])
    op.create_index("ix_work_orders_run_state", "work_orders", ["run_id", "state"])
    op.create_table(
        "attempts", sa.Column("attempt_id", sa.String(128), primary_key=True),
        sa.Column("work_order_id", sa.String(128), sa.ForeignKey("work_orders.work_order_id"), nullable=False),
        sa.Column("state", sa.String(32), nullable=False), sa.Column("terminal_at", UTCDateTime()), *versioned_columns(),
    )
    op.create_index("ix_attempts_state", "attempts", ["state"])
    op.create_index("ix_attempts_work_order_state", "attempts", ["work_order_id", "state"])
    op.create_table(
        "jobs", sa.Column("job_id", sa.String(128), primary_key=True),
        sa.Column("attempt_id", sa.String(128), sa.ForeignKey("attempts.attempt_id"), nullable=False),
        sa.Column("operation_id", sa.String(256), nullable=False), sa.Column("state", sa.String(32), nullable=False),
        sa.Column("backend", sa.String(64), nullable=False), sa.Column("native_handle", sa.String(256)),
        *versioned_columns(), sa.UniqueConstraint("operation_id", name="uq_jobs_operation_id"),
    )
    op.create_index("ix_jobs_state", "jobs", ["state"])
    op.create_index("ix_jobs_attempt_state", "jobs", ["attempt_id", "state"])
    op.create_table(
        "artifacts", sa.Column("artifact_id", sa.String(96), primary_key=True),
        sa.Column("sha256", sa.String(64), nullable=False, unique=True), sa.Column("size", sa.Integer(), nullable=False),
        sa.Column("mime_type", sa.String(256), nullable=False), sa.Column("artifact_type", sa.String(64), nullable=False),
        sa.Column("classification", sa.String(32), nullable=False), sa.Column("producer_type", sa.String(32), nullable=False),
        sa.Column("producer_id", sa.String(128), nullable=False),
        sa.Column("attempt_id", sa.String(128), sa.ForeignKey("attempts.attempt_id")),
        sa.Column("relative_source_path", sa.Text()), sa.Column("created_at", UTCDateTime(), nullable=False),
        sa.CheckConstraint("size >= 0", name="ck_artifacts_size_nonnegative"),
    )
    op.create_index("ix_artifacts_attempt_id", "artifacts", ["attempt_id"])
    op.create_index("ix_artifacts_classification", "artifacts", ["classification"])
    op.create_table(
        "audit_events", sa.Column("event_id", sa.String(128), primary_key=True),
        sa.Column("event_type", sa.String(64), nullable=False),
        sa.Column("run_id", sa.String(128), sa.ForeignKey("research_runs.run_id"), nullable=False),
        sa.Column("entity_type", sa.String(64), nullable=False), sa.Column("entity_id", sa.String(128), nullable=False),
        sa.Column("actor_type", sa.String(64), nullable=False), sa.Column("actor_id", sa.String(128), nullable=False),
        sa.Column("timestamp", UTCDateTime(), nullable=False), sa.Column("correlation_id", sa.String(128), nullable=False),
        sa.Column("causation_id", sa.String(128)), sa.Column("metadata", sa.JSON(), nullable=False),
        sa.CheckConstraint("length(event_type) > 0", name="ck_audit_events_event_type_nonempty"),
    )
    op.create_index("ix_audit_events_run_timestamp", "audit_events", ["run_id", "timestamp"])
    op.create_index("ix_audit_events_entity", "audit_events", ["entity_type", "entity_id"])
    op.create_index("ix_audit_events_correlation_id", "audit_events", ["correlation_id"])


def downgrade() -> None:
    op.drop_table("audit_events")
    op.drop_table("artifacts")
    op.drop_table("jobs")
    op.drop_table("attempts")
    op.drop_table("work_orders")
    op.drop_table("research_runs")
    op.drop_table("workspaces")
