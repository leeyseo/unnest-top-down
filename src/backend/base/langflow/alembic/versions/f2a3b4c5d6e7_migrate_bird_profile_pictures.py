"""migrate legacy profile pictures to birds

Revision ID: f2a3b4c5d6e7
Revises: e1f2a3b4c5d6
Create Date: 2026-07-25 00:00:00.000000

Phase: DATA
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from langflow.utils import migration

revision: str = "f2a3b4c5d6e7"
down_revision: str | None = "e1f2a3b4c5d6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

DEFAULT_PROFILE_IMAGE = "Birds/01-owl.svg"


def upgrade() -> None:
    conn = op.get_bind()
    if not migration.table_exists("user", conn) or not migration.column_exists("user", "profile_image", conn):
        return
    user = sa.table("user", sa.column("profile_image", sa.String()))
    conn.execute(
        sa.update(user)
        .where(sa.or_(user.c.profile_image.like("People/%"), user.c.profile_image.like("Space/%")))
        .values(profile_image=DEFAULT_PROFILE_IMAGE)
    )


def downgrade() -> None:
    # Original per-user selections cannot be reconstructed safely.
    pass
