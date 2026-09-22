"""Workspace name column (#246)

Revision ID: 0010
Revises: 0009
Create Date: 2026-10-04
"""

import sqlalchemy as sa
from alembic import op

revision = "0010"
down_revision = "0009"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # The label half of the #246 identity split: every existing row
    # keeps its operator-chosen id as the immutable id AND takes it
    # as its name, so paths, caches, and keyed surfaces stay put —
    # new creates mint a random UUID id and carry the operator's
    # label here instead.
    op.add_column("workspaces", sa.Column("name", sa.String(), nullable=True))
    op.execute("UPDATE workspaces SET name = id")
    op.create_index("ix_workspaces_name", "workspaces", ["name"], unique=True)


def downgrade() -> None:
    op.drop_index("ix_workspaces_name", table_name="workspaces")
    op.drop_column("workspaces", "name")
