"""Index the secret-audit workspace column (#305).

Revision ID: 0012
Revises: 0011
Create Date: 2026-09-24
"""

from alembic import op

revision = "0012"
down_revision = "0011"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # The decider registration replays one workspace's newest audit
    # rows (#305): the table is append-only with no pruning, so the
    # per-workspace scan degrades as it grows without the index.
    op.create_index(
        "ix_secret_audit_workspace_id", "secret_audit", ["workspace_id"]
    )


def downgrade() -> None:
    op.drop_index("ix_secret_audit_workspace_id", table_name="secret_audit")
