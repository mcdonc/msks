"""The bridge hub (#1102): the daemon-side registry of the
clients that hold a workspace's forward proxies' bridge.

The bridge is client-held by design: a forward proxy row reaches
the guest's own ``127.0.0.1`` through ssh — the same transport
``msks ssh`` rides — and ssh belongs where the identity is. The
daemon owns the rows and their listeners; a connected holder (the
one ``msks start`` leaves behind, or ``msks proxy hold`` on
demand) serves every accepted connection, the bytes relayed over
its websocket.

The daemon runs no ssh, holds no key material, and pins no host
keys: those all stay with the client. A workspace's rows read
``listening`` while at least one holder serves and ``failed`` with
the named cause while none does — the up-fact is the websocket
itself, so it cannot lie.

A root leaf module like the proxy subsystem: the composition
layer sets it on ``app.state``, and the proxy subsystem and the
API routes reach it by attribute.
"""

import asyncio
import contextlib
import json

from .spec.proxy import CLOSE, OPEN, pack_frame, unpack_frame

#: The cause a row reads while no holder serves it. The remedy is
#: the client surface: start the workspace from the CLI (its
#: holder outlives the command), or run the hold command on demand.
NO_HOLDER = (
    "no client holds the bridge — start the workspace from a "
    "client (the holder it leaves serves after the command "
    "exits), or run 'msks proxy hold <workspace>'"
)


class NoHolder(Exception):
    """The refusal a proxy connection takes while no holder
    serves its workspace."""


#: The proxy events a bridge transition re-publishes for the
#: workspace's forward rows (#1102 the listeners' own
#: transitions publish; a bridge coming or going repaints the
#: rows the same way).
UP_EVENT = "proxy.listening"
DOWN_EVENT = "proxy.failed"


def control(
    kind: str,
    cid: int,
    port: int | None = None,
    host: str | None = None,
) -> str:
    """One control text frame: the kind, its connection id, and
    the guest-side ``host:port`` an ``open`` names (the host
    rides only when the row names one beyond the loopback)."""
    body = {"t": kind, "cid": cid}
    if port is not None:
        body["port"] = port
    if host is not None:
        body["host"] = host
    return json.dumps(body)


class ChannelWriter:
    """The writer half of one relayed connection: the holder's
    websocket, framed by connection id."""

    def __init__(self, holder: Holder, cid: int) -> None:  # noqa: F821
        self.holder = holder
        self.cid = cid

    def write(self, data: bytes) -> None:
        self.holder.outbox.append(pack_frame(self.cid, data))

    async def drain(self) -> None:
        await self.holder.flush()

    async def close(self) -> None:
        reader = self.holder.channels.pop(self.cid, None)
        if reader is not None:
            reader.feed_eof()
        await self.holder.send_control(CLOSE, self.cid)

    async def wait_closed(self) -> None:
        return None


class Holder:
    """One serving client: its websocket, its relayed connections
    by id, and the outbound frames queued by channel writes (the
    proxy pumps write synchronously and flush on drain)."""

    def __init__(self, socket) -> None:
        self.socket = socket
        self.channels: dict[int, asyncio.StreamReader] = {}
        self.outbox: list[bytes] = []
        self.next_cid = 0

    async def send_control(
        self,
        kind: str,
        cid: int,
        port: int | None = None,
        host: str | None = None,
    ) -> None:
        await self.socket.send_text(control(kind, cid, port, host))

    async def flush(self) -> None:
        """Move queued frames to the websocket."""
        while self.outbox:
            await self.socket.send_bytes(self.outbox.pop(0))


