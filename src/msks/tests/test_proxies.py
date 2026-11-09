"""The workspace proxy subsystem (#807, #808): the proxy manager's
listeners and bridges, the model's proxy rows, and the nft
admission rendering — against fakes with real sockets."""

import asyncio
import contextlib
import json
from pathlib import Path

import pytest
from msks.app import build_app
from msks.microvm import VmSpec
from msks.net.manager import NetAttachment
from msks.proxies import ProxyManager, with_status
from msks.settings import NetSettings, ServerSettings, Settings


class FakeNet:
    """The net seam: an echo listener behind ``forward_stream``
    (the dial the forward websocket rides), with call tracking."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, int]] = []
        self.tracked: list[tuple] = []
        self.untracked: list[tuple] = []
        self.refusal: Exception | None = None
        self._server: asyncio.AbstractServer | None = None

    async def _echo(self, reader, writer) -> None:
        while True:
            data = await reader.read(4096)
            if not data:
                break
            writer.write(data.upper())
            await writer.drain()
        writer.close()

    async def forward_stream(self, workspace_id: str, port: int):
        self.calls.append((workspace_id, port))
        if self.refusal is not None:
            raise self.refusal
        if self._server is None:
            self._server = await asyncio.start_server(
                self._echo, "127.0.0.1", 0
            )
        address = self._server.sockets[0].getsockname()
        return await asyncio.open_connection(*address)

    def track_forward(self, workspace_id: str, writer) -> None:
        self.tracked.append((workspace_id, writer))

    def untrack_forward(self, workspace_id: str, writer) -> None:
        self.untracked.append((workspace_id, writer))

    def attachment(self, workspace_id: str) -> NetAttachment | None:
        return ATTACHMENT

    async def refresh_proxies(self, workspace_id: str) -> None:
        self.calls.append(("refresh", workspace_id))


def attachment() -> NetAttachment:
    return NetAttachment(
        workspace_id="ws-a",
        tap="msks-tap",
        mac="02:00:00:00:00:01",
        guest_ip="172.31.0.1",
        tap_ip="127.0.0.1",
        slice=0,
    )


ATTACHMENT = attachment()


async def echo_target() -> tuple[asyncio.AbstractServer, int]:
    """A daemon-side service for the remote direction: answers in
    lower case, so a bridged byte's path reads from the answer."""

    async def serve(reader, writer) -> None:
        while True:
            data = await reader.read(4096)
            if not data:
                break
            writer.write(data.lower())
            await writer.drain()
        writer.close()

    server = await asyncio.start_server(serve, "127.0.0.1", 0)
    return server, server.sockets[0].getsockname()[1]


