"""The workspace disk epoch (#697).

Revision ID: 0019
Revises: 0018
Create Date: 2026-10-30
"""

import sqlalchemy as sa
from alembic import op

revision = "0019"
down_revision = "0018"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "workspaces",
        sa.Column(
            "disk_epoch", sa.Integer(), nullable=False, server_default="0"
        ),
    )


def downgrade() -> None:
    op.drop_column("workspaces", "disk_epoch")
