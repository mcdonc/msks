"""The workspace seed epoch (#803).

Revision ID: 0021
Revises: 0020
Create Date: 2026-11-08
"""

import sqlalchemy as sa
from alembic import op

revision = "0021"
down_revision = "0020"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "workspaces",
        sa.Column(
            "seed_epoch", sa.Integer(), nullable=False, server_default="0"
        ),
    )


def downgrade() -> None:
    op.drop_column("workspaces", "seed_epoch")
