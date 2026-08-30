"""Add typed collaboration message relationships."""

from alembic import op
import sqlalchemy as sa

revision = "0023"
down_revision = "0022"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("collaboration_messages") as batch:
        batch.add_column(sa.Column("delegation_id", sa.String(128), nullable=True))
        batch.add_column(sa.Column("invocation_id", sa.String(128), nullable=True))
        batch.add_column(sa.Column("reply_to_message_id", sa.String(128), nullable=True))
        batch.create_foreign_key("fk_messages_delegation", "delegations", ["delegation_id"], ["delegation_id"])
        batch.create_foreign_key("fk_messages_invocation", "agent_invocations", ["invocation_id"], ["invocation_id"])
        batch.create_foreign_key("fk_messages_reply", "collaboration_messages", ["reply_to_message_id"], ["message_id"])
        batch.create_check_constraint(
            "ck_messages_purpose",
            "purpose IN ('DISCUSSION','STATUS','QUESTION','DIRECTIVE','NOTICE')",
        )


def downgrade() -> None:
    with op.batch_alter_table("collaboration_messages") as batch:
        batch.drop_constraint("ck_messages_purpose", type_="check")
        batch.drop_constraint("fk_messages_reply", type_="foreignkey")
        batch.drop_constraint("fk_messages_invocation", type_="foreignkey")
        batch.drop_constraint("fk_messages_delegation", type_="foreignkey")
        batch.drop_column("reply_to_message_id")
        batch.drop_column("invocation_id")
        batch.drop_column("delegation_id")
