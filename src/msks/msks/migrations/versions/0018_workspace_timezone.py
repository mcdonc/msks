"""The workspace timezone (#633).

Revision ID: 0018
Revises: 0017
Create Date: 2026-10-27
"""

import sqlalchemy as sa
from alembic import op

revision = "0018"
down_revision = "0017"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "workspaces",
        sa.Column("timezone", sa.String(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("workspaces", "timezone")
