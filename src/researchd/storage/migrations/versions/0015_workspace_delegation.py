"""Add the bounded remote Workspace Delegation Plane."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from researchd.storage.types import UTCDateTime


revision = "0015"
down_revision = "0014"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "workspace_grants",
        sa.Column("workspace_grant_id", sa.String(128), primary_key=True),
        sa.Column("delegation_id", sa.String(128), sa.ForeignKey("delegations.delegation_id"), nullable=False, unique=True),
        sa.Column("source_workspace_id", sa.String(128), sa.ForeignKey("workspaces.workspace_id"), nullable=False),
        sa.Column("source_revision", sa.String(256)),
        sa.Column("source_manifest_sha256", sa.String(64)),
        sa.Column("access_mode", sa.String(32), nullable=False),
        sa.Column("allowed_paths", sa.JSON(), nullable=False),
        sa.Column("excluded_paths", sa.JSON(), nullable=False),
        sa.Column("classification_ceiling", sa.String(32), nullable=False),
        sa.Column("max_total_bytes", sa.Integer(), nullable=False),
        sa.Column("max_file_count", sa.Integer(), nullable=False),
        sa.Column("max_single_file_bytes", sa.Integer(), nullable=False),
        sa.Column("lease_seconds", sa.Integer(), nullable=False),
        sa.Column("lease_started_at", UTCDateTime()),
        sa.Column("lease_expires_at", UTCDateTime()),
        sa.Column("renewal_policy", sa.String(32), nullable=False),
        sa.Column("transport_kind", sa.String(32), nullable=False),
        sa.Column("reconciliation_mode", sa.String(32), nullable=False),
        sa.Column("state", sa.String(32), nullable=False),
        sa.Column("cleanup_state", sa.String(32), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("created_at", UTCDateTime(), nullable=False),
        sa.Column("updated_at", UTCDateTime(), nullable=False),
        sa.CheckConstraint("max_total_bytes > 0", name="ck_workspace_grants_total_bytes"),
        sa.CheckConstraint("max_file_count > 0", name="ck_workspace_grants_file_count"),
        sa.CheckConstraint("max_single_file_bytes > 0", name="ck_workspace_grants_single_file"),
        sa.CheckConstraint("lease_seconds > 0 AND lease_seconds <= 86400", name="ck_workspace_grants_lease_seconds"),
    )
    op.create_index("ix_workspace_grants_state_lease", "workspace_grants", ["state", "lease_expires_at"])
    op.create_table(
        "workspace_snapshots",
        sa.Column("workspace_snapshot_id", sa.String(128), primary_key=True),
        sa.Column("workspace_grant_id", sa.String(128), sa.ForeignKey("workspace_grants.workspace_grant_id"), nullable=False),
        sa.Column("source_revision", sa.String(256)),
        sa.Column("manifest_sha256", sa.String(64), nullable=False),
        sa.Column("manifest", sa.JSON(), nullable=False),
        sa.Column("total_bytes", sa.Integer(), nullable=False),
        sa.Column("file_count", sa.Integer(), nullable=False),
        sa.Column("created_at", UTCDateTime(), nullable=False),
    )
    op.create_index("ix_workspace_snapshots_grant", "workspace_snapshots", ["workspace_grant_id", "created_at"])
    op.create_table(
        "workspace_transports",
        sa.Column("workspace_transport_id", sa.String(128), primary_key=True),
        sa.Column("workspace_grant_id", sa.String(128), sa.ForeignKey("workspace_grants.workspace_grant_id"), nullable=False),
        sa.Column("transport_kind", sa.String(32), nullable=False),
        sa.Column("transport_handle", sa.JSON(), nullable=False),
        sa.Column("remote_workspace_handle", sa.String(1024), nullable=False),
        sa.Column("state", sa.String(32), nullable=False),
        sa.Column("created_at", UTCDateTime(), nullable=False),
        sa.Column("closed_at", UTCDateTime()),
    )
    op.create_index("ix_workspace_transports_grant", "workspace_transports", ["workspace_grant_id", "created_at"])
    op.create_table(
        "workspace_reconciliations",
        sa.Column("workspace_reconciliation_id", sa.String(128), primary_key=True),
        sa.Column("workspace_grant_id", sa.String(128), sa.ForeignKey("workspace_grants.workspace_grant_id"), nullable=False),
        sa.Column("workspace_transport_id", sa.String(128), sa.ForeignKey("workspace_transports.workspace_transport_id"), nullable=False),
        sa.Column("base_manifest_sha256", sa.String(64), nullable=False),
        sa.Column("result_manifest_sha256", sa.String(64), nullable=False),
        sa.Column("result_artifact_id", sa.String(96), sa.ForeignKey("artifacts.artifact_id"), nullable=False),
        sa.Column("state", sa.String(32), nullable=False),
        sa.Column("summary", sa.Text(), nullable=False),
        sa.Column("created_at", UTCDateTime(), nullable=False),
        sa.Column("completed_at", UTCDateTime()),
    )
    op.create_index("ix_workspace_reconciliations_grant", "workspace_reconciliations", ["workspace_grant_id", "created_at"])
    with op.batch_alter_table("agent_invocations") as batch:
        batch.add_column(sa.Column(
            "workspace_grant_id",
            sa.String(128),
            sa.ForeignKey("workspace_grants.workspace_grant_id", name="fk_agent_invocations_workspace_grant"),
        ))


def downgrade() -> None:
    with op.batch_alter_table("agent_invocations") as batch:
        batch.drop_constraint("fk_agent_invocations_workspace_grant", type_="foreignkey")
        batch.drop_column("workspace_grant_id")
    op.drop_index("ix_workspace_reconciliations_grant", table_name="workspace_reconciliations")
    op.drop_table("workspace_reconciliations")
    op.drop_index("ix_workspace_transports_grant", table_name="workspace_transports")
    op.drop_table("workspace_transports")
    op.drop_index("ix_workspace_snapshots_grant", table_name="workspace_snapshots")
    op.drop_table("workspace_snapshots")
    op.drop_index("ix_workspace_grants_state_lease", table_name="workspace_grants")
    op.drop_table("workspace_grants")
