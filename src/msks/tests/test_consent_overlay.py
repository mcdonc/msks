"""The consent overlay (#358): Pilot-driven, fake seams.

The overlay rides a workspace page inside the tree app: the data
seam (decide, revoke, the mode switch) is scripted and recorded,
the page's decider link rides the FakeWS/FakeFactory connections,
and the page's own harness lives in test_main_tui.py. The pure
helpers (rows, focus, labels) get direct unit tests in
test_consent_tui_helpers.py.
"""

import asyncio
import json
import time

import websockets
from msks.client.tui import consent as consent_mod
from msks.client.tui import consent_ui
from msks.client.tui.consent_ui import OneFlight, RulesScreen
from msks.client.tui.link import DeciderLink
from msks.client.tui.main_app import (
    ConsentOverlay,
    MsksTuiApp,
    TuiFollow,
    WorkspaceScreen,
)
from test_consent_tui import (
    request_frame as shared_request_frame,
)
from test_consent_tui import (
    rules_frame as shared_rules_frame,
)
from textual.widgets import Static

WS = "ws-dev"


def request_frame(rid, host="api.example", port=443):
    """The shared fixture, scoped to this suite's workspace — the
    controller filters foreign frames (#280 review)."""
    return shared_request_frame(rid, host, port, workspace=WS)


def rules_frame():
    """The shared fixture, scoped to this suite's workspace."""
    return shared_rules_frame(workspace=WS)


def resolved_frame(rid, decision="allowed") -> str:
    """One hold's resolution as the daemon sends it."""
    return json.dumps(
        {
            "event": "egress.resolved",
            "data": {"request_id": rid, "decision": decision},
        }
    )


def empty_rules_frame(mode: str = "allow") -> str:
    """A rules frame with no verdicts and no allowlist — the
    default's mode is the posture that holds nothing allowed, so
    the picker's highlight sits one down from static."""
    return json.dumps(
        {
            "event": "egress.rules",
            "data": {
                "workspace_id": WS,
                "mode": mode,
                "allow_list": [],
                "allowed": [],
                "denied": [],
            },
        }
    )


def mode_frame(mode: str) -> str:
    """A mode switch's refreshed rules frame."""
    return empty_rules_frame(mode)


def same_rows_frame(mode: str) -> str:
    """The fixture snapshot's rows under another mode — the
    same-membership refresh a mode switch sends while verdicts
    stand."""
    return json.dumps(
        {
            "event": "egress.rules",
            "data": {
                "workspace_id": WS,
                "mode": mode,
                "allow_list": [".debian.org"],
                "allowed": [
                    {
                        "id": "a1",
                        "dest_host": "api.example",
                        "dest_port": 443,
                        "decision": "allowed",
                        "duration": "5m",
                        "decided_at": 200.0,
                        "decided_by": "token",
                    }
                ],
                "denied": [
                    {
                        "id": "d1",
                        "dest_host": "203.0.113.7",
                        "dest_port": 0,
                        "decision": "denied",
                        "duration": "forever",
                        "decided_at": 201.0,
                        "decided_by": "token",
                    }
                ],
            },
        }
    )


def secret_frame(kind: str, **data) -> str:
    """One interceptor audit frame as the daemon sends it (#201)."""
    payload = {"workspace_id": WS, "name": "api"}
    payload.update(data)
    return json.dumps({"event": f"secret.{kind}", "data": payload})


PAGE_ROW = {
    "id": WS,
    "name": "dev",
    "status": "running",
    "egress_mode": "interactive",
    "image_hash": "a" * 64,
    "created_at": "2026-01-02T03:04:05",
    "host": "host-1",
}


class FakeWS:
    """One scripted websocket connection that stays open like a
    real one: frames stream, then recv blocks until close() (or
    raises the scripted close code). An instant StopAsyncIteration
    would make the link reconnect and reset its state — the real
    socket holds the connection, so the fake must too."""

    def __init__(
        self, frames: list[str] | None = None, close_code: int | None = None
    ) -> None:
        self.frames = list(frames or [])
        self.close_code = close_code
        self.sent: list[str] = []
        self.closed = False
        self._wakeup: asyncio.Event = asyncio.Event()

    def push(self, frame: str) -> None:
        """Deliver a frame to a parked connection (the link's recv
        is parked once the initial frames ran out — appending to
        the list alone would never wake it)."""
        self.frames.append(frame)
        self._wakeup.set()

    async def send(self, text: str) -> None:
        self.sent.append(text)

    async def close(self) -> None:
        self.closed = True
        self._wakeup.set()

    def __aiter__(self):
        return self

    async def __anext__(self) -> str:
        while True:
            if self.frames:
                return self.frames.pop(0)
            if self.close_code is not None:
                raise websockets.ConnectionClosed(
                    websockets.frames.Close(self.close_code, "bye"),
                    None,
                )
            if self.closed:
                raise StopAsyncIteration
            self._wakeup.clear()
            await self._wakeup.wait()


class FakeFactory:
    """Yields scripted connections in order, one per call."""

    def __init__(self, connections: list[FakeWS]) -> None:
        self.connections = list(connections)
        self.made: list[FakeWS] = []

    def __call__(self):
        ws = self.connections.pop(0) if self.connections else FakeWS([])
        self.made.append(ws)
        return Entering(ws)


class Entering:
    """The async-context-manager half the link awaits."""

    def __init__(self, ws: FakeWS) -> None:
        self.ws = ws

    async def __aenter__(self) -> FakeWS:
        return self.ws

    async def __aexit__(self, *exc) -> None:
        return None


class OverlayData:
    """The page's daemon calls, scripted and recorded: the verdicts,
    the revokes, and the mode switches."""

    def __init__(self) -> None:
        self.decided: list[tuple] = []
        self.revoked: list[tuple] = []
        self.modes: list[tuple] = []
        self.fail: set[str] = set()
        self.refusal = "daemon away"

    async def workspaces(self) -> list[dict]:
        return [dict(PAGE_ROW)]

    async def set_egress_mode(
        self, workspace_id: str, mode: str, *, confirm_empty: bool = False
    ) -> dict:
        self.modes.append((workspace_id, mode, confirm_empty))
        if "mode" in self.fail:
            raise RuntimeError(self.refusal)
        return {
            "workspace_id": workspace_id,
            "mode": mode,
            "allow_list": [],
            "allowed": [],
            "denied": [],
            "applied": True,
        }

    async def decide(
        self,
        workspace_id: str,
        request_id: str,
        decision: str,
        duration: str,
    ) -> dict:
        self.decided.append((workspace_id, request_id, decision, duration))
        if "decide" in self.fail:
            raise RuntimeError(self.refusal)
        return {"request_id": request_id}

    async def revoke(self, workspace_id: str, request_id: str) -> dict:
        self.revoked.append((workspace_id, request_id))
        if "revoke" in self.fail:
            raise RuntimeError(self.refusal)
        return {"request_id": request_id}


def make_page(factory: FakeFactory, *, data: OverlayData | None = None):
    """The tree app with one workspace page whose decider link rides
    the scripted connections."""
    data = data or OverlayData()

    def link_factory() -> DeciderLink:
        return DeciderLink(
            WS, ws_factory=factory, reconnect_delays=(0.01, 0.01, 0.01)
        )

    page = WorkspaceScreen(dict(PAGE_ROW), link_factory=link_factory)
    app = MsksTuiApp(TuiFollow(), data=data)
    return app, page, data


def on_page(app) -> bool:
    return isinstance(app.screen, WorkspaceScreen)


def on_overlay(app) -> bool:
    return isinstance(app.screen, ConsentOverlay)


