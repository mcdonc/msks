"""Workspace egress flag (#52).

Revision ID: 0003
Revises: 0002
Create Date: 2026-09-13
"""

import sqlalchemy as sa
from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # server_default false keeps pre-egress rows NIC-less: the new
    # default (egress on) applies to rows created after the upgrade.
    op.add_column(
        "workspaces",
        sa.Column("egress", sa.Boolean(), nullable=False, server_default=sa.false()),
    )


def downgrade() -> None:
    op.drop_column("workspaces", "egress")
