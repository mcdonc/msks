"""The bridge hub (#1102): holder registration, the relayed
channel dataplane, and the transitions a row's status paints —
against fake websockets."""

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from msks.app import build_app
from msks.bridge import (
    DOWN_EVENT,
    NO_HOLDER,
    UP_EVENT,
    BridgeHub,
    NoHolder,
    control,
)
from msks.microvm import VmSpec
from msks.settings import NetSettings, ServerSettings, Settings
from msks.spec.proxy import CLOSE, OPEN, pack_frame


class FakeSocket:
    """The websocket stand-in: sent frames recorded, receive
    scripted."""

    def __init__(self) -> None:
        self.sent: list = []
        self.incoming: list = []

    async def send_text(self, message: str) -> None:
        self.sent.append(("text", message))

    async def send_bytes(self, data: bytes) -> None:
        self.sent.append(("bytes", data))

    async def close(self, code: int = 1000) -> None:
        self.sent.append(("close", code))


async def hub_app(tmp_path: Path):
    """An app whose model carries one workspace with one forward
    row, for the transition publishes."""
    app = build_app(
        Settings(
            server=ServerSettings(db_path=tmp_path / "h.db"),
            net=NetSettings(enabled=False),
        )
    )
    app.state.model.migrate()
    await app.state.model.create_workspace(
        VmSpec(workspace_id="ws-a", kernel=Path("/k"), rootfs=Path("/r"))
    )
    await app.state.model.create_proxy(
        "ws-a", "forward", "127.0.0.1", 9, None, 8080, None
    )
    return app


async def test_registration_is_the_up_fact() -> None:
    hub = BridgeHub(object())
    socket = FakeSocket()
    assert hub.up("ws-a") is False
    assert hub.cause("ws-a") == NO_HOLDER
    holder = hub.register("ws-a", socket)
    assert hub.up("ws-a") is True
    hub.unregister("ws-a", holder)
    assert hub.up("ws-a") is False
    # Unregistering twice, or a stranger, changes nothing.
    hub.unregister("ws-a", holder)
    assert hub.up("ws-a") is False


async def test_a_second_holder_serves_on_the_firsts_departure() -> None:
    hub = BridgeHub(object())
    one, two = (
        hub.register("ws-a", FakeSocket()),
        hub.register("ws-a", FakeSocket()),
    )
    hub.unregister("ws-a", one)
    assert hub.up("ws-a") is True
    hub.unregister("ws-a", two)
    assert hub.up("ws-a") is False


async def test_open_channel_relays_both_ways() -> None:
    hub = BridgeHub(object())
    socket = FakeSocket()
    holder = hub.register("ws-a", socket)
    reader, writer = await hub.open_channel("ws-a", 8080)
    assert socket.sent == [("text", control(OPEN, 0, 8080))]
    # Guest-bound bytes: the writer queues, the drain moves them.
    writer.write(b"ping")
    await writer.drain()
    assert socket.sent[-1] == ("bytes", pack_frame(0, b"ping"))
    # Guest-offered bytes: the feed lands in the reader.
    hub.feed(holder, pack_frame(0, b"pong"))
    assert await reader.read(64) == b"pong"
    # Either side's close ends the channel.
    await writer.close()
    assert socket.sent[-1] == ("text", control(CLOSE, 0))
    hub.end_channel(holder, 0)
    assert await reader.read(64) == b""


async def test_open_channel_without_a_holder_refuses() -> None:
    hub = BridgeHub(object())
    with pytest.raises(NoHolder) as caught:
        await hub.open_channel("ws-a", 8080)
    assert NO_HOLDER in str(caught.value)


