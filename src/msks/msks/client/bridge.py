"""The client half of the bridge (#1102): hold a workspace's
forward proxies, carrying their connections into the guest.

The daemon owns the rows and their listeners; this process — the
holder — serves every connection they accept. Each one becomes an
ssh ``-W 127.0.0.1:<port>`` channel over the forward websocket
(``ProxyCommand msks forward <ws> 22``), the same transport
``msks ssh`` rides, signed with the workspace's own identity (the
operator's agent when it holds the key, else the configured
identity file — the ``session_identity`` ladder). The private half
stays in this process or the operator's agent; the daemon holds
no key material anywhere.

``msks start`` leaves a holder behind when the workspace has
forward rows, so the proxies serve after the command exits; the
holder exits when its websocket ends (the workspace stopped, the
daemon went away). ``msks proxy hold`` runs one on demand.
"""

import asyncio
import contextlib
import json
import shlex
from urllib.parse import quote

import websockets

from ..spec.owners import AMBIGUOUS_CLOSE
from ..spec.proxy import CLOSE, OPEN, pack_frame, unpack_frame
from . import wsdial
from .env import env_token, env_url, is_unix, ssl_context
from .identity import known_hosts_path
from .rest import fetch_ssh_key
from .ssh import proxy_command, session_identity, workspace_user
from .wsdial import ws_url

#: One ssh channel read's byte budget, the same chunk the daemon
#: side moves.
READ_CHUNK = 4096

#: The ssh binary the holder drives — the same one ``msks ssh``
#: shells out to.
SSH = "ssh"

CLOSE_CODE_REASONS = {
    4401: "bridge refused (bad or missing token)",
    4404: "no such workspace",
    AMBIGUOUS_CLOSE: "ambiguous workspace name",
    4501: "bridge unavailable (no NIC)",
}


def bridge_ws_url(base_url: str, workspace_id: str) -> str:
    """The bridge websocket's URL."""
    return ws_url(
        base_url,
        f"/api/v1/workspaces/{quote(workspace_id, safe='')}/bridge",
    )


def close_code_message(closed: websockets.ConnectionClosed) -> str | None:
    """The one-line failure for a daemon close code, or None for a
    clean end (the workspace stopped; the holder exits quietly)."""
    if closed.rcvd is None or closed.rcvd.code not in CLOSE_CODE_REASONS:
        return None
    reason = closed.rcvd.reason.strip()
    detail = f": {reason}" if reason else ""
    return f"msks: {CLOSE_CODE_REASONS[closed.rcvd.code]}{detail}"


def channel_argv(
    workspace_id: str,
    agent: str,
    identity: str,
    user: str,
    port: int,
    known_hosts: str,
) -> list[str]:
    """One relayed connection's ssh argv: a ``-W`` channel to the
    guest's own loopback port, over the forward websocket, as the
    workspace's login user, signed through the same agent the
    session identity resolved."""
    return [
        SSH,
        "-o",
        f"ProxyCommand={proxy_command(workspace_id)}",
        "-o",
        f"UserKnownHostsFile={shlex.quote(known_hosts)}",
        "-o",
        "StrictHostKeyChecking=accept-new",
        "-o",
        "IdentitiesOnly=yes",
        "-o",
        f"IdentityAgent={shlex.quote(agent)}",
        "-i",
        shlex.quote(identity),
        "-l",
        user,
        "-W",
        f"127.0.0.1:{port}",
        workspace_id,
    ]


def parse_control(message: str) -> tuple[str, int, int | None]:
    """(kind, cid, port) of one control text frame — the port
    rides an ``open`` alone — raising for a frame this side does
    not speak."""
    body = json.loads(message)
    kind, cid = body["t"], int(body["cid"])
    port = int(body["port"]) if kind == OPEN and "port" in body else None
    return kind, cid, port


