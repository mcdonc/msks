"""The forward websocket (#108, #109): raw bytes between the
client and a guest TCP port — the pipe ssh's ProxyCommand
rides."""

import asyncio
import contextlib

from fastapi import APIRouter, WebSocket

from ...microvm.errors import MicrovmError
from .deps import authed_accept
from .streams import close_reason, pump_streams


def forward_port(raw: str) -> tuple[int, str | None]:
    """The forward's TCP port from the path, or the refusal reason."""
    try:
        port = int(raw)
    except ValueError:
        return 0, f"port must be an integer, got {raw!r}"
    if not 1 <= port <= 65535:
        return 0, f"port={port} out of range"
    return port, None


def forward_allowed(app, row: dict, port: int) -> str | None:
    """The policy seam between auth and dial (#108).

    Every forward passes today: a token that reached this far already
    owns the workspace's root console, so no guest port is a privilege
    escalation. When port-scoped tokens arrive, the refusal reason
    this returns is the whole mechanism.
    """
    return None


def router(app, hub) -> APIRouter:
    """The forward websocket."""
    api = APIRouter()

    @api.websocket("/api/v1/workspaces/{workspace_id}/forward/{port}")
    async def forward(socket: WebSocket, workspace_id: str, port: str) -> None:
        # Service-plane bridge (#109): raw bytes between the client
        # and a guest TCP port the caller names — the pipe ssh's
        # ProxyCommand rides. The token authenticates through the
        # handshake's Authorization header (#216), the same scheme
        # as every other msks surface: one form to document and
        # test, and the token never lands in a URL. Each websocket is
        # one guest TCP connection.
        if not await authed_accept(app, socket):
            return
        row = await app.state.model.get_workspace(workspace_id)
        if row is None:
            await socket.close(code=4404)
            return
        workspace_id = row["id"]
        target_port, problem = forward_port(port)
        if problem is not None:
            await socket.close(code=4400, reason=close_reason(problem))
            return
        refusal = forward_allowed(app, row, target_port)
        if refusal is not None:
            await socket.close(code=4403, reason=close_reason(refusal))
            return
        if not row.get("egress"):
            await socket.close(
                code=4501,
                reason=close_reason(
                    f"workspace {workspace_id} has no NIC "
                    "(created without egress)"
                ),
            )
            return
        try:
            reader, writer = await app.state.net.forward_stream(
                workspace_id, target_port
            )
        except MicrovmError as exc:
            await socket.close(code=4501, reason=close_reason(str(exc)))
            return
        try:
            # The opened publish lives inside the try so a cancellation
            # between dial and pump cannot skip the writer's cleanup.
            await hub.publish(
                "forward.opened", {"id": workspace_id, "port": target_port}
            )
            app.state.net.track_forward(workspace_id, writer)
            try:
                await pump_streams(socket, reader, writer)
            finally:
                app.state.net.untrack_forward(workspace_id, writer)
        finally:
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()
            # A detached task, not an await: teardown can cancel this
            # coroutine mid-finally (an await would raise CancelledError
            # and skip the event), and publish never blocks — it fans
            # out to subscriber queues synchronously.
            asyncio.create_task(
                hub.publish(
                    "forward.closed", {"id": workspace_id, "port": target_port}
                )
            )

    return api