async def test_feed_skips_frames_it_cannot_place() -> None:
    hub = BridgeHub(object())
    holder = hub.register("ws-a", FakeSocket())
    hub.feed(holder, b"\x00\x01")  # shorter than an id
    hub.feed(holder, pack_frame(9, b"x"))  # no such connection
    reader, _writer = await hub.open_channel("ws-a", 8080)
    cid = holder.next_cid - 1
    hub.feed(holder, pack_frame(cid, b"placed"))
    hub.end_channel(holder, 9)  # never stood: quiet
    hub.end_channel(holder, cid)
    # Only the placeable frame reached the reader: the two before it
    # placed nothing, and ending a channel that never stood raised nothing.
    assert await reader.read() == b"placed"


def test_control_carries_the_port_on_opens_alone() -> None:
    assert json.loads(control(OPEN, 3, 8080)) == {
        "t": OPEN,
        "cid": 3,
        "port": 8080,
    }
    assert json.loads(control(CLOSE, 3)) == {"t": CLOSE, "cid": 3}


async def test_close_workspace_takes_the_holders() -> None:
    hub = BridgeHub(object())
    hub.register("ws-a", FakeSocket())
    taken = hub.close_workspace("ws-a")
    assert len(taken) == 1 and hub.up("ws-a") is False
    assert hub.close_workspace("ws-a") == []


async def test_transitions_publish_the_row_events(tmp_path: Path) -> None:
    """The first holder paints the forward rows listening; the
    last one's departure paints them failed with the cause — the
    same events the listeners' own binds publish."""
    app = await hub_app(tmp_path)
    hub = app.state.bridge
    queue = app.state.hub.subscribe()
    first = hub.register("ws-a", FakeSocket())
    await hub.publish_up("ws-a")
    # A second holder changes nothing: still up, no repaint.
    second = hub.register("ws-a", FakeSocket())
    await hub.publish_up("ws-a")
    # One holder going, with another standing: still up.
    hub.unregister("ws-a", first)
    await hub.publish_down("ws-a")
    # The last one going paints the failure.
    hub.unregister("ws-a", second)
    await hub.publish_down("ws-a")
    frames = [
        json.loads(await asyncio.wait_for(queue.get(), 2)) for _ in range(2)
    ]
    assert [(f["event"], f["data"]["proxy"]) for f in frames] == [
        ("proxy.listening", 1),
        ("proxy.failed", 1),
    ]
    assert NO_HOLDER in frames[1]["data"]["reason"]
    await app.state.model.close()


async def test_publish_rows_survives_a_hubless_state_and_a_model() -> None:
    """A state with no event hub publishes nothing, and a model
    read that fails under the transition is absorbed: the bridge's
    paints are best-effort like every proxy step."""

    sent = []

    async def publish(event, payload, owner=None):
        sent.append((event, payload))

    reached = []

    async def row_owner(workspace_id):
        reached.append(workspace_id)
        return None

    class Bare:
        class state:
            hub = None

    hub = BridgeHub(Bare())
    hub.row_owner = row_owner
    assert await hub.publish_rows("ws-a", UP_EVENT) is None  # no hub: quiet
    assert reached == []  # and nothing past the hub check ran

    class Raising:
        class state:
            hub = SimpleNamespace(publish=publish)

            @staticmethod
            async def list_proxies(workspace_id):
                raise RuntimeError("gone")

    hub = BridgeHub(Raising())
    await hub.publish_rows(
        "ws-a", DOWN_EVENT
    )  # the read's failure is absorbed
    assert sent == []  # and no row reached the hub


async def test_channel_writer_wait_closed_lands() -> None:
    hub = BridgeHub(object())
    hub.register("ws-a", FakeSocket())
    reader, writer = await hub.open_channel("ws-a", 8080)
    assert await writer.wait_closed() is None
    assert not reader.at_eof()  # the wait lands; the channel stands


async def test_publish_rows_skips_remote_rows(tmp_path: Path) -> None:
    """A remote row rides no bridge: the transition paints the
    forward rows alone."""
    app = await hub_app(tmp_path)
    await app.state.model.create_proxy(
        "ws-a", "remote", None, 9200, "127.0.0.1", 9200, None
    )
    queue = app.state.hub.subscribe()
    hub = app.state.bridge
    hub.register("ws-a", FakeSocket())
    await hub.publish_up("ws-a")
    frame = json.loads(await asyncio.wait_for(queue.get(), 2))
    assert frame["data"]["direction"] == "forward"
    assert queue.empty()
    await app.state.model.close()


