"""Workspace ownership (#1082).

Revision ID: 0024
Revises: 0023
Create Date: 2026-11-20
"""

import os

import sqlalchemy as sa
from alembic import op

revision = "0024"
down_revision = "0023"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # The owner column lands first, as a plain ALTER: the default IS
    # the backfill — the migrating process's uid, a constant literal
    # at DDL time — and every existing row reads it, so a deployed
    # single-user install wakes with every row owned by the daemon's
    # own uid, exactly the visibility it had (#1082). Plain-first is
    # the torn-migration contract too (#10): a re-run after a torn
    # head (DDL committed, stamp lost) fails HERE with the
    # duplicate-column error the daemon's heal recognizes, before
    # any constraint work runs.
    op.add_column(
        "workspaces",
        sa.Column(
            "owner_uid",
            sa.Integer(),
            nullable=False,
            server_default=str(os.geteuid()),
        ),
    )
    # The constraint swap, in ONE batch rebuild — the 0010 rule: a
    # tear leaves both constraints or neither. The daemon-wide
    # unique on ``name`` leaves (the naming convention gives the
    # reflected, unnamed autoindex a name the rebuild can drop) and
    # the ``(owner_uid, name)`` pair arrives: names become unique
    # per owner instead of per daemon, so two owners may each hold
    # a ``build``.
    with op.batch_alter_table(
        "workspaces",
        naming_convention={"uq": "uq_%(table_name)s_%(column_0_name)s"},
    ) as batch:
        batch.drop_constraint("uq_workspaces_name", type_="unique")
        batch.create_unique_constraint(
            "uq_workspaces_owner_name", ["owner_uid", "name"]
        )


def downgrade() -> None:
    # The reverse swap drops the owner column and its pair
    # constraint in one rebuild. One row per name survives — the
    # smallest id, deterministic — because the older schema's
    # daemon-wide uniqueness cannot hold a multi-owner database,
    # and a downgrade past this point is an operator act against
    # one.
    op.execute(
        "DELETE FROM workspaces WHERE name IS NOT NULL AND id NOT IN "
        "(SELECT MIN(id) FROM workspaces GROUP BY name)"
    )
    with op.batch_alter_table("workspaces") as batch:
        batch.drop_constraint("uq_workspaces_owner_name", type_="unique")
        batch.drop_column("owner_uid")
        # The pre-#1082 shape returns: the daemon-wide unique on
        # ``name``, unnamed exactly as 0010 left it, so a re-upgrade
        # of 0024 (the torn test's round trip) finds and re-drops it.
        batch.create_unique_constraint(None, ["name"])
