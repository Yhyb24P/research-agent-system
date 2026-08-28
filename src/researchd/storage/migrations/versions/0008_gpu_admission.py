"""Persist exclusive GPU admission leases."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from researchd.storage.types import UTCDateTime

revision: str = "0008"
down_revision: str | None = "0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "gpu_leases",
        sa.Column("lease_id", sa.String(128), primary_key=True),
        sa.Column("job_id", sa.String(128), sa.ForeignKey("jobs.job_id"), nullable=False),
        sa.Column("device_id", sa.String(64), nullable=False),
        sa.Column("state", sa.String(32), nullable=False),
        sa.Column("created_at", UTCDateTime(), nullable=False),
        sa.Column("released_at", UTCDateTime()),
        sa.UniqueConstraint("job_id", "device_id", name="uq_gpu_leases_job_device"),
    )
    op.create_index("ix_gpu_leases_device_state", "gpu_leases", ["device_id", "state"])
    op.create_index("ix_gpu_leases_job_state", "gpu_leases", ["job_id", "state"])


def downgrade() -> None:
    op.drop_table("gpu_leases")
