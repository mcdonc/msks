"""Workspace attached mounts (#756).

Revision ID: 0020
Revises: 0019
Create Date: 2026-11-04
"""

import sqlalchemy as sa
from alembic import op

revision = "0020"
down_revision = "0019"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "workspaces",
        sa.Column("attached_mounts", sa.Text(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("workspaces", "attached_mounts")
