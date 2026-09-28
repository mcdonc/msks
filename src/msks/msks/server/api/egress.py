"""The egress-consent routes (#69, #280): the rule-management
view, the posture switch, the held-request trail, and a decider's
verdict on a hold."""

import contextlib
import logging

from fastapi import APIRouter, Depends, HTTPException, Query

from ...spec.egress import (
    DECISION_ALLOWED,
    DECISIONS,
    DURATIONS,
    EGRESS_MODES,
    MODE_STATIC,
    EgressPolicy,
    parse_allowlist,
)
from .deps import require_token
from .rows import workspace_or_404
from .schemas import EgressDecide, EgressPolicySet
from .volumes import move_lock

LOG = logging.getLogger(__name__)


async def egress_consent_allowed(app, workspace_id: str) -> bool:
    """Whether an in-effect allowed verdict covers the workspace
    (#280) — the static switch's other escape from the
    nothing-effectively-allowed guard: an ``allow forever`` a
    decider granted is as good as an allowlist entry."""
    rows = await app.state.model.egress_consent.list_active(workspace_id)
    return any(row["decision"] == DECISION_ALLOWED for row in rows)


async def verdict_refusal(app, workspace_id, request_id, body) -> tuple | None:
    """(status, detail) when a decide request is malformed or names
    another workspace's hold — validated here so an invalid value
    answers a 400 (and a foreign hold a 404) instead of silently
    denying the held SYN and stranding its row."""
    if body.decision not in ("allow", "deny"):
        return 400, f"decision must be allow or deny, got {body.decision!r}"
    if body.duration not in DURATIONS:
        return 400, f"duration must be one of {list(DURATIONS)}"
    row = await app.state.model.egress_consent.get_request(request_id)
    if row is None or row["workspace_id"] != workspace_id:
        return 404, "no consent request with that id for that workspace"
    return None


