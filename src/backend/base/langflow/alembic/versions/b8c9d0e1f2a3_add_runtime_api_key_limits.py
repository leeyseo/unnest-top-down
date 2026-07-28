"""add runtime api key limits

Revision ID: b8c9d0e1f2a3
Revises: a7b8c9d0e1f2
Create Date: 2026-07-23 00:00:00.000000

Phase: EXPAND
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from langflow.utils import migration

revision: str = "b8c9d0e1f2a3"
down_revision: str | None = "a7b8c9d0e1f2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLE_NAME = "apikey"
LIMITS = {
    "rate_limit_per_minute": 60,
    "max_concurrent_runs": 4,
    "max_request_bytes": 10 * 1024 * 1024,
    "daily_quota": 10_000,
}


def upgrade() -> None:
    conn = op.get_bind()
    if not migration.table_exists(TABLE_NAME, conn):
        return
    with op.batch_alter_table(TABLE_NAME, schema=None) as batch_op:
        for name, default in LIMITS.items():
            if not migration.column_exists(TABLE_NAME, name, conn):
                batch_op.add_column(
                    sa.Column(name, sa.Integer(), nullable=False, server_default=sa.text(str(default)))
                )


def downgrade() -> None:
    conn = op.get_bind()
    if not migration.table_exists(TABLE_NAME, conn):
        return
    with op.batch_alter_table(TABLE_NAME, schema=None) as batch_op:
        for name in reversed(LIMITS):
            if migration.column_exists(TABLE_NAME, name, conn):
                batch_op.drop_column(name)