class BridgeHub:
    """The holders on ``app.state.bridge``, keyed by workspace.

    Registers and unregisters the route's websockets, opens
    relayed connections for the proxy listeners, and answers the
    up-fact a row's status reads.
    """

    def __init__(self, app) -> None:
        self.app = app
        self.holders: dict[str, list[Holder]] = {}

    # --- registration ------------------------------------------------

    def register(self, workspace_id: str, socket) -> Holder:
        """A holder's websocket arrived: it serves from now
        until it goes."""
        holder = Holder(socket)
        self.holders.setdefault(workspace_id, []).append(holder)
        return holder

    def unregister(self, workspace_id: str, holder: Holder) -> None:
        """The holder's websocket went: its channels end (their
        readers see EOF, or a connected peer hangs forever), and
        the workspace keeps only its remaining holders."""
        held = self.holders.get(workspace_id)
        if held is not None:
            with contextlib.suppress(ValueError):
                held.remove(holder)
            if not held:
                self.holders.pop(workspace_id, None)
        self.retire(holder)

    def close_workspace(self, workspace_id: str) -> list[Holder]:
        """The workspace's holders, taken with it (the detach
        hook): the route closes each websocket it returns, the
        holding clients exit with theirs, and their channels end
        (the readers see EOF)."""
        taken = self.holders.pop(workspace_id, [])
        for holder in taken:
            self.retire(holder)
        return taken

    def retire(self, holder: Holder) -> None:
        """End a departing holder's channels: every reader sees
        EOF, whether or not the workspace keeps other holders."""
        for reader in holder.channels.values():
            reader.feed_eof()
        holder.channels.clear()

    # --- the bridge's transitions ------------------------------------

    async def publish_up(self, workspace_id: str) -> None:
        """The first holder arrived: the forward rows a listing
        paints move to ``listening`` (best-effort, like every
        proxy step in a boot)."""
        if len(self.holders.get(workspace_id, ())) == 1:
            await self.publish_rows(workspace_id, UP_EVENT)

    async def publish_down(self, workspace_id: str) -> None:
        """The last holder went: the forward rows carry the named
        cause until the next holder. The fact is re-read after the
        rows read — a holder arriving under it must not be painted
        failed by the stale half of the transition."""
        if not self.holders.get(workspace_id):
            await self.publish_rows(workspace_id, DOWN_EVENT, NO_HOLDER)
            if self.holders.get(workspace_id):
                await self.publish_rows(workspace_id, UP_EVENT)

    async def row_owner(self, workspace_id: str) -> int | None:
        """The workspace row's owner, for the scoped frames #1095
        promised; None when the row cannot be read."""
        with contextlib.suppress(Exception):
            workspace = await self.app.state.model.get_workspace(workspace_id)
            if workspace is not None:
                return workspace.get("owner_uid")
        return None

    async def publish_rows(
        self, workspace_id: str, event: str, reason: str | None = None
    ) -> None:
        """One bridge transition as the proxy events the row
        surfaces already paint (the hub reaches the model and the
        event bus the same way the proxy subsystem does)."""
        hub = getattr(self.app.state, "hub", None)
        if hub is None:
            return
        with contextlib.suppress(Exception):
            owner = await self.row_owner(workspace_id)
            for row in await self.app.state.model.list_proxies(workspace_id):
                if event == UP_EVENT and not self.row_listening(row):
                    # The up-paint belongs to the rows whose
                    # listeners serve; a failed bind keeps its own
                    # cause (#1102 review round 3).
                    continue
                await self.publish_row(
                    hub, workspace_id, row, event, reason, owner
                )

    def row_listening(self, row: dict) -> bool:
        """Whether the up-paint may reach the row: False only for
        a row the proxy subsystem itself reports ``failed`` (its
        bind refused — the bridge's arrival repaints nothing over
        that cause); True for a listening or stopped row and when
        no subsystem answers (a bare state, the tests' shape)."""
        proxies = getattr(self.app.state, "proxies", None)
        if proxies is None:
            return True
        with contextlib.suppress(Exception):
            return proxies.status(row)[0] != "failed"
        return True

    async def publish_row(
        self,
        hub,
        workspace_id: str,
        row: dict,
        event: str,
        reason: str | None,
        owner: int | None,
    ) -> None:
        """One row's transition frame: the payload the listeners'
        own events carry, scoped to the row's owner (#1095)."""
        if row["direction"] == "remote":
            return
        payload = {
            "id": workspace_id,
            "proxy": row["id"],
            "direction": row["direction"],
            "listen_port": row["listen_port"],
            "target_port": row["target_port"],
        }
        if reason is not None:
            payload["reason"] = reason
        await hub.publish(event, payload, owner=owner)

    # --- the row status' facts ---------------------------------------

    def up(self, workspace_id: str) -> bool:
        """Whether a holder serves the workspace."""
        return bool(self.holders.get(workspace_id))

    def cause(self, workspace_id: str) -> str:
        """The named cause a row reads while none does."""
        return NO_HOLDER

    # --- the dataplane -------------------------------------------------

    async def open_channel(
        self, workspace_id: str, port: int, host: str | None = None
    ):
        """(reader, writer) for one accepted proxy connection,
        relayed to a serving holder. ``host`` rides when the row
        names a target beyond the guest's own loopback; the
        holder's ssh resolves it inside the workspace. Refuses
        with the named cause when no holder serves."""
        held = self.holders.get(workspace_id)
        if not held:
            raise NoHolder(self.cause(workspace_id))
        holder = held[0]
        cid = holder.next_cid
        holder.next_cid += 1
        reader = asyncio.StreamReader()
        holder.channels[cid] = reader
        await holder.send_control(OPEN, cid, port, host)
        return reader, ChannelWriter(holder, cid)

    def feed(self, holder: Holder, data: bytes) -> None:
        """One binary frame from a holder to its connection."""
        with contextlib.suppress(ValueError):
            cid, payload = unpack_frame(data)
            reader = holder.channels.get(cid)
            if reader is not None:
                reader.feed_data(payload)

    def end_channel(self, holder: Holder, cid: int) -> None:
        """One relayed connection ended from the holder's side."""
        reader = holder.channels.pop(cid, None)
        if reader is not None:
            reader.feed_eof()
