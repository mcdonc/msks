"""workspace persistent-artifact columns (#14)

Revision ID: 0002
Revises: 0001
Create Date: 2026-09-13
"""

import sqlalchemy as sa
from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("workspaces", sa.Column("image_hash", sa.String(), nullable=True))
    op.add_column("workspaces", sa.Column("host", sa.String(), nullable=True))
    op.add_column(
        "workspaces",
        sa.Column("root_mib", sa.Integer(), nullable=False, server_default="10240"),
    )
    op.add_column(
        "workspaces",
        sa.Column("home_mib", sa.Integer(), nullable=False, server_default="2048"),
    )


def downgrade() -> None:
    op.drop_column("workspaces", "home_mib")
    op.drop_column("workspaces", "root_mib")
    op.drop_column("workspaces", "host")
    op.drop_column("workspaces", "image_hash")
