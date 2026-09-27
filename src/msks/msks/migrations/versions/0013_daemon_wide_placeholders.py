"""Daemon-wide placeholder coverage (#339).

Revision ID: 0013
Revises: 0012
Create Date: 2026-11-18

The placeholders table swaps its ``workspace_id`` column for a
``workspaces`` JSON-array column (the row's coverage: ``[]`` is the
daemon-wide row, a non-empty array is scoped to exactly those ids),
and the secret_audit table records the same coverage per event. The
workspaces table gains ``secret_coverage`` — the per-workspace
escape hatch that exempts a workspace from daemon-wide rows.

The placeholder rebuild is raw SQL staged through ``_new`` tables,
not a sandwich of ``batch_alter_table`` blocks around the backfill
UPDATE: the copy transforms each row (one workspace id becomes a
one-element JSON array), which batch mode's name-based column copy
cannot express. Everything runs in the migration's single
transaction, so a torn run (DDL committed, alembic stamp lost — the
#10 story :meth:`msks.model.Model.heal_torn_migration` exists for)
left the **finished** shape. The upgrade therefore inspects the
live columns first and only rebuilds a table that still carries the
old shape: a re-run after a torn stamp finds the new shape, does
nothing, and the upgrade proceeds to stamp head. A stray staging
table (only possible from a hand-mangled database) is dropped
before the rebuild — it holds nothing the live rows do not.

The downgrade stages through ``_old`` tables the same way: the
legacy-shape table is built and filled **from the live new-shape
rows** before the live table goes away, and 0008/0012's indexes
come back with the shapes that created them. A daemon-wide row
downgrades onto the workspace id ``*`` and a daemon-wide audit
event onto the empty string — the lossy edge a downgrade accepts,
named here so nobody meets it unawares.
"""

import sqlalchemy as sa
from alembic import op

revision = "0013"
down_revision = "0012"
branch_labels = None
depends_on = None


def table_columns(name: str) -> set[str]:
    """The live table's column names — the tear story's compass:
    the shape, not the stamp, says whether this migration's work
    already landed."""
    return {
        row[1]
        for row in op.get_bind().execute(sa.text(f"PRAGMA table_info({name})"))
    }


def rebuild_placeholders() -> None:
    """One staged copy: build the new shape, fill it, swap it in,
    restore the indexes the drop took with it."""
    op.execute(
        """
        CREATE TABLE placeholders_new (
            id INTEGER NOT NULL PRIMARY KEY,
            workspaces TEXT NOT NULL,
            name VARCHAR NOT NULL,
            sentinel VARCHAR NOT NULL,
            dests TEXT NOT NULL,
            backend_ref VARCHAR NOT NULL,
            created_at DATETIME NOT NULL,
            expires_at DATETIME,
            CONSTRAINT uq_placeholders_name UNIQUE (name, workspaces)
        )
        """
    )
    op.execute(
        """
        INSERT INTO placeholders_new
            (id, workspaces, name, sentinel, dests, backend_ref,
             created_at, expires_at)
        SELECT id, '["' || workspace_id || '"]', name, sentinel, dests,
               backend_ref, created_at, expires_at
        FROM placeholders
        """
    )
    op.execute("DROP TABLE placeholders")
    op.execute("ALTER TABLE placeholders_new RENAME TO placeholders")
    op.create_index(
        "ix_placeholders_sentinel",
        "placeholders",
        ["sentinel"],
        unique=True,
    )
    op.create_index(
        "ix_placeholders_backend_ref",
        "placeholders",
        ["backend_ref"],
        unique=True,
    )


