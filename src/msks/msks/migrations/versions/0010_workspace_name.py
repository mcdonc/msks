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
    # One transaction, one shape (#246 review): batch mode rebuilds
    # the table with the column AND its unique constraint inside the
    # same CREATE — a torn migration (DDL committed, stamp lost)
    # leaves both or neither, so the daemon's stamp-past healing can
    # never accept the column without the uniqueness backstop. A bare
    # add_column + create_index pair would tear between the two, and
    # sqlite refuses ADD COLUMN ... UNIQUE outright.
    #
    # No backfill on purpose: pre-#246 rows keep their operator-chosen
    # id as the id and a NULL name — their label IS their id, so ref
    # resolution addresses them unchanged, and a NULL name is a legal
    # state (nameless creates) the migration need not distinguish.
    with op.batch_alter_table("workspaces") as batch:
        batch.add_column(
            sa.Column("name", sa.String(), nullable=True, unique=True)
        )


def downgrade() -> None:
    with op.batch_alter_table("workspaces") as batch:
        batch.drop_column("name")
