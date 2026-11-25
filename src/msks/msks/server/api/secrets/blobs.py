"""The ciphertext half of the secret surface (#513, #514): the
mint's per-recipient blob write, the unlock POST's validate,
cache, and re-encrypt sequence, and the staleness reads that
name their refusals."""

import time

from fastapi import HTTPException

from ....secretcache import NotEncryptableError
from ....secretstore import blob_recipients, eligible_recipients
from .views import coverage_fields, owned_or_404


async def mint_blobs_for(
    app, row: dict, coverage: list[str], value: str
) -> None:
    """Encrypt one mint's value to every covering workspace key
    and store the blobs (#514), caching the plaintext for the
    unlock window — the minting operator just supplied it, so the
    swap path stands ready from the 201 on.

    The named refusals: a scoped row's covered workspace without
    an ssh public key, or with a key the crypto leaf cannot
    encrypt to, and an owner-wide row (#1083) no accepting
    workspace of its owner can receive — each names the workspace
    and the constraint (:class:`NotEncryptableError`).
    """
    recipients = await app.state.model.secret_recipient_keys(
        coverage, row["owner_uid"]
    )
    if coverage:
        # A scoped row's every named workspace must receive a
        # blob: the refusal names the one that cannot.
        for workspace_id in coverage:
            if workspace_id not in recipients:
                raise NotEncryptableError(
                    workspace_id,
                    "it carries no ssh public key — mint the "
                    "workspace's identity key first",
                )
    else:
        # An owner-wide row mints one blob per accepting workspace
        # of its owner (#1083) whose key can receive it; incapable
        # acceptors are skipped (staleness names them), and an
        # owner with no capable acceptor at all takes the named
        # refusal.
        recipients = eligible_recipients(recipients)
        if not recipients:
            raise NotEncryptableError(
                "*",
                "no accepting workspace carries a key the secret "
                "store can encrypt to (ssh-ed25519 or ssh-rsa "
                "required)",
            )
    blobs = blob_recipients(value.encode(), recipients)
    await app.state.model.write_secret_blobs(row["id"], blobs)
    app.state.secrets.plain.store({row["backend_ref"]: value})


async def validated_unlock_row(app, item, caller: int) -> tuple[dict, str]:
    """One unlock item's checks (#514): the row exists, the caller
    owns it (#1083 — another owner's row answers the same 404 as an
    absent one), its ciphertext was served under the live session
    window, and the value survives the same strip the mint
    applies. Returns the row and its stripped value."""
    row = await app.state.model.get_placeholder(item.placeholder_id)
    if row is None:
        raise HTTPException(status_code=404, detail="no such placeholder")
    owned_or_404(row, caller)
    if not app.state.secrets.plain.served_alive(item.placeholder_id):
        raise HTTPException(
            status_code=422,
            detail=(
                f"{row['name']}'s ciphertext was not served in "
                "the live session window; GET "
                "/api/v1/secrets/pending first"
            ),
        )
    value = item.value.strip().lstrip("\ufeff").strip()
    if not value:
        raise HTTPException(status_code=422, detail="the value is empty")
    return row, value


async def refresh_blobs_for(app, row: dict, value: str) -> dict:
    """Re-encrypt one unlocked row's value to its **current**
    recipient set (#514's lazy rule): a coverage edit heals at the
    unlock, because the fresh blobs replace the whole set. A
    workspace's stored key is fixed at create (#41), so a key
    rotation for the same workspace never reaches this refresh.
    Recipients whose keys cannot hold a blob are
    skipped — they read back from staleness, and one incapable
    key never blocks a row's others. A row whose eligible set is
    EMPTY keeps its existing blobs untouched: they are the only
    ciphertext copies of the value, and a replace-all write with
    no recipients would drop them for good (the mint refuses this
    same condition by name; the unlock answers it by keeping the
    old set until coverage heals). Returns the fresh staleness.
    """
    recipients = eligible_recipients(
        await app.state.model.secret_recipient_keys(
            row["workspaces"], row["owner_uid"]
        )
    )
    if not recipients:
        # The skip is named in the reply: the operator sees the
        # row kept its blobs and nothing healed this round.
        return {
            "staleness": await row_staleness(app, row),
            "refreshed": False,
        }
    await app.state.model.write_secret_blobs(
        row["id"], blob_recipients(value.encode(), recipients)
    )
    return {
        "staleness": await row_staleness(app, row),
        "refreshed": True,
    }


async def row_staleness(app, row: dict) -> dict:
    """The row's staleness map, empty-shaped for an absent row."""
    staleness = await app.state.model.secret_blob_staleness(row["id"])
    return (
        staleness
        if staleness is not None
        else {
            "missing": [],
            "stale": [],
        }
    )


async def accept_unlock(app, hub, body, caller: int) -> dict:
    """One unlock POST's mutate half (#514), already under the
    store lock: validate every row against the served window,
    enter the plaintext into the cache (this POST is the session
    the timer resets on), re-encrypt fresh blobs per row, and
    record the audit + event trail. A re-encryption failure 503s
    with the cache warm and the rows it reached named — the
    operator's unlock stood; the row's staleness names what did
    not heal."""
    accepted = [
        (row, value)
        for item in body.values
        for row, value in [await validated_unlock_row(app, item, caller)]
    ]
    plain = app.state.secrets.plain
    plain.store({row["backend_ref"]: value for row, value in accepted})
    # The trail precedes the re-encryption: the unlock was accepted
    # whatever the blob refresh then does, so a 503 from the write
    # leaves the healed rows with their audit rows and events
    # intact (#514 review, delta).
    await publish_unlocks(app, hub, accepted)
    refreshed = await reencrypt_rows(app, accepted)
    return {
        "unlocked": [row["id"] for row, _ in accepted],
        "rows": refreshed,
    }


async def reencrypt_rows(app, accepted: list) -> list[dict]:
    """Re-encrypt each unlocked row's blobs; a failure 503s with
    the cache warm and the rows that healed named (#514 review).
    """
    refreshed: list[dict] = []
    try:
        for row, value in accepted:
            refreshed.append(
                {"id": row["id"], **await refresh_blobs_for(app, row, value)}
            )
    except OSError as exc:
        raise HTTPException(
            status_code=503,
            detail=(
                "unlock accepted (the cache is warm) but re-encryption "
                f"failed after {len(refreshed)} row(s) ({exc}); the "
                "rows' staleness names what did not heal"
            ),
        ) from None
    return refreshed


async def publish_unlocks(app, hub, accepted: list) -> None:
    """The same trail every row mutation leaves (#514 review): one
    audit row and one event per unlocked row, the coverage named
    like every lifecycle event's. Each frame's audience is the
    row's owner (#1090)."""
    for row, _value in accepted:
        audit_id = await app.state.model.record_audit("unlock", row)
        await hub.publish(
            "secret.unlock",
            {
                "placeholder_id": row["id"],
                "audit_id": audit_id,
                **coverage_fields(row),
                "name": row["name"],
                "ts": time.time(),
            },
            owner=row["owner_uid"],
        )
