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

The placeholder rebuild is raw SQL in one transaction, not a
sandwich of ``batch_alter_table`` blocks around the backfill
UPDATE: the copy transforms each row (one workspace id becomes a
one-element JSON array), which batch mode's name-based column copy
cannot express, and a torn multi-block pass (DDL committed, stamp
lost) could leave the column nullable with the old unique
constraint — a shape the daemon's stamp-past healing would then
accept as final. One CREATE–INSERT–DROP–RENAME sequence leaves
either shape, never a mix; a torn re-run fails on the CREATE and
stamps past onto the finished shape.
"""

import sqlalchemy as sa
from alembic import op

revision = "0013"
down_revision = "0012"
branch_labels = None
depends_on = None


def upgrade() -> None:
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
    # The drop takes the ix_secret_audit_workspace_id index with it;
    # the per-workspace replay matches coverage by content (#339),
    # which a plain column index cannot serve.
    op.execute("DROP TABLE secret_audit")
    op.execute("ALTER TABLE secret_audit_new RENAME TO secret_audit")
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
    with op.batch_alter_table("workspaces") as batch:
        batch.drop_column("secret_coverage")
    op.execute("DROP TABLE secret_audit")
    op.execute(
        """
        CREATE TABLE secret_audit (
            id INTEGER NOT NULL PRIMARY KEY,
            kind VARCHAR NOT NULL,
            workspace_id VARCHAR NOT NULL,
            name VARCHAR NOT NULL,
            dests TEXT NOT NULL,
            created_at DATETIME NOT NULL
        )
        """
    )
    # A downgrade is lossy by design: a daemon-wide event has no
    # single workspace to land on, so it records the empty string.
    op.execute(
        """
        INSERT INTO secret_audit
            (id, kind, workspace_id, name, dests, created_at)
        SELECT id, kind,
               CASE workspaces WHEN '[]' THEN '' ELSE
                   json_extract(workspaces, '$[0]') END,
               name, dests, created_at
        FROM secret_audit
        """
    )
    op.execute("DROP TABLE placeholders")
    op.execute(
        """
        CREATE TABLE placeholders (
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
        INSERT INTO placeholders
            (id, workspace_id, name, sentinel, dests, backend_ref,
             created_at, expires_at)
        SELECT id,
               CASE workspaces WHEN '[]' THEN '*' ELSE
                   json_extract(workspaces, '$[0]') END,
               name, sentinel, dests, backend_ref, created_at, expires_at
        FROM placeholders
        """
    )