class Holder:
    """The serving loop's state: the websocket, one ssh channel
    per connection id, and the workspace's login facts. The spawn
    seam lets the tests drive a fake ssh."""

    def __init__(self, socket, workspace_id: str, user: str) -> None:
        self.socket = socket
        self.workspace_id = workspace_id
        self.user = user
        self.known_hosts = known_hosts_path(workspace_id)
        self.channels: dict[int, object] = {}
        self.spawner = asyncio.create_subprocess_exec

    async def open(self, cid: int, port: int, agent: str, ident: str):
        """Serve one relayed connection: an ssh channel piped to
        the websocket both ways, until either end closes it."""
        argv = channel_argv(
            self.workspace_id,
            agent,
            ident,
            self.user,
            port,
            self.known_hosts,
        )
        process = await self.spawner(
            *argv,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        pump = asyncio.create_task(self.pump_out(cid, process))
        self.channels[cid] = (process, pump)

    async def pump_out(self, cid: int, process) -> None:
        """The ssh channel's stdout to the websocket until it
        ends; the connection closes with it."""
        while True:
            data = await process.stdout.read(READ_CHUNK)
            if not data:
                break
            await self.socket.send(pack_frame(cid, data))
        await self.socket.send(json.dumps({"t": CLOSE, "cid": cid}))
        self.channels.pop(cid, None)

    async def feed(self, data: bytes) -> None:
        """One binary frame from the daemon to its ssh channel's
        stdin; a frame this side cannot place is dropped (the
        channel's own end closes the connection)."""
        try:
            cid, payload = unpack_frame(data)
        except ValueError:
            return
        held = self.channels.get(cid)
        if held is None or held[0].stdin is None:
            return
        with contextlib.suppress(BrokenPipeError, OSError):
            held[0].stdin.write(payload)
            await held[0].stdin.drain()

    def close(self, cid: int) -> None:
        """One relayed connection ended: the ssh channel stops."""
        held = self.channels.pop(cid, None)
        if held is None:
            return
        process, pump = held
        with contextlib.suppress(ProcessLookupError):
            process.terminate()
        pump.cancel()

    async def sweep(self) -> None:
        """End every ssh channel (the websocket's end)."""
        for cid in list(self.channels):
            self.close(cid)


async def dispatch_control(
    holder: Holder, message: str, agent: str, ident: str
) -> None:
    """One control frame's effect: an ``open`` starts an ssh
    channel, a ``close`` ends one; a frame this side does not
    speak acts on nothing."""
    try:
        kind, cid, port = parse_control(message)
    except ValueError, KeyError, TypeError:
        return  # a frame this side does not speak acts on nothing
    if kind == OPEN and port is not None:
        await holder.open(cid, port, agent, ident)
    elif kind == CLOSE:
        holder.close(cid)


async def serve(socket, holder: Holder, agent: str, ident: str) -> None:
    """Answer the daemon's frames until the websocket ends: a
    text frame dispatches, a binary frame feeds its channel."""
    while True:
        message = await socket.recv()
        if isinstance(message, str):
            await dispatch_control(holder, message, agent, ident)
            continue
        await holder.feed(message)


async def hold(
    workspace_id: str, url: str, token: str, ssl_ctx, key: dict, holder
) -> None:
    """The holder's session: connect the bridge websocket and
    serve it until it ends, naming the daemon's refusals."""
    address = bridge_ws_url(url, workspace_id)
    connection = wsdial.dial(address, token, ssl_ctx, max_size=2**22)
    async with await connection as socket:
        server = Holder(socket, workspace_id, workspace_user(key))
        try:
            await serve(
                socket, server, holder.server_address, holder.identity_path
            )
        except websockets.ConnectionClosed as closed:
            message = close_code_message(closed)
            if message is not None:
                raise SystemExit(message) from None
        finally:
            await server.sweep()


def run_hold(workspace_id: str, transport=None) -> int:
    """Hold the workspace's bridge from this client: resolve the
    identity exactly as a session would, serve the rows'
    connections until the websocket ends."""
    token = env_token()
    url = env_url()
    ssl_ctx = None if is_unix(url) else ssl_context()
    key = asyncio.run(
        fetch_ssh_key(url, token, workspace_id, transport=transport)
    )
    with session_identity(key, workspace_id) as resolved:
        asyncio.run(hold(workspace_id, url, token, ssl_ctx, key, resolved))
    return 0
