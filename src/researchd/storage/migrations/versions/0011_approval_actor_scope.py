"""Scope approval requests to runs and preserve requester actor provenance."""
from collections.abc import Sequence
import sqlalchemy as sa
from alembic import op

revision = "0011"
down_revision = "0010"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("approval_requests") as batch:
        batch.add_column(sa.Column("run_id", sa.String(128), sa.ForeignKey("research_runs.run_id", name="fk_approval_requests_run")))
        batch.add_column(sa.Column("work_order_id", sa.String(128), sa.ForeignKey("work_orders.work_order_id", name="fk_approval_requests_work_order")))
        batch.add_column(sa.Column("requester_actor_type", sa.String(32), nullable=False, server_default="legacy"))
        batch.add_column(sa.Column("requester_actor_id", sa.String(128)))


def downgrade() -> None:
    with op.batch_alter_table("approval_requests") as batch:
        batch.drop_constraint("fk_approval_requests_work_order", type_="foreignkey")
        batch.drop_constraint("fk_approval_requests_run", type_="foreignkey")
        batch.drop_column("requester_actor_id")
        batch.drop_column("requester_actor_type")
        batch.drop_column("work_order_id")
        batch.drop_column("run_id")
