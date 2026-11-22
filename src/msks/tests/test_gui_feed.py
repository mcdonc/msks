"""The GUI's live events feed (#946): one websocket subscription
on the bridge's loop, delivering pushed frames, reconnecting
with backoff on every drop. The feed is plain asyncio — these
tests drive it against a fake socket, no Qt, no daemon, no
display; the bridge's own tests carry the signals and the task's
life.
"""

import asyncio
import importlib.util
import logging

import pytest
import websockets

if importlib.util.find_spec("PySide6") is None:
    pytest.skip(
        "PySide6 is not installed on this host",
        allow_module_level=True,
    )

from msks.client import egress as egress_mod  # noqa: E402
from msks.client import frames as frames_mod  # noqa: E402
from msks.client.gui import feed as feed_mod  # noqa: E402


class FakeWS:
    """The events socket's shape for the feed's tests: an async
    context manager whose stream yields the queued texts, ends
    cleanly when the script runs out (the daemon's close), or
    raises the exception the script names."""

    def __init__(self, script: list | None = None) -> None:
        self.script = list(script or [])
        self.closed = False
        #: A hanging socket: serves, never yields — the tail
        #: dial a test parks the reconnecting feed on.
        self.hang = False

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    def __aiter__(self):
        return self

    async def __anext__(self) -> str:
        if self.hang:
            await asyncio.sleep(3600)
        if not self.script:
            raise StopAsyncIteration
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    async def close(self) -> None:
        self.closed = True


class BrokenDial:
    """A dial that fails before the socket exists — the daemon
    an outage keeps from answering."""

    async def __aenter__(self):
        raise OSError("connection refused")

    async def __aexit__(self, *exc):
        return False


def hang_socket() -> FakeWS:
    """A socket that serves and never yields: the parked tail a
    test's dial hands the feed after its script runs out."""
    socket = FakeWS()
    socket.hang = True
    return socket


class Recorder:
    """The feed's four callbacks, recorded for the asserts."""

    def __init__(self) -> None:
        self.frames: list[dict] = []
        self.drops: list[str] = []
        self.reconnects = 0
        self.serves = 0

    def wire(self, feed: feed_mod.EventsFeed) -> None:
        feed.on_frame = self.frames.append
        feed.on_drop = self.drops.append
        feed.on_reconnect = self.note_reconnect
        feed.on_serve = self.note_serve

    def note_reconnect(self) -> None:
        self.reconnects += 1

    def note_serve(self) -> None:
        self.serves += 1


def drive(feed: feed_mod.EventsFeed) -> asyncio.Task:
    """The feed's loop as a task, tiny delays — the ladder's
    steps run in milliseconds, so a test's sleeps cover them."""
    feed.reconnect_delays = (0.001, 0.001, 0.001)
    return asyncio.get_running_loop().create_task(feed.run())


async def settle(task: asyncio.Task, seconds: float = 0.05) -> None:
    """Let the loop turn long enough for the scripted exchanges
    to land; the feed never stops on its own, so the task stays
    alive for the caller's cancel."""
    await asyncio.sleep(seconds)
    assert not task.done()


def test_frames_deliver_and_junk_skips() -> None:
    """One connection's frames cross decoded to the callback
    (#946): the lifecycle JSON the hub publishes, in stream
    order — and a line that is not a frame (unparseable, or JSON
    that is not an object) skips, logged, never fatal."""

    async def scenario() -> None:
        raws = [
            '{"event": "workspace.status", "data": {"id": "ws-a"}}',
            "not json at all",
            '["a", "list"]',
            '{"event": "workspace.stopped", "data": {"id": "ws-a"}}',
        ]
        # After the scripted socket, the dial fails forever: the
        # reconnecting feed must not replay the script.
        dials = iter([FakeWS(raws), BrokenDial()])
        feed = feed_mod.EventsFeed(ws_factory=lambda: next(dials))
        seen = Recorder()
        seen.wire(feed)
        task = drive(feed)
        await settle(task)
        task.cancel()
        assert [frame["event"] for frame in seen.frames] == [
            "workspace.status",
            "workspace.stopped",
        ]
        assert seen.reconnects == 0  # the first connection is no recovery
        assert seen.serves == 1  # the first connection serves once (#1019)

    asyncio.run(scenario())


def refused_closed(code: int) -> websockets.ConnectionClosed:
    """The wire's close exception with a received close code —
    the shape a peer's close handshake lands."""
    return websockets.ConnectionClosed(websockets.frames.Close(code, ""), None)


