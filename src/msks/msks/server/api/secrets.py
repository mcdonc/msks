"""The placeholder-secret routes (#198, #339, #305): mint, list,
check, renew, revoke, the audit trail, and a workspace's
daemon-wide coverage flip."""

import asyncio
import base64
import contextlib
import json
import logging
import time
from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import Response
from sqlalchemy.exc import IntegrityError

from ...model.secrets import SECRET_COVERAGES, coverage_label
from ...secretcache import NotEncryptableError
from ...secretstore import (
    backend_ref,
    blob_recipients,
    eligible_recipients,
    new_sentinel,
    recipient_hashes,
    valid_name,
)
from ...spec.probe import (
    PROBE_HOST,
    PROBE_NAME,
    PROBE_PORT,
    PROBE_SECRET_B64,
    PROBE_USERNAME,
)
from .deps import require_token
from .rows import workspace_or_404
from .schemas import (
    DEST_PATTERN,
    SecretCoverageSet,
    SecretMint,
    SecretRenew,
    SecretUnlock,
)

LOG = logging.getLogger(__name__)


async def seed_probe_placeholder(app) -> None:
    """Seed the probe placeholder at first-time startup (#424).

    The daemon-wide row named ``probe``: allowlisting the probe
    host, carrying the fixed credential blob as its secret, so the
    probe works with zero operator minting — the sentinel is read
    back over the authenticated API (``GET /api/v1/probe``).
    Seeded exactly when the placeholder table is empty — a fresh
    daemon's first startup; a daemon that already holds the row
    re-arms it every boot instead (#514 review: the credential is
    public, so the daemon keeps its own probe swapping across
    restarts). The two halves fail differently: a seed whose row
    cannot be created, valued, or audited rolls the row back and
    the daemon stays up without it (loud, non-fatal — the
    bootstrap default image's posture: the operator can mint the
    row by hand with the fixed credential as the value), while a
    failing re-arm only logs — the row keeps exactly what it had.

    Runs before any workspace can be attached, so no arming step
    belongs here: the placeholder-driven arm happens at each
    attach as always.
    """
    model = app.state.model
    ref = backend_ref([], PROBE_NAME)
    existing = await model.placeholder_by_ref(ref)
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
    await mint_probe_row(app, ref)


