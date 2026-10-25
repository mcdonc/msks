"""Workspace mounts (#620, part #621).

Revision ID: 0017
Revises: 0016
Create Date: 2026-12-18
"""

import sqlalchemy as sa
from alembic import op

revision = "0017"
down_revision = "0016"
branch_labels = None
depends_on = None


def _columns() -> set[str]:
    """The live workspaces table's column names — the shape, not
    the stamp, says whether this migration's work already landed
    (the tear story 0013–0015 carry)."""
    return {
        row[1]
        for row in op.get_bind().execute(
            sa.text("PRAGMA table_info(workspaces)")
        )
    }


def upgrade() -> None:
    if "mounts" in _columns():
        return
    op.add_column(
        "workspaces",
        sa.Column("mounts", sa.Text(), nullable=True),
    )


def downgrade() -> None:
    if "mounts" not in _columns():
        return
    op.drop_column("workspaces", "mounts")
