"""The seeded probe placeholder (#424): the owner-wide row named
``probe`` the daemon mints for itself at first-time startup, so
an operator can verify interception from inside a workspace with
zero minting. This module owns the seed's two halves — the first
boot's create-and-arm and every later boot's re-arm — beside the
re-arm the probe endpoint's read performs."""

import logging

from sqlalchemy.exc import IntegrityError

from ....model.workspaces import daemon_uid
from ....secretstore import (
    backend_ref,
    blob_recipients,
    eligible_recipients,
    new_sentinel,
    recipient_hashes,
)
from ....spec.probe import (
    PROBE_HOST,
    PROBE_NAME,
    PROBE_SECRET_B64,
)

LOG = logging.getLogger(__name__)


async def seed_probe_placeholder(app) -> None:
    """Seed the probe placeholder at first-time startup (#424).

    The owner-wide row named ``probe`` (the daemon's own uid,
    #1083): allowlisting the probe host, carrying the fixed
    credential blob as its secret, so the probe works with zero
    operator minting — the sentinel is read back over the
    authenticated API (``GET /api/v1/probe``). Seeded exactly when
    the placeholder table is empty — a fresh daemon's first
    startup; a daemon that already holds the row re-arms it every
    boot instead (#514 review: the credential is public, so the
    daemon keeps its own probe swapping across restarts). The two
    halves fail differently: a seed whose row cannot be created,
    valued, or audited rolls the row back and the daemon stays up
    without it (loud, non-fatal — the bootstrap default image's
    posture: the operator can mint the row by hand with the fixed
    credential as the value), while a failing re-arm only logs —
    the row keeps exactly what it had.

    Runs before any workspace can be attached, so no arming step
    belongs here: the placeholder-driven arm happens at each
    attach as always.
    """
    model = app.state.model
    # The row is looked up by owner and label, not by ref: a row
    # minted before #1083 keeps its legacy ref spelling forever
    # (the migration adds the owner column without rewriting
    # refs), and the backfill assigns such rows the daemon's uid
    # — both spellings answer here.
    existing = await model.placeholder_for(
        [], PROBE_NAME, owner_uid=daemon_uid()
    )
    if existing is not None:
        try:
            await arm_probe_row(app, existing)
        except Exception:  # noqa: BLE001 - best-effort, never boot-fatal
            # The row stands exactly as it was: locked until an
            # operator unlocks it with the public value. A
            # convenience re-arm of a public credential never
            # gates daemon availability (#514 review, delta).
            LOG.exception(
                "probe placeholder re-arm failed; the row keeps its "
                "previous blobs and cache state"
            )
        return
    if await model.list_placeholders():
        return
    await mint_probe_row(app)


async def mint_probe_row(app) -> None:
    """The first boot's seed half (#424): create the row, value
    it through the legacy store, arm it, and audit the mint. A
    failure rolls the row back whole and the daemon stays up
    without it."""
    model = app.state.model
    sentinel = new_sentinel(owner_wide=True)
    try:
        row = await model.create_placeholder(
            [],
            PROBE_NAME,
            sentinel,
            [PROBE_HOST],
            backend_ref(daemon_uid(), [], PROBE_NAME),
            None,
            owner_uid=daemon_uid(),
        )
    except IntegrityError:
        # A racing first boot won the unique index: its row stands,
        # this one's work is done.
        return
    try:
        await arm_probe_row(app, row)
        await model.record_audit("mint", row)
    except Exception:  # noqa: BLE001 - named below, non-fatal
        # A seed whose row cannot be created, valued, or audited
        # rolls back whole — a live owner-wide row with no value
        # behind its ref would answer every swap fail-closed — and
        # the daemon stays up without the probe row (the mint
        # route's own posture).
        await model.delete_placeholder(row["id"])
        LOG.exception("probe placeholder seed failed; rolled back")


async def arm_probe_row(app, row: dict) -> None:
    """The probe row's #514 half: cache the public credential for
    the unlock window and refresh the row's blobs to the current
    accepting workspaces. The credential is public and fixed, so
    the daemon arming its own probe row grants nothing a token
    holder cannot read from ``GET /api/v1/probe``. The blob write
    is skipped when the row already holds exactly the current
    recipients under the current key hashes — a read endpoint
    should not rewrite the database per fetch when nothing
    changed."""
    app.state.secrets.plain.store({row["backend_ref"]: PROBE_SECRET_B64})
    recipients = eligible_recipients(
        await app.state.model.secret_recipient_keys(
            row["workspaces"], row["owner_uid"]
        )
    )
    if not recipients:
        return
    fresh = recipient_hashes(recipients)
    standing = {
        blob["recipient"]: blob["key_hash"]
        for blob in await app.state.model.secret_blobs(row["id"])
    }
    if standing == fresh:
        return
    await app.state.model.write_secret_blobs(
        row["id"], blob_recipients(PROBE_SECRET_B64.encode(), recipients)
    )
