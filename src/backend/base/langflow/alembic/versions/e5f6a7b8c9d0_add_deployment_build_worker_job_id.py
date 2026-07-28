"""add deployment build worker job id

Revision ID: e5f6a7b8c9d0
Revises: d4e5f6a7b8c9
Create Date: 2026-07-23 00:00:00.000000

Phase: EXPAND
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from langflow.utils import migration

revision: str = "e5f6a7b8c9d0"
down_revision: str | None = "d4e5f6a7b8c9"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    conn = op.get_bind()
    if not migration.column_exists("deployment_build", "worker_job_id", conn):
        op.add_column("deployment_build", sa.Column("worker_job_id", sa.String(), nullable=True))
        op.create_index("ix_deployment_build_worker_job_id", "deployment_build", ["worker_job_id"])


def downgrade() -> None:
    conn = op.get_bind()
    if migration.column_exists("deployment_build", "worker_job_id", conn):
        op.drop_index("ix_deployment_build_worker_job_id", table_name="deployment_build")
        op.drop_column("deployment_build", "worker_job_id")
