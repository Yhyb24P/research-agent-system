"""Scope collaboration messages and classify their text payload."""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision = "0014"
down_revision = "0013"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("collaboration_messages") as batch:
        batch.add_column(sa.Column(
            "work_order_id", sa.String(128),
            sa.ForeignKey("work_orders.work_order_id", name="fk_collaboration_messages_work_order"),
        ))
        batch.add_column(sa.Column(
            "classification", sa.String(32), nullable=False,
            server_default="PROJECT_PRIVATE",
        ))


def downgrade() -> None:
    with op.batch_alter_table("collaboration_messages") as batch:
        batch.drop_constraint("fk_collaboration_messages_work_order", type_="foreignkey")
        batch.drop_column("classification")
        batch.drop_column("work_order_id")
