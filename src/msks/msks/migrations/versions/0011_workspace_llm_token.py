"""Workspace LLM token column (#259)

Revision ID: 0011
Revises: 0010
Create Date: 2026-10-14
"""

import sqlalchemy as sa
from alembic import op

revision = "0011"
down_revision = "0010"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Nullable on purpose: pre-#259 rows carry no token (their seeds
    # ran before one existed), and a NULL token authenticates
    # nothing — the remint endpoint mints one on demand.
    with op.batch_alter_table("workspaces") as batch:
        batch.add_column(sa.Column("llm_token", sa.Text(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("workspaces") as batch:
        batch.drop_column("llm_token")
