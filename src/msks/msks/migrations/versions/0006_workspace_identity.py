"""The workspace's minted ssh identity (#111).

Revision ID: 0006
Revises: 0005
Create Date: 2026-09-29
"""

import sqlalchemy as sa
from alembic import op

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("workspaces", sa.Column("ssh_pubkey", sa.Text(), nullable=True))
    op.add_column("workspaces", sa.Column("ssh_privkey", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("workspaces", "ssh_privkey")
    op.drop_column("workspaces", "ssh_pubkey")