def test_a_drop_tells_and_a_frame_proves_the_recovery() -> None:
    """The outage's own heartbeat (#946): a socket that breaks
    mid-stream tells the drop with the wire's reason, the clean
    close the daemon's own, the failed dial its refusal — each
    attempt re-telling while the feed retries — and the
    reconnect is proven by a frame, not a handshake: the
    connection that serves after a drop tells it once, on its
    first delivered frame, the standing drop cleared for the
    next."""

    async def scenario() -> None:
        frame = '{"event": "workspace.status", "data": {"id": "a"}}'
        sockets = [
            # Serves a frame, then the wire cuts.
            FakeWS([frame, RuntimeError("wire cut")]),
            # The recovery: a frame proves it, then the daemon
            # closes.
            FakeWS([frame]),
            # The dial after the close: the daemon is down.
            BrokenDial(),
        ]

        def dial():
            # After the script, a hanging socket: the feed parks
            # on it, telling nothing more for the asserts.
            return sockets.pop(0) if sockets else hang_socket()

        feed = feed_mod.EventsFeed(ws_factory=dial)
        seen = Recorder()
        seen.wire(feed)
        task = drive(feed)
        await settle(task)
        # Both delivered frames — the recovery's as decoded as
        # the first's.
        assert seen.frames == [
            {"event": "workspace.status", "data": {"id": "a"}},
            {"event": "workspace.status", "data": {"id": "a"}},
        ]
        assert seen.drops == [
            "the socket broke: wire cut",
            "the daemon closed the events socket",
            "the dial failed: connection refused",
        ]
        # One recovery, proven by the second connection's frame;
        # the parked tail delivered none. The serve pokes told
        # twice (#1019): the first connection and the recovery
        # both served — each is a re-read moment for the screens,
        # frames published while no connection stood never
        # landed.
        assert seen.reconnects == 1
        assert seen.serves == 2
        task.cancel()

    asyncio.run(scenario())


def test_a_failed_dial_climbs_the_ladder_and_retries() -> None:
    """The dial that cannot connect (#946): the drop tells with
    the dial's own reason on every attempt — never a silent
    None — and the ladder climbs to its cap; a socket that
    finally serves resets it and recovers."""

    async def scenario() -> None:
        frame = '{"event": "secret.unlock", "data": {"id": "a"}}'
        sockets: list = [
            BrokenDial(),
            BrokenDial(),
            FakeWS([frame]),  # serves a frame, then closes clean
        ]

        def dial():
            return sockets.pop(0) if sockets else hang_socket()

        feed = feed_mod.EventsFeed(ws_factory=dial)
        seen = Recorder()
        seen.wire(feed)
        task = drive(feed)
        await settle(task)
        assert seen.drops == [
            "the dial failed: connection refused",
            "the dial failed: connection refused",
            "the daemon closed the events socket",
        ]
        # The one serving connection delivered a frame after the
        # drops: it tells the recovery; the parked tail — no
        # frame — tells nothing.
        assert seen.reconnects == 1
        task.cancel()

    asyncio.run(scenario())


def test_a_refused_token_retries_slowly_and_tells_no_recovery(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The daemon's token refusal (#216, #946 — the TUI wire's
    own read): a 4401 close tells the drop as the refusal, and
    the retry after it takes the fixed slow interval, not the
    ladder — a token the daemon does not hold cannot become
    valid by reconnecting. The refused connection delivered no
    frame, so it proves no recovery to tell."""

    async def scenario() -> None:
        monkeypatch.setattr(feed_mod, "REFUSED_RETRY_S", 0.001)
        frame = '{"event": "workspace.status", "data": {"id": "a"}}'
        sockets: list = [
            FakeWS([frame, refused_closed(4401)]),
            FakeWS([frame]),
        ]

        def dial():
            return sockets.pop(0) if sockets else hang_socket()

        feed = feed_mod.EventsFeed(ws_factory=dial)
        seen = Recorder()
        seen.wire(feed)
        task = drive(feed)
        await settle(task)
        assert seen.drops == [
            "the daemon refused the token",
            "the daemon closed the events socket",
        ]
        # The first frame of the first connection proves nothing
        # (the standing drop came later); the slow retry's frame
        # does.
        assert seen.reconnects == 1
        assert feed_mod.refused_close(refused_closed(4401))
        assert not feed_mod.refused_close(refused_closed(1000))
        task.cancel()

    asyncio.run(scenario())


def test_a_bare_feed_runs_without_its_callbacks(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The feed unwired (#946): the bridge sets the callbacks,
    but a bare feed still runs — frames land, drops and
    recoveries tell to nobody — and a socket whose close itself
    breaks stays best-effort without taking the loop: one
    broken close never kills the connection's cleanup, and the
    failure is named in the log, never swallowed (AGENTS.md's
    exception rule — #1002's review)."""

    async def scenario() -> None:
        class Unclosable(FakeWS):
            async def close(self) -> None:
                raise RuntimeError("close broke")

        sockets: list = [
            Unclosable(
                [
                    '{"event": "workspace.status", "data": {"id": "a"}}',
                    RuntimeError("wire cut"),
                ]
            ),
            # Delivers a frame after the drop: the reconnect tells
            # to nobody, but tells.
            FakeWS(['{"event": "secret.unlock", "data": {"id": "a"}}']),
        ]

        def dial():
            return sockets.pop(0) if sockets else hang_socket()

        feed = feed_mod.EventsFeed(ws_factory=dial)
        task = drive(feed)
        await settle(task)
        assert not task.done()
        task.cancel()

    caplog.set_level(logging.DEBUG, logger="msks.client.gui.feed")
    asyncio.run(scenario())
    assert any(
        "the events socket's close failed" in record.message
        and record.levelno == logging.DEBUG
        for record in caplog.records
    )


