"""The workspace login user (#248).

Revision ID: 0009
Revises: 0008
Create Date: 2026-10-07
"""

import sqlalchemy as sa
from alembic import op

revision = "0009"
down_revision = "0008"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "workspaces",
        sa.Column("login_user", sa.String(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("workspaces", "login_user")
