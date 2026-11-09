"""Workspace proxies (#807, #808).

Revision ID: 0022
Revises: 0021
Create Date: 2026-12-19

The ``workspace_proxies`` table: one row per daemon-owned TCP
proxy tied to a workspace. A forward row names a daemon-side
listener and the guest port it reaches; a remote row names a
listener on the workspace's tap and the daemon-side service it
bridges to. The daemon reads the rows at every workspace start
and brings each listener up.
"""

import sqlalchemy as sa
from alembic import op

revision = "0022"
down_revision = "0021"
branch_labels = None
depends_on = None


def _tables() -> set[str]:
    """The live table names — the shape, not the stamp, says
    whether this migration's work already landed."""
    return {
        row[0]
        for row in op.get_bind().execute(
            sa.text("SELECT name FROM sqlite_master WHERE type='table'")
        )
    }


def upgrade() -> None:
    if "workspace_proxies" in _tables():
        return
    op.create_table(
        "workspace_proxies",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("workspace_id", sa.String(), nullable=False),
        sa.Column("direction", sa.String(), nullable=False),
        sa.Column("listen_host", sa.Text(), nullable=True),
        sa.Column("listen_port", sa.Integer(), nullable=False),
        sa.Column("target_host", sa.Text(), nullable=True),
        sa.Column("target_port", sa.Integer(), nullable=False),
        sa.Column("label", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sqlite_autoincrement=True,
    )
    op.create_index(
        "ix_workspace_proxies_workspace_id",
        "workspace_proxies",
        ["workspace_id"],
    )


def downgrade() -> None:
    if "workspace_proxies" not in _tables():
        return
    op.drop_index(
        "ix_workspace_proxies_workspace_id", table_name="workspace_proxies"
    )
    op.drop_table("workspace_proxies")