async def free_port() -> int:
    """A port the kernel confirms free (bound then released)."""
    server = await asyncio.start_server(lambda r, w: None, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    server.close()
    await server.wait_closed()
    return port


@pytest.fixture
async def proxies(tmp_path: Path):
    app = build_app(
        Settings(
            server=ServerSettings(db_path=tmp_path / "p.db"),
            net=NetSettings(enabled=False),
        )
    )
    app.state.model.migrate()
    await app.state.model.create_workspace(
        VmSpec(
            workspace_id="ws-a",
            kernel=Path("/k"),
            rootfs=Path("/r"),
            egress=True,
        )
    )
    net = FakeNet()
    app.state.net = net
    queue = app.state.hub.subscribe()
    try:
        yield app.state.proxies, app.state.model, net, queue, app
    finally:
        await app.state.model.close()


async def next_event(queue) -> tuple[str, dict]:
    """The next proxy event off the hub, its type and payload."""
    payload = await asyncio.wait_for(queue.get(), timeout=2.0)
    frame = json.loads(payload)
    return frame["event"], frame["data"]


async def create_forward_row(model, listen_port: int) -> dict:
    return await model.create_proxy(
        "ws-a", "forward", "127.0.0.1", listen_port, None, 22, "ssh"
    )


async def exchange(port: int, payload: bytes) -> bytes:
    """One round trip through a listener on loopback."""
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    writer.write(payload)
    await writer.drain()
    answer = await asyncio.wait_for(reader.read(4096), timeout=2.0)
    writer.close()
    with contextlib.suppress(Exception):
        await writer.wait_closed()
    return answer


async def test_forward_proxy_bridges_and_tracks(proxies) -> None:
    manager, model, net, _queue, _app = proxies
    port = await free_port()
    row = await create_forward_row(model, port)
    live = await manager.start_one(row, ATTACHMENT)
    assert live["status"] == "listening"
    assert await exchange(port, b"hello\n") == b"HELLO\n"
    assert net.calls == [("ws-a", 22)]
    assert net.tracked
    await manager.stop_one(row["id"])
    # The closed session untracks its guest stream (detach's
    # cleanup contract) — stop_one waits the handler out.
    assert net.untracked


async def test_forward_refusal_keeps_listener(proxies, caplog) -> None:
    manager, model, net, _queue, _app = proxies
    net.refusal = RuntimeError("no live network attachment")
    port = await free_port()
    row = await create_forward_row(model, port)
    live = await manager.start_one(row, ATTACHMENT)
    assert live["status"] == "listening"
    # The refused dial ends the connection, not the listener.
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    assert await reader.read(4096) == b""
    writer.close()
    reader2, writer2 = await asyncio.open_connection("127.0.0.1", port)
    writer2.close()
    await manager.stop_one(row["id"])


async def test_remote_proxy_bridges_to_daemon_side(proxies) -> None:
    manager, model, _net, _queue, _app = proxies
    target, target_port = await echo_target()
    async with target:
        listen_port = await free_port()
        row = await model.create_proxy(
            "ws-a",
            "remote",
            None,
            listen_port,
            "127.0.0.1",
            target_port,
            None,
        )
        live = await manager.start_one(row, ATTACHMENT)
        assert live["status"] == "listening"
        assert await exchange(listen_port, b"UPPER\n") == b"upper\n"
        await manager.stop_one(row["id"])


async def test_remote_dead_target_ends_connection(proxies) -> None:
    manager, model, _net, _queue, _app = proxies
    dead_port = await free_port()
    listen_port = await free_port()
    row = await model.create_proxy(
        "ws-a", "remote", None, listen_port, "127.0.0.1", dead_port, None
    )
    live = await manager.start_one(row, ATTACHMENT)
    assert live["status"] == "listening"
    reader, writer = await asyncio.open_connection("127.0.0.1", listen_port)
    assert await reader.read(4096) == b""
    writer.close()
    await manager.stop_one(row["id"])


async def test_remote_without_attachment_records_stopped(proxies) -> None:
    manager, model, _net, _queue, _app = proxies
    row = await model.create_proxy(
        "ws-a", "remote", None, 9200, "127.0.0.1", 9200, None
    )
    live = await manager.start_one(row, None)
    assert live["status"] == "stopped"
    assert manager.status(row) == ("stopped", None)


async def test_failed_bind_records_reason_and_retries(proxies) -> None:
    manager, model, _net, queue, _app = proxies
    port = await free_port()
    squatter = await asyncio.start_server(lambda r, w: None, "127.0.0.1", port)
    row = await create_forward_row(model, port)
    live = await manager.start_one(row, ATTACHMENT)
    assert live["status"] == "failed"
    assert live["reason"]
    assert next_event_ok(await next_event(queue), "proxy.failed")
    squatter.close()
    await squatter.wait_closed()
    live = await manager.start_one(row, ATTACHMENT)
    assert live["status"] == "listening"
    await manager.stop_one(row["id"])


def next_event_ok(event: tuple[str, dict], expected: str) -> bool:
    assert event[0] == expected
    assert event[1]["id"] == "ws-a"
    return True


async def test_start_workspace_is_idempotent(proxies) -> None:
    manager, model, _net, _queue, _app = proxies
    port = await free_port()
    await create_forward_row(model, port)
    await manager.start_workspace("ws-a", ATTACHMENT)
    await manager.start_workspace("ws-a", ATTACHMENT)
    assert await exchange(port, b"again\n") == b"AGAIN\n"
    await manager.stop_workspace("ws-a")


async def test_stop_workspace_closes_listeners(proxies) -> None:
    manager, model, _net, queue, _app = proxies
    port = await free_port()
    row = await create_forward_row(model, port)
    await manager.start_one(row, ATTACHMENT)
    assert next_event_ok(await next_event(queue), "proxy.listening")
    await manager.stop_workspace("ws-a")
    with pytest.raises(OSError):
        await exchange(port, b"late\n")
    assert next_event_ok(await next_event(queue), "proxy.closed")


async def test_status_of_unknown_row_is_stopped(proxies) -> None:
    manager, _model, _net, _queue, _app = proxies
    assert manager.status({"id": 999}) == ("stopped", None)


async def test_bound_remote_ports_tracks_live_binds(proxies) -> None:
    """Rows alone never qualify (#807 review): only a remote
    listener that is up carries an admission, so a failed bind
    admits nothing."""
    manager, model, _net, _queue, _app = proxies
    await create_forward_row(model, await free_port())
    remote_port = await free_port()
    await model.create_proxy(
        "ws-a", "remote", None, remote_port, "127.0.0.1", remote_port, None
    )
    assert await manager.bound_remote_ports("ws-a") == []
    await manager.start_workspace("ws-a", ATTACHMENT)
    assert await manager.bound_remote_ports("ws-a") == [remote_port]
    await manager.stop_workspace("ws-a")
    assert await manager.bound_remote_ports("ws-a") == []


async def test_events_publish_on_listener_transitions(proxies) -> None:
    manager, model, _net, queue, _app = proxies
    port = await free_port()
    row = await create_forward_row(model, port)
    await manager.start_one(row, ATTACHMENT)
    assert next_event_ok(await next_event(queue), "proxy.listening")
    await manager.stop_one(row["id"])
    assert next_event_ok(await next_event(queue), "proxy.closed")


# --- the model layer -------------------------------------------------


async def test_proxy_rows_crud_and_cascade(tmp_path: Path) -> None:
    app = build_app(
        Settings(
            server=ServerSettings(db_path=tmp_path / "m.db"),
            net=NetSettings(enabled=False),
        )
    )
    app.state.model.migrate()
    model = app.state.model
    await model.create_workspace(
        VmSpec(workspace_id="ws-a", kernel=Path("/k"), rootfs=Path("/r"))
    )
    await model.create_workspace(
        VmSpec(workspace_id="ws-b", kernel=Path("/k"), rootfs=Path("/r"))
    )
    row = await model.create_proxy(
        "ws-a", "forward", "127.0.0.1", 2201, None, 22, "ssh"
    )
    assert row["direction"] == "forward"
    assert row["listen_host"] == "127.0.0.1"
    assert row["target_host"] is None
    listed = await model.list_proxies("ws-a")
    assert [r["id"] for r in listed] == [row["id"]]
    assert await model.list_proxies() == listed
    fetched = await model.get_proxy("ws-a", row["id"])
    # created_at's tz suffix differs across the sqlite round-trip;
    # every other field rides unchanged.
    assert {k: v for k, v in fetched.items() if k != "created_at"} == {
        k: v for k, v in row.items() if k != "created_at"
    }
    assert await model.get_proxy("ws-b", row["id"]) is None
    assert await model.delete_proxy("ws-b", row["id"]) is False
    assert await model.delete_proxy("ws-a", row["id"]) is True
    assert await model.list_proxies("ws-a") == []
    # The workspace delete cascades its rows.
    again = await model.create_proxy(
        "ws-a", "remote", None, 9200, "127.0.0.1", 9200, None
    )
    assert again["id"] != row["id"]
    await model.delete_workspace("ws-a")
    assert await model.list_proxies("ws-a") == []
    await app.state.model.close()


def test_with_status_merges_fields() -> None:
    assert with_status({"id": 1}, "listening", None) == {
        "id": 1,
        "status": "listening",
        "reason": None,
    }


def test_manager_constructs_plain() -> None:
    # The manager caches only the app (the ownership rule).
    manager = ProxyManager(object())
    assert manager.status({"id": 1}) == ("stopped", None)


async def test_stop_one_on_a_failed_entry_just_drops_it(proxies) -> None:
    manager, model, _net, queue, _app = proxies
    port = await free_port()
    squatter = await asyncio.start_server(lambda r, w: None, "127.0.0.1", port)
    row = await create_forward_row(model, port)
    live = await manager.start_one(row, ATTACHMENT)
    assert live["status"] == "failed"
    assert manager.status(row) == ("failed", live["reason"])
    assert next_event_ok(await next_event(queue), "proxy.failed")
    # The failed entry has no listener: stopping it drops the
    # record without a proxy.closed event.
    await manager.stop_one(row["id"])
    assert manager.status(row) == ("stopped", None)
    assert queue.empty()
    squatter.close()
    await squatter.wait_closed()


async def test_stop_all_ends_every_listener(proxies) -> None:
    manager, model, _net, _queue, _app = proxies
    one, two = await free_port(), await free_port()
    await create_forward_row(model, one)
    await create_forward_row(model, two)
    await manager.start_workspace("ws-a", ATTACHMENT)
    await manager.stop_all()
    for port in (one, two):
        with pytest.raises(OSError):
            await exchange(port, b"late\n")


async def test_publish_skips_a_hubless_app() -> None:
    # The shutdown-time shape: a state whose hub is absent
    # publishes nothing and raises nothing.
    from types import SimpleNamespace

    manager = ProxyManager(SimpleNamespace(state=SimpleNamespace(hub=None)))
    await manager.publish({"workspace_id": "ws", "id": 1}, "proxy.listening")


def test_bind_point_shapes() -> None:
    from msks.proxies import bind_point

    forward = {"direction": "forward", "listen_host": None, "listen_port": 1}
    assert bind_point(forward, None) == ("127.0.0.1", 1)
    named = {**forward, "listen_host": "0.0.0.0"}
    assert bind_point(named, None) == ("0.0.0.0", 1)
    remote = {"direction": "remote", "listen_host": None, "listen_port": 2}
    assert bind_point(remote, None) is None
    assert bind_point(remote, ATTACHMENT) == ("127.0.0.1", 2)


async def test_stop_ends_in_flight_connections(proxies) -> None:
    """A stop cancels the sessions still bridging: two idle
    connections close with the listener, and the per-task cleanup
    (the tracked guest streams, the discarded task records) runs
    for both."""
    manager, model, _net, _queue, _app = proxies
    port = await free_port()
    row = await create_forward_row(model, port)
    await manager.start_one(row, ATTACHMENT)
    held = []
    for _ in range(2):
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        held.append((reader, writer))
        await asyncio.sleep(0.05)  # the handler task registers itself
    await manager.stop_one(row["id"])
    for reader, _writer in held:
        assert await reader.read(100) == b""


async def test_handler_closes_its_stream_without_a_live_entry(proxies) -> None:
    """A connection the listener accepted after its entry left
    (a stop landing mid-accept) still gets its cleanup: the
    handler bridges and closes with no task record to keep."""
    manager, model, net, _queue, _app = proxies
    row = await create_forward_row(model, await free_port())
    handler = manager.connect_handler(row)
    reader = asyncio.StreamReader()
    reader.feed_eof()  # nothing to bridge: the pump ends at once

    # A minimal writer stand-in: records the close.
    class Sink:
        closed = False

        def close(self):
            self.closed = True

        async def wait_closed(self):
            return None

    sink = Sink()
    net.refusal = RuntimeError("no live network attachment")
    await handler(reader, sink)
    assert sink.closed


async def test_stop_workspace_ends_each_listener(proxies) -> None:
    manager, model, _net, _queue, _app = proxies
    one, two = await free_port(), await free_port()
    await create_forward_row(model, one)
    await create_forward_row(model, two)
    await manager.start_workspace("ws-a", ATTACHMENT)
    await manager.stop_workspace("ws-a")
    for port in (one, two):
        with pytest.raises(OSError):
            await exchange(port, b"late\n")


async def test_stop_workspace_spares_other_workspaces(proxies) -> None:
    """The sweep skips another workspace's listener: only the
    named workspace's rows come down."""
    manager, model, _net, _queue, _app = proxies
    mine, theirs = await free_port(), await free_port()
    await create_forward_row(model, mine)
    await model.create_proxy(
        "ws-b", "forward", "127.0.0.1", theirs, None, 22, None
    )
    await manager.start_workspace("ws-a", ATTACHMENT)
    await manager.start_workspace("ws-b", ATTACHMENT)
    await manager.stop_workspace("ws-a")
    with pytest.raises(OSError):
        await exchange(mine, b"late\n")
    assert await exchange(theirs, b"still here\n") == b"STILL HERE\n"
    await manager.stop_workspace("ws-b")
