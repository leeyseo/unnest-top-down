"""add runtime document lifecycle

Revision ID: a7b8c9d0e1f2
Revises: f6a7b8c9d0e1
Create Date: 2026-07-23 00:00:00.000000

Phase: EXPAND
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from langflow.utils import migration

revision: str = "a7b8c9d0e1f2"
down_revision: str | None = "f6a7b8c9d0e1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _json_type() -> sa.JSON:
    return sa.JSON().with_variant(sa.dialects.postgresql.JSONB(), "postgresql")


def upgrade() -> None:
    conn = op.get_bind()
    if not migration.table_exists("runtime_document", conn):
        op.create_table(
            "runtime_document",
            sa.Column("id", sa.Uuid(), nullable=False),
            sa.Column("user_id", sa.Uuid(), nullable=False),
            sa.Column("knowledge_base_id", sa.Uuid(), nullable=False),
            sa.Column("name", sa.String(), nullable=False),
            sa.Column("status", sa.String(), nullable=False),
            sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("purge_after", sa.DateTime(timezone=True), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
            sa.CheckConstraint(
                "status IN ('pending', 'active', 'failed', 'trash')",
                name="ck_runtime_document_status",
            ),
            sa.ForeignKeyConstraint(["knowledge_base_id"], ["knowledge_base.id"], ondelete="CASCADE"),
            sa.ForeignKeyConstraint(["user_id"], ["user.id"], ondelete="CASCADE"),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("knowledge_base_id", "name", name="uq_runtime_document_kb_name"),
        )
        op.create_index("ix_runtime_document_user_id", "runtime_document", ["user_id"])
        op.create_index("ix_runtime_document_knowledge_base_id", "runtime_document", ["knowledge_base_id"])
        op.create_index("ix_runtime_document_status", "runtime_document", ["status"])
        op.create_index("ix_runtime_document_purge_after", "runtime_document", ["purge_after"])

    if not migration.table_exists("document_version", conn):
        op.create_table(
            "document_version",
            sa.Column("id", sa.Uuid(), nullable=False),
            sa.Column("document_id", sa.Uuid(), nullable=False),
            sa.Column("version_number", sa.Integer(), nullable=False),
            sa.Column("checksum", sa.String(), nullable=False),
            sa.Column("mime_type", sa.String(), nullable=False),
            sa.Column("size_bytes", sa.BigInteger(), nullable=False),
            sa.Column("storage_path", sa.String(), nullable=False),
            sa.Column("document_metadata", _json_type(), nullable=False),
            sa.Column("status", sa.String(), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
            sa.CheckConstraint(
                "status IN ('pending', 'active', 'superseded', 'failed')",
                name="ck_document_version_status",
            ),
            sa.ForeignKeyConstraint(["document_id"], ["runtime_document.id"], ondelete="CASCADE"),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("document_id", "version_number", name="uq_document_version_number"),
        )
        op.create_index("ix_document_version_document_id", "document_version", ["document_id"])
        op.create_index("ix_document_version_checksum", "document_version", ["checksum"])
        op.create_index("ix_document_version_status", "document_version", ["status"])

    if not migration.table_exists("index_generation", conn):
        op.create_table(
            "index_generation",
            sa.Column("id", sa.Uuid(), nullable=False),
            sa.Column("knowledge_base_id", sa.Uuid(), nullable=False),
            sa.Column("fingerprint", sa.String(), nullable=False),
            sa.Column("status", sa.String(), nullable=False),
            sa.Column("is_active", sa.Boolean(), nullable=False),
            sa.Column("backend_reference", _json_type(), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
            sa.Column("activated_at", sa.DateTime(timezone=True), nullable=True),
            sa.CheckConstraint(
                "status IN ('building', 'ready', 'active', 'failed', 'retired')",
                name="ck_index_generation_status",
            ),
            sa.ForeignKeyConstraint(["knowledge_base_id"], ["knowledge_base.id"], ondelete="CASCADE"),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint(
                "knowledge_base_id",
                "fingerprint",
                name="uq_index_generation_kb_fingerprint",
            ),
        )
        op.create_index("ix_index_generation_knowledge_base_id", "index_generation", ["knowledge_base_id"])
        op.create_index("ix_index_generation_status", "index_generation", ["status"])
        op.create_index("ix_index_generation_is_active", "index_generation", ["is_active"])


def downgrade() -> None:
    conn = op.get_bind()
    for table in ("index_generation", "document_version", "runtime_document"):
        if migration.table_exists(table, conn):
            op.drop_table(table)