def test_the_ladder_resets_on_a_served_connection() -> None:
    """The backoff arithmetic (#946, the TUI wire's own): the
    ladder's first delay at the reset and below it, capped at
    the last — a connection that served resets the count, a run
    of failures climbs to the cap and stays there."""
    delays = (1.0, 2.0, 5.0)
    assert feed_mod.backoff(delays, 0) == 1.0
    assert feed_mod.backoff(delays, 1) == 1.0
    assert feed_mod.backoff(delays, 2) == 2.0
    assert feed_mod.backoff(delays, 3) == 5.0
    assert feed_mod.backoff(delays, 30) == 5.0
    assert feed_mod.backoff((), 4) == 0.0


def test_the_default_dial_addresses_the_daemon(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The default dial (#946): the daemon's own address
    vocabulary — the events path under the configured URL, the
    token on the handshake, no TLS context for the local
    listener and the shared one for a TCP daemon. The connect
    objects are lazy (the handshake rides ``__aenter__``), so the
    two shapes build without a daemon."""
    monkeypatch.setenv("MSKSC_TOKEN", "a-test-token")
    monkeypatch.setenv("MSKSC_URL", "http://127.0.0.1:1")
    monkeypatch.delenv("MSKSC_SOCKET", raising=False)
    tcp = egress_mod.env_dial_events()
    assert str(getattr(tcp, "uri", "")) == ("ws://127.0.0.1:1/api/v1/events")
    monkeypatch.setenv("MSKSC_URL", "http+unix:///tmp/msks.sock")
    local = egress_mod.env_dial_events()
    local_uri = str(getattr(local, "uri", ""))
    assert local_uri.startswith("ws://unix")
    assert local_uri.endswith("/api/v1/events")


def test_frame_decoding() -> None:
    """The wire text as a frame, or junk (#946, #1019): an object
    decodes, anything else — unparseable, an array, a bare word
    — reads as nothing. The decoder lives on the shared leaf
    both clients read (:mod:`msks.client.frames`)."""
    assert feed_mod.decode_frame('{"event": "e"}') == {"event": "e"}
    assert feed_mod.decode_frame("[1]") is None
    assert feed_mod.decode_frame("words") is None
    assert feed_mod.decode_frame("12") is None
    assert frames_mod.decode_frame is feed_mod.decode_frame  # the one decoder


def test_the_listing_frame_families() -> None:
    """Which frames move the listing (#1019): the lifecycle,
    volume, and catalog families and the mode switch re-read
    it; the proxies, secrets, and holds paint through their own
    surfaces and the listing re-reads nothing for them."""
    moved = [
        {"event": "workspace.created"},
        {"event": "workspace.status"},
        {"event": "workspace.mounts"},
        {"event": "home.imported"},
        {"event": "image.changed"},
        {"event": "egress.rules"},
    ]
    still = [
        {"event": "proxy.listening"},
        {"event": "secret.minted"},
        {"event": "egress.request"},
        {"event": "workspace"},  # a prefix, not a family member
        {},
    ]
    assert [frames_mod.listing_frame(frame) for frame in moved] == [
        True
    ] * len(moved)
    assert [frames_mod.listing_frame(frame) for frame in still] == [
        False
    ] * len(still)


def test_the_row_frame_families() -> None:
    """Which frames move one workspace's row (#1019): the
    lifecycle and volume families naming the workspace — under
    either naming convention the stream uses — and the catalog
    family, which names none and moves every row's image cell;
    another workspace's frame and every other family move
    nothing."""
    mine = [
        {"event": "workspace.status", "data": {"id": "ws-a"}},
        {"event": "home.imported", "data": {"workspace_id": "ws-a"}},
        {"event": "image.changed", "data": {}},
    ]
    not_mine = [
        {"event": "workspace.status", "data": {"id": "ws-b"}},
        {"event": "home.imported", "data": {}},
        {"event": "proxy.listening", "data": {"id": "ws-a"}},
        {"event": "egress.rules", "data": {"id": "ws-a"}},
    ]
    assert [frames_mod.row_frame(frame, "ws-a") for frame in mine] == [
        True
    ] * len(mine)
    assert [frames_mod.row_frame(frame, "ws-a") for frame in not_mine] == [
        False
    ] * len(not_mine)
