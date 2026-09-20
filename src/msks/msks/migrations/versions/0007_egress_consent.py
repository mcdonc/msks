"""Egress consent (#69).

Revision ID: 0007
Revises: 0006
Create Date: 2026-09-20
"""

import sqlalchemy as sa
from alembic import op

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # The mode every pre-#69 row keeps: ``allow`` is the
    # default-permit posture #52 shipped, so an upgraded daemon
    # changes no workspace's egress behavior.
    op.add_column(
        "workspaces",
        sa.Column(
            "egress_mode",
            sa.String(),
            nullable=False,
            server_default="allow",
        ),
    )
    # A JSON array of specs (validated at create); NULL is an empty
    # allowlist.
    op.add_column(
        "workspaces",
        sa.Column("egress_allowlist", sa.Text(), nullable=True),
    )
    op.create_table(
        "egress_consent",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("workspace_id", sa.String(), nullable=False),
        sa.Column("dest_host", sa.String(), nullable=False),
        sa.Column("dest_port", sa.Integer(), nullable=False),
        sa.Column("decision", sa.String(), nullable=False),
        sa.Column("duration", sa.String(), nullable=True),
        sa.Column("requested_at", sa.Float(), nullable=False),
        sa.Column("decided_at", sa.Float(), nullable=True),
        sa.Column("decided_by", sa.String(), nullable=True),
        sa.Column("revoked_at", sa.Float(), nullable=True),
        sa.Column("revoked_by", sa.String(), nullable=True),
        sa.Column("hmac", sa.String(), nullable=True),
    )
    op.create_index(
        "ix_egress_consent_workspace_id",
        "egress_consent",
        ["workspace_id"],
    )
    op.create_index(
        "uq_egress_consent_pending",
        "egress_consent",
        ["workspace_id", "dest_host", "dest_port"],
        unique=True,
        sqlite_where=sa.text("decision = 'pending'"),
    )
    op.create_index(
        "uq_egress_consent_static_denied",
        "egress_consent",
        ["workspace_id", "dest_host", "dest_port"],
        unique=True,
        sqlite_where=sa.text("decision = 'denied' AND decided_by IS NULL"),
    )
    op.create_index(
        "uq_egress_consent_static_allowed",
        "egress_consent",
        ["workspace_id", "dest_host", "dest_port"],
        unique=True,
        sqlite_where=sa.text("decision = 'allowed' AND decided_by IS NULL"),
    )


def downgrade() -> None:
    op.drop_index("uq_egress_consent_static_allowed", "egress_consent")
    op.drop_index("uq_egress_consent_static_denied", "egress_consent")
    op.drop_index("uq_egress_consent_pending", "egress_consent")
    op.drop_index("ix_egress_consent_workspace_id", "egress_consent")
    op.drop_table("egress_consent")
    op.drop_column("workspaces", "egress_allowlist")
    op.drop_column("workspaces", "egress_mode")
