"""add runtime audit chain

Revision ID: c9d0e1f2a3b4
Revises: b8c9d0e1f2a3
Create Date: 2026-07-23 00:00:00.000000

Phase: EXPAND
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from langflow.utils import migration

revision: str = "c9d0e1f2a3b4"
down_revision: str | None = "b8c9d0e1f2a3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    conn = op.get_bind()
    if not migration.table_exists("runtime_audit_event", conn):
        op.create_table(
            "runtime_audit_event",
            sa.Column("id", sa.Uuid(), nullable=False),
            sa.Column("sequence", sa.BigInteger(), nullable=False),
            sa.Column("previous_hash", sa.String(length=64), nullable=False),
            sa.Column("event_hash", sa.String(length=64), nullable=False),
            sa.Column("event_type", sa.String(), nullable=False),
            sa.Column("actor_user_id", sa.Uuid(), nullable=True),
            sa.Column("resource_type", sa.String(), nullable=True),
            sa.Column("resource_id", sa.String(), nullable=True),
            sa.Column("details", sa.JSON(), nullable=False),
            sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("event_hash", name="uq_runtime_audit_event_hash"),
            sa.UniqueConstraint("sequence", name="uq_runtime_audit_event_sequence"),
        )
        op.create_index("ix_runtime_audit_event_actor", "runtime_audit_event", ["actor_user_id", "occurred_at"])
        op.create_index("ix_runtime_audit_event_event_type", "runtime_audit_event", ["event_type"])
        op.create_index("ix_runtime_audit_event_occurred_at", "runtime_audit_event", ["occurred_at"])

    if not migration.table_exists("runtime_audit_checkpoint", conn):
        op.create_table(
            "runtime_audit_checkpoint",
            sa.Column("id", sa.Uuid(), nullable=False),
            sa.Column("event_sequence", sa.BigInteger(), nullable=False),
            sa.Column("event_hash", sa.String(length=64), nullable=False),
            sa.Column("signature", sa.Text(), nullable=False),
            sa.Column("public_key", sa.Text(), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.PrimaryKeyConstraint("id"),
        )
        op.create_index(
            "ix_runtime_audit_checkpoint_sequence",
            "runtime_audit_checkpoint",
            ["event_sequence"],
        )


def downgrade() -> None:
    conn = op.get_bind()
    if migration.table_exists("runtime_audit_checkpoint", conn):
        op.drop_index("ix_runtime_audit_checkpoint_sequence", table_name="runtime_audit_checkpoint")
        op.drop_table("runtime_audit_checkpoint")
    if migration.table_exists("runtime_audit_event", conn):
        op.drop_index("ix_runtime_audit_event_occurred_at", table_name="runtime_audit_event")
        op.drop_index("ix_runtime_audit_event_event_type", table_name="runtime_audit_event")
        op.drop_index("ix_runtime_audit_event_actor", table_name="runtime_audit_event")
        op.drop_table("runtime_audit_event")
