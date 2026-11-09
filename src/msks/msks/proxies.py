"""The workspace proxy subsystem (#807, #808): daemon-owned,
persisted TCP proxies.

Two directions, both one row in the database and one listener in
this process:

- **forward** (the ssh ``-L`` shape): the daemon binds a port on
  its own host and every accepted connection opens a guest TCP
  connection through the same dial the forward websocket rides
  (#109) — one guest connection per accepted connection.
- **remote** (the ssh ``-R`` shape): the daemon binds a port on
  the workspace's tap address, and every connection a guest
  process makes to it is bridged to a daemon-side service.

The net manager asks this subsystem to start a workspace's
listeners at attach (the same hook the LLM and probe listeners
ride) and stops them at detach, so every stop, kill, and delete
ends them and every start re-establishes them — the persistence
the rows carry. A proxy whose bind fails (a taken port, an
address the host cannot bind) records the failure as its status
and publishes the cause; the boot proceeds, exactly the
best-effort posture the auxiliary listeners carry.

A root leaf module: the composition layer (``app.py``) and the
net manager (by attribute, through ``app.state``) reach it, and
it imports nothing from the daemon's other packages.
"""

import asyncio
import contextlib
import logging
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

#: The default daemon-side bind for a forward proxy: loopback. The
#: workspace's ports stay between the operator and the daemon's
#: host until an operator names a wider address deliberately.
DEFAULT_LISTEN_HOST = "127.0.0.1"

#: The default daemon-side target for a remote proxy: the daemon's
#: own loopback, where the host's services live.
DEFAULT_TARGET_HOST = "127.0.0.1"

#: One direction's byte budget per read — the same chunk the
#: forward client and console pump move.
READ_CHUNK = 4096

#: How long a listener's close waits its stalled peers (#807
#: review): the stop paths run under the workspace's net guard,
#: and a peer that never ACKs the FIN must not hold it. The
#: handlers are cancelled first, so the wait normally lands at
#: once.
STOP_GRACE_S = 5.0


@dataclass
class LiveProxy:
    """One proxy's process-local state: the bound server, its
    in-flight connection tasks, or — when the bind failed — the
    named cause. Exactly one of ``server`` and ``failure`` is
    set."""

    row: dict
    server: asyncio.AbstractServer | None = None
    failure: str | None = None
    tasks: set = field(default_factory=set)

    @property
    def workspace_id(self) -> str:
        return self.row["workspace_id"]


def pump(
    local_reader, local_writer, remote_reader, remote_writer
) -> asyncio.Task:
    """Bytes both ways between two stream pairs, as one task that
    ends when either side ends and closes both writers."""

    async def copy(source, sink) -> None:
        while True:
            data = await source.read(READ_CHUNK)
            if not data:
                return
            sink.write(data)
            await sink.drain()

    async def run() -> None:
        tasks = {
            asyncio.create_task(copy(local_reader, remote_writer)),
            asyncio.create_task(copy(remote_reader, local_writer)),
        }
        done, pending = await asyncio.wait(
            tasks, return_when=asyncio.FIRST_COMPLETED
        )
        for task in pending:
            task.cancel()
        for task in pending:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        for task in done:
            with contextlib.suppress(Exception):
                task.result()

    return asyncio.create_task(run())