def rebuild_secret_audit() -> None:
    """The audit table's staged copy; the drop takes 0012's
    workspace_id index with it, and the per-workspace replay matches
    coverage by content (#339), which a plain column index cannot
    serve."""
    op.execute(
        """
        CREATE TABLE secret_audit_new (
            id INTEGER NOT NULL PRIMARY KEY,
            kind VARCHAR NOT NULL,
            workspaces TEXT NOT NULL,
            name VARCHAR NOT NULL,
            dests TEXT NOT NULL,
            created_at DATETIME NOT NULL
        )
        """
    )
    op.execute(
        """
        INSERT INTO secret_audit_new
            (id, kind, workspaces, name, dests, created_at)
        SELECT id, kind, '["' || workspace_id || '"]', name, dests,
               created_at
        FROM secret_audit
        """
    )
    op.execute("DROP TABLE secret_audit")
    op.execute("ALTER TABLE secret_audit_new RENAME TO secret_audit")


def upgrade() -> None:
    if "workspace_id" in table_columns("placeholders"):
        # A stray staging table can only come from a hand-mangled
        # database; it holds nothing the live rows do not.
        op.execute("DROP TABLE IF EXISTS placeholders_new")
        rebuild_placeholders()
    if "workspace_id" in table_columns("secret_audit"):
        op.execute("DROP TABLE IF EXISTS secret_audit_new")
        rebuild_secret_audit()
    if "secret_coverage" not in table_columns("workspaces"):
        with op.batch_alter_table("workspaces") as batch:
            batch.add_column(
                sa.Column(
                    "secret_coverage",
                    sa.String(),
                    nullable=False,
                    server_default="all",
                )
            )


def downgrade() -> None:
    op.execute(
        """
        CREATE TABLE placeholders_old (
            id INTEGER NOT NULL PRIMARY KEY,
            workspace_id VARCHAR NOT NULL,
            name VARCHAR NOT NULL,
            sentinel VARCHAR NOT NULL,
            dests TEXT NOT NULL,
            backend_ref VARCHAR NOT NULL,
            created_at DATETIME NOT NULL,
            expires_at DATETIME,
            CONSTRAINT uq_placeholders_name UNIQUE (workspace_id, name)
        )
        """
    )
    op.execute(
        """
        INSERT INTO placeholders_old
            (id, workspace_id, name, sentinel, dests, backend_ref,
             created_at, expires_at)
        SELECT id,
               CASE workspaces WHEN '[]' THEN '*' ELSE
                   json_extract(workspaces, '$[0]') END,
               name, sentinel, dests, backend_ref, created_at, expires_at
        FROM placeholders
        """
    )
    op.execute("DROP TABLE placeholders")
    op.execute("ALTER TABLE placeholders_old RENAME TO placeholders")
    op.execute(
        """
        CREATE TABLE secret_audit_old (
            id INTEGER NOT NULL PRIMARY KEY,
            kind VARCHAR NOT NULL,
            workspace_id VARCHAR NOT NULL,
            name VARCHAR NOT NULL,
            dests TEXT NOT NULL,
            created_at DATETIME NOT NULL
        )
        """
    )
    op.execute(
        """
        INSERT INTO secret_audit_old
            (id, kind, workspace_id, name, dests, created_at)
        SELECT id, kind,
               CASE workspaces WHEN '[]' THEN '' ELSE
                   json_extract(workspaces, '$[0]') END,
               name, dests, created_at
        FROM secret_audit
        """
    )
    op.execute("DROP TABLE secret_audit")
    op.execute("ALTER TABLE secret_audit_old RENAME TO secret_audit")
    with op.batch_alter_table("workspaces") as batch:
        batch.drop_column("secret_coverage")
    # The indexes 0008 and 0012 created alongside these shapes.
    op.create_index(
        "ix_placeholders_workspace_id", "placeholders", ["workspace_id"]
    )
    op.create_index(
        "ix_placeholders_sentinel",
        "placeholders",
        ["sentinel"],
        unique=True,
    )
    op.create_index(
        "ix_placeholders_backend_ref",
        "placeholders",
        ["backend_ref"],
        unique=True,
    )
    op.create_index(
        "ix_secret_audit_workspace_id", "secret_audit", ["workspace_id"]
    )
