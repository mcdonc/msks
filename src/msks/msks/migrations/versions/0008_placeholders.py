"""The placeholders and secret-audit tables (#198).

Revision ID: 0008
Revises: 0007
Create Date: 2026-10-06
"""

import sqlalchemy as sa
from alembic import op

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "placeholders",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("workspace_id", sa.String(), nullable=False),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("sentinel", sa.String(), nullable=False),
        sa.Column("dests", sa.Text(), nullable=False),
        sa.Column("backend_ref", sa.String(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("expires_at", sa.DateTime(), nullable=True),
        sa.UniqueConstraint(
            "workspace_id", "name", name="uq_placeholders_name"
        ),
    )
    op.create_index(
        "ix_placeholders_workspace_id", "placeholders", ["workspace_id"]
    )
    op.create_index("ix_placeholders_sentinel", "placeholders", ["sentinel"])
    op.create_index(
        "ix_placeholders_backend_ref", "placeholders", ["backend_ref"]
    )
    op.create_table(
        "secret_audit",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("kind", sa.String(), nullable=False),
        sa.Column("workspace_id", sa.String(), nullable=False),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("dests", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("secret_audit")
    op.drop_index("ix_placeholders_backend_ref", table_name="placeholders")
    op.drop_index("ix_placeholders_sentinel", table_name="placeholders")
    op.drop_index("ix_placeholders_workspace_id", table_name="placeholders")
    op.drop_table("placeholders")
