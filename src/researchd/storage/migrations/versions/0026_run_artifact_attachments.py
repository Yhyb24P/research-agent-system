"""Associate trusted local file ingress with a durable ResearchRun."""

from alembic import op
import sqlalchemy as sa

from researchd.storage.types import UTCDateTime

revision = "0026"
down_revision = "0025"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "run_artifact_attachments",
        sa.Column("attachment_id", sa.String(96), primary_key=True),
        sa.Column("command_id", sa.String(128), nullable=False, unique=True),
        sa.Column(
            "run_id",
            sa.String(128),
            sa.ForeignKey("research_runs.run_id"),
            nullable=False,
        ),
        sa.Column(
            "artifact_id",
            sa.String(96),
            sa.ForeignKey("artifacts.artifact_id"),
            nullable=False,
        ),
        sa.Column(
            "message_id",
            sa.String(128),
            sa.ForeignKey("collaboration_messages.message_id"),
        ),
        sa.Column(
            "recipient_agent_id",
            sa.String(128),
            sa.ForeignKey("agents.agent_id"),
        ),
        sa.Column("source_name", sa.String(255), nullable=False),
        sa.Column("actor_type", sa.String(32), nullable=False),
        sa.Column("actor_id", sa.String(128), nullable=False),
        sa.Column("created_at", UTCDateTime(), nullable=False),
    )
    op.create_index(
        "ix_run_artifact_attachments_run_created",
        "run_artifact_attachments",
        ["run_id", "created_at"],
    )
    op.create_index(
        "ix_run_artifact_attachments_artifact",
        "run_artifact_attachments",
        ["artifact_id"],
    )
    op.create_index(
        "ix_run_artifact_attachments_recipient",
        "run_artifact_attachments",
        ["recipient_agent_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_run_artifact_attachments_recipient",
        table_name="run_artifact_attachments",
    )
    op.drop_index(
        "ix_run_artifact_attachments_artifact",
        table_name="run_artifact_attachments",
    )
    op.drop_index(
        "ix_run_artifact_attachments_run_created",
        table_name="run_artifact_attachments",
    )
    op.drop_table("run_artifact_attachments")
