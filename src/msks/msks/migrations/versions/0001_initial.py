"""initial schema: tokens + workspaces

Revision ID: 0001
Revises:
Create Date: 2026-09-12
"""

import sqlalchemy as sa
from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "tokens",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("token_hash", sa.String(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("revoked", sa.Boolean(), nullable=False),
    )
    op.create_index(
        "ix_tokens_token_hash", "tokens", ["token_hash"], unique=True
    )
    op.create_table(
        "workspaces",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("kernel", sa.String(), nullable=False),
        sa.Column("initrd", sa.String(), nullable=True),
        sa.Column("rootfs", sa.String(), nullable=False),
        sa.Column("cmdline", sa.Text(), nullable=False),
        sa.Column("cpus", sa.Integer(), nullable=False),
        sa.Column("mem_mib", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("workspaces")
    op.drop_index("ix_tokens_token_hash", table_name="tokens")
    op.drop_table("tokens")