def router(app) -> APIRouter:
    """The egress-consent routes."""
    api = APIRouter()

    # --- egress consent (#69) ------------------------------------------------

    @api.get(
        "/api/v1/workspaces/{workspace_id}/egress",
        dependencies=[Depends(require_token)],
    )
    async def get_egress(workspace_id: str) -> dict:
        """The rule-management view: mode, static allowlist, and the
        in-effect verdicts."""
        row = await workspace_or_404(app, workspace_id)
        workspace_id = row["id"]
        frame = await app.state.consent.rules_frame(workspace_id)
        return frame or {"workspace_id": workspace_id}

    @api.put(
        "/api/v1/workspaces/{workspace_id}/egress/policy",
        dependencies=[Depends(require_token)],
    )
    async def set_egress_policy(
        workspace_id: str, body: EgressPolicySet
    ) -> dict:
        """Switch a workspace's egress posture (#280): the row's
        mode (and, when given, its allowlist) change now; a running
        workspace swaps its whole table in one nft transaction —
        established flows survive — and a stopped one builds the
        new posture at its next start. Verdicts carry: an
        ``allow forever`` granted under one mode keeps acting under
        the next.

        Switching to ``static`` with nothing effectively allowed
        (an empty allowlist and no in-effect allowed verdict) is
        refused unless the request confirms it — that posture
        answers every name NXDOMAIN, an offline workspace, and the
        refusal names the escape.
        """
        row = await workspace_or_404(app, workspace_id)
        workspace_id = row["id"]
        if body.mode not in EGRESS_MODES:
            raise HTTPException(
                status_code=400,
                detail=f"egress_mode must be one of {list(EGRESS_MODES)}, "
                f"got {body.mode!r}",
            )
        try:
            parse_allowlist(body.allow_list or [])
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from None
        async with move_lock(app, workspace_id):
            # Re-read under the lock (#280 review, round 2): the
            # specs an omitted allow_list keeps and the facts a
            # failed swap rolls back to both come from THIS row —
            # a pre-lock copy can be a posture a concurrent switch
            # already superseded.
            row = await app.state.model.get_workspace(workspace_id)
            if row is None:
                raise HTTPException(
                    status_code=404, detail="no such workspace"
                )
            # The under-lock specs: an omitted allowlist keeps the
            # fresh row's list (a bad explicit list already answered
            # its 400 above, so this parse cannot fail).
            specs = (
                parse_allowlist(body.allow_list)
                if body.allow_list is not None
                else tuple(row.get("egress_allowlist") or ())
            )
            if (
                body.mode == MODE_STATIC
                and not specs
                and not body.confirm_empty
                and not await egress_consent_allowed(app, workspace_id)
            ):
                raise HTTPException(
                    status_code=400,
                    detail=(
                        "static mode with nothing effectively allowed "
                        "answers every name NXDOMAIN (an offline "
                        "workspace); pass allow_list entries, or set "
                        "confirm_empty to switch anyway"
                    ),
                )
            wrote = await app.state.model.set_egress_policy(
                workspace_id, body.mode, body.allow_list
            )
            if not wrote:
                raise HTTPException(
                    status_code=404, detail="no such workspace"
                )
            # The live swap under the same lock a boot holds: a
            # concurrent start cannot attach the old posture after
            # the flip reads no attachment and skips.
            policy = EgressPolicy(workspace_id, body.mode, specs)
            try:
                applied = await app.state.net.apply_policy(
                    workspace_id, policy
                )
            except BaseException:
                # The swap failed whole — the old table still
                # enforces — so the row must not claim a posture
                # the workspace does not run: the under-lock row's
                # facts go back before the refusal surfaces.
                with contextlib.suppress(Exception):
                    await app.state.model.set_egress_policy(
                        workspace_id,
                        row.get("egress_mode") or "allow",
                        (
                            None
                            if body.allow_list is None
                            else list(row.get("egress_allowlist") or ())
                        ),
                    )
                raise
        LOG.info(
            "egress mode set: ws=%s mode=%s allowlist=%s applied=%s by=token",
            workspace_id[:8],
            body.mode,
            len(specs),
            applied,
        )
        await app.state.consent.broadcast_rules(workspace_id)
        frame = await app.state.consent.rules_frame(workspace_id)
        return {
            **(frame or {"workspace_id": workspace_id}),
            "applied": applied,
        }

    @api.get(
        "/api/v1/workspaces/{workspace_id}/egress/requests",
        dependencies=[Depends(require_token)],
    )
    async def list_egress_requests(
        workspace_id: str,
        decision: str | None = None,
        limit: int = Query(default=100, ge=1, le=1000),
    ) -> list[dict]:
        """The consent rows (audit trail), newest first; the
        ``decision`` query filters one lifecycle state and
        ``limit`` bounds the page (1..1000, newest first)."""
        row = await workspace_or_404(app, workspace_id)
        workspace_id = row["id"]
        if decision is not None and decision not in DECISIONS:
            raise HTTPException(
                status_code=400,
                detail=f"decision must be one of {list(DECISIONS)}",
            )
        return await app.state.model.egress_consent.list_requests(
            workspace_id, decision, limit
        )

    @api.post(
        "/api/v1/workspaces/{workspace_id}/egress/requests/{request_id}",
        dependencies=[Depends(require_token)],
    )
    async def decide_egress(
        workspace_id: str, request_id: str, body: EgressDecide
    ) -> dict:
        """A decider's verdict on a held request (#69): the held
        SYN releases on allow, refuses fast on deny; the duration
        sets how long enforcement honors the verdict."""
        row = await workspace_or_404(app, workspace_id)
        workspace_id = row["id"]
        refusal = await verdict_refusal(app, workspace_id, request_id, body)
        if refusal is not None:
            raise HTTPException(*refusal)
        verdict = await app.state.consent.resolve(
            request_id,
            "allowed" if body.decision == "allow" else "denied",
            "token",
            body.duration,
        )
        if verdict is None:
            raise HTTPException(
                status_code=404,
                detail="no held request with that id",
            )
        return {
            "id": request_id,
            # The decided enforcement TTL the verdict also carries
            # is the data plane's business (#401); the decider sees
            # the verdict itself.
            "verdict": {
                key: verdict[key]
                for key in ("decision", "reason", "duration")
                if key in verdict
            },
        }

    @api.delete(
        "/api/v1/workspaces/{workspace_id}/egress/requests/{request_id}",
        dependencies=[Depends(require_token)],
    )
    async def revoke_egress(workspace_id: str, request_id: str) -> dict:
        """Undo an in-effect verdict (#69): the row flips to
        revoked, its flow rules and tracked connections clear, and
        the destination gates again at the next connection."""
        row = await workspace_or_404(app, workspace_id)
        workspace_id = row["id"]
        row = await app.state.model.egress_consent.get_request(request_id)
        if row is None or row["workspace_id"] != workspace_id:
            raise HTTPException(
                status_code=404,
                detail="no consent request with that id for that workspace",
            )
        row = await app.state.consent.revoke(request_id, "token")
        if row is None:
            raise HTTPException(
                status_code=404,
                detail="no in-effect verdict with that id",
            )
        return {"id": request_id, "revoked": True}

    return api