async def mint_probe_row(app, ref: str) -> None:
    """The first boot's seed half (#424): create the row, value
    it through the legacy store, arm it, and audit the mint. A
    failure rolls the row back whole and the daemon stays up
    without it."""
    model = app.state.model
    sentinel = new_sentinel(daemon_wide=True)
    try:
        row = await model.create_placeholder(
            [], PROBE_NAME, sentinel, [PROBE_HOST], ref, None
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
        # rolls back whole — a live daemon-wide row with no value
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
        await app.state.model.secret_recipient_keys(row["workspaces"])
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
    daemon-wide row.
    """
    view = {
        "id": row["id"],
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
    ``*`` for the daemon-wide row, the first covered id scoped —
    so an older client's parser still keys the row while the list
    carries the truth."""
    workspaces = row["workspaces"]
    return {
        "workspace_id": workspaces[0] if workspaces else "*",
        "workspaces": workspaces,
    }


async def mint_blobs_for(
    app, row: dict, coverage: list[str], value: str
) -> None:
    """Encrypt one mint's value to every covering workspace key
    and store the blobs (#514), caching the plaintext for the
    unlock window — the minting operator just supplied it, so the
    swap path stands ready from the 201 on.

    The named refusals: a scoped row's covered workspace without
    an ssh public key, or with a key the crypto leaf cannot
    encrypt to, and a daemon-wide row no accepting workspace can
    receive — each names the workspace and the constraint
    (:class:`NotEncryptableError`).
    """
    recipients = await app.state.model.secret_recipient_keys(coverage)
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
        # A daemon-wide row mints one blob per accepting workspace
        # whose key can receive it; incapable acceptors are
        # skipped (staleness names them), and a daemon with no
        # capable acceptor at all takes the named refusal.
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


async def validated_unlock_row(app, item) -> tuple[dict, str]:
    """One unlock item's checks (#514): the row exists, its
    ciphertext was served under the live session window, and the
    value survives the same strip the mint applies. Returns the
    row and its stripped value."""
    row = await app.state.model.get_placeholder(item.placeholder_id)
    if row is None:
        raise HTTPException(status_code=404, detail="no such placeholder")
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
    recipient set (#514's lazy rule): coverage edits and key
    rotations heal at the unlock, because the fresh blobs replace
    the whole set. Recipients whose keys cannot hold a blob are
    skipped — they read back from staleness, and one incapable
    key never blocks a row's others. A row whose eligible set is
    EMPTY keeps its existing blobs untouched: they are the only
    ciphertext copies of the value, and a replace-all write with
    no recipients would drop them for good (the mint refuses this
    same condition by name; the unlock answers it by keeping the
    old set until coverage heals). Returns the fresh staleness.
    """
    recipients = eligible_recipients(
        await app.state.model.secret_recipient_keys(row["workspaces"])
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


async def accept_unlock(app, hub, body) -> dict:
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
        for row, value in [await validated_unlock_row(app, item)]
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
    like every lifecycle event's."""
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
        )


def router(app, hub) -> APIRouter:
    """The placeholder-secret routes and the store-manifest
    closures they share."""
    api = APIRouter()

    # --- secrets (#198) -----------------------------------------------

    # One lock around every placeholder-mutating sequence (mint,
    # revoke, expiry sweep): the refs-query → manifest-write pair is
    # a TOCTOU otherwise — a stale snapshot landing last would strip
    # a live declaration, and a same-label mint race could leave an
    # uncertified value behind a 201-certified row. Store operations
    # are rare, so one global lock costs nothing.
    app.state.store_lock = asyncio.Lock()

    async def refresh_quietly(workspace_id: str) -> None:
        """Re-evaluate the armed state, logging instead of failing:
        the operation that triggered it already stood, and the next
        placeholder event (or the next attach) retries the arm."""
        try:
            await app.state.interceptor.refresh(workspace_id)
        except Exception:  # noqa: BLE001 - logged, never fatal here
            LOG.exception(
                "interceptor refresh for %s failed; armed state retries "
                "on the next placeholder event",
                workspace_id,
            )

    def arm_targets(coverage: list[str]) -> list[str]:
        """The workspaces a placeholder's landing must arm (#339):
        its covered set, or every attached workspace for the
        daemon-wide row — a refresh with no attachment is a no-op,
        so stopped workspaces ride along harmlessly."""
        return coverage or app.state.net.attached_workspaces()

    def detection_targets(coverage: list[str]) -> list[str]:
        """Attached workspaces outside a row's coverage (#339): their
        entry tables gain the row's sentinel for detection only, so
        a foreign sentinel used from them publishes an
        off-allowlist sighting. A daemon-wide row covers them all,
        so it has none."""
        if not coverage:
            return []
        covered = set(coverage)
        return [
            workspace_id
            for workspace_id in app.state.net.attached_workspaces()
            if workspace_id not in covered
        ]

    def validated_dests(dests: list[str]) -> list[str]:
        """Lowercased, de-duplicated, pattern-checked destinations."""
        seen = []
        for dest in dests:
            entry = dest.lower().rstrip(".")
            if not DEST_PATTERN.match(entry):
                raise HTTPException(
                    status_code=422,
                    detail=(
                        f"dest {dest!r} must be an exact hostname or a "
                        "label-anchored suffix like .example.com"
                    ),
                )
            if entry not in seen:
                seen.append(entry)
        # An empty list never reaches here: pydantic's min_length=1
        # on the request model rejects it at validation.
        return seen

    @api.post("/api/v1/secrets", dependencies=[Depends(require_token)])
    async def mint_secret(body: SecretMint) -> Response:
        if not valid_name(body.name):
            raise HTTPException(
                status_code=422,
                detail=(
                    "name must be letters, numbers, or underscores "
                    "without a leading digit"
                ),
            )
        if not body.value.strip():
            raise HTTPException(status_code=422, detail="the value is empty")
        # Stripped here, at the daemon, the same way both clients
        # strip: the store's secretspec `set` trims stdin itself
        # (0.20), so an unstripped value would leave the daemon's
        # cache and the agefile holding different bytes (#423
        # follow-up review) — one strip at the door keeps them one.
        # The strip drops a UTF-8 BOM too, whatever door the value
        # arrived through: a BOM would send every swap an invisible
        # prefix no external service accepts.
        value = body.value.strip().lstrip("\ufeff").strip()
        dests = validated_dests(body.dests)
        # Coverage resolution (#339): the two spellings cannot mix;
        # neither given mints the daemon-wide row (the default), a
        # non-empty list scopes the row to exactly those ids (each
        # ref resolved name-or-id, #246, to the immutable id).
        if body.workspace_id is not None and body.workspaces is not None:
            raise HTTPException(
                status_code=422,
                detail=(
                    "workspace_id and workspaces are two spellings of "
                    "the mint's coverage; send one"
                ),
            )
        refs = list(body.workspaces or [])
        if body.workspace_id is not None:
            refs = [body.workspace_id]
        coverage: list[str] = []
        for ref in refs:
            workspace = await app.state.model.get_workspace(ref)
            if workspace is None:
                raise HTTPException(
                    status_code=404, detail=f"no such workspace: {ref}"
                )
            coverage.append(workspace["id"])
        coverage = sorted(set(coverage))
        label = coverage_label(coverage)
        if (
            await app.state.model.placeholder_for(coverage, body.name)
            is not None
        ):
            raise HTTPException(
                status_code=409,
                detail=(
                    f"{label} already has a placeholder named {body.name}"
                ),
            )
        ref = backend_ref(coverage, body.name)
        if await app.state.model.placeholder_by_ref(ref) is not None:
            # Distinct labels can sanitize to one ref; the collision
            # is answered before anything touches the shared store
            # entry (a 409 here keeps manifest and value intact).
            raise HTTPException(
                status_code=409,
                detail=(
                    f"{label}/{body.name} collides with an"
                    f" existing placeholder on backend ref {ref};"
                    " pick another name"
                ),
            )
        expires = (
            datetime.now(UTC) + timedelta(seconds=body.ttl_s)
            if body.ttl_s is not None
            else None
        )
        sentinel = new_sentinel(daemon_wide=not coverage)
        async with app.state.store_lock:
            # Row before value, all under the store lock: an
            # uncertified byte can never land behind a ref a winning
            # row does not own — a losing same-label mint 409s on the
            # insert (or the pre-checks) and never touches the store,
            # and a failed write rolls its own row back.
            try:
                row = await app.state.model.create_placeholder(
                    coverage,
                    body.name,
                    sentinel,
                    dests,
                    ref,
                    expires,
                )
            except IntegrityError:
                raise HTTPException(
                    status_code=409, detail="placeholder name collision"
                ) from None
            # The value lands as one ciphertext blob per covering
            # workspace key (#514): the daemon holds no plaintext
            # at rest — the mint's own plaintext rides the unlock
            # cache until its window ends. Recipients are read
            # under the same lock: the refusal names a workspace
            # whose key cannot receive a blob, and the row rolls
            # back whole.
            try:
                await mint_blobs_for(app, row, coverage, value)
            except (NotEncryptableError, OSError) as exc:
                # Roll the row back: a placeholder whose blobs never
                # landed would swap empty on the wire.
                await app.state.model.delete_placeholder(row["id"])
                app.state.secrets.plain.forget([ref], [row["id"]])
                raise HTTPException(
                    status_code=(
                        422 if isinstance(exc, NotEncryptableError) else 503
                    ),
                    detail=str(exc),
                ) from None
        # The sentinel rides this reply and the on-demand row fetch
        # (#440). Arming is placeholder-driven (#199): a scoped mint against
        # a running workspace redirects its web egress from here;
        # a daemon-wide mint redirects every attached workspace's
        # (#339). A mint that cannot arm stands or falls whole — a
        # live placeholder leaking its raw sentinel toward the wire
        # is the one outcome this feature exists to prevent — so the
        # row and its value roll back and the refusal names the
        # cause (#260 review).
        try:
            for workspace_id in arm_targets(coverage):
                await app.state.interceptor.refresh(workspace_id)
        except Exception as exc:  # noqa: BLE001 - rolled back below
            async with app.state.store_lock:
                # delete_placeholder drops the row's blobs with it
                # (#514); the plaintext leaves the cache here too.
                await app.state.model.delete_placeholder(row["id"])
                app.state.secrets.plain.forget([ref], [row["id"]])
            # A target that armed before a sibling's refresh failed
            # keeps its redirect over a row that no longer exists —
            # the covering set is empty now, so the quiet sweep
            # disarms it (#339 review).
            for workspace_id in arm_targets(coverage):
                await refresh_quietly(workspace_id)
            raise HTTPException(
                status_code=503,
                detail=(
                    "mint rolled back: the coverage's interceptor "
                    f"could not arm ({exc})"
                ),
            ) from exc
        # Every other armed workspace's entry table gains the new
        # sentinel for detection (#339): a foreign sentinel used
        # from it reads as an off-allowlist sighting. Best-effort —
        # the row stands, and the next placeholder event retries.
        for workspace_id in detection_targets(coverage):
            await refresh_quietly(workspace_id)
        # The mint record and its event trail the arm: a rolled-back
        # mint never existed, so the audit table and the stream say
        # nothing about it (#260 review). The reverse edge is
        # accepted knowingly: an audit/publish failure here 500s
        # with the placeholder live and armed — recovery is revoke
        # and re-mint, and suppressing it would silently drop the
        # mint's trail instead.
        audit_id = await app.state.model.record_audit("mint", row)
        # The audit row commits before this publish: a registration
        # whose replay read lands in the gap delivers the row twice,
        # and the client's audit-id dedup drops the second (#305).
        await hub.publish(
            "secret.mint",
            {
                "placeholder_id": row["id"],
                "audit_id": audit_id,
                **coverage_fields(row),
                "name": body.name,
                "dests": dests,
                "ts": time.time(),
            },
        )
        # The sentinel rides this reply and the on-demand row fetch
        # (#440); the value is the operator's own and rides no
        # response at all (#423): it was supplied at mint and never
        # echoed.
        return Response(
            status_code=201,
            content=json.dumps(placeholder_view(row)),
            media_type="application/json",
        )

    @api.get("/api/v1/secrets", dependencies=[Depends(require_token)])
    async def list_secrets() -> list[dict]:
        return [
            placeholder_view(row, sentinel=False)
            for row in await app.state.model.list_placeholders()
        ]

    @api.get("/api/v1/probe", dependencies=[Depends(require_token)])
    async def probe_endpoint() -> dict:
        """The seeded probe placeholder's sentinel and recipe (#424),
        token-gated.

        The credential this placeholder swaps to is a fixed public
        probe value — the sentinel gates nothing an attacker wants —
        so serving it over the authenticated API costs nothing a
        token holder does not already hold (the same rationale the
        workspace identity's public half carries). A revoked or
        re-scoped row answers 404 with the re-mint recipe in the
        detail: the credential is the fixed blob the endpoint
        itself serves.
        """
        # Every sentinel fetch re-arms the row (#514 review,
        # delta): an unlock window that expired since the last arm
        # would otherwise leave the probe locked mid-run, and this
        # endpoint already serves the credential it re-arms with.
        # The store lock covers the read-and-arm pair — a revoke or
        # expiry sweep holding the same lock cannot land between
        # the row read and the arm, so the arm never re-populates
        # a revoked row's cache entry or writes orphan ciphertext
        # behind a deleted rowid. Best-effort — a read endpoint
        # never fails because the arm could not.
        async with app.state.store_lock:
            row = await app.state.model.placeholder_for([], PROBE_NAME)
            if row is None:
                raise HTTPException(
                    status_code=404,
                    detail=(
                        "the probe placeholder is absent; it is seeded "
                        "at daemon startup, or mint it by hand with "
                        f"dests [{PROBE_HOST!r}] and the value "
                        f"{PROBE_SECRET_B64} (the fixed probe "
                        "credential)"
                    ),
                )
            try:
                await arm_probe_row(app, row)
            except Exception:  # noqa: BLE001 - logged, the read stands
                LOG.exception("probe placeholder re-arm at read failed")
        return {
            "placeholder_id": row["id"],
            "sentinel": row["sentinel"],
            "host": PROBE_HOST,
            "port": PROBE_PORT,
            "username": PROBE_USERNAME,
            "secret": PROBE_SECRET_B64,
        }

    @api.post(
        "/api/v1/secrets/{placeholder_id}/renew",
        dependencies=[Depends(require_token)],
    )
    async def renew_secret(placeholder_id: int, body: SecretRenew) -> dict:
        prior = await app.state.model.get_placeholder(placeholder_id)
        if prior is None:
            raise HTTPException(status_code=404, detail="no such placeholder")
        expires = datetime.now(UTC) + timedelta(seconds=body.ttl_s)
        await app.state.model.renew_placeholder(placeholder_id, expires)
        row = await app.state.model.get_placeholder(placeholder_id)
        if row is None:
            # The expiry sweep can retire the row between the two
            # reads; a renew that lost its row answers 404, not 500.
            raise HTTPException(status_code=404, detail="no such placeholder")
        # A renew can revive a workspace's last live placeholder, so
        # the armed state re-evaluates (#199). A renew that cannot
        # (re)arm restores the deadline it extended: a live
        # placeholder without its redirect would leak the raw
        # sentinel toward the wire — the outcome the mint rollback
        # exists to prevent (#260 review, round 5). A daemon-wide
        # row's renew re-arms every attached workspace (#339).
        try:
            for workspace_id in arm_targets(row["workspaces"]):
                await app.state.interceptor.refresh(workspace_id)
        except Exception as exc:  # noqa: BLE001 - rolled back below
            await app.state.model.renew_placeholder(
                placeholder_id, prior_deadline(prior)
            )
            raise HTTPException(
                status_code=503,
                detail=(
                    "renew rolled back: the coverage's interceptor "
                    f"could not arm ({exc})"
                ),
            ) from exc
        return placeholder_view(row, sentinel=False)

    @api.delete(
        "/api/v1/secrets/{placeholder_id}",
        dependencies=[Depends(require_token)],
    )
    async def revoke_secret(placeholder_id: int) -> dict:
        row = await app.state.model.get_placeholder(placeholder_id)
        if row is None:
            raise HTTPException(status_code=404, detail="no such placeholder")
        async with app.state.store_lock:
            # Row first: revocation takes effect on the next request.
            # The row's blobs go in the same transaction and the
            # cached plaintext and served marker with them: a
            # revoked row's window is closed, and a marker left
            # behind could admit an unlock for a rowid a later
            # re-mint inherits (#514 review).
            await app.state.model.delete_placeholder(placeholder_id)
            app.state.secrets.plain.forget(
                [row["backend_ref"]], [placeholder_id]
            )
            audit_id = await app.state.model.record_audit("revoke", row)
        # The lock above released before the publish: a registration
        # in the commit-to-publish gap replays the row, and the
        # client's audit-id dedup drops whichever delivery is second
        # (#305) — the dedup is load-bearing here, not a belt.
        await hub.publish(
            "secret.revoke",
            {
                "placeholder_id": placeholder_id,
                "audit_id": audit_id,
                **coverage_fields(row),
                "name": row["name"],
                "ts": time.time(),
            },
        )
        # The redirect stands down when the last placeholder went
        # (#199) — the row is already gone, so refresh reads the new
        # state. Every attached workspace re-evaluates (#339): the
        # covered set disarms when the row was its last, and every
        # other armed table drops the sentinel from its detection
        # half. The revoke stands even when a re-evaluation fails:
        # the row is deleted, so nothing swaps either way.
        for workspace_id in app.state.net.attached_workspaces():
            await refresh_quietly(workspace_id)
        return {"revoked": placeholder_id}

    @api.get("/api/v1/secrets/audit", dependencies=[Depends(require_token)])
    async def list_secret_audit() -> list[dict]:
        return await app.state.model.list_audit()

    @api.get("/api/v1/secrets/pending", dependencies=[Depends(require_token)])
    async def pending_secrets() -> list[dict]:
        """Every live row's ciphertext blobs, with staleness flags
        and the cache's lock state (#514) — the fetch the client's
        unlock flow starts from. The reply marks each row's blobs
        with the workspace id that holds the decrypting key, so
        the client decrypts only what its identities open; the
        staleness map names the recipients the next unlock will
        refresh. Serving the list opens the session window an
        unlock requires (#508): only rows whose ciphertext went
        out under the live window accept plaintext back.
        """
        model = app.state.model
        rows = await model.list_placeholders()
        app.state.secrets.plain.served([row["id"] for row in rows])
        return [await pending_row(app, row) for row in rows]

    @api.post("/api/v1/secrets/unlock", dependencies=[Depends(require_token)])
    async def unlock_secrets(body: SecretUnlock) -> dict:
        """Accept decrypted plaintext back into the daemon (#514):
        the values enter the timeout-scoped cache — this POST is
        the operator session the timer resets on — and each row
        re-encrypts fresh blobs to its current recipient set at
        the same time, so coverage edits and key rotations heal
        here. A row whose ciphertext the pending list did not
        serve under the live window is refused by name: the
        operator fetches pending first.
        """
        # The store lock covers the whole mutate half: a revoke or
        # expiry sweep running between the row check and the blob
        # write would leave orphan ciphertext behind a deleted row
        # (#514 review) — the mint and the sweep hold the same
        # lock, so the sequence is one serial block.
        async with app.state.store_lock:
            return await accept_unlock(app, hub, body)

    @api.get(
        "/api/v1/secrets/{placeholder_id}",
        dependencies=[Depends(require_token)],
    )
    async def get_secret(placeholder_id: int) -> dict:
        """One placeholder row with its sentinel (#440),
        token-gated — the fetch the TUI's Enter-on-a-row panel
        makes. The listing omits sentinels; this reply carries
        the row's, the same posture the probe endpoint serves
        under (#424): a token holder already holds the daemon's
        operator surface, so reading a sentinel back costs
        nothing the holder does not already hold. The value
        stays absent from every view (#423); ``locked`` names the
        cache's state (#514) — false while the row's plaintext
        stands ready to swap, true when the next swap would
        answer the locked refusal.

        Registered below the audit route on purpose: FastAPI
        matches in registration order, and a parameterized GET
        above it would shadow the literal ``/audit`` path with a
        422."""
        row = await app.state.model.get_placeholder(placeholder_id)
        if row is None:
            raise HTTPException(status_code=404, detail="no such placeholder")
        return {
            **placeholder_view(row),
            "locked": not app.state.secrets.plain.knows(row["backend_ref"]),
        }

    @api.put(
        "/api/v1/workspaces/{workspace_id}/secret-coverage",
        dependencies=[Depends(require_token)],
    )
    async def set_secret_coverage(
        workspace_id: str, body: SecretCoverageSet
    ) -> dict:
        """Flip a workspace's daemon-wide placeholder posture
        (#339): ``all`` takes daemon-wide placeholder coverage — a
        live daemon-wide row arms the workspace from the flip on —
        and ``scoped`` exempts it (only placeholders minted directly
        at it arm it; a daemon-wide sentinel used from it reads as
        an off-allowlist sighting).

        The armed state re-evaluates with the flip. A flip that
        cannot (re)arm restores the prior setting: a live
        daemon-wide row without its redirect would leak the raw
        sentinel toward the wire — the outcome the mint rollback
        exists to prevent.
        """
        row = await workspace_or_404(app, workspace_id)
        workspace_id = row["id"]
        if body.secret_coverage not in SECRET_COVERAGES:
            raise HTTPException(
                status_code=400,
                detail=(
                    "secret_coverage must be one of "
                    f"{list(SECRET_COVERAGES)}, got "
                    f"{body.secret_coverage!r}"
                ),
            )
        prior = row.get("secret_coverage") or "all"
        wrote = await app.state.model.set_secret_coverage(
            workspace_id, body.secret_coverage
        )
        if not wrote:
            raise HTTPException(status_code=404, detail="no such workspace")
        try:
            await app.state.interceptor.refresh(workspace_id)
        except Exception as exc:  # noqa: BLE001 - rolled back below
            with contextlib.suppress(Exception):
                await app.state.model.set_secret_coverage(workspace_id, prior)
            raise HTTPException(
                status_code=503,
                detail=(
                    "coverage flip rolled back: the workspace's "
                    f"interceptor could not re-arm ({exc})"
                ),
            ) from exc
        return {
            "workspace_id": workspace_id,
            "secret_coverage": body.secret_coverage,
        }

    return api