def overlay_in(app) -> ConsentOverlay | None:
    """The page's overlay wherever it sits in the stack (a pushed
    rules or events screen stands above it)."""
    for screen in app.screen_stack:
        if isinstance(screen, ConsentOverlay):
            return screen
    return None


def queue_children(app) -> int:
    """The queue's row count; -1 with no overlay (or inside a
    rebuild's swap window)."""
    overlay = overlay_in(app)
    if overlay is None:
        return -1
    try:
        return len(overlay.query_one("#consent-rows").children)
    except Exception:
        return -1


async def open_page(pilot, app, page) -> WorkspaceScreen:
    """Push the page and let its rows and link settle."""
    app.push_screen(page)
    await wait_for(lambda: on_page(app))
    await wait_for(lambda: page.actions_widget() is not None)
    return page


async def open_overlay(pilot, app, page, *, auto: bool = False):
    """Push the page, then the consent overlay over it by hand. A
    hold that already auto-opened one is reused — the panel is the
    panel, whichever path opened it. The panel's widgets settle
    before the caller reads them (compose streams asynchronously)."""
    await open_page(pilot, app, page)
    if page.overlay is None:
        page.push_overlay(auto=auto)
    await wait_for(
        lambda: page.overlay is not None and panel_settled(page.overlay)
    )
    return page.overlay


def panel_settled(overlay) -> bool:
    """Whether the panel's own widgets have mounted."""
    try:
        overlay.query_one("#consent-status")
        overlay.query_one("#consent-rows")
        return True
    except Exception:
        return False


def held_row(app, index: int) -> str:
    rows = overlay_in(app).query_one("#consent-rows")
    return str(rows.children[index].query_one(Static).content)


def rules_children(app) -> int:
    """The rules screen's row count; -1 inside a rebuild's swap
    window (or when the rules screen is not on top)."""
    try:
        return len(app.screen.query_one("#rule-rows").children)
    except Exception:
        return -1


def events_children(app) -> int:
    """The events screen's row count; -1 inside a rebuild's swap
    window (or when the events screen is not on top)."""
    try:
        return len(app.screen.query_one("#event-rows").children)
    except Exception:
        return -1


def status_line(app) -> str:
    return str(overlay_in(app).query_one("#consent-status").content)


def page_consent(app, page) -> str:
    """The page's consent line; empty while the page still mounts."""
    try:
        return str(page.query_one("#consent", Static).content)
    except Exception:
        return ""


def focused_request_id_or_none(app) -> str | None:
    """The queue's focused id, or None in a swap window."""
    from msks.client.tui.consent_ui import focused_request_id

    overlay = overlay_in(app)
    if overlay is None:
        return None
    try:
        return focused_request_id(overlay.query_one("#consent-rows"))
    except Exception:
        return None


def rule_rows_is(app, rows) -> bool:
    """Whether the rules list on top is still the very widget (a
    swap window reads as no list at all)."""
    try:
        return app.screen.query_one("#rule-rows") is rows
    except Exception:
        return False


def rules_focus(app) -> str | None:
    """The focused rule id, or None during a swap window."""
    try:
        from msks.client.tui.consent_ui import focused_rule_id

        return focused_rule_id(app.screen.query_one("#rule-rows"))
    except Exception:
        return None


async def wait_for(
    condition, timeout: float = 10.0, delay: float = 0.02
) -> None:
    """Poll a render condition until a wall-clock deadline (UI
    updates land on the message pump, not synchronously with the
    worker's frames). The budget is monotonic wall-clock time, not
    a try count: under full-suite load a multi-second event-loop
    stall stretches every iteration, and a fixed try count then
    gives up on a condition that lands moments later (#322)."""
    deadline = time.monotonic() + timeout
    while True:
        if condition():
            return
        if time.monotonic() >= deadline:
            # A loop parked past the deadline gets one pump cycle to
            # land the condition before the poll gives up: the frames
            # that arrived during the park are queued, not processed.
            await asyncio.sleep(delay)
            if condition():
                return
            raise AssertionError("condition never landed")
        await asyncio.sleep(delay)


