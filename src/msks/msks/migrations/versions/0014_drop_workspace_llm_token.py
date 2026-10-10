"""Drop the workspace LLM token column (#483)

Revision ID: 0014
Revises: 0013
Create Date: 2026-10-15

The proxy authenticates by tap (#483): the per-workspace credential
the column held was readable by every process in the guest and is
now minted nowhere. Rows keep their values only as dead bytes; the
column goes.

The tear story mirrors 0013's (#10): the upgrade inspects the live
columns instead of trusting the stamp, so a database whose 0014
DDL already committed but whose stamp stayed behind re-runs as a
no-op and proceeds to head.
"""

import sqlalchemy as sa
from alembic import op

revision = "0014"
down_revision = "0013"
branch_labels = None
depends_on = None


def _columns() -> set[str]:
    """The live workspaces table's column names — the shape, not
    the stamp, says whether this migration's work already landed."""
    return {
        row[1]
        for row in op.get_bind().execute(
            sa.text("PRAGMA table_info(workspaces)")
        )
    }


def upgrade() -> None:
    if "llm_token" not in _columns():
        return
    with op.batch_alter_table("workspaces") as batch:
        batch.drop_column("llm_token")


def downgrade() -> None:
    if "llm_token" in _columns():
        return
    with op.batch_alter_table("workspaces") as batch:
        batch.add_column(sa.Column("llm_token", sa.Text(), nullable=True))
