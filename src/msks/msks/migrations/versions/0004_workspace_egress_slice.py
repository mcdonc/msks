"""The recorded egress pool slice (#52, #70 review).

Revision ID: 0004
Revises: 0003
Create Date: 2026-09-14
"""

import sqlalchemy as sa
from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "workspaces", sa.Column("egress_slice", sa.Integer(), nullable=True)
    )


def downgrade() -> None:
    op.drop_column("workspaces", "egress_slice")