async def press_until(pilot, key: str, landed, timeout: float = 10.0) -> None:
    """Press a key until its effect lands — an action pressed inside
    a rebuild's swap window no-ops (by design), so the tests retry.
    The budget is a wall-clock deadline for the same stall reason
    as wait_for (#322)."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        await pilot.press(key)
        try:
            if landed():
                return
        except Exception:
            pass
        await asyncio.sleep(0.05)
    raise AssertionError(f"{key!r} never took effect")


async def open_screen(pilot, app, key: str, name: str) -> None:
    """Press a key until its screen stands on top — the press can
    land inside a rebuild's swap window and no-op (by design), so
    the tests retry."""
    await press_until(pilot, key, lambda: type(app.screen).__name__ == name)


async def decide_the(controller, rid: str, decision: str = "allowed") -> None:
    """Land one hold's resolution in the controller (the daemon's
    frame the verdict or the timeout produces)."""
    controller.apply_frame(resolved_frame(rid, decision))


# -- the queue ------------------------------------------------------------


async def test_the_queue_lifecycle() -> None:
    factory = FakeFactory(
        [
            FakeWS(
                [
                    request_frame("r1", host="api.example", port=443),
                    request_frame("r2", host="raw.example", port=0),
                ]
            ),
            FakeWS([]),
        ]
    )
    app, page, data = make_page(factory)
    async with app.run_test() as pilot:
        overlay = await open_overlay(pilot, app, page)
        await wait_for(lambda: queue_children(app) == 2)
        await pilot.pause()
        assert "api.example:443" in held_row(app, 0)
        assert "raw.example (all ports)" in held_row(app, 1)
        assert WS in status_line(app)
        assert "connected" in status_line(app)
        # a allows the focused (first) hold with the default duration.
        await pilot.press("a")
        await wait_for(lambda: len(data.decided) == 1)
        assert data.decided == [(WS, "r1", "allow", "tilrestart")]
        # A resolved frame drops its row.
        await decide_the(page.link.controller, "r1")
        overlay.tick()
        await wait_for(lambda: queue_children(app) == 1)
        # d denies the survivor (retried: the press can land in the
        # rebuild's swap window right after the resolve).
        await press_until(pilot, "d", lambda: len(data.decided) == 2)
        assert data.decided[-1] == (WS, "r2", "deny", "tilrestart")
        # q parks the overlay; the page keeps running beneath it.
        await pilot.press("q")
        await wait_for(lambda: on_page(app))
        assert page.overlay is None


async def test_the_duration_picker() -> None:
    factory = FakeFactory([FakeWS([request_frame("r1")]), FakeWS([])])
    app, page, data = make_page(factory)
    async with app.run_test() as pilot:
        await open_overlay(pilot, app, page)
        await pilot.pause()
        await open_screen(pilot, app, "A", "DurationScreen")
        # Default highlight is tilrestart; two ups land on 5m.
        await pilot.press("up", "up", "enter")
        await wait_for(lambda: len(data.decided) == 1)
        assert data.decided == [(WS, "r1", "allow", "5m")]
        # D + Escape cancels: nothing more decided.
        factory.made[0].push(request_frame("r3"))
        await wait_for(lambda: queue_children(app) == 2)
        await press_until(
            pilot, "D", lambda: type(app.screen).__name__ == "DurationScreen"
        )
        await pilot.press("escape")
        await pilot.pause()
        assert len(data.decided) == 1


async def test_the_picker_decides_the_hold_it_opened_on() -> None:
    """The duration picker captures the focused hold's id when it
    opens: a row change (or a resolution) while the picker is open
    cannot retarget the verdict."""
    factory = FakeFactory([FakeWS([request_frame("r1")]), FakeWS([])])
    app, page, data = make_page(factory)
    async with app.run_test() as pilot:
        await open_overlay(pilot, app, page)
        await wait_for(lambda: queue_children(app) == 1)
        await open_screen(pilot, app, "A", "DurationScreen")
        # The hold resolves while the picker is open (the timeout
        # won the race); the picked duration still names r1.
        await decide_the(page.link.controller, "r1")
        await pilot.press("enter")  # tilrestart
        await wait_for(lambda: len(data.decided) == 1)
        assert data.decided == [(WS, "r1", "allow", "tilrestart")]


async def test_verdict_keys_without_a_focused_row() -> None:
    factory = FakeFactory([FakeWS([]), FakeWS([])])
    app, page, data = make_page(factory)
    async with app.run_test() as pilot:
        await open_overlay(pilot, app, page)
        await pilot.pause()
        await pilot.press("a")
        await pilot.press("d")
        await pilot.pause()
        assert data.decided == []  # nothing focused: no verdict
        assert "no hold focused" in status_line(app)


async def test_enter_decides_nothing() -> None:
    """Enter carries no verdict (#358): the queue is a ListView, and
    Enter fires its selection — a stray Enter aimed at the page's
    action list when the hold arrived must not decide anything.
    Only an explicit letter decides."""
    factory = FakeFactory([FakeWS([request_frame("r1")]), FakeWS([])])
    app, page, data = make_page(factory)
    async with app.run_test() as pilot:
        await open_overlay(pilot, app, page)
        await wait_for(lambda: queue_children(app) == 1)
        await pilot.press("enter")
        await pilot.press("enter")
        await pilot.pause()
        assert data.decided == []
        assert on_overlay(app)  # the tree never left the overlay


async def test_a_resolved_hold_above_focus_never_retargets() -> None:
    """The destructive-key retarget the third review proved: a hold
    resolving ABOVE the focused one shifts every index; with the
    fresh-list rebuild the focused id keeps the verdict on the row
    the operator sees lit."""
    factory = FakeFactory([FakeWS([request_frame("r1")]), FakeWS([])])
    app, page, data = make_page(factory)
    async with app.run_test() as pilot:
        overlay = await open_overlay(pilot, app, page)
        await wait_for(lambda: queue_children(app) == 1)
        factory.made[0].push(request_frame("r2"))
        factory.made[0].push(request_frame("r3"))
        await wait_for(lambda: queue_children(app) == 3)
        await press_until(
            pilot, "down", lambda: focused_request_id_or_none(app) == "r2"
        )
        # r1 (above) resolves: the rebuild must keep r2 focused.
        await decide_the(page.link.controller, "r1")
        overlay.tick()
        await wait_for(
            lambda: (
                queue_children(app) == 2
                and focused_request_id_or_none(app) == "r2"
            )
        )
        await press_until(pilot, "a", lambda: len(data.decided) == 1)
        assert data.decided == [(WS, "r2", "allow", "tilrestart")]


async def test_the_focused_hold_leaving_falls_to_a_live_target() -> None:
    """The focused hold resolving must not leave the keys inert or
    aimed at a phantom: the rebuild falls back to the top."""
    factory = FakeFactory([FakeWS([request_frame("r1")]), FakeWS([])])
    app, page, data = make_page(factory)
    async with app.run_test() as pilot:
        overlay = await open_overlay(pilot, app, page)
        await wait_for(lambda: queue_children(app) == 1)
        factory.made[0].push(request_frame("r2"))
        await wait_for(lambda: len(page.link.controller.pending) == 2)
        await decide_the(page.link.controller, "r1")
        overlay.tick()
        await wait_for(
            lambda: (
                queue_children(app) == 1
                and focused_request_id_or_none(app) == "r2"
            )
        )
        await press_until(pilot, "d", lambda: len(data.decided) == 1)
        assert data.decided[0][1] == "r2"


# -- the overlay's lifecycle (#358) ---------------------------------------


async def test_an_auto_opened_overlay_closes_when_the_queue_empties() -> None:
    factory = FakeFactory([FakeWS([request_frame("r1")]), FakeWS([])])
    app, page, _data = make_page(factory)
    async with app.run_test() as pilot:
        await open_page(pilot, app, page)
        # The burst's first hold opens the overlay by itself.
        await wait_for(lambda: queue_children(app) == 1)
        page.tick()
        await wait_for(lambda: on_overlay(app))
        assert page.overlay.auto is True
        # The hold resolves: the overlay closes itself.
        await decide_the(page.link.controller, "r1")
        page.overlay.tick()
        await wait_for(lambda: on_page(app))
        assert page.overlay is None


async def test_the_close_waits_while_rules_sits_above() -> None:
    """The auto-close holds off while the rules screen is stacked
    over the overlay: back returns to the overlay first, and the
    next tick takes the panel down."""
    factory = FakeFactory([FakeWS([request_frame("r1")]), FakeWS([])])
    app, page, _data = make_page(factory)
    async with app.run_test() as pilot:
        overlay = await open_overlay(pilot, app, page, auto=True)
        await wait_for(lambda: queue_children(app) == 1)
        await press_until(
            pilot, "r", lambda: type(app.screen).__name__ == "RulesScreen"
        )
        await decide_the(page.link.controller, "r1")
        overlay.tick()
        await pilot.pause()
        assert type(app.screen).__name__ == "RulesScreen"  # held off
        # Back returns to the overlay — or the panel's own timer
        # tick may already have taken it down once it surfaced
        # (both orders are the pinned behavior: the close waits for
        # the rules screen to leave, then lands). The budgets ride
        # high: under a full parallel suite a worker's event loop
        # stalls past the default window (#322).
        await press_until(
            pilot, "r", lambda: on_overlay(app) or on_page(app), timeout=30
        )
        overlay.tick()
        await wait_for(lambda: on_page(app), timeout=30)  # closes


async def test_a_manual_open_stays_until_closed() -> None:
    """Opened by hand the overlay is the consent panel: an empty
    queue keeps it standing (rules, events, and the mode switch all
    start here), and only the operator's key closes it."""
    factory = FakeFactory([FakeWS([rules_frame()]), FakeWS([])])
    app, page, _data = make_page(factory)
    async with app.run_test() as pilot:
        overlay = await open_overlay(pilot, app, page)
        await pilot.pause()
        assert "No held requests" in str(
            overlay.query_one("#consent-empty").content
        )
        overlay.tick()
        overlay.tick()
        await pilot.pause()
        assert on_overlay(app)  # no self-close on a manual open
        await pilot.press("escape")
        await wait_for(lambda: on_page(app))


async def test_a_parked_burst_never_re_pops_until_it_empties() -> None:
    """Closing the overlay on holds is the operator saying "not
    now" for the burst: later holds in the same burst add header
    counts, not panels — the next burst opens one again."""
    factory = FakeFactory([FakeWS([rules_frame()]), FakeWS([])])
    app, page, _data = make_page(factory)
    async with app.run_test() as pilot:
        await open_page(pilot, app, page)
        factory.made[0].push(request_frame("r1"))
        await wait_for(lambda: len(page.link.controller.pending) == 1)
        page.tick()
        await wait_for(lambda: on_overlay(app))
        await pilot.press("q")  # park on a non-empty queue
        await wait_for(lambda: on_page(app))
        factory.made[0].push(request_frame("r2"))
        await wait_for(lambda: len(page.link.controller.pending) == 2)
        page.tick()
        await pilot.pause()
        assert on_page(app)  # parked: no re-pop
        # The burst empties; the next burst opens again.
        await decide_the(page.link.controller, "r1")
        await decide_the(page.link.controller, "r2")
        page.tick()
        await pilot.pause()
        assert page.parked_ids is None
        factory.made[0].push(request_frame("r3"))
        await wait_for(lambda: len(page.link.controller.pending) == 1)
        page.tick()
        await wait_for(lambda: on_overlay(app))


async def test_a_delayed_manual_push_never_stacks_a_second_panel() -> None:
    """One panel stands at a time: the Enter that opens the consent
    action runs as a worker, and a page tick can auto-open the
    panel between the keypress and the worker body — the delayed
    push no-ops instead of stacking a second overlay."""
    factory = FakeFactory([FakeWS([request_frame("r1")]), FakeWS([])])
    app, page, _data = make_page(factory)
    async with app.run_test() as pilot:
        await open_page(pilot, app, page)
        await wait_for(lambda: len(page.link.controller.pending) == 1)
        page.tick()  # the auto-open wins the race
        await wait_for(lambda: on_overlay(app))
        page.push_overlay(auto=False)  # the delayed worker's push
        await pilot.pause()
        stacked = [
            s for s in app.screen_stack if isinstance(s, ConsentOverlay)
        ]
        assert len(stacked) == 1
        assert page.overlay is stacked[0]
        await pilot.press("q")
        await wait_for(lambda: on_page(app))  # one q reaches the page


async def test_a_park_survives_a_link_drop() -> None:
    """Parking during a drop holds: the count folds the connection
    state (0 while disconnected), but the park reads the
    controller's queue — the replay re-lands the same holds after
    the reconnection, and the panel the operator closed stays
    closed."""
    factory = FakeFactory([FakeWS([request_frame("r1")]), FakeWS([])])
    app, page, _data = make_page(factory)
    async with app.run_test() as pilot:
        await open_page(pilot, app, page)
        await wait_for(lambda: len(page.link.controller.pending) == 1)
        page.tick()
        await wait_for(lambda: on_overlay(app))
        page.link.state = "reconnecting"  # the drop: holds stand
        await pilot.press("q")
        assert page.parked_ids is not None  # recorded against the queue
        page.tick()
        # the drop's folded count clears nothing: the park reads ids
        page.link.state = "connected"  # the replay re-lands them
        page.link.replay_pending = True  # mid-window, truth in flight
        page.tick()
        assert page.parked_ids is not None
        page.link.replay_pending = False
        page.tick()
        await pilot.pause()
        assert on_page(app)  # no re-open for a parked burst


async def test_a_park_survives_the_reconnects_reset_window() -> None:
    """The registration's reset wipes the controller's snapshot
    before the replay re-lands it: a tick inside that window reads
    an empty queue, and the park must hold anyway — the replay
    re-lands the same holds, and the panel the operator closed
    stays closed. The park ends when none of its ids stand in a
    settled queue; a new burst then opens a panel again."""
    ws1 = FakeWS([request_frame("r1")])
    ws2 = FakeWS([])  # the replay arrives after the registration
    factory = FakeFactory([ws1, ws2])
    app, page, _data = make_page(factory)
    async with app.run_test() as pilot:
        await open_page(pilot, app, page)
        await wait_for(lambda: len(page.link.controller.pending) == 1)
        page.tick()
        await wait_for(lambda: on_overlay(app))
        await pilot.press("q")  # park on the burst
        # The link drops and reconnects: the registration resets the
        # controller, and the replay has not landed yet.
        await ws1.close()
        await wait_for(lambda: len(factory.made) == 2)
        await wait_for(lambda: page.link.replay_pending)
        page.tick()  # a tick inside the window: empty queue
        assert page.parked_ids is not None  # the park held
        # The replay re-lands the same hold: still parked.
        ws2.push(rules_frame())
        ws2.push(request_frame("r1"))
        await wait_for(lambda: not page.link.replay_pending)
        page.tick()
        await pilot.pause()
        assert on_page(app)  # no re-pop for the replayed hold
        assert page.parked_ids is not None
        # The hold resolves: the park ends, a new burst may open.
        await decide_the(page.link.controller, "r1")
        page.tick()
        await pilot.pause()
        assert page.parked_ids is None
        ws2.push(request_frame("r2"))
        await wait_for(lambda: len(page.link.controller.pending) == 1)
        page.tick()
        await wait_for(lambda: on_overlay(app))  # the new burst opens


async def test_the_auto_open_waits_for_a_stacked_modal() -> None:
    """A hold arriving while the mode picker is open stacks nothing
    under it: the open waits for the modal to leave, then lands."""
    factory = FakeFactory([FakeWS([rules_frame()]), FakeWS([])])
    app, page, _data = make_page(factory)
    async with app.run_test() as pilot:
        await open_page(pilot, app, page)
        page.open_mode_picker()
        await wait_for(lambda: type(app.screen).__name__ == "ModeScreen")
        factory.made[0].push(request_frame("r1"))
        await wait_for(lambda: len(page.link.controller.pending) == 1)
        page.tick()
        await pilot.pause()
        assert type(app.screen).__name__ == "ModeScreen"  # nothing stacked
        await pilot.press("escape")  # the picker leaves
        await wait_for(lambda: on_page(app))
        page.tick()
        await wait_for(lambda: on_overlay(app))  # now it opens


# -- verdict failures ------------------------------------------------------


async def test_decide_and_revoke_failures_flash() -> None:
    factory = FakeFactory([FakeWS([request_frame("r1")]), FakeWS([])])
    app, page, data = make_page(factory)
    data.fail.add("decide")
    async with app.run_test() as pilot:
        overlay = await open_overlay(pilot, app, page)
        await pilot.pause()
        await pilot.press("a")
        await wait_for(lambda: "decide failed" in status_line(app))
        # A failing revoke flashes the same way.
        data.fail.discard("decide")
        data.fail.add("revoke")
        await overlay.revoke_rule("r1")
        await wait_for(lambda: "revoke failed" in status_line(app))


async def test_a_truncated_closing_tag_failure_flashes_literally() -> None:
    """A failure message ending in a truncated closing tag — rich's
    escape leaves a bare ``[/`` alone, which still raises in the
    parser — flashes literally too: every bracket shape renders,
    none wedges the status line (#318)."""
    factory = FakeFactory([FakeWS([request_frame("r1")]), FakeWS([])])
    app, page, data = make_page(factory)
    data.fail.add("decide")
    data.refusal = "unknown workspace [/dev"
    async with app.run_test() as pilot:
        overlay = await open_overlay(pilot, app, page)
        await pilot.pause()
        await pilot.press("a")
        await wait_for(
            lambda: "decide failed: unknown workspace" in status_line(app)
        )
        assert "\\[/dev" in status_line(app)
        overlay.update_status()  # renders, no MarkupError
        await pilot.pause()


async def test_a_bracketed_failure_message_flashes_literally() -> None:
    """A failure message carrying rich markup brackets (a TLS
    handshake failure prints ``[SSL: ...]``) flashes literally —
    the seam escapes the exception text, so the status line's
    render never raises and the message the operator needs
    reaches the screen (#318)."""
    factory = FakeFactory([FakeWS([request_frame("r1")]), FakeWS([])])
    app, page, data = make_page(factory)
    data.fail.add("decide")
    data.refusal = "[SSL: CERTIFICATE_VERIFY_FAILED] nope[/]"
    async with app.run_test() as pilot:
        overlay = await open_overlay(pilot, app, page)
        await pilot.pause()
        await pilot.press("a")
        await wait_for(
            lambda: (
                "decide failed: [SSL: CERTIFICATE_VERIFY_FAILED]"
                in status_line(app)
            )
        )
        assert "nope\\[/]" in status_line(app)
        overlay.update_status()
        await pilot.pause()
        # A failing revoke with bracketed text flashes the same way.
        data.fail.discard("decide")
        data.fail.add("revoke")
        await overlay.revoke_rule("r1")
        await wait_for(lambda: "revoke failed: [SSL" in status_line(app))
        assert "nope\\[/]" in status_line(app)
        overlay.update_status()
        await pilot.pause()


# -- the rules screen ------------------------------------------------------


async def test_the_rules_screen_revokes() -> None:
    factory = FakeFactory([FakeWS([rules_frame()]), FakeWS([])])
    app, page, data = make_page(factory)
    async with app.run_test() as pilot:
        overlay = await open_overlay(pilot, app, page)
        await open_screen(pilot, app, "r", "RulesScreen")
        await wait_for(lambda: rules_children(app) == 2)
        header = str(app.screen.query_one("#allowlist").content)
        assert "mode interactive" in header
        assert ".debian.org" in header

        def rule_text(i: int) -> str:
            return str(
                app.screen.query_one("#rule-rows")
                .children[i]
                .query_one(Static)
                .content
            )

        await wait_for(lambda: "allowed" in rule_text(0))
        assert "forever" in rule_text(1)
        # x revokes the focused rule (allowed, first).
        await pilot.press("x")
        await wait_for(lambda: len(data.revoked) == 1)
        assert data.revoked == [(WS, "a1")]
        # r (or escape) returns to the overlay, hold still focused.
        await pilot.press("r")
        await wait_for(lambda: on_overlay(app))
        assert overlay.queue_rows() is not None


async def test_a_rules_screen_with_nothing_focused_revokes_nothing() -> None:
    """A rules screen with no rows (nothing to focus) revokes
    nothing on `x`; the screen also constructs host-free — the mode
    switch is a callback, not an app method."""
    empty = consent_mod.ConsentController()
    screen = RulesScreen(empty, lambda rid: None, lambda: None)
    assert screen is not None
    factory = FakeFactory([FakeWS([empty_rules_frame()]), FakeWS([])])
    app, page, data = make_page(factory)
    async with app.run_test() as pilot:
        await open_overlay(pilot, app, page)
        await open_screen(pilot, app, "r", "RulesScreen")
        await wait_for(lambda: rules_children(app) == 0)
        await pilot.press("x")
        await pilot.pause()
        assert data.revoked == []


async def test_the_rules_screen_repaints_in_place() -> None:
    """An unchanged row set takes an in-place countdown repaint, not
    the remove-and-mount swap — the once-a-second flash #301
    reports. The list keeps its identity (and with it the focus and
    indexes) across a tick and across a same-membership frame; a
    membership change still swaps in a fresh list."""
    factory = FakeFactory([FakeWS([rules_frame()]), FakeWS([])])
    app, page, _data = make_page(factory)
    async with app.run_test() as pilot:
        overlay = await open_overlay(pilot, app, page)
        await pilot.press("r")
        await wait_for(lambda: rules_children(app) == 2)
        rows = app.screen.query_one("#rule-rows")
        await press_until(pilot, "down", lambda: rules_focus(app) == "d1")
        # A tick on the same snapshot: in place, identity kept.
        overlay.tick()
        await wait_for(lambda: rule_rows_is(app, rows))
        assert rules_focus(app) == "d1"
        # The survivors' countdown text follows the clock: a1's 5m
        # verdict, decided_at 200, reads off the controller clock
        # the test now owns.
        clock = {"now": 300.0}
        page.link.controller._clock = lambda: clock["now"]

        def row_text(i: int) -> str:
            return str(
                app.screen.query_one("#rule-rows")
                .children[i]
                .query_one(Static)
                .content
            )

        overlay.tick()
        await wait_for(lambda: "3m left" in row_text(0))  # 500-300
        clock["now"] = 360.0
        overlay.tick()
        await wait_for(lambda: "2m left" in row_text(0))  # 500-360
        await wait_for(lambda: rule_rows_is(app, rows))  # still in place
        # A same-membership frame (a mode switch lands in it): the
        # header follows the mode, the rows never swap.
        page.link.controller.apply_frame(same_rows_frame("allow"))
        overlay.tick()
        await wait_for(
            lambda: (
                "mode allow" in str(app.screen.query_one("#allowlist").content)
            )
        )
        await wait_for(lambda: rule_rows_is(app, rows))
        assert rules_focus(app) == "d1"
        # A membership change (a1 revoked): the fresh-list swap.
        page.link.controller.apply_frame(empty_rules_frame("allow"))
        overlay.tick()
        # Pin the swap itself, not a row count: a count of 0 also
        # reads on the old list mid-swap under load (#322).
        await wait_for(
            lambda: (
                rules_children(app) == 0
                and app.screen.query_one("#rule-rows") is not rows
            )
        )


async def test_rules_rebuild_self_heals_without_an_old_list() -> None:
    """A rebuild that finds no list (one that died mid-swap) mounts
    the fresh one anew — the swap is also the heal."""
    factory = FakeFactory([FakeWS([rules_frame()]), FakeWS([])])
    app, page, _data = make_page(factory)
    async with app.run_test() as pilot:
        await open_overlay(pilot, app, page)
        await pilot.press("r")
        await wait_for(lambda: rules_children(app) == 2)
        await app.screen.query_one("#rule-rows").remove()
        app.screen.schedule_refresh()
        await wait_for(lambda: rules_children(app) == 2)


async def test_events_rebuild_self_heals_without_an_old_list() -> None:
    """The events screen's rebuild heals the same way: a missing
    list always rebuilds, unchanged log or not."""
    factory = FakeFactory(
        [FakeWS([rules_frame(), secret_frame("swap")]), FakeWS([])]
    )
    app, page, _data = make_page(factory)
    async with app.run_test() as pilot:
        await open_overlay(pilot, app, page)
        await pilot.press("e")
        await wait_for(lambda: events_children(app) == 1)
        await app.screen.query_one("#event-rows").remove()
        app.screen.schedule_refresh()
        await wait_for(lambda: events_children(app) == 1)


async def test_the_rules_screen_refreshes_on_frames() -> None:
    """The rules screen refreshes while it is on top of the
    overlay: a frame landing repaints the rows without a visit,
    and `m` reaches the host's picker from there."""
    factory = FakeFactory([FakeWS([rules_frame()]), FakeWS([])])
    app, page, _data = make_page(factory)
    async with app.run_test() as pilot:
        overlay = await open_overlay(pilot, app, page)
        await press_until(
            pilot, "r", lambda: type(app.screen).__name__ == "RulesScreen"
        )
        await wait_for(lambda: rules_children(app) == 2)
        page.link.controller.apply_frame(empty_rules_frame("allow"))
        overlay.tick()
        await wait_for(lambda: rules_children(app) == 0)
        await open_screen(pilot, app, "m", "ModeScreen")
        await pilot.press("escape")
        await wait_for(lambda: type(app.screen).__name__ == "RulesScreen")


# -- the events screen -----------------------------------------------------


async def test_the_events_screen() -> None:
    factory = FakeFactory(
        [
            FakeWS([rules_frame(), secret_frame("swap", host="a.example")]),
            FakeWS([]),
        ]
    )
    app, page, _data = make_page(factory)
    async with app.run_test() as pilot:
        overlay = await open_overlay(pilot, app, page)
        await wait_for(lambda: len(page.link.controller.events) == 1)
        await open_screen(pilot, app, "e", "EventsScreen")
        await wait_for(lambda: events_children(app) == 1)
        assert "!" not in str(
            app.screen.query_one("#event-rows")
            .children[0]
            .query_one(Static)
            .content
        )
        # A live sighting lands: the row carries the marker.
        factory.made[0].push(secret_frame("sighting", host="evil.example"))
        overlay.tick()
        await wait_for(lambda: events_children(app) == 2)
        assert str(
            app.screen.query_one("#event-rows")
            .children[0]
            .query_one(Static)
            .content
        ).startswith("! ")
        # e or r returns to the overlay.
        await pilot.press("r")
        await wait_for(lambda: on_overlay(app))


async def test_the_events_screen_states_when_empty() -> None:
    factory = FakeFactory([FakeWS([rules_frame()]), FakeWS([])])
    app, page, _data = make_page(factory)
    async with app.run_test() as pilot:
        await open_overlay(pilot, app, page)
        await open_screen(pilot, app, "e", "EventsScreen")
        await wait_for(
            lambda: (
                "No placeholder events"
                in str(app.screen.query_one("#events-empty").content)
            )
        )


# -- the sightings' flash (#201 over #358) ---------------------------------


async def test_a_sighting_flashes_the_page_line() -> None:
    """An off-allowlist sighting interrupts wherever the operator
    is: with no overlay up, the page's consent line carries the
    alarm for its TTL."""
    factory = FakeFactory([FakeWS([rules_frame()]), FakeWS([])])
    app, page, _data = make_page(factory)
    async with app.run_test() as pilot:
        await open_page(pilot, app, page)
        factory.made[0].push(secret_frame("sighting", host="evil.example"))
        await wait_for(lambda: page.link.sightings)
        page.tick()
        assert "! sighting" in page_consent(app, page)
        assert "evil.example" in page_consent(app, page)


async def test_a_sighting_flashes_the_overlay_while_it_is_up() -> None:
    factory = FakeFactory([FakeWS([rules_frame()]), FakeWS([])])
    app, page, _data = make_page(factory)
    async with app.run_test() as pilot:
        await open_overlay(pilot, app, page)
        factory.made[0].push(secret_frame("sighting", host="evil.example"))
        await wait_for(lambda: page.link.sightings)
        page.tick()
        assert "! sighting" in status_line(app)
        assert "evil.example" in status_line(app)


async def test_a_foreign_sighting_never_flashes() -> None:
    """Another workspace's sighting stays off this page (a foreign
    flash would be a false alarm — the controller filters)."""
    factory = FakeFactory([FakeWS([rules_frame()]), FakeWS([])])
    app, page, _data = make_page(factory)
    async with app.run_test() as pilot:
        await open_page(pilot, app, page)
        foreign = json.dumps(
            {
                "event": "secret.sighting",
                "data": {
                    "workspace_id": "ws-other",
                    "name": "api",
                    "host": "evil.example",
                    "seq": 2,
                },
            }
        )
        factory.made[0].push(foreign)
        await pilot.pause()
        await asyncio.sleep(0.05)
        page.tick()
        assert "! sighting" not in page_consent(app, page)


# -- the mode picker over the overlay --------------------------------------


async def test_the_mode_picker_opens_from_the_overlay() -> None:
    """`m` on the overlay opens the page's picker directly (#301
    over #358): the operator watching holds escalates or relaxes
    the posture without detouring through the rules screen; the
    pick goes through the data seam, and `m` again under the open
    picker stacks nothing."""
    factory = FakeFactory([FakeWS([rules_frame()]), FakeWS([])])
    app, page, data = make_page(factory)
    async with app.run_test() as pilot:
        await open_overlay(pilot, app, page)
        await wait_for(lambda: "mode interactive" in status_line(app))
        await open_screen(pilot, app, "m", "ModeScreen")
        await pilot.press("m")  # under the picker: inert
        await pilot.pause()
        assert (
            len(
                [
                    s
                    for s in app.screen_stack
                    if type(s).__name__ == "ModeScreen"
                ]
            )
            == 1
        )
        await pilot.press("up")  # interactive -> static
        await pilot.press("enter")
        await wait_for(lambda: len(data.modes) == 1)
        assert data.modes == [(WS, "static", False)]
        await wait_for(lambda: on_overlay(app))


async def test_the_status_line_names_the_link_state() -> None:
    """The empty line and the status line stay honest about the
    link: a drop names itself, a rejected registration names its
    reason — silence is never data."""
    factory = FakeFactory([FakeWS([rules_frame()]), FakeWS([])])
    app, page, _data = make_page(factory)
    async with app.run_test() as pilot:
        overlay = await open_overlay(pilot, app, page)
        await wait_for(lambda: "mode interactive" in status_line(app))
        page.link.state = "reconnecting"
        overlay.sync_empty([])
        assert "reconnecting" in str(
            overlay.query_one("#consent-empty").content
        )
        page.link.state = "rejected"
        page.link.reject_reason = "unknown workspace"
        overlay.update_status()
        assert "unknown workspace" in status_line(app)


async def test_the_status_line_shows_the_mode() -> None:
    """The overlay's status line names the current mode at all
    times (#301): `—` until the first rules frame lands, the
    snapshot's mode after, and a mode switch's reply repaints it."""
    factory = FakeFactory([FakeWS([]), FakeWS([])])
    app, page, _data = make_page(factory)
    async with app.run_test() as pilot:
        overlay = await open_overlay(pilot, app, page)
        await pilot.pause()
        overlay.update_status()
        assert "mode —" in status_line(app)
        page.link.controller.apply_frame(rules_frame())
        overlay.tick()
        await wait_for(lambda: "mode interactive" in status_line(app))
        page.link.controller.apply_frame(mode_frame("allow"))
        overlay.tick()
        await wait_for(lambda: "mode allow" in status_line(app))


async def test_m_stays_inert_under_a_modal() -> None:
    """`m` under the duration picker and the confirmation opens
    nothing: the keys route to the active screen alone — a picker
    stacked under a modal would be a screen nobody can see."""
    factory = FakeFactory(
        [FakeWS([request_frame("r1"), empty_rules_frame()]), FakeWS([])]
    )
    app, page, _data = make_page(factory)
    async with app.run_test() as pilot:
        await open_overlay(pilot, app, page)
        await wait_for(lambda: queue_children(app) == 1)
        await open_screen(pilot, app, "A", "DurationScreen")
        await pilot.press("m")
        await pilot.pause()
        assert not [
            s for s in app.screen_stack if type(s).__name__ == "ModeScreen"
        ]
        await pilot.press("escape")
        await wait_for(lambda: type(app.screen).__name__ != "DurationScreen")
        # The empty-static confirmation: m under it stays inert too.
        await open_screen(pilot, app, "m", "ModeScreen")
        await pilot.press("down")  # allow -> static
        await pilot.press("enter")
        await wait_for(lambda: type(app.screen).__name__ == "ConfirmScreen")
        await pilot.press("m")
        await pilot.pause()
        assert not [
            s for s in app.screen_stack if type(s).__name__ == "ModeScreen"
        ]
        await pilot.press("n")
        await wait_for(lambda: type(app.screen).__name__ != "ConfirmScreen")


async def test_mode_picker_confirms_an_empty_static_switch() -> None:
    """static with nothing effectively allowed asks first: a no
    decides nothing, a yes sends the confirmed switch
    (confirm_empty — the daemon's escape from its own refusal)."""
    factory = FakeFactory([FakeWS([empty_rules_frame()]), FakeWS([])])
    app, page, data = make_page(factory)
    async with app.run_test() as pilot:
        await open_overlay(pilot, app, page)
        await open_screen(pilot, app, "m", "ModeScreen")
        await pilot.press("down")  # allow -> static
        await pilot.press("enter")
        await pilot.press("n")  # the confirmation: declined
        await wait_for(
            lambda: not isinstance(app.screen, consent_ui.ConfirmScreen)
        )
        assert data.modes == []
        await open_screen(pilot, app, "m", "ModeScreen")
        await pilot.press("down")
        await pilot.press("enter")
        await pilot.press("y")  # the confirmation: taken
        await wait_for(lambda: len(data.modes) == 1)
        assert data.modes == [(WS, "static", True)]


async def test_mode_picker_without_a_snapshot_confirms_static() -> None:
    """`m` before any rules frame lands: the picker highlights the
    row's recorded mode (the snapshot's stand-in), and a static
    pick with no snapshot asks the offline question — nothing
    effectively allowed is the reading of no rules at all."""
    factory = FakeFactory([FakeWS([]), FakeWS([])])
    app, page, data = make_page(factory)
    async with app.run_test() as pilot:
        await open_overlay(pilot, app, page)
        await open_screen(pilot, app, "m", "ModeScreen")
        await pilot.press("up")  # the row's interactive -> static
        await pilot.press("enter")
        await pilot.press("y")  # the confirmation: taken
        await wait_for(lambda: len(data.modes) == 1)
        assert data.modes == [(WS, "static", True)]


async def test_the_events_screen_skips_rebuilds_when_unchanged() -> None:
    """An unchanged log takes no list rebuild — event rows are
    static (nothing ticks), so the per-tick cost is the fingerprint
    compare, and a reading operator's list keeps its identity."""
    factory = FakeFactory(
        [
            FakeWS([rules_frame(), secret_frame("swap", host="a.example")]),
            FakeWS([]),
        ]
    )
    app, page, _data = make_page(factory)
    async with app.run_test() as pilot:
        await open_overlay(pilot, app, page)
        await open_screen(pilot, app, "e", "EventsScreen")
        await wait_for(lambda: events_children(app) == 1)
        rows = app.screen.query_one("#event-rows")
        # An unchanged log takes the header-only path: the rebuild
        # runs (forced here — the tick's fingerprint gate keeps an
        # unchanged log from even scheduling) and the list keeps
        # its identity.
        app.screen.schedule_refresh()
        await wait_for(lambda: app.screen.query_one("#event-rows") is rows)
        assert events_children(app) == 1


async def test_the_picker_with_an_unknown_current() -> None:
    """A current mode the picker does not know (a row without a
    recorded mode and no snapshot yet) leaves the default
    highlight alone."""
    factory = FakeFactory([FakeWS([]), FakeWS([])])
    app, page, _data = make_page(factory)
    async with app.run_test() as pilot:
        await open_page(pilot, app, page)
        page.row = dict(PAGE_ROW, egress_mode=None)
        page.open_mode_picker()

        def highlight():
            try:
                return app.screen.query_one("#modes").highlighted
            except Exception:
                return "pending"  # the compose stream settles async

        await wait_for(lambda: highlight() != "pending")
        assert highlight() == 0


async def test_mode_picker_escape_cancels() -> None:
    """Escape closes the picker deciding nothing."""
    factory = FakeFactory([FakeWS([rules_frame()]), FakeWS([])])
    app, page, data = make_page(factory)
    async with app.run_test() as pilot:
        await open_overlay(pilot, app, page)
        await open_screen(pilot, app, "m", "ModeScreen")
        await pilot.press("escape")
        await wait_for(
            lambda: not isinstance(app.screen, consent_ui.ModeScreen)
        )
        assert data.modes == []


async def test_mode_switch_failure_flashes() -> None:
    """A failed switch (the daemon's named refusal among them)
    flashes on the page's consent line, never crashes the tree."""
    factory = FakeFactory([FakeWS([rules_frame()]), FakeWS([])])
    app, page, data = make_page(factory)
    data.fail.add("mode")
    async with app.run_test() as pilot:
        await open_overlay(pilot, app, page)
        await open_screen(pilot, app, "m", "ModeScreen")
        await pilot.press("up")  # interactive -> static
        await pilot.press("enter")
        await wait_for(lambda: len(data.modes) == 1)
        await wait_for(lambda: "mode switch failed" in page_consent(app, page))


async def test_modal_keys_do_not_reach_the_hidden_queue() -> None:
    """The verdict keys stay inert under a pushed screen (the
    screen-separation rule #358): `a` decides nothing while the
    picker is open over the rules screen over the overlay, and `q`
    closes the modal instead of the panel."""
    factory = FakeFactory([FakeWS([request_frame("r1")]), FakeWS([])])
    app, page, data = make_page(factory)
    async with app.run_test() as pilot:
        await open_overlay(pilot, app, page)
        await wait_for(lambda: queue_children(app) == 1)
        await open_screen(pilot, app, "r", "RulesScreen")
        await open_screen(pilot, app, "m", "ModeScreen")
        await pilot.press("a")
        await pilot.press("d")
        await pilot.press("e")  # the events screen stays stacked nowhere
        await asyncio.sleep(0.05)
        assert data.decided == []  # the hidden hold stays undecided
        await pilot.press("q")  # the modal's own binding
        await wait_for(lambda: type(app.screen).__name__ == "RulesScreen")
        assert app.is_running  # q closed the modal, not the tree
        assert not [
            s for s in app.screen_stack if type(s).__name__ == "EventsScreen"
        ]


# -- the swap windows and the rebuild flights ------------------------------


async def test_the_countdown_repaint_skips_a_row_that_left() -> None:
    """A row that left between the membership read and the repaint
    pass is skipped, not crashed on (the next tick rebuilds); two
    survivors both take their countdown's repaint."""
    factory = FakeFactory(
        [FakeWS([request_frame("r1"), request_frame("r2")]), FakeWS([])]
    )
    app, page, _data = make_page(factory)
    async with app.run_test() as pilot:
        overlay = await open_overlay(pilot, app, page)
        await wait_for(lambda: queue_children(app) == 2)
        rows = overlay.query_one("#consent-rows")
        ordered = page.link.controller.ordered()

        class Gone:
            """A hold whose row left between the membership read and
            the pass — the skip the guard exists for."""

            id = "gone"

        overlay.repaint_countdowns(rows, ordered)  # both survivors
        overlay.repaint_countdowns(rows, [*ordered, Gone()])
        await pilot.pause()
        assert queue_children(app) == 2


async def test_a_tick_survives_widgets_that_left_under_it() -> None:
    """A tick whose widgets left under it (a teardown race, a
    mid-swap death) is noise, not a crash: the tick swallows the
    missing query and the next one repaints what stands."""
    factory = FakeFactory([FakeWS([rules_frame()]), FakeWS([])])
    app, page, _data = make_page(factory)
    async with app.run_test() as pilot:
        overlay = await open_overlay(pilot, app, page)
        await wait_for(lambda: overlay.queue_rows() is not None)
        await overlay.query_one("#consent-empty").remove()
        overlay.tick()  # the empty line's query raises inside: swallowed
        await wait_for(lambda: overlay.queue_rows() is not None)


async def test_queue_rows_returns_none_in_a_swap_window() -> None:
    factory = FakeFactory([FakeWS([request_frame("r1")]), FakeWS([])])
    app, page, _data = make_page(factory)
    async with app.run_test() as pilot:
        overlay = await open_overlay(pilot, app, page)
        await wait_for(lambda: overlay.queue_rows() is not None)
        rows = overlay.queue_rows()
        await rows.remove()
        assert overlay.queue_rows() is None
        overlay.tick()  # the missing list self-heals
        await wait_for(lambda: overlay.queue_rows() is not None)


async def test_action_revoke_in_a_swap_window() -> None:
    """`x` pressed inside a rebuild's swap window reads as nothing
    focused and revokes nothing."""
    factory = FakeFactory([FakeWS([rules_frame()]), FakeWS([])])
    app, page, data = make_page(factory)
    async with app.run_test() as pilot:
        await open_overlay(pilot, app, page)
        await open_screen(pilot, app, "r", "RulesScreen")
        await wait_for(lambda: rules_children(app) == 2)
        rows = app.screen.query_one("#rule-rows")
        await rows.remove()
        await app.screen.action_revoke()
        assert data.revoked == []


async def test_pick_duration_and_decide_in_a_swap_window() -> None:
    """`A` pressed inside a rebuild's swap window reads as nothing
    focused: the picker stays closed, nothing flashes."""
    factory = FakeFactory([FakeWS([request_frame("r1")]), FakeWS([])])
    app, page, _data = make_page(factory)
    async with app.run_test() as pilot:
        overlay = await open_overlay(pilot, app, page)
        await wait_for(lambda: queue_children(app) == 1)
        rows = overlay.queue_rows()
        await rows.remove()
        await overlay.pick_duration("allow")
        await pilot.pause()
        assert type(app.screen).__name__ == "ConsentOverlay"
        assert "no hold focused" in status_line(app)


async def test_a_mid_swap_death_self_heals_on_the_next_tick() -> None:
    """A rebuild that died mid-swap leaves no list; the tick sees
    the membership differ from nothing and rebuilds — the queue
    self-heals instead of wedging blank."""
    factory = FakeFactory(
        [FakeWS([request_frame("r1"), request_frame("r2")]), FakeWS([])]
    )
    app, page, _data = make_page(factory)
    async with app.run_test() as pilot:
        overlay = await open_overlay(pilot, app, page)
        await wait_for(lambda: queue_children(app) == 2)
        rows = overlay.queue_rows()
        await rows.remove()
        overlay.tick()
        await wait_for(lambda: queue_children(app) == 2)


async def test_rebuild_re_arms_when_frames_land_mid_flight() -> None:
    """A frame landing while the rebuild is in flight re-arms: the
    fresh state applies the moment the flight lands."""
    factory = FakeFactory([FakeWS([request_frame("r1")]), FakeWS([])])
    app, page, _data = make_page(factory)
    async with app.run_test() as pilot:
        overlay = await open_overlay(pilot, app, page)
        await wait_for(lambda: queue_children(app) == 1)
        landing = asyncio.Event()

        real_rebuild = overlay.rebuilds._rebuild  # zero-arg: reads at call

        async def gated_rebuild() -> None:
            await real_rebuild()
            landing.set()

        overlay.rebuilds._rebuild = gated_rebuild
        factory.made[0].push(request_frame("r2"))
        await wait_for(lambda: queue_children(app) == 2)
        await wait_for(landing.is_set)


async def test_a_dying_rebuild_logs_and_re_arms() -> None:
    """A rebuild that dies mid-flight logs (on a live owner) and
    carries a request that armed while it was dying."""
    factory = FakeFactory([FakeWS([request_frame("r1")]), FakeWS([])])
    app, page, _data = make_page(factory)
    async with app.run_test() as pilot:
        overlay = await open_overlay(pilot, app, page)
        await wait_for(lambda: queue_children(app) == 1)
        armed_while_dying = {"late": False}

        async def dying() -> None:
            raise RuntimeError("boom")

        overlay.rebuilds._rebuild = dying

        original_request = overlay.rebuilds.request

        def spying_request() -> None:
            if overlay.rebuilds.scheduled:
                armed_while_dying["late"] = True
            original_request()

        overlay.rebuilds.request = spying_request  # type: ignore[method-assign]
        overlay.rebuilds.request()
        await wait_for(lambda: not overlay.rebuilds.scheduled)
        assert armed_while_dying["late"]


# -- OneFlight (the shared single-flight rebuild) --------------------------


async def test_a_dying_flight_carries_a_late_request() -> None:
    """A flight that dies mid-await carries a request that armed
    while it was dying: the re-armed flight runs the newer state
    (the events screen has no per-tick re-request to recover the
    loss on its own)."""
    gate = asyncio.Event()
    runs: list[int] = []

    async def rebuild() -> None:
        runs.append(1)
        await gate.wait()
        if len(runs) == 1:
            raise RuntimeError("boom")

    flight = OneFlight(rebuild, lambda: True, "test")
    flight.request()
    await asyncio.sleep(0)  # the flight parks inside the gate
    flight.request()  # arms while the flight is dying
    gate.set()
    await wait_for(lambda: len(runs) == 2)
    await wait_for(lambda: not flight.scheduled)


async def test_schedule_rebuild_on_a_stopped_app_is_a_noop() -> None:
    class StoppedApp:
        is_running = False

    class Host:
        app = StoppedApp()

    flight = OneFlight(lambda: None, lambda: Host().app.is_running, "test")
    flight.request()
    await asyncio.sleep(0)
    assert not flight.scheduled  # the flight never armed


async def test_a_flight_dying_at_teardown_stays_quiet(
    monkeypatch,
) -> None:
    """A flight that dies after its owner stopped (teardown unmounted
    the tree under a mid-swap rebuild) stays quiet: no log, no
    carried re-arm — not a bug worth a traceback after exit."""
    logged: list[str] = []

    class RecordingLog:
        def exception(self, *args):
            logged.append(args[0])

    monkeypatch.setattr(consent_ui, "logger", RecordingLog())
    alive = {"go": True}
    gate = asyncio.Event()
    runs: list[int] = []

    async def dying() -> None:
        runs.append(1)
        await gate.wait()
        raise RuntimeError("teardown race")

    flight = OneFlight(dying, lambda: alive["go"], "test")
    flight.request()
    await asyncio.sleep(0)  # the flight parks inside the gate
    alive["go"] = False  # the owner stops mid-flight
    gate.set()
    await wait_for(lambda: not flight.scheduled)
    assert not logged  # the dead owner logs nothing
    assert runs == [1]  # no re-arm carried the death forward


def test_backoff_and_refused_close() -> None:
    delays = (1.0, 2.0, 5.0)
    assert consent_ui.backoff(delays, 0) == 1.0
    assert consent_ui.backoff(delays, 1) == 1.0
    assert consent_ui.backoff(delays, 2) == 2.0
    assert consent_ui.backoff(delays, 99) == 5.0
    assert consent_ui.backoff((), 3) == 0.0
    closed = websockets.ConnectionClosed(
        websockets.frames.Close(4401, "bad token"), None
    )
    assert consent_ui.refused_close(closed)
    clean = websockets.ConnectionClosed(
        websockets.frames.Close(1000, "bye"), None
    )
    assert not consent_ui.refused_close(clean)


async def test_close_ws_swallows_a_dead_peer() -> None:
    class DeadClose:
        async def close(self):
            raise OSError("already gone")

    await consent_ui.close_ws(DeadClose())  # no raise


def test_the_default_ws_factory_dials_the_events_url(
    monkeypatch,
) -> None:
    dialed: list[dict] = []

    class FakeConnect:
        def __init__(self, **kwargs):
            dialed.append(kwargs)

    monkeypatch.setattr(consent_ui.websockets, "connect", FakeConnect)
    monkeypatch.setenv("MSKSC_URL", "https://d")
    monkeypatch.setenv("MSKSC_TOKEN", "t")
    consent_ui.default_ws_factory()
    assert dialed[0]["uri"].startswith("wss://d")


def test_ws_connect_kwargs_and_shared_ssl(monkeypatch) -> None:
    """The plain-ws URL takes no ssl argument; the https one rides
    the shared context."""
    import ssl

    ctx = ssl.create_default_context()
    monkeypatch.setattr(consent_ui, "_SHARED_SSL", [ctx])
    kwargs = consent_ui.ws_connect_kwargs("http://d", "t", ctx)
    assert kwargs["ssl"] is None
    kwargs = consent_ui.ws_connect_kwargs("https://d", "t", ctx)
    assert kwargs["ssl"] is ctx
    assert consent_ui.shared_ssl() is ctx
