"""The placeholder-secret routes (#198, #339, #305): mint, list,
renew, revoke, the audit trail, the pending and unlock exchange,
the probe endpoint, and a workspace's owner-wide coverage flip."""

import asyncio
import contextlib
import json
import logging
import time
from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import Response
from sqlalchemy.exc import IntegrityError

from ....model.secrets import SECRET_COVERAGES, coverage_label
from ....secretcache import NotEncryptableError
from ....secretstore import backend_ref, new_sentinel, valid_name
from ....spec.probe import (
    PROBE_HOST,
    PROBE_NAME,
    PROBE_PORT,
    PROBE_SECRET_B64,
    PROBE_USERNAME,
)
from ..deps import authed_caller, require_token
from ..rows import workspace_or_404
from ..schemas import (
    DEST_PATTERN,
    SecretCoverageSet,
    SecretMint,
    SecretRenew,
    SecretUnlock,
)
from .blobs import accept_unlock, mint_blobs_for
from .probe import arm_probe_row
from .views import (
    coverage_fields,
    owned_or_404,
    pending_row,
    placeholder_view,
    prior_deadline,
)

LOG = logging.getLogger(__name__)


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

    async def arm_targets(row: dict) -> list[str]:
        """The workspaces a placeholder's landing must arm (#339,
        #1083): its covered set, or every attached workspace of
        the row's owner for the owner-wide row — a refresh with no
        attachment is a no-op, so stopped workspaces ride along
        harmlessly."""
        if row["workspaces"]:
            return list(row["workspaces"])
        owned = await app.state.model.owned_workspace_ids(row["owner_uid"])
        return [
            workspace_id
            for workspace_id in app.state.net.attached_workspaces()
            if workspace_id in owned
        ]

    def detection_targets(armed: list[str]) -> list[str]:
        """Attached workspaces a placeholder's landing did not arm
        (#339, #1083): their entry tables gain the row's sentinel
        for detection only, so a foreign sentinel used from them
        publishes an off-allowlist sighting. The owner-wide row
        arms its own owner's attached workspaces; every other
        attached workspace — another owner's, or the owner's own
        scoped one — detects it."""
        armed_set = set(armed)
        return [
            workspace_id
            for workspace_id in app.state.net.attached_workspaces()
            if workspace_id not in armed_set
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

    @api.post("/api/v1/secrets")
    async def mint_secret(
        body: SecretMint, caller: int = Depends(authed_caller)
    ) -> Response:
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
        # The recording owner (#1083) is the caller verbatim:
        # workspace rows record the creating peer the same way
        # (#1082 — root creates as uid 0), so a row covers its
        # minter's own workspaces under the same match. The TCP
        # surface already maps every token caller onto the
        # daemon's uid at the auth seam, so the single-user
        # deployment's mints keep the reach they always had.
        owner = caller
        # Coverage resolution (#339): the two spellings cannot mix;
        # neither given mints the owner-wide row (the default), a
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
        coverage = sorted(set(await owned_coverage(app, refs, caller, caller)))
        label = coverage_label(coverage)
        if (
            await app.state.model.placeholder_for(
                coverage, body.name, owner_uid=owner
            )
            is not None
        ):
            raise HTTPException(
                status_code=409,
                detail=(
                    f"{label} already has a placeholder named {body.name}"
                ),
            )
        ref = backend_ref(owner, coverage, body.name)
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
        sentinel = new_sentinel(owner_wide=not coverage)
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
                    owner_uid=owner,
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
        # an owner-wide mint redirects every attached workspace of
        # the minting owner (#339, #1083). A mint that cannot arm stands
        # or falls whole — a live placeholder leaking its raw sentinel
        # toward the wire is the one outcome this feature exists to
        # prevent — so the row and its value roll back and the refusal
        # names the cause (#260 review).
        armed = await arm_targets(row)
        try:
            for workspace_id in armed:
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
            for workspace_id in armed:
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
        for workspace_id in detection_targets(armed):
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
        # The frame's audience is the minting owner (#1090).
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
            owner=owner,
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

    @api.get("/api/v1/secrets")
    async def list_secrets(
        caller: int = Depends(authed_caller),
    ) -> list[dict]:
        """Every live row the caller owns (#390, #1083), each
        carrying the store's lock state (#515): ``locked`` is True
        when the daemon's cache holds no plaintext for the row —
        the flag the client's listings surface so a cold row is
        seen, not discovered at the swap's refusal. A caller sees
        its own mints alone; root and the daemon's own uid see
        every owner's."""
        return [
            dict(
                placeholder_view(row, sentinel=False),
                locked=not app.state.secrets.plain.knows(row["backend_ref"]),
            )
            for row in await app.state.model.list_placeholders(viewer=caller)
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

    @api.post("/api/v1/secrets/{placeholder_id}/renew")
    async def renew_secret(
        placeholder_id: int,
        body: SecretRenew,
        caller: int = Depends(authed_caller),
    ) -> dict:
        prior = await app.state.model.get_placeholder(placeholder_id)
        if prior is None:
            raise HTTPException(status_code=404, detail="no such placeholder")
        owned_or_404(prior, caller)
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
        # exists to prevent (#260 review, round 5). An owner-wide
        # row's renew re-arms every attached workspace of its owner
        # (#339, #1083).
        try:
            for workspace_id in await arm_targets(row):
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

    @api.delete("/api/v1/secrets/{placeholder_id}")
    async def revoke_secret(
        placeholder_id: int,
        caller: int = Depends(authed_caller),
    ) -> dict:
        row = await app.state.model.get_placeholder(placeholder_id)
        if row is None:
            raise HTTPException(status_code=404, detail="no such placeholder")
        owned_or_404(row, caller)
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
        # (#305) — the dedup is load-bearing here, not a belt. The
        # frame's audience is the row's owner (#1090).
        await hub.publish(
            "secret.revoke",
            {
                "placeholder_id": placeholder_id,
                "audit_id": audit_id,
                **coverage_fields(row),
                "name": row["name"],
                "ts": time.time(),
            },
            owner=row["owner_uid"],
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

    @api.get("/api/v1/secrets/audit")
    async def list_secret_audit(
        caller: int = Depends(authed_caller),
    ) -> list[dict]:
        """The audit trail, newest first, the listing's owner rule
        (#1083): a caller reads its own rows' events alone; the
        bypass uids read every owner's."""
        return await app.state.model.list_audit(viewer=caller)

    @api.get("/api/v1/secrets/pending")
    async def pending_secrets(
        caller: int = Depends(authed_caller),
    ) -> list[dict]:
        """Every live row the caller owns (#1083), as ciphertext
        blobs with staleness flags and the cache's lock state
        (#514) — the fetch the client's unlock flow starts from;
        the bypass uids read every owner's rows. The reply marks
        each row's blobs with the workspace id that holds the
        decrypting key, so the client decrypts only what its
        identities open; the staleness map names the recipients
        the next unlock will refresh. Serving the list opens the
        session window an unlock requires (#508): only rows whose
        ciphertext went out under the live window accept plaintext
        back.
        """
        model = app.state.model
        rows = await model.list_placeholders(viewer=caller)
        app.state.secrets.plain.served([row["id"] for row in rows])
        return [await pending_row(app, row) for row in rows]

    @api.post("/api/v1/secrets/unlock")
    async def unlock_secrets(
        body: SecretUnlock,
        caller: int = Depends(authed_caller),
    ) -> dict:
        """Accept decrypted plaintext back into the daemon (#514,
        #1083): only the caller's own rows answer — the values
        enter the timeout-scoped cache — this POST is the operator
        session the timer resets on — and each row re-encrypts
        fresh blobs to its current recipient set at the same time,
        so a coverage edit heals here. A workspace's stored key is
        fixed at create (#41), so no key rotation reaches this
        refresh. A row whose
        ciphertext the pending list did not serve under the live
        window is refused by name: the operator fetches pending
        first.
        """
        # The store lock covers the whole mutate half: a revoke or
        # expiry sweep running between the row check and the blob
        # write would leave orphan ciphertext behind a deleted row
        # (#514 review) — the mint and the sweep hold the same
        # lock, so the sequence is one serial block.
        async with app.state.store_lock:
            return await accept_unlock(app, hub, body, caller)

    @api.get("/api/v1/secrets/{placeholder_id}")
    async def get_secret(
        placeholder_id: int,
        caller: int = Depends(authed_caller),
    ) -> dict:
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
        owned_or_404(row, caller)
        return {
            **placeholder_view(row),
            "locked": not app.state.secrets.plain.knows(row["backend_ref"]),
        }

    @api.put("/api/v1/workspaces/{workspace_id}/secret-coverage")
    async def set_secret_coverage(
        workspace_id: str,
        body: SecretCoverageSet,
        caller: int = Depends(authed_caller),
    ) -> dict:
        """Flip a workspace's owner-wide placeholder posture
        (#339, #1083): ``all`` takes owner-wide placeholder
        coverage — a live owner-wide row of the workspace's owner
        arms the workspace from the flip on — and ``scoped``
        exempts it (only placeholders minted directly at it arm it;
        an owner-wide sentinel used from it reads as an
        off-allowlist sighting).

        The armed state re-evaluates with the flip. A flip that
        cannot (re)arm restores the prior setting: a live
        owner-wide row without its redirect would leak the raw
        sentinel toward the wire — the outcome the mint rollback
        exists to prevent.
        """
        row = await workspace_or_404(app, workspace_id, caller)
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


async def owned_coverage(
    app, refs: list[str], caller: int, owner: int
) -> list[str]:
    """The mint's coverage resolution (#339, #1083): each ref
    resolves name-or-id through the route-facing seam (#1082's
    owner filter — a foreign ref answers 404, a plural name the
    409), and a workspace another owner holds is refused by name
    for the bypass callers that can still address it: a row this
    caller mints would cover none of it."""
    coverage: list[str] = []
    for ref in refs:
        workspace = await workspace_or_404(app, ref, caller)
        if workspace["owner_uid"] != owner:
            raise HTTPException(
                status_code=422,
                detail=(
                    f"workspace {ref} belongs to uid "
                    f"{workspace['owner_uid']}; a placeholder minted "
                    f"by uid {owner} covers none of its workspaces. "
                    "Mint as the owner instead."
                ),
            )
        coverage.append(workspace["id"])
    return coverage
