"""The workspace page's decider registration (#999): one
events connection the page owns beside the window's feed —
the registration frame at every serve, the pending snapshot
and rules posture landing on the TUI's pure consent
controller, the reconnect ladder re-registering with the
controller reset (holds that resolved during the outage drop,
not linger), and the daemon's refusal of the registration
ending the link with its reason told. The presence tells
(#1052) beside them: the registration's answer tells the
serve once per connection, every connection end and failed
dial tells the drop with its reason — the retire's cancel
included — and the refusal keeps its own tell. The link is
plain asyncio — these tests drive it against a fake socket,
no Qt, no daemon, no display; the bridge's own tests carry
the signals and the task's life, the page's own the lifetime
bracket.
"""

import asyncio
import importlib.util
import json

import pytest
import websockets

if importlib.util.find_spec("PySide6") is None:
    pytest.skip(
        "PySide6 is not installed on this host",
        allow_module_level=True,
    )

from msks.client import egress as egress_mod  # noqa: E402
from msks.client.gui import decider as decider_mod  # noqa: E402
from msks.client.tui.consent import (  # noqa: E402
    ADDED,
    IGNORED,
    REJECTED,
    RESOLVED,
    RULES,
    ConsentController,
)


class FakeWS:
    """The events socket's shape for the link's tests: an async
    context manager whose stream yields the queued texts, ends
    cleanly when the script runs out (the daemon's close), or
    raises the exception the script names — and whose sends the
    registration assert reads."""

    def __init__(self, script: list | None = None) -> None:
        self.script = list(script or [])
        self.sends: list[str] = []
        self.closed = False

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def send(self, raw: str) -> None:
        self.sends.append(raw)

    def __aiter__(self):
        return self

    async def __anext__(self) -> str:
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


def refused_closed(code: int) -> websockets.ConnectionClosed:
    """The wire's close exception with a received close code —
    the shape a peer's close handshake lands."""
    return websockets.ConnectionClosed(websockets.frames.Close(code, ""), None)


def rules_raw(ws: str = "ws-a", mode: str = "interactive") -> str:
    """One ``egress.rules`` frame: the workspace's posture."""
    return json.dumps(
        {
            "event": "egress.rules",
            "data": {
                "workspace_id": ws,
                "mode": mode,
                "allow_list": ["api.github.com"],
            },
        }
    )


def request_raw(
    rid: str = "req-1",
    ws: str = "ws-a",
    host: str = "api.github.com",
    port: int = 443,
    at: float = 1000.0,
    expires: float = 1120.0,
) -> str:
    """One ``egress.request`` frame: a held flow."""
    return json.dumps(
        {
            "event": "egress.request",
            "data": {
                "workspace_id": ws,
                "request": {
                    "id": rid,
                    "workspace_id": ws,
                    "dest_host": host,
                    "dest_port": port,
                    "requested_at": at,
                    "expires_at": expires,
                },
            },
        }
    )


def resolved_raw(rid: str = "req-1", ws: str = "ws-a") -> str:
    """One ``egress.resolved`` frame: a hold's exit."""
    return json.dumps(
        {
            "event": "egress.resolved",
            "data": {
                "workspace_id": ws,
                "request_id": rid,
                "decision": "allow",
            },
        }
    )


def rejected_raw(reason: str | None = None) -> str:
    """One ``egress.decider_rejected`` frame, with its reason or
    without."""
    data = {} if reason is None else {"reason": reason}
    return json.dumps({"event": "egress.decider_rejected", "data": data})


class Recorder:
    """The link's four callbacks, recorded for the asserts."""

    def __init__(self) -> None:
        self.outcomes: list[tuple[str, object]] = []
        self.refusals: list[str] = []
        self.serves = 0
        self.drops: list[str] = []

    def wire(self, link: decider_mod.DeciderLink) -> None:
        link.on_frame = self.note_frame
        link.on_rejected = self.refusals.append
        link.on_serve = self.note_serve
        link.on_drop = self.drops.append

    def note_frame(self, outcome: str, payload: object) -> None:
        self.outcomes.append((outcome, payload))

    def note_serve(self) -> None:
        self.serves += 1


def drive(
    link: decider_mod.DeciderLink,
    delays: tuple[float, ...] = (0.001, 0.001, 0.001),
) -> asyncio.Task:
    """The link's loop as a task, tiny delays — the ladder's
    steps run in milliseconds, so a test's sleeps cover them."""
    link.reconnect_delays = delays
    return asyncio.get_running_loop().create_task(link.run())


async def settle(seconds: float = 0.05) -> None:
    """Let the loop turn long enough for the scripted exchanges
    to land."""
    await asyncio.sleep(seconds)


