"""Owner-scoped secrets (#1083).

Revision ID: 0025
Revises: 0024
Create Date: 2026-11-29

The placeholders table gains ``owner_uid`` — the peer credential
that minted the row — and the secret_audit table records the same
owner per event. The #339 coverage model keeps its shape with one
word changed: ``[]`` (daemon-wide) becomes owner-wide, so a row's
label is unique per owner and per coverage set, and the legacy
daemon-wide unique on ``(name, workspaces)`` becomes the
``(owner_uid, name, workspaces)`` triple.
"""

import os

import sqlalchemy as sa
from alembic import op

revision = "0025"
down_revision = "0024"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # The owner columns land first, as plain ALTERs: the default IS
    # the backfill — the migrating process's uid, a constant
    # literal at DDL time — and every existing row reads it, so a
    # deployed single-user install wakes with every row owned by
    # the daemon's own uid, exactly the reach it had (#1083).
    # Plain-first is the torn-migration contract too (#10): a
    # re-run after a torn head (DDL committed, stamp lost) fails
    # HERE with the duplicate-column error the daemon's heal
    # recognizes, before any constraint work runs.
    op.add_column(
        "placeholders",
        sa.Column(
            "owner_uid",
            sa.Integer(),
            nullable=False,
            server_default=str(os.geteuid()),
        ),
    )
    op.add_column(
        "secret_audit",
        sa.Column(
            "owner_uid",
            sa.Integer(),
            nullable=False,
            server_default=str(os.geteuid()),
        ),
    )
    # The constraint swap, in ONE batch rebuild — the 0010 rule: a
    # tear leaves both constraints or neither. The daemon-wide
    # unique on ``(name, workspaces)`` leaves and the
    # ``(owner_uid, name, workspaces)`` triple arrives: the same
    # label may live on two owners' rows with the same coverage,
    # one row per label per owner per coverage set.
    with op.batch_alter_table(
        "placeholders",
        naming_convention={"uq": "uq_%(table_name)s_%(column_0_name)s"},
    ) as batch:
        batch.drop_constraint("uq_placeholders_name", type_="unique")
        batch.create_unique_constraint(
            "uq_placeholders_owner_name",
            ["owner_uid", "name", "workspaces"],
        )


def downgrade() -> None:
    # The reverse swap drops one row per (name, workspaces) pair —
    # the smallest id, deterministic — because the older schema's
    # daemon-wide uniqueness cannot hold a multi-owner database,
    # and a downgrade past this point is an operator act against
    # one.
    op.execute(
        "DELETE FROM placeholders WHERE id NOT IN "
        "(SELECT MIN(id) FROM placeholders GROUP BY name, workspaces)"
    )
    with op.batch_alter_table(
        "placeholders",
        naming_convention={"uq": "uq_%(table_name)s_%(column_0_name)s"},
    ) as batch:
        batch.drop_constraint("uq_placeholders_owner_name", type_="unique")
        batch.create_unique_constraint(
            "uq_placeholders_name", ["name", "workspaces"]
        )
        batch.drop_column("owner_uid")
    with op.batch_alter_table("secret_audit") as batch:
        batch.drop_column("owner_uid")
