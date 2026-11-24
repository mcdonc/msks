"""The bridge relay websocket (#1102): the ssh-agent protocol
between the daemon's bridge and the client that serves it.

The bridge is an ssh session the daemon holds into a running
workspace, and it authenticates with the operator's key: the
client that opens this websocket answers the session's signature
requests from its own agent. The daemon binds a unix socket (its
bridge master signs through it, ``IdentityAgent``), and every
connection the master makes to that socket becomes one connection
id here — text frames carry the comings and goings, binary frames
carry the agent protocol bytes tagged with the id.

The relay ends when the websocket ends; the established master
does not. A client that sees ``established`` may close at once:
the session rides its own keepalives, and the next relay (the
next ``msks start`` or ``msks proxy bridge`` from a client that
holds the identity) is only needed to build a new one.
"""

import asyncio
import contextlib
import json

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from ...bridge import BridgeError
from ...spec.proxy import CLOSE, ESTABLISHED, OPEN, pack_frame
from ..api.deps import authed_accept, caller_uid
from ..api.rows import ws_row_or_close
from ..api.streams import close_reason

#: One relay read's byte budget — the same chunk the console and
#: forward pumps move. Agent-protocol frames are far smaller.
RELAY_CHUNK = 4096


def control(kind: str, cid: int) -> str:
    """One control text frame: the kind and its connection id."""
    return json.dumps({"t": kind, "cid": cid})


def parse_control(message: str) -> tuple[str, int] | None:
    """(kind, cid) of a control text frame, or None when the frame
    is not one this relay speaks."""
    try:
        body = json.loads(message)
        return body["t"], int(body["cid"])
    except ValueError, KeyError, TypeError:
        return None


class Relay:
    """One live relay: the unix socket the bridge master signs
    through, its connections multiplexed over the websocket by id.

    Owned by the route; the frames flow only while both ends do.
    """

    def __init__(self, socket: WebSocket, path: str) -> None:
        self.socket = socket
        self.path = path
        self.connections: dict[int, tuple] = {}
        self.next_cid = 0
        self.server: asyncio.AbstractServer | None = None

    async def start(self) -> None:
        """Bind the relay socket and begin serving agent
        connections; each accepted connection announces itself to
        the client and pumps until either end closes it."""
        self.server = await asyncio.start_unix_server(self.handler, self.path)

    async def handler(self, reader, writer) -> None:
        """One agent connection: announce, pump its bytes to the
        websocket, and announce the end."""
        cid = self.next_cid
        self.next_cid += 1
        self.connections[cid] = (reader, writer)
        try:
            await self.socket.send_text(control(OPEN, cid))
            while True:
                data = await reader.read(RELAY_CHUNK)
                if not data:
                    break
                await self.socket.send_bytes(pack_frame(cid, data))
            await self.socket.send_text(control(CLOSE, cid))
        except asyncio.CancelledError:
            raise
        except Exception:
            # The websocket went mid-connection: the sweep at the
            # relay's end closes this writer with the rest.
            pass
        finally:
            self.close(cid)

    def close(self, cid: int) -> None:
        """End one agent connection and forget it."""
        held = self.connections.pop(cid, None)
        if held is None:
            return
        held[1].close()

    async def sweep(self) -> None:
        """End every agent connection and stop serving: the relay
        socket goes away with the websocket that carried it."""
        if self.server is not None:
            self.server.close()
            with contextlib.suppress(Exception):
                await self.server.wait_closed()
            self.server = None
        for cid in list(self.connections):
            self.close(cid)


async def relay_bytes(relay: Relay, data: bytes) -> None:
    """One binary frame from the client to its agent connection,
    or nowhere when the id is short or unknown."""
    if len(data) < 4:
        return
    cid = int.from_bytes(data[:4], "big")
    held = relay.connections.get(cid)
    if held is None:
        return
    held[1].write(data[4:])
    await held[1].drain()


async def relay_text(relay: Relay, text: str | None) -> None:
    """One control frame from the client: a close ends its agent
    connection; every other frame (and no frame at all) is
    nothing."""
    if text is None:
        return
    parsed = parse_control(text)
    if parsed is not None and parsed[0] == CLOSE:
        relay.close(parsed[1])


async def relay_receive(socket: WebSocket, relay: Relay) -> None:
    """The websocket's receive half: client bytes to their agent
    connection, client closes to the connection's end. Returns
    when the websocket closes (the caller treats that as the
    relay's end)."""
    while True:
        message = await socket.receive()
        if message["type"] != "websocket.receive":
            return
        if message.get("bytes") is not None:
            await relay_bytes(relay, message["bytes"])
            continue
        await relay_text(relay, message.get("text"))


def router(app, hub) -> APIRouter:
    """The bridge relay websocket."""
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
        attachment = app.state.net.attachment(workspace_id)
        if attachment is None:
            await socket.close(
                code=4501,
                reason=close_reason(
                    f"workspace {workspace_id} is not running"
                ),
            )
            return
        manager = app.state.bridge
        try:
            paths = manager.paths(workspace_id)
        except BridgeError as exc:
            await socket.close(code=4501, reason=close_reason(str(exc)))
            return
        if not manager.claim_relay(workspace_id):
            await socket.close(
                code=4501,
                reason=close_reason(
                    "a bridge relay is already serving this workspace"
                ),
            )
            return
        relay = Relay(socket, str(paths["relay"]))
        receive_task = None
        try:
            # The relay socket binds before the master spawns (the
            # master signs through it), so the directory it lives
            # in exists from this side too — establish also guards,
            # but the bind comes first.
            paths["dir"].mkdir(parents=True, exist_ok=True)
            with contextlib.suppress(OSError):
                # A relay socket left by a crashed daemon serves
                # nothing; the fresh one replaces it.
                paths["relay"].unlink()
            await relay.start()
            if not manager.up(workspace_id):
                cause = await manager.establish(
                    row, attachment.guest_ip, paths["relay"]
                )
                if cause is not None:
                    await socket.close(code=4502, reason=close_reason(cause))
                    return
            await socket.send_text(control(ESTABLISHED, 0))
            receive_task = asyncio.create_task(relay_receive(socket, relay))
            done, _pending = await asyncio.wait(
                {receive_task}, return_when=asyncio.FIRST_COMPLETED
            )
            for task in done:
                # The receive half ends in WebSocketDisconnect when
                # the client goes — the relay's normal end. Anything
                # else is a real fault and propagates.
                with contextlib.suppress(WebSocketDisconnect):
                    task.result()
        finally:
            if receive_task is not None:
                receive_task.cancel()
                with contextlib.suppress(BaseException):
                    await receive_task
            await relay.sweep()
            with contextlib.suppress(OSError):
                paths["relay"].unlink()
            manager.release_relay(workspace_id)

    return api