async def wait_done(task: asyncio.Task) -> None:
    """The deadline pump for a link that ends itself — the
    daemon's refusal of the registration."""
    deadline = asyncio.get_running_loop().time() + 10.0
    while not task.done():
        if asyncio.get_running_loop().time() > deadline:
            pytest.fail("the link never ended itself")
        await asyncio.sleep(0.001)


def registration(workspace_id: str) -> str:
    """The registration frame's wire text, for the sends'
    assert."""
    return decider_mod.registration_frame(workspace_id)


def test_the_registration_lands_the_snapshot() -> None:
    """One connection's whole exchange (#999): the registration
    frame goes out the moment the socket serves, the rules
    posture and each pending hold land on the controller, a
    hold resolves on its frame, and what the controller cannot
    own — a line that is not a frame, a foreign workspace's
    hold — lands nowhere, every outcome crossing to
    ``on_frame`` in arrival order."""

    async def scenario() -> None:
        raws = [
            rules_raw(),
            request_raw("req-1"),
            request_raw("req-2"),
            "not json at all",
            request_raw("req-9", ws="ws-b"),
            resolved_raw("req-2"),
        ]
        # After the scripted socket, the dial fails forever: the
        # reconnecting link must not replay the script.
        dials = iter([FakeWS(raws), BrokenDial()])
        link = decider_mod.DeciderLink("ws-a", ws_factory=lambda: next(dials))
        seen = Recorder()
        seen.wire(link)
        task = drive(link)
        await settle()
        task.cancel()
        assert link.controller.pending.keys() == {"req-1"}
        assert link.controller.rules is not None
        assert link.controller.rules.mode == "interactive"
        assert [outcome for outcome, _ in seen.outcomes] == [
            RULES,
            ADDED,
            ADDED,
            IGNORED,
            IGNORED,
            RESOLVED,
        ]
        held = seen.outcomes[1][1]
        assert held.dest_host == "api.github.com"
        assert held.dest_port == 443

    asyncio.run(scenario())


def test_a_rejected_registration_ends_the_link() -> None:
    """The daemon's refusal (#999): an unknown workspace answers
    ``egress.decider_rejected``, the reason crosses to
    ``on_rejected`` for the panel to surface, and the link ends
    itself — the frames behind the refusal never land, the
    socket closes with the link, and no reconnect asks again."""

    async def scenario() -> None:
        socket = FakeWS(
            [rejected_raw("unknown workspace"), request_raw("req-1")]
        )
        link = decider_mod.DeciderLink("ws-nope", ws_factory=lambda: socket)
        seen = Recorder()
        seen.wire(link)
        task = drive(link)
        await wait_done(task)
        assert seen.refusals == ["unknown workspace"]
        assert link.reject_reason == "unknown workspace"
        assert link.controller.pending == {}
        assert socket.closed
        # The loop ended on its own, cancelled nowhere.
        assert not task.cancelled()

    asyncio.run(scenario())


def test_a_reconnect_re_registers_and_replays_the_snapshot() -> None:
    """The drop and its reconnect (#999): the registration goes
    out again on the new socket, the controller's reset drops
    the holds that resolved during the outage — the replay
    re-lands only what still stands — and a wire that breaks
    mid-stream takes the same reconnect as a clean close."""

    async def scenario() -> None:
        first = FakeWS(
            [rules_raw(), request_raw("req-1"), request_raw("req-2")]
        )
        # The replay after the outage: req-2 resolved while the
        # link was down, so the snapshot the daemon answers with
        # carries req-1 alone. The exhausted dial fails — a
        # serving tail would re-register and reset the
        # controller again, and the state under assert would
        # not be the replay's own.
        second = FakeWS([rules_raw(mode="static"), request_raw("req-1")])
        second.script.append(RuntimeError("wire cut"))
        sockets = [first, second]

        def dial():
            return sockets.pop(0) if sockets else BrokenDial()

        link = decider_mod.DeciderLink("ws-a", ws_factory=dial)
        seen = Recorder()
        seen.wire(link)
        task = drive(link)
        await settle()
        task.cancel()
        assert first.sends == [registration("ws-a")]
        assert second.sends == [registration("ws-a")]
        assert link.controller.pending.keys() == {"req-1"}
        assert link.controller.rules is not None
        assert link.controller.rules.mode == "static"

    asyncio.run(scenario())


