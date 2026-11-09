"""The workspace proxy routes (#807, #808): create, list, and
remove the persisted forward and remote TCP proxies, with each
row's live status.

Routes stay thin: the model layer owns the rows, the proxy
subsystem owns the listeners, and the net manager owns the
admission swap a remote proxy's live change drives."""

from fastapi import APIRouter, Depends, HTTPException

from ...proxies import DEFAULT_LISTEN_HOST, DEFAULT_TARGET_HOST
from .deps import require_token
from .rows import workspace_or_404
from .schemas import ProxyCreate


def direction_refusal(body: ProxyCreate) -> str | None:
    """The named refusal when a direction's host fields disagree
    with it: a forward row carries the daemon-side listen host and
    no target host (its target is the guest); a remote row carries
    the daemon-side target host and no listen host (its bind is
    the workspace's tap address, a runtime fact)."""
    if body.direction == "forward" and body.target_host is not None:
        return (
            "a forward proxy reaches the guest's port; "
            "target_host names nothing"
        )
    if body.direction == "remote" and body.listen_host is not None:
        return (
            "a remote proxy binds the workspace's tap; "
            "listen_host names nothing"
        )
    return None


def duplicate_conflict(rows: list[dict], body: ProxyCreate) -> str | None:
    """The 409 detail when a new row would squat a live bind.

    A forward proxy's bind is daemon-wide: any workspace's row on
    the same address and port collides, so the check spans every
    row. A remote proxy's bind is its own tap, so only the same
    workspace's port collides."""
    listen_host = body.listen_host or DEFAULT_LISTEN_HOST
    for row in rows:
        if row["direction"] != body.direction:
            continue
        if row["listen_port"] != body.listen_port:
            continue
        if (
            body.direction == "forward"
            and (row.get("listen_host") or DEFAULT_LISTEN_HOST) != listen_host
        ):
            continue
        return (
            f"a {body.direction} proxy on port {body.listen_port}"
            f" already exists (workspace {row['workspace_id']}, "
            f"proxy {row['id']})"
        )
    return None


def with_live_status(proxies, row: dict) -> dict:
    """One row dict with its live status fields attached."""
    status, reason = proxies.status(row)
    return {**row, "status": status, "reason": reason}


def router(app, hub) -> APIRouter:
    """The proxy routes."""
    api = APIRouter()

    @api.get(
        "/api/v1/workspaces/{workspace_id}/proxies",
        dependencies=[Depends(require_token)],
    )
    async def list_workspace_proxies(workspace_id: str) -> list[dict]:
        row = await workspace_or_404(app, workspace_id)
        rows = await app.state.model.list_proxies(row["id"])
        return [with_live_status(app.state.proxies, proxy) for proxy in rows]

    @api.get(
        "/api/v1/proxies",
        dependencies=[Depends(require_token)],
    )
    async def list_proxies() -> list[dict]:
        """Every workspace's proxy rows with live status — the
        daemon-wide read the TUI's proxies page (#809) serves."""
        rows = await app.state.model.list_proxies()
        return [with_live_status(app.state.proxies, proxy) for proxy in rows]

    @api.post(
        "/api/v1/workspaces/{workspace_id}/proxies",
        dependencies=[Depends(require_token)],
    )
    async def create_proxy(workspace_id: str, body: ProxyCreate) -> dict:
        row = await workspace_or_404(app, workspace_id)
        workspace_id = row["id"]
        if not row.get("egress"):
            # The same refusal the forward websocket carries: a
            # workspace without a NIC has neither the guest dial a
            # forward needs nor the tap a remote one binds.
            raise HTTPException(
                status_code=400,
                detail=(
                    f"workspace {workspace_id} has no NIC "
                    "(created without egress)"
                ),
            )
        refusal = direction_refusal(body)
        if refusal is not None:
            raise HTTPException(status_code=400, detail=refusal)
        conflict = duplicate_conflict(
            await app.state.model.list_proxies(), body
        )
        if conflict is not None:
            raise HTTPException(status_code=409, detail=conflict)
        created = await app.state.model.create_proxy(
            workspace_id,
            body.direction,
            (
                body.listen_host or DEFAULT_LISTEN_HOST
                if body.direction == "forward"
                else None
            ),
            body.listen_port,
            (
                body.target_host or DEFAULT_TARGET_HOST
                if body.direction == "remote"
                else None
            ),
            body.target_port,
            body.label,
        )
        # A running workspace takes the listener at once; a stopped
        # one records the row and its next start brings it up.
        live = await app.state.proxies.start_one(
            created, app.state.net.attachment(workspace_id)
        )
        if body.direction == "remote" and live["status"] != "stopped":
            await app.state.net.refresh_proxies(workspace_id)
        await hub.publish(
            "proxy.created",
            {
                "id": workspace_id,
                "proxy": created["id"],
                "direction": created["direction"],
                "listen_port": created["listen_port"],
                "target_port": created["target_port"],
            },
        )
        return live

    @api.delete(
        "/api/v1/workspaces/{workspace_id}/proxies/{proxy_id}",
        dependencies=[Depends(require_token)],
    )
    async def delete_proxy(workspace_id: str, proxy_id: int) -> dict:
        row = await workspace_or_404(app, workspace_id)
        workspace_id = row["id"]
        proxy = await app.state.model.get_proxy(workspace_id, proxy_id)
        if proxy is None:
            raise HTTPException(status_code=404, detail="no such proxy")
        attachment = app.state.net.attachment(workspace_id)
        await app.state.proxies.stop_one(proxy_id)
        if proxy["direction"] == "remote" and attachment is not None:
            await app.state.net.refresh_proxies(workspace_id)
        await app.state.model.delete_proxy(workspace_id, proxy_id)
        await hub.publish(
            "proxy.removed",
            {
                "id": workspace_id,
                "proxy": proxy_id,
                "direction": proxy["direction"],
                "listen_port": proxy["listen_port"],
                "target_port": proxy["target_port"],
            },
        )
        return {"id": proxy_id, "workspace_id": workspace_id, "removed": True}

    return api
