"""The placeholder-secret routes (#198, #339, #305): mint, list,
check, renew, revoke, the audit trail, and a workspace's
daemon-wide coverage flip."""

import asyncio
import contextlib
import json
import logging
import time
from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import Response
from sqlalchemy.exc import IntegrityError

from ...model.secrets import SECRET_COVERAGES, coverage_label
from ...secretstore import (
    SecretStoreError,
    backend_ref,
    new_secret_value,
    new_sentinel,
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
)

LOG = logging.getLogger(__name__)


async def seed_probe_placeholder(app) -> None:
    """Seed the probe placeholder at first-time startup (#424).

    The daemon-wide row named ``probe``: allowlisting the probe
    host, carrying the fixed credential blob as its secret, so the
    probe works with zero operator minting — the sentinel is read
    back over the authenticated API (``GET /api/v1/probe``).
    Seeded exactly when the placeholder table is empty — a fresh
    daemon's first startup; a daemon that already holds rows
    (an operator's mints, or a migrated state) keeps them
    untouched. A store that cannot serve rolls the row back and
    the daemon stays up without it (loud, non-fatal — the
    bootstrap default image's posture: fixing the store and
    restarting with an empty table is the recovery, because a
    row minted by hand cannot carry the probe's fixed credential
    (#423 — values are daemon-minted)).

    Runs before any workspace can be attached, so no arming step
    belongs here: the placeholder-driven arm happens at each
    attach as always.
    """
    model = app.state.model
    if app.state.secrets.settings.root is None:
        # A store with no root (a directly-constructed Settings in
        # tests) has no manifest to declare the ref and no provider
        # to hold the value — nothing to seed into.
        return
    if await model.list_placeholders():
        return
    sentinel = new_sentinel(daemon_wide=True)
    ref = backend_ref([], PROBE_NAME)
    try:
        row = await model.create_placeholder(
            [], PROBE_NAME, sentinel, [PROBE_HOST], ref, None
        )
    except IntegrityError:
        # A racing first boot won the unique index: its row stands,
        # this one's work is done.
        return
    try:
        await sync_probe_manifest(app)
        await app.state.secrets.write(ref, PROBE_SECRET_B64)
        await model.record_audit("mint", row)
    except Exception:  # noqa: BLE001 - named below, non-fatal
        # A seed whose row cannot be declared, valued, or audited
        # rolls back whole — a live daemon-wide row with no value
        # behind its ref would answer every swap fail-closed — and
        # the daemon stays up without the probe row (the mint
        # route's own posture).
        await model.delete_placeholder(row["id"])
        with contextlib.suppress(Exception):
            await sync_probe_manifest(app)
        LOG.exception("probe placeholder seed failed; rolled back")


async def sync_probe_manifest(app) -> None:
    """Re-render the store manifest off the live rows (off the
    event loop — it is file IO, the mint route's own shape)."""
    refs = await app.state.model.placeholder_refs()
    await asyncio.to_thread(app.state.secrets.sync_manifest, refs)


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
    carries it exactly once; every later view omits it. The minted
    value rides that one reply too (#423): the route injects it
    beside the sentinel, and no later view carries it either.
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

    async def sync_store_manifest() -> None:
        """Re-render the store manifest off the live placeholder rows
        (off the event loop — it is file IO)."""
        refs = await app.state.model.placeholder_refs()
        await asyncio.to_thread(app.state.secrets.sync_manifest, refs)

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
        value = new_secret_value()
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
            # The manifest must declare the ref before the CLI can
            # write it; the row is in, so the live-rows sync carries
            # it. The identity mint and the manifest write raise
            # OSError siblings of the store's own errors (an
            # unwritable root, a full disk) and leave the same
            # valueless live row behind — both roll back here too.
            try:
                await sync_store_manifest()
                await app.state.secrets.write(ref, value)
            except (SecretStoreError, OSError) as exc:
                # Roll the row back: a placeholder whose value never
                # landed would swap empty on the wire.
                await app.state.model.delete_placeholder(row["id"])
                with contextlib.suppress(SecretStoreError, OSError):
                    await sync_store_manifest()
                raise HTTPException(status_code=503, detail=str(exc)) from None
        # The sentinel appears in exactly one response: this one.
        # Arming is placeholder-driven (#199): a scoped mint against
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
                await app.state.model.delete_placeholder(row["id"])
                with contextlib.suppress(SecretStoreError, OSError):
                    await app.state.secrets.delete(ref)
                with contextlib.suppress(SecretStoreError, OSError):
                    await sync_store_manifest()
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
        # The value and the sentinel appear in exactly one
        # response: this one (#423). The operator pastes the value
        # into the external service; workspaces keep receiving
        # only the sentinel.
        view = placeholder_view(row)
        view["value"] = value
        return Response(
            status_code=201,
            content=json.dumps(view),
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
        token holder does not already hold (the llm-token route's
        rationale). A revoked or re-scoped row answers 404 with the
        reason in the detail: values are daemon-minted (#423), so a
        row minted by hand cannot carry the probe's fixed
        credential — the seed is the only mint that can.
        """
        row = await app.state.model.placeholder_for([], PROBE_NAME)
        if row is None:
            raise HTTPException(
                status_code=404,
                detail=(
                    "the probe placeholder is absent; the daemon seeds "
                    "it at startup when the placeholder table is empty — "
                    "values are daemon-minted, so a row minted by hand "
                    "cannot carry the probe's fixed credential"
                ),
            )
        return {
            "placeholder_id": row["id"],
            "sentinel": row["sentinel"],
            "host": PROBE_HOST,
            "port": PROBE_PORT,
            "username": PROBE_USERNAME,
            "secret": PROBE_SECRET_B64,
        }

    @api.post("/api/v1/secrets/check", dependencies=[Depends(require_token)])
    async def check_secret_store() -> dict:
        # The store lock serializes concurrent checks: two probes
        # share the root's probe manifest path, and an interleaved
        # pair would clobber each other's declarations mid-probe.
        # The OSError siblings (an unwritable root, a full disk)
        # answer the same way — the unwritable root is the
        # misconfig this endpoint exists to name (#423 review).
        async with app.state.store_lock:
            try:
                return await app.state.secrets.check()
            except (SecretStoreError, OSError) as exc:
                raise HTTPException(status_code=503, detail=str(exc)) from None

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
        cleaned = True
        async with app.state.store_lock:
            # Row first: revocation takes effect on the next request,
            # whatever the store cleanup then does.
            await app.state.model.delete_placeholder(placeholder_id)
            try:
                await app.state.secrets.delete(row["backend_ref"])
            except SecretStoreError, OSError:
                # The row is gone, so the leftover value is inert; the
                # operator sees it in the response and can re-run
                # check.
                cleaned = False
            audit_id = await app.state.model.record_audit("revoke", row)
            await sync_store_manifest()
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
        return {"revoked": placeholder_id, "store_cleaned": cleaned}

    @api.get("/api/v1/secrets/audit", dependencies=[Depends(require_token)])
    async def list_secret_audit() -> list[dict]:
        return await app.state.model.list_audit()

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