def test_a_refused_close_retries_slowly(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The daemon's token refusal (#216 over #999 — the feed's
    own read): a 4401 close takes the fixed slow interval, not
    the ladder — a token the daemon does not hold cannot become
    valid by reconnecting — and the re-registration after it
    lands on the next socket all the same. A plain close (1000)
    is not a refusal: it takes the ladder."""

    async def scenario() -> None:
        monkeypatch.setattr(decider_mod, "REFUSED_RETRY_S", 0.001)
        frame = request_raw("req-1")
        # The ladder would park the link for an hour: only the
        # slow-retry arm reaches the second socket inside a
        # test's span, and the third dial (after the plain
        # close) parks on the ladder the cancel then takes.
        first = FakeWS([refused_closed(4401)])
        second = FakeWS([frame, refused_closed(1000)])
        sockets = [first, second]

        def dial():
            return sockets.pop(0) if sockets else BrokenDial()

        link = decider_mod.DeciderLink("ws-a", ws_factory=dial)
        seen = Recorder()
        seen.wire(link)
        task = drive(link, delays=(3600.0,))
        await settle()
        assert first.sends == [registration("ws-a")]
        assert second.sends == [registration("ws-a")]
        assert link.controller.pending.keys() == {"req-1"}
        task.cancel()

    asyncio.run(scenario())


def test_a_failed_dial_reconnects_until_the_daemon_answers() -> None:
    """The dial that cannot connect (#999): the link says
    nothing of its own — the window's feed flashes the same
    outage over its own socket — and keeps dialing until a
    socket serves; the registration lands the moment one
    does, and the hold behind it."""

    async def scenario() -> None:
        late = FakeWS([rules_raw(), request_raw("req-1")])
        sockets: list = [BrokenDial(), BrokenDial(), late]

        def dial():
            return sockets.pop(0) if sockets else BrokenDial()

        link = decider_mod.DeciderLink("ws-a", ws_factory=dial)
        seen = Recorder()
        seen.wire(link)
        task = drive(link)
        await settle()
        assert not task.done()
        assert late.sends == [registration("ws-a")]
        assert link.controller.pending.keys() == {"req-1"}
        task.cancel()

    asyncio.run(scenario())


def test_a_bare_link_runs_and_ends_without_its_callbacks() -> None:
    """The link unwired (#999): the bridge sets the callbacks,
    but a bare link still runs — frames land in the controller,
    outcomes and refusals tell to nobody — and a refusal without
    a reason ends it on the default words."""

    async def scenario() -> None:
        socket = FakeWS([request_raw("req-1"), rejected_raw(None)])
        link = decider_mod.DeciderLink("ws-a", ws_factory=lambda: socket)
        assert link.on_frame is None
        assert link.on_rejected is None
        task = drive(link)
        await wait_done(task)
        assert link.controller.pending.keys() == {"req-1"}
        assert link.reject_reason == "the daemon refused the registration"

    asyncio.run(scenario())


def test_the_registration_frame_and_the_default_dial() -> None:
    """The wire's own vocabulary (#999): the registration frame
    is the control frame every decider sends — the TUI's link
    and the standalone watch loop's own — and the link's
    default dial is the environment's own daemon, the feed's
    seam."""
    assert registration("ws-a") == (
        '{"type": "egress.decider", "workspace": "ws-a"}'
    )
    assert (
        decider_mod.DeciderLink("ws-a").ws_factory
        is egress_mod.env_dial_events
    )
    # The refusal outcome the link ends on is the controller's
    # own vocabulary, the same constant both clients read.
    assert REJECTED == "rejected"


def test_a_controller_handed_in_is_the_one_the_link_drives() -> None:
    """The state seam (#1000): the consent presenter owns a
    controller and the page hands it to the registration, so
    the link drives the controller it was given — the frames
    landing on the state the presenter's rows build from, one
    source of truth — while a link built without one keeps its
    own, the registration's whole state its own."""

    async def scenario() -> None:
        given = ConsentController(workspace_id="ws-a")
        socket = FakeWS([rules_raw(), request_raw("req-1")])
        # After the scripted socket the dial fails forever: a
        # serving tail would re-register and reset the
        # controller again, and the state under assert would
        # not be the exchange's own.
        sockets = [socket]

        def dial():
            return sockets.pop(0) if sockets else BrokenDial()

        link = decider_mod.DeciderLink("ws-a", given, ws_factory=dial)
        assert link.controller is given
        task = drive(link)
        await settle()
        task.cancel()
        assert given.pending.keys() == {"req-1"}
        assert given.rules is not None
        assert given.rules.mode == "interactive"
        # Without one handed in, the link builds its own: the
        # registration's own state, not a caller's.
        own = decider_mod.DeciderLink("ws-a")
        assert own.controller is not given
        assert own.controller.workspace_id == "ws-a"

    asyncio.run(scenario())


def test_the_registration_tells_its_presence() -> None:
    """The presence seams (#1052): the registration's answer —
    the first owned frame of a connection — tells the serve
    once, junk and foreign frames telling nothing, and the
    connection's ends each tell the drop with their own reason:
    the clean close, the failed dial, the page's retire."""

    async def scenario() -> None:
        first = FakeWS(
            [
                rules_raw(),
                request_raw("req-1"),
                "not json at all",
                request_raw("req-9", ws="ws-b"),
            ]
        )
        # After the scripted socket the dial fails forever: the
        # outage's own heartbeat, one drop per attempt, until
        # the cancel takes the link.
        sockets = [first]

        def dial():
            return sockets.pop(0) if sockets else BrokenDial()

        link = decider_mod.DeciderLink("ws-a", ws_factory=dial)
        seen = Recorder()
        seen.wire(link)
        task = drive(link)
        await settle()
        task.cancel()
        await settle(0.01)  # the cancel's delivery: the retire's drop
        # One serve for the whole first connection: the first
        # owned frame told it, and the owned frame behind it,
        # the junk, and the foreign hold did not re-tell.
        assert seen.serves == 1
        assert seen.drops[0] == "the daemon closed the events socket"
        assert seen.drops[1] == "the dial failed: connection refused"
        assert seen.drops[-1] == "the page retired the registration"
        assert link.serving is False

    asyncio.run(scenario())


def test_a_reconnect_restores_the_presence() -> None:
    """The drop and its reconnect (#1052): a wire that breaks
    mid-stream tells its drop, and the reconnect's registration
    answer tells the serve again — the indicator reads
    connected, down, connected in step with the wire."""

    async def scenario() -> None:
        first = FakeWS([rules_raw(), request_raw("req-1")])
        first.script.append(RuntimeError("wire cut"))
        second = FakeWS([rules_raw(mode="static"), request_raw("req-1")])
        sockets = [first, second]

        def dial():
            return sockets.pop(0) if sockets else BrokenDial()

        link = decider_mod.DeciderLink("ws-a", ws_factory=dial)
        seen = Recorder()
        seen.wire(link)
        task = drive(link)
        await settle()
        task.cancel()
        assert seen.drops[0] == "the socket broke: wire cut"
        assert seen.serves == 2  # each connection's own answer
        assert link.serving is False  # the cancel's drop told

    asyncio.run(scenario())


def test_the_token_refusals_close_tells_its_own_drop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The close's two arms (#1052): the token refusal's close
    (4401) tells the token's own reason, and a plain close
    (1000) tells the socket's break — the same reading the
    feed's own drops carry."""

    async def scenario() -> None:
        monkeypatch.setattr(decider_mod, "REFUSED_RETRY_S", 0.001)
        plain = refused_closed(1000)
        first = FakeWS([refused_closed(4401)])
        second = FakeWS([request_raw("req-1"), plain])
        sockets = [first, second]

        def dial():
            return sockets.pop(0) if sockets else BrokenDial()

        link = decider_mod.DeciderLink("ws-a", ws_factory=dial)
        seen = Recorder()
        seen.wire(link)
        task = drive(link, delays=(3600.0,))
        await settle()
        assert seen.drops == [
            "the daemon refused the token",
            f"the socket broke: {plain}",
        ]
        # The second connection's hold told its serve before
        # the close took the wire.
        assert seen.serves == 1
        task.cancel()

    asyncio.run(scenario())


def test_a_rejected_registration_tells_no_drop_and_no_serve() -> None:
    """The refusal's own tell (#1052): the rejected registration
    crosses its reason and ends the link — the rejection's own
    frame never tells a serve, and no drop paints an outage
    over the refusal."""

    async def scenario() -> None:
        socket = FakeWS(
            [rejected_raw("unknown workspace"), request_raw("req-1")]
        )
        link = decider_mod.DeciderLink("ws-nope", ws_factory=lambda: socket)
        seen = Recorder()
        seen.wire(link)
        task = drive(link)
        await wait_done(task)
        assert seen.serves == 0
        assert seen.drops == []
        assert seen.refusals == ["unknown workspace"]

    asyncio.run(scenario())


def test_the_retire_tells_its_drop() -> None:
    """The page's retire (#1052): the cancel ends the link with
    a drop told — the indicator reads down while the page is
    away — and the task still ends cancelled, the bridge's own
    contract."""

    async def scenario() -> None:
        socket = FakeWS([rules_raw()])
        sockets = [socket]

        def dial():
            return sockets.pop(0) if sockets else BrokenDial()

        link = decider_mod.DeciderLink("ws-a", ws_factory=dial)
        seen = Recorder()
        seen.wire(link)
        # The ladder parks after the clean close: the cancel is
        # the next end the link meets.
        task = drive(link, delays=(3600.0,))
        await settle()
        assert seen.serves == 1
        task.cancel()
        await settle(0.01)
        assert task.cancelled()
        assert seen.drops == [
            "the daemon closed the events socket",
            "the page retired the registration",
        ]

    asyncio.run(scenario())