async def test_departing_holders_eof_their_channels() -> None:
    """Both departure paths end their channels' readers: a
    connected idle peer must not hang forever (#1102 review)."""
    hub = BridgeHub(object())
    holder = hub.register("ws-a", FakeSocket())
    reader, _writer = await hub.open_channel("ws-a", 8080)
    hub.unregister("ws-a", holder)
    assert await asyncio.wait_for(reader.read(64), 2) == b""

    holder = hub.register("ws-a", FakeSocket())
    reader, _writer = await hub.open_channel("ws-a", 8080)
    hub.close_workspace("ws-a")
    assert await asyncio.wait_for(reader.read(64), 2) == b""


async def test_unregister_of_a_stranger_keeps_the_serving_holder() -> None:
    """The stop path pops the workspace first; the route's
    unregister then arrives as a stranger — twice over, with a
    second holder still serving — and must neither raise nor
    disturb the channels' retirement."""
    hub = BridgeHub(object())
    departing = hub.register("ws-a", FakeSocket())
    serving = hub.register("ws-a", FakeSocket())
    hub.unregister("ws-a", departing)
    assert hub.up("ws-a") is True
    hub.unregister("ws-a", departing)
    assert hub.up("ws-a") is True
    hub.close_workspace("ws-a")
    hub.unregister("ws-a", serving)
    assert hub.up("ws-a") is False


async def test_row_owner_reads_the_rows_owner() -> None:
    """The transition frames scope to the workspace's owner
    (#1095); a row that cannot be read scopes nothing."""
    from types import SimpleNamespace

    class Model:
        def __init__(self, row):
            self.row = row

        async def get_workspace(self, workspace_id):
            if isinstance(self.row, Exception):
                raise self.row
            return self.row

    hub = BridgeHub(SimpleNamespace(state=SimpleNamespace()))
    hub.app.state.model = Model({"owner_uid": 1000})
    assert await hub.row_owner("ws-a") == 1000
    hub.app.state.model = Model({"id": "ws-a"})
    assert await hub.row_owner("ws-a") is None
    hub.app.state.model = Model(RuntimeError("gone"))
    assert await hub.row_owner("ws-a") is None


async def test_a_holder_arriving_under_the_down_publish(tmp_path) -> None:
    """The race the round-2 review named: a holder registering
    while the down-publish reads its rows must end painted up,
    not failed."""
    app = await hub_app(tmp_path)
    hub = app.state.bridge
    queue = app.state.hub.subscribe()

    class Racing:
        """A model whose rows read lands a new holder mid-read."""

        def __init__(self, inner):
            self.inner = inner

        def __getattr__(self, name):
            return getattr(self.inner, name)

        async def list_proxies(self, workspace_id):
            rows = await self.inner.list_proxies(workspace_id)
            hub.register("ws-a", FakeSocket())
            return rows

    app.state.model = Racing(app.state.model)
    hub.register("ws-a", FakeSocket())
    hub.unregister("ws-a", hub.holders["ws-a"][0])
    await hub.publish_down("ws-a")
    frames = [
        json.loads(await asyncio.wait_for(queue.get(), 2)) for _ in range(2)
    ]
    assert [f["event"] for f in frames] == [
        "proxy.failed",
        "proxy.listening",
    ]
    await app.state.model.inner.close()


async def test_a_second_holder_arriving_repaints_nothing(
    tmp_path: Path,
) -> None:
    """The up-publish belongs to the FIRST holder alone: a second
    one arriving changes no row's paint."""
    app = await hub_app(tmp_path)
    hub = app.state.bridge
    queue = app.state.hub.subscribe()
    hub.register("ws-a", FakeSocket())
    await hub.publish_up("ws-a")
    hub.register("ws-a", FakeSocket())
    await hub.publish_up("ws-a")
    frame = json.loads(await asyncio.wait_for(queue.get(), 2))
    assert frame["event"] == "proxy.listening"
    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(queue.get(), 0.3)
    await app.state.model.close()


