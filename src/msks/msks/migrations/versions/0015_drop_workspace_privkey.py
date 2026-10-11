"""Drop the workspace private-half column (#497)

Revision ID: 0015
Revises: 0014
Create Date: 2026-10-15

The operator's own key is the workspace identity (#486): every row
created now carries the public half alone, and the daemon holds no
private key. The column survived for rows a pre-#486 msks minted
against (#111 daemon-side, #121 client-side); it goes now, with the
stored halves as accepted dead bytes — their operators hold the
same halves where the mint left them, and a workspace that needs
ssh again is re-created under the operator's identity.

The tear story mirrors 0014's (#10): the upgrade inspects the live
columns instead of trusting the stamp, so a database whose 0015
DDL already committed but whose stamp stayed behind re-runs as a
no-op and proceeds to head.
"""

import sqlalchemy as sa
from alembic import op

revision = "0015"
down_revision = "0014"
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
    if "ssh_privkey" not in _columns():
        return
    with op.batch_alter_table("workspaces") as batch:
        batch.drop_column("ssh_privkey")


def downgrade() -> None:
    if "ssh_privkey" in _columns():
        return
    with op.batch_alter_table("workspaces") as batch:
        batch.add_column(sa.Column("ssh_privkey", sa.Text(), nullable=True))