class ProxyManager:
    """The proxy subsystem on ``app.state.proxies``.

    Owns the live listeners keyed by proxy row id. Reads the rows
    from the model at every start (the row set can change under
    it), and publishes its transitions on the hub.
    """

    def __init__(self, app) -> None:
        self.app = app
        self._live: dict[int, LiveProxy] = {}

    # --- lifecycle hooks (the net manager calls these) ---------------

    async def start_workspace(self, workspace_id: str, attachment) -> None:
        """Bring up every persisted proxy one workspace owns (the
        attach hook). Each proxy is best-effort alone: one failed
        bind names itself and the rest still come up."""
        rows = await self.app.state.model.list_proxies(workspace_id)
        for row in rows:
            await self.start_one(row, attachment)

    async def stop_workspace(self, workspace_id: str) -> None:
        """End every live listener the workspace owns (the detach
        hook; idempotent)."""
        for proxy_id in sorted(self._live):
            if self._live[proxy_id].workspace_id == workspace_id:
                await self.stop_one(proxy_id)

    async def stop_all(self) -> None:
        """End every live listener (daemon shutdown; idempotent) —
        a forward listener can outlive its workspace's attachment,
        so the net manager's shutdown sweep reaches past the
        attached set."""
        for proxy_id in sorted(self._live):
            await self.stop_one(proxy_id)

    # --- single-proxy operations (the API routes call these) ----------

    async def start_one(self, row: dict, attachment) -> dict:
        """Bind one proxy's listener; returns the row with its
        status. Idempotent: a listener already up stays up, a
        recorded failure gets its retry."""
        existing = self._live.get(row["id"])
        if existing is not None and existing.server is not None:
            return with_status(row, "listening", None)
        self._live.pop(row["id"], None)
        point = bind_point(row, attachment)
        if point is None:
            # A remote proxy without a live attachment records
            # nothing — its status is stopped, and the next start
            # brings it up.
            return with_status(row, "stopped", None)
        return await self.bind_listener(row, point)

    async def bind_listener(self, row: dict, point) -> dict:
        """Bind one proxy's listener at its ``(host, port)``,
        recording the failure as the row's status when the bind
        refuses (a taken port, an address the host cannot bind)."""
        try:
            server = await asyncio.start_server(
                self.connect_handler(row), *point
            )
        except OSError as exc:
            cause = str(exc) or type(exc).__name__
            self._live[row["id"]] = LiveProxy(row=row, failure=cause)
            await self.publish(row, "proxy.failed", detail=cause)
            return with_status(row, "failed", cause)
        self._live[row["id"]] = LiveProxy(row=row, server=server)
        await self.publish(row, "proxy.listening")
        return with_status(row, "listening", None)

    async def stop_one(self, proxy_id: int) -> None:
        """Close one live listener and end its in-flight
        connections; a recorded failure just leaves. Idempotent.
        The close is bounded: a peer that stalls the graceful
        teardown cannot hold the workspace guard this stop runs
        under (#807 review; the same reason close_forwards closes
        without waiting)."""
        live = self._live.pop(proxy_id, None)
        if live is None:
            return
        if live.server is None:
            return
        for task in live.tasks:
            task.cancel()
        live.server.close()
        with contextlib.suppress(Exception):
            # wait_closed also gathers the cancelled handlers'
            # cleanup (their finally closes the local writer and
            # drops their task records).
            await asyncio.wait_for(
                live.server.wait_closed(), timeout=STOP_GRACE_S
            )
        await self.publish(live.row, "proxy.closed")

    # --- reads ---------------------------------------------------------

    def status(self, row: dict) -> tuple[str, str | None]:
        """One row's live state: ``(listening, None)``,
        ``(failed, cause)``, or ``(stopped, None)``. A forward
        row binds without its workspace running (each connection
        refuses until the guest does); ``stopped`` is a remote
        row whose workspace is not up, or a row no listener
        serves."""
        live = self._live.get(row["id"])
        if live is None:
            return "stopped", None
        if live.server is not None:
            return "listening", None
        return "failed", live.failure

    async def bound_remote_ports(self, workspace_id: str) -> list[int]:
        """The workspace's remote listen ports that carry a live
        listener — the admission set the net manager's ingress
        chain carries for the tap. Rows alone never qualify: a
        row whose bind failed would admit a port something else
        squats (#807 review)."""
        return sorted(
            live.row["listen_port"]
            for live in self._live.values()
            if live.workspace_id == workspace_id
            and live.server is not None
            and live.row["direction"] == "remote"
        )

    # --- internals ------------------------------------------------------

    def connect_handler(self, row: dict):
        """The per-connection coroutine factory for one proxy's
        listener, carrying the row's direction to its bridge."""

        async def handle(reader, writer) -> None:
            task = asyncio.current_task()
            live = self._live.get(row["id"])
            if live is not None and task is not None:
                live.tasks.add(task)
            try:
                await self.bridge(row, reader, writer)
            finally:
                if live is not None and task is not None:
                    live.tasks.discard(task)
                writer.close()
                with contextlib.suppress(Exception):
                    await writer.wait_closed()

        return handle

    async def bridge(self, row: dict, reader, writer) -> None:
        """One accepted connection bridged per the row's direction.
        A refusal (the guest is not listening, the daemon-side
        target is down) ends the connection — the listener stays up
        for the next one, the same contract the forward client's
        ``--local`` listener keeps."""
        if row["direction"] == "remote":
            await self.bridge_remote(row, reader, writer)
        else:
            await self.bridge_forward(row, reader, writer)

    async def bridge_remote(self, row: dict, reader, writer) -> None:
        """The remote direction: dial the daemon-side service and
        pump bytes both ways until either end finishes."""
        host = row.get("target_host") or DEFAULT_TARGET_HOST
        try:
            target_reader, target_writer = await asyncio.open_connection(
                host, row["target_port"]
            )
        except OSError as exc:
            logger.info(
                "proxy %s: dial %s:%s refused: %s",
                row["id"],
                host,
                row["target_port"],
                exc,
            )
            return
        session = pump(reader, writer, target_reader, target_writer)
        try:
            await session
        finally:
            target_writer.close()
            with contextlib.suppress(Exception):
                await target_writer.wait_closed()

    async def bridge_forward(self, row: dict, reader, writer) -> None:
        """The forward direction: one guest TCP connection through
        the same dial the forward websocket rides, tracked so a
        detach ends it with the tap."""
        net = self.app.state.net
        guest_writer = None
        try:
            guest_reader, guest_writer = await net.forward_stream(
                row["workspace_id"], row["target_port"]
            )
            net.track_forward(row["workspace_id"], guest_writer)
            try:
                await pump(reader, writer, guest_reader, guest_writer)
            finally:
                net.untrack_forward(row["workspace_id"], guest_writer)
        except Exception as exc:
            logger.info(
                "proxy %s: guest port %s refused: %s",
                row["id"],
                row["target_port"],
                exc,
            )
        finally:
            if guest_writer is not None:
                guest_writer.close()
                with contextlib.suppress(Exception):
                    await guest_writer.wait_closed()

    async def publish(
        self, row: dict, event: str, detail: str | None = None
    ) -> None:
        """One proxy transition on the hub (and its journal line,
        the hub's own #762 rule)."""
        hub = self.app.state.hub
        if hub is None:
            return
        payload = {
            "id": row["workspace_id"],
            "proxy": row["id"],
            "direction": row["direction"],
            "listen_port": row["listen_port"],
            "target_port": row["target_port"],
        }
        if detail is not None:
            payload["reason"] = detail
        await hub.publish(event, payload)


def bind_point(row: dict, attachment) -> tuple[str, int] | None:
    """One row's ``(host, port)`` bind, or None when nothing can
    bind yet: a forward proxy binds the daemon-side address its
    row names (loopback by default), a remote one binds its
    workspace's tap — an address that exists only while the
    workspace runs."""
    if row["direction"] == "remote":
        return (
            None
            if attachment is None
            else (
                attachment.tap_ip,
                row["listen_port"],
            )
        )
    return row.get("listen_host") or DEFAULT_LISTEN_HOST, row["listen_port"]


def with_status(row: dict, status: str, reason: str | None) -> dict:
    """One row dict with its live status fields attached."""
    return {**row, "status": status, "reason": reason}
