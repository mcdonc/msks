"""The client half of the bridge relay (#1102): serve the
daemon's signature requests from the workspace's own identity.

The daemon's bridge is an ssh session into the workspace,
authenticated with the operator's key. This module is the client
end of its agent relay: it opens the relay websocket, resolves
the workspace's private half exactly as ``msks ssh`` does (the
operator's agent when it holds the key, else the transient agent
staging the identity file — ``session_identity`` in
:mod:`msks.client.ssh`), and answers the daemon's signature
requests over the wire. The private half never leaves this
process or the operator's agent; only signature requests cross.

``msks start`` runs the flow for a workspace with forward proxy
rows; ``msks proxy bridge`` runs it on demand — the repair path
after a dropped session, a guest sshd restart, or a daemon crash
the VM survived.
"""

import asyncio
import contextlib
import json
from urllib.parse import quote

import websockets

from ..spec.owners import AMBIGUOUS_CLOSE
from ..spec.proxy import (
    CLOSE,
    ESTABLISHED,
    OPEN,
    pack_frame,
    unpack_frame,
)
from . import wsdial
from .env import env_token, env_url, is_unix, ssl_context
from .rest import fetch_ssh_key
from .ssh import session_identity
from .wsdial import ws_url

#: One agent-connection read's byte budget, the same chunk the
#: daemon's relay moves.
READ_CHUNK = 4096

CLOSE_CODE_REASONS = {
    4401: "bridge refused (bad or missing token)",
    4404: "no such workspace",
    AMBIGUOUS_CLOSE: "ambiguous workspace name",
    4501: (
        "bridge unavailable (no NIC, not running, a relay already "
        "serving, or the state path is too long)"
    ),
    4502: "bridge establish failed",
}


def bridge_ws_url(base_url: str, workspace_id: str) -> str:
    """The bridge relay websocket's URL."""
    return ws_url(
        base_url,
        f"/api/v1/workspaces/{quote(workspace_id, safe='')}/bridge",
    )


def close_code_message(closed: websockets.ConnectionClosed) -> str | None:
    """The one-line failure for a daemon close code, or None for a
    clean end (the session established and either side closed)."""
    if closed.rcvd is None or closed.rcvd.code not in CLOSE_CODE_REASONS:
        return None
    reason = closed.rcvd.reason.strip()
    detail = f": {reason}" if reason else ""
    return f"msks: {CLOSE_CODE_REASONS[closed.rcvd.code]}{detail}"


class RelayClient:
    """The relay's client end: one connection to the local agent
    per ``open`` the daemon sends, its bytes framed back by id."""

    def __init__(self, socket, agent_path: str) -> None:
        self.socket = socket
        self.agent_path = agent_path
        self.connections: dict[int, tuple] = {}
        self.tasks: set = set()

    async def open(self, cid: int) -> None:
        """Serve one agent connection the daemon opened."""
        try:
            reader, writer = await asyncio.open_unix_connection(
                self.agent_path
            )
        except OSError:
            with contextlib.suppress(Exception):
                await self.socket.send(json.dumps({"t": CLOSE, "cid": cid}))
            return
        self.connections[cid] = (reader, writer)
        task = asyncio.create_task(self.pump(cid, reader))
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)

    async def pump(self, cid: int, reader) -> None:
        """Agent bytes to the websocket until the agent connection
        ends, then tell the daemon it ended."""
        while True:
            data = await reader.read(READ_CHUNK)
            if not data:
                break
            await self.socket.send(pack_frame(cid, data))
        await self.socket.send(json.dumps({"t": CLOSE, "cid": cid}))

    def close(self, cid: int) -> None:
        """End one agent connection and forget it."""
        held = self.connections.pop(cid, None)
        if held is not None:
            held[1].close()

    async def sweep(self) -> None:
        """End every agent connection (the relay's end)."""
        for cid in list(self.connections):
            self.close(cid)
        for task in list(self.tasks):
            task.cancel()
        for task in list(self.tasks):
            with contextlib.suppress(BaseException):
                await task


def parse_control(message: str) -> tuple[str, int] | None:
    """(kind, cid) of one control text frame, or None when the
    frame is not one this side speaks."""
    try:
        body = json.loads(message)
        return body["t"], int(body["cid"])
    except ValueError, KeyError, TypeError:
        return None


async def deliver(relay: RelayClient, data: bytes) -> None:
    """One binary frame to its agent connection, or nowhere when
    the id is short or unknown."""
    try:
        cid, payload = unpack_frame(data)
    except ValueError:
        return
    held = relay.connections.get(cid)
    if held is None:
        return
    held[1].write(payload)
    await held[1].drain()


async def dispatch_control(relay: RelayClient, kind: str, cid: int) -> bool:
    """One control frame's effect on the relay; True when the
    serve ends (the established report ends it — the caller
    closes the websocket and the sweep ends the connections)."""
    if kind == ESTABLISHED:
        return True
    if kind == OPEN:
        await relay.open(cid)
    elif kind == CLOSE:
        relay.close(cid)
    return False


async def serve(socket, agent_path: str) -> None:
    """Answer the daemon's relay frames until the session is
    established (the caller then closes) or the websocket ends."""
    relay = RelayClient(socket, agent_path)
    try:
        while True:
            message = await socket.recv()
            if isinstance(message, str):
                parsed = parse_control(message)
                if parsed is None:
                    continue
                if await dispatch_control(relay, *parsed):
                    return
            else:
                await deliver(relay, message)
    finally:
        await relay.sweep()


async def establish(
    workspace_id: str, url: str, token: str, ssl_ctx, holder
) -> None:
    """Open the relay with a resolved identity holder and serve it
    until the daemon reports the bridge established — or names its
    refusal through a close code."""
    address = bridge_ws_url(url, workspace_id)
    connection = wsdial.dial(address, token, ssl_ctx, max_size=2**22)
    async with await connection as socket:
        try:
            await serve(socket, holder.server_address)
        except websockets.ConnectionClosed as closed:
            message = close_code_message(closed)
            if message is not None:
                raise SystemExit(message) from None


def run_bridge(workspace_id: str, transport=None) -> int:
    """Establish the workspace's bridge from this client: resolve
    the identity exactly as a session would, serve the relay until
    the daemon's session stands, and say so."""
    token = env_token()
    url = env_url()
    ssl_ctx = None if is_unix(url) else ssl_context()
    key = asyncio.run(
        fetch_ssh_key(url, token, workspace_id, transport=transport)
    )
    with session_identity(key, workspace_id) as holder:
        asyncio.run(establish(workspace_id, url, token, ssl_ctx, holder))
    print(f"msks: {workspace_id} bridge established")
    return 0
