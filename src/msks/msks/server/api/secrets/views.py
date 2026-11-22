"""The API-facing shapes of placeholder rows (#198, #339, #1083):
the views the routes answer with, the lifecycle event frames'
coverage members, and the owner mask the mutation and visibility
routes take."""

import base64
from datetime import UTC, datetime

from fastapi import HTTPException

from ....model.workspaces import sees_all_rows


def owned_or_404(row: dict, caller: int) -> None:
    """The owner mask the mutation and visibility routes take
    (#1083): a row another owner holds answers the same 404 as an
    absent one — revoke, renew, the row and sentinel fetch, the
    pending list, and the unlock — while the bypass uids (root,
    the daemon's own) see every row, the listing's rule."""
    if not sees_all_rows(caller) and row["owner_uid"] != caller:
        raise HTTPException(status_code=404, detail="no such placeholder")


def prior_deadline(row: dict) -> datetime | None:
    """The row's pre-renew deadline, as a datetime the model takes
    back (None restores an unbounded lifetime). The stored value is
    naive UTC on the sqlite round-trip; replace() normalizes."""
    raw = row.get("expires_at")
    if raw is None:
        return None
    return datetime.fromisoformat(raw).replace(tzinfo=UTC)


def placeholder_view(row: dict, sentinel: bool = True) -> dict:
    """The API-facing view of a placeholder row (#198).

    The sentinel appears only when *sentinel* is set — mint's 201
    and the on-demand row fetch (#440) carry it; every listing
    view omits it. The value appears in no view at all (#423):
    the operator supplied it at mint and it is never echoed.
    ``workspaces`` is the row's coverage (#339): ``[]`` is the
    owner-wide row, and ``owner_uid`` (#1083) names the minting
    owner.
    """
    view = {
        "id": row["id"],
        "owner_uid": row["owner_uid"],
        "workspaces": row["workspaces"],
        "name": row["name"],
        "dests": row["dests"],
        "created_at": row["created_at"],
        "expires_at": row["expires_at"],
    }
    if sentinel:
        view["sentinel"] = row["sentinel"]
    return view


async def pending_row(app, row: dict) -> dict:
    """One row's pending view (#514): the row's identity and
    coverage, one base64 ciphertext per recipient workspace, the
    staleness map (#513's rule), and whether the daemon's cache
    holds the plaintext — the lock the operator's client reads
    before offering an unlock."""
    blobs = await app.state.model.secret_blobs(row["id"])
    staleness = await app.state.model.secret_blob_staleness(row["id"])
    return {
        "id": row["id"],
        "workspaces": row["workspaces"],
        "name": row["name"],
        "dests": row["dests"],
        "created_at": row["created_at"],
        "expires_at": row["expires_at"],
        "blobs": [
            {
                "recipient": blob["recipient"],
                "ciphertext": base64.b64encode(blob["ciphertext"]).decode(),
            }
            for blob in blobs
        ],
        "staleness": staleness
        if staleness is not None
        else {"missing": [], "stale": []},
        "unlocked": app.state.secrets.plain.knows(row["backend_ref"]),
    }


def coverage_fields(row: dict) -> dict:
    """A lifecycle event's coverage members (#339): the full list,
    plus the legacy ``workspace_id`` string every frame keeps —
    ``*`` for the owner-wide row, the first covered id scoped —
    so an older client's parser still keys the row while the list
    carries the truth."""
    workspaces = row["workspaces"]
    return {
        "workspace_id": workspaces[0] if workspaces else "*",
        "workspaces": workspaces,
    }
