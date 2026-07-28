"""add on-prem deployment release tables

Revision ID: d4e5f6a7b8c9
Revises: c7a8b9d0e1f2
Create Date: 2026-07-23 00:00:00.000000

Phase: EXPAND
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from langflow.utils import migration

revision: str = "d4e5f6a7b8c9"
down_revision: str | None = "c7a8b9d0e1f2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _json_type() -> sa.JSON:
    return sa.JSON().with_variant(sa.dialects.postgresql.JSONB(), "postgresql")


def upgrade() -> None:
    conn = op.get_bind()
    if conn.dialect.name == "postgresql":
        op.execute("ALTER TYPE deployment_provider_key_enum ADD VALUE IF NOT EXISTS 'unnest-on-prem'")

    if not migration.table_exists("deployment_release", conn):
        op.create_table(
            "deployment_release",
            sa.Column("id", sa.Uuid(), nullable=False),
            sa.Column("user_id", sa.Uuid(), nullable=False),
            sa.Column("version", sa.String(), nullable=False),
            sa.Column("agent_flow_version_id", sa.Uuid(), nullable=False),
            sa.Column("ingestion_flow_version_id", sa.Uuid(), nullable=False),
            sa.Column("subflow_version_ids", _json_type(), nullable=False),
            sa.Column("config", _json_type(), nullable=False),
            sa.Column("manifest", _json_type(), nullable=False),
            sa.Column("api_version", sa.String(), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
            sa.ForeignKeyConstraint(["agent_flow_version_id"], ["flow_version.id"], ondelete="RESTRICT"),
            sa.ForeignKeyConstraint(["ingestion_flow_version_id"], ["flow_version.id"], ondelete="RESTRICT"),
            sa.ForeignKeyConstraint(["user_id"], ["user.id"], ondelete="CASCADE"),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("user_id", "version", name="uq_deployment_release_user_version"),
        )
        op.create_index("ix_deployment_release_user_id", "deployment_release", ["user_id"])
        op.create_index("ix_deployment_release_version", "deployment_release", ["version"])
        op.create_index(
            "ix_deployment_release_agent_flow_version_id", "deployment_release", ["agent_flow_version_id"]
        )
        op.create_index(
            "ix_deployment_release_ingestion_flow_version_id", "deployment_release", ["ingestion_flow_version_id"]
        )

    if not migration.table_exists("deployment_build", conn):
        op.create_table(
            "deployment_build",
            sa.Column("id", sa.Uuid(), nullable=False),
            sa.Column("release_id", sa.Uuid(), nullable=False),
            sa.Column("architecture", sa.String(), nullable=False),
            sa.Column("status", sa.String(), nullable=False),
            sa.Column("logs", sa.Text(), nullable=False),
            sa.Column("scan_report", _json_type(), nullable=False),
            sa.Column("critical_override_reason", sa.String(), nullable=True),
            sa.Column("overridden_by_user_id", sa.Uuid(), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
            sa.ForeignKeyConstraint(["overridden_by_user_id"], ["user.id"], ondelete="SET NULL"),
            sa.ForeignKeyConstraint(["release_id"], ["deployment_release.id"], ondelete="CASCADE"),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("release_id", "architecture", name="uq_deployment_build_release_arch"),
        )
        op.create_index("ix_deployment_build_release_id", "deployment_build", ["release_id"])
        op.create_index("ix_deployment_build_architecture", "deployment_build", ["architecture"])
        op.create_index("ix_deployment_build_status", "deployment_build", ["status"])

    if not migration.table_exists("deployment_artifact", conn):
        op.create_table(
            "deployment_artifact",
            sa.Column("id", sa.Uuid(), nullable=False),
            sa.Column("build_id", sa.Uuid(), nullable=False),
            sa.Column("artifact_type", sa.String(), nullable=False),
            sa.Column("location", sa.String(), nullable=False),
            sa.Column("digest", sa.String(), nullable=False),
            sa.Column("size_bytes", sa.BigInteger(), server_default="0", nullable=False),
            sa.Column("checksums", _json_type(), nullable=False),
            sa.Column("sbom", _json_type(), nullable=False),
            sa.Column("signature", sa.Text(), nullable=True),
            sa.Column("pinned", sa.Boolean(), nullable=False),
            sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
            sa.ForeignKeyConstraint(["build_id"], ["deployment_build.id"], ondelete="CASCADE"),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("build_id", "artifact_type", name="uq_deployment_artifact_build_type"),
        )
        op.create_index("ix_deployment_artifact_build_id", "deployment_artifact", ["build_id"])
        op.create_index("ix_deployment_artifact_digest", "deployment_artifact", ["digest"])
        op.create_index("ix_deployment_artifact_expires_at", "deployment_artifact", ["expires_at"])

    if not migration.table_exists("deployment_acceptance_test", conn):
        op.create_table(
            "deployment_acceptance_test",
            sa.Column("id", sa.Uuid(), nullable=False),
            sa.Column("release_id", sa.Uuid(), nullable=False),
            sa.Column("name", sa.String(), nullable=False),
            sa.Column("required", sa.Boolean(), nullable=False),
            sa.Column("request", _json_type(), nullable=False),
            sa.Column("expected", _json_type(), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
            sa.ForeignKeyConstraint(["release_id"], ["deployment_release.id"], ondelete="CASCADE"),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("release_id", "name", name="uq_deployment_acceptance_test_release_name"),
        )
        op.create_index(
            "ix_deployment_acceptance_test_release_id", "deployment_acceptance_test", ["release_id"]
        )


def downgrade() -> None:
    conn = op.get_bind()
    for table in (
        "deployment_acceptance_test",
        "deployment_artifact",
        "deployment_build",
        "deployment_release",
    ):
        if migration.table_exists(table, conn):
            op.drop_table(table)

    # PostgreSQL cannot remove one enum value without rebuilding the type.
    # Keeping the unused value is safe and preserves downgrade compatibility.
