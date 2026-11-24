"""The bridge websocket (#1102): the client that holds a
workspace's forward proxies' bridge.

The holder connects, and from then on every connection the rows'
listeners accept is relayed to it — control frames carrying the
comings and goings, binary frames carrying the bytes by connection
id. The holder carries each one into the guest over ssh; the
daemon runs no ssh and holds no key material.

The websocket's life is the bridge's life: it registers at
upgrade, unregisters at its end, and the workspace's rows read
``listening`` over exactly that fact.
"""

import json

from fastapi import APIRouter, WebSocket

from ...bridge import BridgeHub
from ...spec.proxy import CLOSE
from ..api.deps import authed_accept, caller_uid
from ..api.rows import ws_row_or_close
from ..api.streams import close_reason


def router(app, hub) -> APIRouter:
    """The bridge websocket."""
    api = APIRouter()

    @api.websocket("/api/v1/workspaces/{workspace_id}/bridge")
    async def bridge(socket: WebSocket, workspace_id: str) -> None:
        if not await authed_accept(app, socket):
            return
        row = await ws_row_or_close(
            app, socket, workspace_id, caller_uid(socket.scope)
        )
        if row is None:
            return
        workspace_id = row["id"]
        if not row.get("egress"):
            await socket.close(
                code=4501,
                reason=close_reason(
                    f"workspace {workspace_id} has no NIC "
                    "(created without egress)"
                ),
            )
            return
        manager: BridgeHub = app.state.bridge
        holder = manager.register(workspace_id, socket)
        try:
            await manager.publish_up(workspace_id)
            while True:
                message = await socket.receive()
                if message["type"] != "websocket.receive":
                    return
                if message.get("bytes") is not None:
                    manager.feed(holder, message["bytes"])
                    continue
                await dispatch_control(manager, holder, message)
        finally:
            manager.unregister(workspace_id, holder)
            await manager.publish_down(workspace_id)

    return api


async def dispatch_control(manager: BridgeHub, holder, message) -> None:
    """One control frame from the holder: a close ends its
    relayed connection; anything else (and no frame at all) is
    nothing."""
    text = message.get("text")
    if text is None:
        return
    try:
        body = json.loads(text)
        cid = int(body["cid"])
    except ValueError, KeyError, TypeError:
        return  # a frame this side does not speak acts on nothing
    if body["t"] == CLOSE:
        manager.end_channel(holder, cid)