async def test_row_owner_of_a_missing_row_scopes_nothing() -> None:
    """A workspace row the model cannot find scopes its frames to
    nobody (the unscoped broadcast the subscribers allow)."""

    class Model:
        @staticmethod
        async def get_workspace(workspace_id):
            return None

    from types import SimpleNamespace

    hub = BridgeHub(SimpleNamespace(state=SimpleNamespace()))
    hub.app.state.model = Model()
    assert await hub.row_owner("gone") is None


async def test_open_channel_carries_the_named_host() -> None:
    """A row whose target names a host beyond the loopback: the
    open frame carries it beside the port, and the holder's ssh
    resolves it inside the workspace (#1102)."""
    hub = BridgeHub(object())
    socket = FakeSocket()
    hub.register("ws-a", socket)
    await hub.open_channel("ws-a", 5432, "db.internal")
    opened = json.loads(socket.sent[0][1])
    assert opened == {
        "t": OPEN,
        "cid": 0,
        "port": 5432,
        "host": "db.internal",
    }
    # No host named: the frame stays lean.
    await hub.open_channel("ws-a", 22)
    opened = json.loads(socket.sent[1][1])
    assert "host" not in opened


async def test_up_paint_skips_rows_whose_bind_failed(tmp_path) -> None:
    """The up-transition paints the rows whose listeners serve
    alone: a failed bind keeps its own cause, and its row is not
    repainted listening by the bridge's arrival."""

    app = await hub_app(tmp_path)
    queue = app.state.hub.subscribe()
    hub = app.state.bridge

    class RefusedBinds:
        def status(self, row):
            return ("failed", "bind refused")

    app.state.proxies = RefusedBinds()
    hub.register("ws-a", FakeSocket())
    await hub.publish_up("ws-a")
    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(queue.get(), 0.3)
    await app.state.model.close()


async def test_the_down_paint_takes_every_forward_row(tmp_path) -> None:
    """The down-transition's frame is the bridge's own fact
    (#1127): a row that reads failed only because no holder
    serves still takes it — the open client's re-hold watch keys
    on the frame, and the up-paint's bind-failure skip must not
    eat the down's whole audience (the holder is already
    unregistered when the down publishes, so every live forward
    row reads failed then)."""

    app = await hub_app(tmp_path)
    queue = app.state.hub.subscribe()
    hub = app.state.bridge

    class ServedRows:
        """The read a live forward row answers at down time:
        failed with the missing-holder cause, whatever its own
        bind did."""

        def status(self, row):
            from msks.bridge import NO_HOLDER

            return ("failed", NO_HOLDER)

    app.state.proxies = ServedRows()
    hub.register("ws-a", FakeSocket())
    await hub.publish_up("ws-a")  # the up-paint skips: the read is failed
    hub.unregister("ws-a", hub.holders["ws-a"][0])
    await hub.publish_down("ws-a")
    frame = json.loads(await asyncio.wait_for(queue.get(), 0.3))
    assert frame["event"] == "proxy.failed"
    assert frame["data"]["id"] == "ws-a"
    await app.state.model.close()


async def test_row_listening_guards_the_bare_states() -> None:
    """No proxy subsystem answers: the paint proceeds (the tests'
    bare-state shape); a status read that fails does the same."""
    from types import SimpleNamespace

    hub = BridgeHub(SimpleNamespace(state=SimpleNamespace()))
    assert hub.row_listening({"id": 1}) is True
    hub.app.state.proxies = SimpleNamespace(
        status=lambda row: (_ for _ in ()).throw(RuntimeError("gone"))
    )
    assert hub.row_listening({"id": 1}) is True
