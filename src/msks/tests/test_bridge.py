"""The bridge hub (#1102): holder registration, the relayed
channel dataplane, and the transitions a row's status paints —
against fake websockets."""

import asyncio
import json
from pathlib import Path

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
    hub.end_channel(holder, 9)  # never stood: quiet


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

    class Bare:
        class state:
            hub = None

    hub = BridgeHub(Bare())
    await hub.publish_rows("ws-a", UP_EVENT)  # no hub: quiet

    class Raising:
        class state:
            hub = object()

            @staticmethod
            async def list_proxies(workspace_id):
                raise RuntimeError("gone")

    hub = BridgeHub(Raising())
    await hub.publish_rows("ws-a", DOWN_EVENT)  # read refused: quiet


async def test_channel_writer_wait_closed_lands() -> None:
    hub = BridgeHub(object())
    hub.register("ws-a", FakeSocket())
    _reader, writer = await hub.open_channel("ws-a", 8080)
    await writer.wait_closed()


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
