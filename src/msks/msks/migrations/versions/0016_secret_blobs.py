"""Per-recipient secret blob storage (#513, #504).

Revision ID: 0016
Revises: 0015
Create Date: 2026-11-27

The ``secret_blobs`` table: one row per placeholder value per
recipient — the value as a whole age file encrypted to the
recipient workspace's ssh public half, the ``key_hash`` column
recording the public key line the blob was minted against, so
staleness (a rotated key, a grown coverage set) is derivable from
rows alone. The daemon that stores these rows holds no key that
decrypts them (#504); the SecretSpec agefile the table's rows
gradually replace is untouched here — #516 migrates the values
and retires it.
"""

import sqlalchemy as sa
from alembic import op

revision = "0016"
down_revision = "0015"
branch_labels = None
depends_on = None


def _tables() -> set[str]:
    """The live table names — the shape, not the stamp, says
    whether this migration's work already landed (the tear story
    0013–0015 carry; #620 part #621 extends it past the
    torn-0015 replay the 0015 test walks, which now re-runs this
    step too)."""
    return {
        row[0]
        for row in op.get_bind().execute(
            sa.text("SELECT name FROM sqlite_master WHERE type='table'")
        )
    }


def upgrade() -> None:
    if "secret_blobs" in _tables():
        return
    op.create_table(
        "secret_blobs",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("placeholder_id", sa.Integer(), nullable=False),
        sa.Column("recipient", sa.String(), nullable=False),
        sa.Column("key_hash", sa.String(), nullable=False),
        sa.Column("ciphertext", sa.LargeBinary(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint(
            "placeholder_id", "recipient", name="uq_secret_blobs_placeholder"
        ),
    )
    op.create_index(
        "ix_secret_blobs_placeholder_id", "secret_blobs", ["placeholder_id"]
    )


def downgrade() -> None:
    if "secret_blobs" not in _tables():
        return
    op.drop_index("ix_secret_blobs_placeholder_id", table_name="secret_blobs")
    op.drop_table("secret_blobs")
