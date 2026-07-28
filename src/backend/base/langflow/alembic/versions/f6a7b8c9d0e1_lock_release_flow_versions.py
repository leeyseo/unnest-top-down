"""lock release flow versions

Revision ID: f6a7b8c9d0e1
Revises: e5f6a7b8c9d0
Create Date: 2026-07-23 00:00:00.000000

Phase: EXPAND
"""

from collections.abc import Sequence
from uuid import UUID, uuid4

import sqlalchemy as sa
from alembic import op
from langflow.utils import migration

revision: str = "f6a7b8c9d0e1"
down_revision: str | None = "e5f6a7b8c9d0"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    conn = op.get_bind()
    if migration.table_exists("deployment_release_flow_version", conn):
        return
    op.create_table(
        "deployment_release_flow_version",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("release_id", sa.Uuid(), nullable=False),
        sa.Column("flow_version_id", sa.Uuid(), nullable=False),
        sa.Column("role", sa.String(), nullable=False),
        sa.ForeignKeyConstraint(["flow_version_id"], ["flow_version.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["release_id"], ["deployment_release.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "release_id",
            "flow_version_id",
            name="uq_deployment_release_flow_version",
        ),
    )
    op.create_index(
        "ix_deployment_release_flow_version_release_id",
        "deployment_release_flow_version",
        ["release_id"],
    )
    op.create_index(
        "ix_deployment_release_flow_version_flow_version_id",
        "deployment_release_flow_version",
        ["flow_version_id"],
    )
    releases = sa.table(
        "deployment_release",
        sa.column("id", sa.Uuid()),
        sa.column("agent_flow_version_id", sa.Uuid()),
        sa.column("ingestion_flow_version_id", sa.Uuid()),
        sa.column("subflow_version_ids", sa.JSON()),
    )
    links = sa.table(
        "deployment_release_flow_version",
        sa.column("id", sa.Uuid()),
        sa.column("release_id", sa.Uuid()),
        sa.column("flow_version_id", sa.Uuid()),
        sa.column("role", sa.String()),
    )
    rows = conn.execute(
        sa.select(
            releases.c.id,
            releases.c.agent_flow_version_id,
            releases.c.ingestion_flow_version_id,
            releases.c.subflow_version_ids,
        )
    )
    for release_id, agent_id, ingestion_id, subflow_ids in rows:
        attached = {
            agent_id: "agent",
            ingestion_id: "ingestion",
            **dict.fromkeys((UUID(str(value)) for value in (subflow_ids or [])), "subflow"),
        }
        conn.execute(
            sa.insert(links),
            [
                {
                    "id": uuid4(),
                    "release_id": release_id,
                    "flow_version_id": flow_version_id,
                    "role": role,
                }
                for flow_version_id, role in attached.items()
            ],
        )


def downgrade() -> None:
    conn = op.get_bind()
    if migration.table_exists("deployment_release_flow_version", conn):
        op.drop_table("deployment_release_flow_version")
