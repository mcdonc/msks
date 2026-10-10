"""The egress consent page (#454): Pilot-driven, fake seams.

The page rides a workspace page inside the tree app: the data
seam (decide, revoke, the mode switch) is scripted and recorded,
the workspace page's decider link rides the FakeWS/FakeFactory
connections, and the page's own harness lives in
test_main_tui.py. The pure helpers (rows, focus, labels) get
direct unit tests in test_consent_tui_helpers.py.
"""

import asyncio
import json
import time
from types import SimpleNamespace

import websockets
from msks.client.tui import consent_ui
from msks.client.tui.consent_ui import (
    ConsentPage,
    DurationScreen,
    OneFlight,
    hold_flash,
)
from msks.client.tui.follow import TuiFollow
from msks.client.tui.link import DeciderLink
from msks.client.tui.main_app import MsksTuiApp
from msks.client.tui.workspace import WorkspaceScreen
from test_consent_tui import (
    request_frame as shared_request_frame,
)
from test_consent_tui import (
    rules_frame as shared_rules_frame,
)
from textual.css.query import NoMatches
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


class PageData:
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


def make_page(factory: FakeFactory, *, data: PageData | None = None):
    """The tree app with one workspace page whose decider link rides
    the scripted connections."""
    data = data or PageData()

    def link_factory() -> DeciderLink:
        return DeciderLink(
            WS, ws_factory=factory, reconnect_delays=(0.01, 0.01, 0.01)
        )

    page = WorkspaceScreen(dict(PAGE_ROW), link_factory=link_factory)
    app = MsksTuiApp(TuiFollow(), data=data)
    return app, page, data


def on_page(app) -> bool:
    return isinstance(app.screen, WorkspaceScreen)


def on_consent(app) -> bool:
    return isinstance(app.screen, ConsentPage)


def consent_in(app) -> ConsentPage | None:
    """The open consent page wherever it sits in the stack (a pushed
    picker stands above it)."""
    for screen in app.screen_stack:
        if isinstance(screen, ConsentPage):
            return screen
    return None


def hold_children(app) -> int:
    """The holds zone's row count; -1 with no page open (or inside a
    rebuild's swap window)."""
    page = consent_in(app)
    if page is None:
        return -1
    try:
        return len(page.query_one("#hold-rows").children)
    except Exception:
        return -1


def rules_children(app) -> int:
    """The verdicts zone's row count; -1 inside a rebuild's swap
    window (or when the page is not open)."""
    page = consent_in(app)
    if page is None:
        return -1
    try:
        return len(page.query_one("#rule-rows").children)
    except Exception:
        return -1


async def open_page(pilot, app, page) -> WorkspaceScreen:
    """Push the page and let its rows and link settle."""
    app.push_screen(page)
    await wait_for(lambda: on_page(app))
    await wait_for(lambda: page.actions_widget() is not None)
    return page


async def open_consent(pilot, app, page) -> ConsentPage:
    """Push the page, then the egress consent page over it by hand.
    The page's widgets settle before the caller reads them (compose
    streams asynchronously)."""
    await open_page(pilot, app, page)
    page.open_consent_page()
    await wait_for(lambda: page_settled(consent_in(app)))
    return consent_in(app)


def page_settled(page) -> bool:
    """Whether the page's own widgets have mounted."""
    if page is None:
        return False
    try:
        page.query_one("#consent-status")
        page.query_one("#hold-rows")
        page.query_one("#rule-rows")
        return True
    except Exception:
        return False


def row_text(page, zone_id: str, index: int) -> str:
    """One row's text. Inside a rebuild's swap window the query raises:
    the zone's list is mounted, focused, and highlighted while its row's
    ``Static`` is still mounting — the shape #489 names. The read is
    polled, so that window is a retry, not a failure, and a row that
    never answers still reports the query error."""
    if page is None:
        raise AssertionError("no consent page open")
    row = page.query_one(f"#{zone_id}").children[index]
    return str(row.query_one(Static).content)


def held_row(app, index: int) -> str:
    """The held row's text — a single read that raises inside a rebuild's
    swap window; the polls that read it retry there (#489)."""
    return row_text(consent_in(app), "hold-rows", index)


def rule_row_text(app, index: int) -> str:
    """The verdict row's text — the same single read, verdicts zone
    (#489)."""
    return row_text(consent_in(app), "rule-rows", index)


def status_line(app) -> str:
    return str(consent_in(app).query_one("#consent-status").content)


def header_line(app) -> str:
    """The consent page's header name line; empty while the page
    still mounts."""
    try:
        return str(consent_in(app).query_one("#header").content)
    except Exception:
        return ""


def page_consent(app, page) -> str:
    """The workspace page's consent line; empty while the page still
    mounts."""
    try:
        return str(page.query_one("#consent", Static).content)
    except Exception:
        return ""


def focused_request_id_or_none(app) -> str | None:
    """The holds zone's focused id, or None in a swap window."""
    from msks.client.tui.consent_ui import focused_request_id

    page = consent_in(app)
    if page is None:
        return None
    try:
        return focused_request_id(page.query_one("#hold-rows"))
    except Exception:
        return None


def rules_focus(app) -> str | None:
    """The focused rule id, or None during a swap window."""
    try:
        from msks.client.tui.consent_ui import focused_rule_id

        return focused_rule_id(consent_in(app).query_one("#rule-rows"))
    except Exception:
        return None


def focused_zone(app) -> str | None:
    """Which zone owns the focus: "holds", "rules", or None."""
    page = consent_in(app)
    if page is None:
        return None
    if page.hold_rows() is not None and page.hold_rows().has_focus:
        return "holds"
    if page.rule_rows() is not None and page.rule_rows().has_focus:
        return "rules"
    return None


async def poll(
    condition, timeout: float = 10.0, delay: float = 0.02
) -> object:
    """Poll a condition until it lands a truthy value, and hand back that
    value. A read that raises is a read inside a rebuild's swap window,
    not a failure: it is retried until the deadline (#489). On timeout
    the report follows the reads — a poll that only ever raised
    re-raises the final read's error; once any read has answered, the
    failure is the content mismatch it is (a window opening at the
    deadline does not outrank the answers before it), so triage is not
    misdirected down the flake path this file retired."""
    deadline = time.monotonic() + timeout
    last_error = None
    answered = False
    while True:
        try:
            value = condition()
        except Exception as exc:
            value, last_error = None, exc
        else:
            last_error = None
            answered = True
        if value:
            return value
        parked = time.monotonic()
        await asyncio.sleep(delay)
        # The park ran past its requested span: the runner was
        # descheduled, or the pump worked the queue — span the
        # budget never had. Credit it back (#468).
        ran = time.monotonic() - parked
        if ran > delay:
            deadline += ran - delay
        if time.monotonic() >= deadline:
            # The cycle's pump work gets one read before the poll
            # gives up: the frames that landed during the park are
            # read here, not before it.
            try:
                value = condition()
            except Exception as exc:
                value, last_error = None, exc
            if value:
                return value
            if last_error is not None and not answered:
                raise last_error
            raise AssertionError("condition never landed")


async def wait_for(
    condition, timeout: float = 10.0, delay: float = 0.02
) -> None:
    """Poll a render condition until it lands, on poll's budget. A
    condition that raises is retried there, so a read inside a
    rebuild's swap window no longer ends the poll (#489)."""
    await poll(condition, timeout, delay)


async def press_until(pilot, key: str, landed, timeout: float = 10.0) -> None:
    """Press a key until its effect lands — an action pressed inside
    a rebuild's swap window no-ops (by design), so the tests retry.
    The budget is time the app actually ran, for the same starved-
    runner reason as wait_for (#322, #468): a press cycle that ran
    past its requested sleep credits the overshoot back to the
    deadline, so a descheduled worker cannot spend the budget. On
    timeout the report follows the reads, poll's rule: a check that
    only ever raised re-raises its error; once a check has answered,
    the failure is the key not taking effect."""
    deadline = time.monotonic() + timeout
    last_error = None
    answered = False
    while True:
        started = time.monotonic()
        await pilot.press(key)
        try:
            if landed():
                return
            last_error = None
            answered = True
        except Exception as exc:
            last_error = exc
        await asyncio.sleep(0.05)
        ran = time.monotonic() - started
        if ran > 0.05:
            deadline += ran - 0.05
        if time.monotonic() >= deadline:
            if last_error is not None and not answered:
                raise last_error
            raise AssertionError(f"{key!r} never took effect")


async def open_screen(pilot, app, key: str, name: str) -> None:
    """Press a key until its screen stands on top — the press can
    land inside a rebuild's swap window and no-op (by design), so
    the tests retry."""
    await press_until(pilot, key, lambda: type(app.screen).__name__ == name)


async def leave_page(pilot, app, timeout: float = 30.0) -> None:
    """Press the back key until the workspace page stands again —
    under a loaded runner a key press can be dropped or land
    unprocessed, and the tests retry as the operator would
    (#322)."""
    await press_until(pilot, "q", lambda: on_page(app), timeout=timeout)


async def decide_the(controller, rid: str, decision: str = "allowed") -> None:
    """Land one hold's resolution in the controller (the daemon's
    frame the verdict or the timeout produces)."""
    controller.apply_frame(resolved_frame(rid, decision))


# -- the page's shape (#454) ------------------------------------------------


async def test_the_page_carries_the_workspace_header() -> None:
    """#460: the consent page opens under the workspace page's own
    two-line header — the name with its status (and the
    pending-egress count while holds wait), the id, image, host,
    and created date muted beneath — so the identity stays visible
    while verdicts are made, and the count rides the header as
    holds land and resolve."""
    factory = FakeFactory(
        [FakeWS([request_frame("r1"), rules_frame()]), FakeWS([])]
    )
    app, page, _data = make_page(factory)
    async with app.run_test() as pilot:
        cp = await open_consent(pilot, app, page)
        await wait_for(lambda: hold_children(app) == 1)
        header = header_line(app)
        assert "dev" in header
        assert "running" in header
        # The count lands with the page's first repaint (the same
        # per-second tick the workspace page's header rides).
        await wait_for(lambda: "egress to decide: 1" in header_line(app))
        meta = str(cp.query_one("#header-meta").content)
        assert WS in meta
        assert "host-1" in meta
        assert "created 2026-01-02" in meta
        await decide_the(page.link.controller, "r1")
        cp.tick()
        await wait_for(lambda: "egress to decide" not in header_line(app))


async def test_the_page_shows_both_zones() -> None:
    """The consent page holds both lists: the held requests with
    countdowns above the in-effect verdicts, the mode line and
    static allowlist at the top."""
    factory = FakeFactory(
        [
            FakeWS([request_frame("r1"), rules_frame()]),
            FakeWS([]),
        ]
    )
    app, page, _data = make_page(factory)
    async with app.run_test() as pilot:
        cp = await open_consent(pilot, app, page)
        await wait_for(lambda: hold_children(app) == 1)
        await wait_for(lambda: rules_children(app) == 2)
        header = str(cp.query_one("#allowlist").content)
        assert header.startswith("allowlist:")
        assert ".debian.org" in header
        # The zone labels carry their counts (#470 C6) and the
        # verdict keys' hint stands under the queue's label
        # (#470 C2) — the counts follow within a tick of the
        # frames that land them.
        await wait_for(
            lambda: (
                "held requests (1)"
                in str(cp.query_one("#holds-label").content)
            )
        )
        await wait_for(
            lambda: (
                "in effect (2)" in str(cp.query_one("#rules-label").content)
            )
        )
        assert "a allow" in str(cp.query_one("#holds-hint").content)
        await wait_for(lambda: "api.example:443" in held_row(app, 0))
        await wait_for(lambda: "allowed" in rule_row_text(app, 0))


async def test_the_page_opens_with_the_holds_zone_focused() -> None:
    """A hold waiting when the page opens puts the focus where the
    decision is; an empty queue starts in the verdicts."""
    factory = FakeFactory(
        [FakeWS([request_frame("r1"), rules_frame()]), FakeWS([])]
    )
    app, page, _data = make_page(factory)
    async with app.run_test() as pilot:
        await open_consent(pilot, app, page)
        await wait_for(lambda: hold_children(app) == 1)
        await wait_for(lambda: focused_zone(app) == "holds")
    factory = FakeFactory([FakeWS([rules_frame()]), FakeWS([])])
    app, page, _data = make_page(factory)
    async with app.run_test() as pilot:
        await open_consent(pilot, app, page)
        await wait_for(lambda: rules_children(app) == 2)
        await wait_for(lambda: focused_zone(app) == "rules")


async def test_the_arrows_cross_between_the_zones() -> None:
    """The arrows alone reach both zones in reading order (#454):
    down off the holds list's bottom enters the verdicts at their
    first row; up off the verdicts' top returns to the holds at
    the row nearest the boundary crossed."""
    factory = FakeFactory(
        [
            FakeWS(
                [
                    request_frame("r1"),
                    request_frame("r2"),
                    rules_frame(),
                ]
            ),
            FakeWS([]),
        ]
    )
    app, page, _data = make_page(factory)
    async with app.run_test() as pilot:
        await open_consent(pilot, app, page)
        await wait_for(lambda: hold_children(app) == 2)
        await wait_for(lambda: rules_children(app) == 2)
        await wait_for(lambda: focused_zone(app) == "holds")
        await press_until(
            pilot, "down", lambda: focused_request_id_or_none(app) == "r2"
        )
        # The interior walk: up from r2 returns to r1 without
        # leaving the zone (the edge is the bottom alone).
        await press_until(
            pilot, "up", lambda: focused_request_id_or_none(app) == "r1"
        )
        await press_until(
            pilot, "down", lambda: focused_request_id_or_none(app) == "r2"
        )
        await press_until(pilot, "down", lambda: focused_zone(app) == "rules")
        assert rules_focus(app) == "a1"
        # Down through the verdicts' interior and off their bottom:
        # nothing stands below the page's last zone — the walk
        # stays on the last row.
        await press_until(pilot, "down", lambda: rules_focus(app) == "d1")
        # The no-handoff edges, driven directly (a keypress proving
        # a no-op cannot wait on a state that never changes, and a
        # press can land inert inside a rebuild's swap window):
        # down off the verdicts' bottom stays on the last row —
        # nothing stands below the page's last zone.
        cp = consent_in(app)
        cp.rule_rows().action_edge_next()
        await pilot.pause()
        assert rules_focus(app) == "d1"
        assert focused_zone(app) == "rules"
        # Up off the verdicts' top returns to the holds at their
        # last row — the wait takes the zone AND the row (a stale
        # highlight on the unfocused zone's list would satisfy a
        # row-only wait without the walk crossing at all).
        await press_until(
            pilot,
            "up",
            lambda: (
                focused_zone(app) == "holds"
                and focused_request_id_or_none(app) == "r2"
            ),
        )
        assert focused_zone(app) == "holds"
        # Up off the holds zone's top stays put: nothing stands
        # above it (the mode line and status line take no focus) —
        # the direct call, for the same swap-window reason.
        cp.hold_rows().action_edge_previous()  # r2 -> r1 (interior)
        await pilot.pause()
        assert focused_request_id_or_none(app) == "r1"
        cp.hold_rows().action_edge_previous()  # at the top: no handoff
        await pilot.pause()
        assert focused_request_id_or_none(app) == "r1"
        assert focused_zone(app) == "holds"
        # The direct entry lands on the last row as well (the
        # keypress crossing above already walked it).
        cp.enter_holds()
        await pilot.pause()
        assert focused_request_id_or_none(app) == "r2"
        assert focused_zone(app) == "holds"


async def test_up_from_empty_verdicts_enters_the_empty_holds() -> None:
    """Up off an empty verdicts list hands the walk to the holds
    zone even with nothing to highlight — the empty list keeps
    the focus, and the verdict keys stand inert there while no
    hold waits (#470 C3): the press decides nothing and flashes
    nothing."""
    factory = FakeFactory([FakeWS([empty_rules_frame()]), FakeWS([])])
    app, page, data = make_page(factory)
    async with app.run_test() as pilot:
        await open_consent(pilot, app, page)
        await wait_for(lambda: focused_zone(app) == "rules")
        await press_until(pilot, "up", lambda: focused_zone(app) == "holds")
        standing = status_line(app)
        await pilot.press("a")
        await pilot.pause()
        assert status_line(app) == standing  # inert: no flash
        assert data.decided == []


async def test_focused_rule_or_none_in_a_swap_window() -> None:
    """The swap-window reads inside the page: the revoke target
    with no list at all reads as nothing focused, and the zone
    focus helpers and the up-edge handoff no-op on a zone whose
    list is mid-swap — the walk waits for the heal."""
    factory = FakeFactory([FakeWS([rules_frame()]), FakeWS([])])
    app, page, _data = make_page(factory)
    async with app.run_test() as pilot:
        cp = await open_consent(pilot, app, page)
        await wait_for(lambda: rules_children(app) == 2)
        await cp.query_one("#rule-rows").remove()
        assert consent_ui.focused_rule_or_none(cp) is None
        cp.focus_rules()  # the verdicts mid-swap: the walk no-ops
        await cp.query_one("#hold-rows").remove()
        cp.focus_holds()  # the holds mid-swap
        cp.enter_holds()  # the up-edge handoff onto a missing list
        # No pause before the read: the page's once-a-second tick
        # timer can fire across any await, and its self-heal would
        # remount the list this assert means to find missing
        # (#468).
        assert cp.hold_rows() is None
        cp.tick()  # both zones self-heal
        await wait_for(lambda: rules_children(app) == 2)
        await wait_for(lambda: hold_children(app) == 0)


async def test_verdict_keys_are_inert_on_a_verdict_row() -> None:
    """``a``/``d``/``A``/``D`` act on the holds zone alone: a
    verdict row is state, not a question, and the letters decide
    nothing there — while ``x`` revokes the focused verdict."""
    factory = FakeFactory([FakeWS([rules_frame()]), FakeWS([])])
    app, page, data = make_page(factory)
    async with app.run_test() as pilot:
        await open_consent(pilot, app, page)
        await wait_for(lambda: rules_children(app) == 2)
        await wait_for(lambda: focused_zone(app) == "rules")
        await pilot.press("a")
        await pilot.press("d")
        await pilot.press("A")
        await pilot.press("D")
        await pilot.pause()
        assert data.decided == []
        await pilot.press("x")
        await wait_for(lambda: len(data.revoked) == 1)
        assert data.revoked == [(WS, "a1")]


async def test_x_is_inert_in_the_holds_zone() -> None:
    """``x`` acts on the verdicts alone: a hold carries nothing to
    revoke."""
    factory = FakeFactory([FakeWS([request_frame("r1")]), FakeWS([])])
    app, page, data = make_page(factory)
    async with app.run_test() as pilot:
        await open_consent(pilot, app, page)
        await wait_for(lambda: hold_children(app) == 1)
        await wait_for(lambda: focused_zone(app) == "holds")
        await pilot.press("x")
        await pilot.pause()
        assert data.revoked == []


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
        cp = await open_consent(pilot, app, page)
        await wait_for(lambda: hold_children(app) == 2)
        await pilot.pause()
        await wait_for(lambda: "api.example:443" in held_row(app, 0))
        await wait_for(lambda: "raw.example (all ports)" in held_row(app, 1))
        assert "dev" in str(cp.query_one("#header").content)
        assert "connected" in status_line(app)
        # a allows the focused (first) hold with the default duration.
        await pilot.press("a")
        await wait_for(lambda: len(data.decided) == 1)
        assert data.decided == [(WS, "r1", "allow", "tilrestart")]
        # A resolved frame drops its row.
        await decide_the(page.link.controller, "r1")
        cp.tick()
        await wait_for(lambda: hold_children(app) == 1)
        # d denies the survivor (retried: the press can land in the
        # rebuild's swap window right after the resolve).
        await press_until(pilot, "d", lambda: len(data.decided) == 2)
        assert data.decided[-1] == (WS, "r2", "deny", "tilrestart")
        # q returns to the workspace page; the page keeps running
        # beneath, and e reopens with the holds still waiting.
        await leave_page(pilot, app)
        assert consent_in(app) is None
        assert len(page.link.controller.pending) == 1
        await open_screen(pilot, app, "e", "ConsentPage")
        await wait_for(lambda: hold_children(app) == 1)


async def test_the_duration_picker() -> None:
    factory = FakeFactory([FakeWS([request_frame("r1")]), FakeWS([])])
    app, page, data = make_page(factory)
    async with app.run_test() as pilot:
        await open_consent(pilot, app, page)
        await pilot.pause()
        await open_screen(pilot, app, "A", "DurationScreen")
        # Default highlight is tilrestart; two ups land on 5m.
        await pilot.press("up", "up", "enter")
        await wait_for(lambda: len(data.decided) == 1)
        assert data.decided == [(WS, "r1", "allow", "5m")]
        # D + Escape cancels: nothing more decided.
        factory.made[0].push(request_frame("r3"))
        await wait_for(lambda: hold_children(app) == 2)
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
        await open_consent(pilot, app, page)
        await wait_for(lambda: hold_children(app) == 1)
        await open_screen(pilot, app, "A", "DurationScreen")
        # The hold resolves while the picker is open (the timeout
        # won the race); the picked duration still names r1.
        await decide_the(page.link.controller, "r1")
        await pilot.press("enter")  # tilrestart
        await wait_for(lambda: len(data.decided) == 1)
        assert data.decided == [(WS, "r1", "allow", "tilrestart")]


async def test_verdict_keys_without_a_focused_row() -> None:
    """The last hold resolving under the holds zone's focus leaves
    an empty focused list: the verdict keys stand inert while no
    hold waits (#470 C3) — they decide nothing and flash
    nothing."""
    factory = FakeFactory([FakeWS([request_frame("r1")]), FakeWS([])])
    app, page, data = make_page(factory)
    async with app.run_test() as pilot:
        cp = await open_consent(pilot, app, page)
        await wait_for(lambda: hold_children(app) == 1)
        await wait_for(lambda: focused_zone(app) == "holds")
        await decide_the(page.link.controller, "r1")
        cp.tick()
        await wait_for(lambda: hold_children(app) == 0)
        standing = status_line(app)
        await pilot.press("a")
        await pilot.press("A")
        await pilot.pause()
        assert status_line(app) == standing  # inert: no flash
        assert data.decided == []


async def test_a_swap_reclaims_focus_from_its_dying_holds_list() -> None:
    """The holds swap carries the verdicts zone's reclaim: a focus
    grab landing inside its remove window (started() processing
    mid-swap on a starved loop) would leave the letters dying on
    the removed list's closed pump — 'a' never took effect under
    the parallel suite (#468). The swap's tail gives the fresh
    list the focus its removal orphaned. The letters take the
    fresh list's word once a hold stands on it again (#470 C3:
    with the queue empty they stand inert, so the proof waits for
    a fresh hold to decide)."""
    factory = FakeFactory([FakeWS([request_frame("r1")]), FakeWS([])])
    app, page, data = make_page(factory)
    async with app.run_test() as pilot:
        cp = await open_consent(pilot, app, page)
        await wait_for(lambda: hold_children(app) == 1)
        await wait_for(lambda: focused_zone(app) == "holds")
        await pilot.pause()  # started() lands; it must not mask the race
        old = cp.query_one("#hold-rows")
        real_remove = old.remove

        async def remove_under_a_grab():
            removal = real_remove()  # the prune repairs synchronously here
            cp.set_focus(old)  # the grab: started() landing inside the window
            return await removal

        old.remove = remove_under_a_grab
        cp.set_focus(None)  # the swap's head reads held_focus False
        await decide_the(page.link.controller, "r1")
        cp.tick()
        # The verdicts zone's rule: wait for the end state itself
        # — the fresh list owns the id, and the focus the grab left
        # on the corpse has been reclaimed onto it.
        await wait_for(
            lambda: (
                cp.hold_rows() is not old
                and cp.focused is not old
                and cp.focused is cp.hold_rows()
            )
        )
        # The letters answer on the fresh list: a new hold lands
        # on it (#470 C3 keeps them inert while the queue stands
        # empty) and 'a' decides it through the reclaimed focus.
        factory.made[0].push(request_frame("r2"))
        await wait_for(lambda: hold_children(app) == 1)
        await press_until(
            pilot,
            "a",
            lambda: (WS, "r2", "allow", "tilrestart") in data.decided,
        )


async def test_enter_opens_the_picker_and_decides_nothing_alone() -> None:
    """#470 C4: Enter on a focused hold opens the duration picker
    — the key answers, the pick decides. Escape closes the picker
    with nothing decided, and a stray Enter aimed at the page
    beneath decides nothing on its own."""
    factory = FakeFactory([FakeWS([request_frame("r1")]), FakeWS([])])
    app, page, data = make_page(factory)
    async with app.run_test() as pilot:
        await open_consent(pilot, app, page)
        await wait_for(lambda: hold_children(app) == 1)
        await wait_for(lambda: focused_zone(app) == "holds")
        # Retried: a press that falls in a rebuild's swap window
        # reads as nothing focused — the loop presses again until
        # the picker stands.
        await press_until(
            pilot, "enter", lambda: isinstance(app.screen, DurationScreen)
        )
        await pilot.press("escape")
        await wait_for(lambda: on_consent(app))
        await pilot.pause()
        assert data.decided == []
        # Enter on the verdicts answers nothing (#470 C4): the
        # picker belongs to a focused hold.
        await press_until(pilot, "down", lambda: focused_zone(app) == "rules")
        await pilot.press("enter")
        await pilot.pause()
        assert on_consent(app)
        assert data.decided == []


async def test_a_resolved_hold_above_focus_never_retargets() -> None:
    """The destructive-key retarget the third review proved: a hold
    resolving ABOVE the focused one shifts every index; with the
    fresh-list rebuild the focused id keeps the verdict on the row
    the operator sees lit."""
    factory = FakeFactory([FakeWS([request_frame("r1")]), FakeWS([])])
    app, page, data = make_page(factory)
    async with app.run_test() as pilot:
        cp = await open_consent(pilot, app, page)
        await wait_for(lambda: hold_children(app) == 1)
        factory.made[0].push(request_frame("r2"))
        factory.made[0].push(request_frame("r3"))
        await wait_for(lambda: hold_children(app) == 3)
        await press_until(
            pilot, "down", lambda: focused_request_id_or_none(app) == "r2"
        )
        # r1 (above) resolves: the rebuild must keep r2 focused.
        await decide_the(page.link.controller, "r1")
        cp.tick()
        await wait_for(
            lambda: (
                hold_children(app) == 2
                and focused_zone(app) == "holds"
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
        cp = await open_consent(pilot, app, page)
        await wait_for(lambda: hold_children(app) == 1)
        factory.made[0].push(request_frame("r2"))
        await wait_for(lambda: len(page.link.controller.pending) == 2)
        await decide_the(page.link.controller, "r1")
        cp.tick()
        await wait_for(
            lambda: (
                hold_children(app) == 1
                and focused_zone(app) == "holds"
                and focused_request_id_or_none(app) == "r2"
            )
        )
        await press_until(pilot, "d", lambda: len(data.decided) == 1)
        assert data.decided[0][1] == "r2"


# -- holds surface passively (#454) ----------------------------------------


async def test_a_hold_arriving_pushes_nothing() -> None:
    """The page pushes nothing on a hold's arrival (#454): the
    header's count names what waits, the consent line flashes the
    destination with the key in, and the workspace page keeps the
    terminal."""
    factory = FakeFactory([FakeWS([rules_frame()]), FakeWS([])])
    app, page, _data = make_page(factory)
    async with app.run_test() as pilot:
        await open_page(pilot, app, page)
        factory.made[0].push(request_frame("r1"))
        await wait_for(lambda: len(page.link.controller.pending) == 1)
        page.tick()
        await pilot.pause()
        assert on_page(app)  # nothing was pushed
        assert consent_in(app) is None
        assert "egress to decide: api.example:443" in page_consent(app, page)
        assert "press e" in page_consent(app, page)
        assert page.pending_count() == 1


async def test_a_new_hold_under_the_open_page_adds_its_row_silently() -> None:
    """A hold arriving while the consent page stands is its row on
    the holds zone; the ids stay unseen and the first tick after
    the return names what arrived — the flash waits for a line
    the operator can see."""
    factory = FakeFactory([FakeWS([request_frame("r1")]), FakeWS([])])
    app, page, _data = make_page(factory)
    async with app.run_test() as pilot:
        await open_consent(pilot, app, page)
        await wait_for(lambda: hold_children(app) == 1)
        page.flash_line.until = 0.0  # any earlier flash has lapsed
        factory.made[0].push(request_frame("r2"))
        await wait_for(lambda: hold_children(app) == 2)
        page.tick()
        await pilot.pause()
        assert "press e" not in page_consent(app, page)
        await leave_page(pilot, app)
        page.tick()
        assert "press e" in page_consent(app, page)
        assert "r2" in page.seen_hold_ids  # named on the return's tick


async def test_a_hold_under_a_modal_waits_for_it_to_leave() -> None:
    """A hold landing while a picker owns the terminal waits: the
    ids stay unseen (a flash on the hidden consent line would be
    consumed unseen — the overlay-era burst guard's rule), and the
    first tick after the picker leaves names the destination."""
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
        assert "press e" not in page_consent(app, page)  # unseen, not spent
        await open_screen(pilot, app, "escape", "WorkspaceScreen")
        page.tick()
        assert "press e" in page_consent(app, page)


async def test_a_burst_flashes_its_count() -> None:
    """Two first-seen holds in one tick take one flash naming the
    count — a FlashLine owns the line for its TTL, and one
    destination a tick would overwrite the other."""
    factory = FakeFactory([FakeWS([rules_frame()]), FakeWS([])])
    app, page, _data = make_page(factory)
    async with app.run_test() as pilot:
        await open_page(pilot, app, page)
        factory.made[0].push(request_frame("r1"))
        factory.made[0].push(request_frame("r2"))
        await wait_for(lambda: len(page.link.controller.pending) == 2)
        page.tick()
        assert "2 egress holds to decide" in page_consent(app, page)


async def test_the_replayed_hold_never_reflashes() -> None:
    """A reconnect's replay re-lands the same holds without
    re-flashing them: the seen set keeps ids, and the holds that
    arrived while the link was down flash once, on the tick after
    the replay lands them."""
    ws1 = FakeWS([request_frame("r1")])
    ws2 = FakeWS([])  # the replay arrives after the registration
    factory = FakeFactory([ws1, ws2])
    app, page, _data = make_page(factory)
    async with app.run_test() as pilot:
        await open_page(pilot, app, page)
        await wait_for(lambda: len(page.link.controller.pending) == 1)
        page.tick()
        assert "press e" in page_consent(app, page)
        page.flash_line.until = 0.0  # the flash has lapsed
        # The link drops and reconnects: the registration resets the
        # controller, and the replay has not landed yet.
        await ws1.close()
        await wait_for(lambda: len(factory.made) == 2)
        await wait_for(lambda: page.link.replay_pending)
        page.tick()  # a tick inside the window: nothing to name
        ws2.push(rules_frame())
        ws2.push(request_frame("r1"))
        await wait_for(lambda: not page.link.replay_pending)
        page.tick()
        await pilot.pause()
        assert "press e" not in page_consent(app, page)  # seen already


# -- verdict failures ------------------------------------------------------


async def test_decide_and_revoke_failures_flash() -> None:
    factory = FakeFactory([FakeWS([request_frame("r1")]), FakeWS([])])
    app, page, data = make_page(factory)
    data.fail.add("decide")
    async with app.run_test() as pilot:
        cp = await open_consent(pilot, app, page)
        await pilot.pause()
        await pilot.press("a")
        await wait_for(lambda: "decide failed" in status_line(app))
        # A failing revoke flashes the same way.
        data.fail.discard("decide")
        data.fail.add("revoke")
        await cp.revoke_rule("r1")
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
        cp = await open_consent(pilot, app, page)
        await pilot.pause()
        await pilot.press("a")
        await wait_for(
            lambda: "decide failed: unknown workspace" in status_line(app)
        )
        assert "\\[/dev" in status_line(app)
        cp.update_status()  # renders, no MarkupError
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
        cp = await open_consent(pilot, app, page)
        await pilot.pause()
        await pilot.press("a")
        await wait_for(
            lambda: (
                "decide failed: [SSL: CERTIFICATE_VERIFY_FAILED]"
                in status_line(app)
            )
        )
        assert "nope\\[/]" in status_line(app)
        cp.update_status()
        await pilot.pause()
        # A failing revoke with bracketed text flashes the same way.
        data.fail.discard("decide")
        data.fail.add("revoke")
        await cp.revoke_rule("r1")
        await wait_for(lambda: "revoke failed: [SSL" in status_line(app))
        assert "nope\\[/]" in status_line(app)
        cp.update_status()
        await pilot.pause()


# -- the verdicts zone ------------------------------------------------------


async def test_the_verdicts_zone_revokes() -> None:
    factory = FakeFactory([FakeWS([rules_frame()]), FakeWS([])])
    app, page, data = make_page(factory)
    async with app.run_test() as pilot:
        cp = await open_consent(pilot, app, page)
        await wait_for(lambda: rules_children(app) == 2)
        header = str(cp.query_one("#allowlist").content)
        assert header.startswith("allowlist:")
        assert ".debian.org" in header
        await wait_for(lambda: "allowed" in rule_row_text(app, 0))
        await wait_for(lambda: "forever" in rule_row_text(app, 1))
        # x revokes the focused rule (allowed, first).
        await pilot.press("x")
        await wait_for(lambda: len(data.revoked) == 1)
        assert data.revoked == [(WS, "a1")]
        # The awaited action completes on its own too (the row
        # stands until the daemon's refreshed frame — the fake
        # seam sends none — so the focused rule is still a1).
        await cp.action_revoke()
        assert data.revoked == [(WS, "a1"), (WS, "a1")]
        # q (or escape) returns to the workspace page.
        await leave_page(pilot, app)


async def test_the_verdicts_with_nothing_focused_revoke_nothing() -> None:
    """A verdicts zone with no rows (nothing to focus) revokes
    nothing on `x` — the awaited action with a focused-but-empty
    zone reads as nothing focused, the same as the key press."""
    factory = FakeFactory([FakeWS([empty_rules_frame()]), FakeWS([])])
    app, page, data = make_page(factory)
    async with app.run_test() as pilot:
        cp = await open_consent(pilot, app, page)
        await wait_for(lambda: rules_children(app) == 0)
        await wait_for(lambda: focused_zone(app) == "rules")
        await pilot.press("x")
        await cp.action_revoke()  # focused zone, nothing highlighted
        await pilot.pause()
        assert data.revoked == []


async def test_the_verdicts_repaint_in_place() -> None:
    """An unchanged row set takes an in-place countdown repaint, not
    the remove-and-mount swap — the once-a-second flash #301
    reports. The list keeps its identity (and with it the focus and
    indexes) across a tick and across a same-membership frame; a
    membership change still swaps in a fresh list."""
    factory = FakeFactory([FakeWS([rules_frame()]), FakeWS([])])
    app, page, _data = make_page(factory)
    async with app.run_test() as pilot:
        cp = await open_consent(pilot, app, page)
        await wait_for(lambda: rules_children(app) == 2)
        rows = cp.query_one("#rule-rows")
        await press_until(pilot, "down", lambda: rules_focus(app) == "d1")
        # A tick on the same snapshot: in place, identity kept.
        cp.tick()
        await wait_for(lambda: cp.query_one("#rule-rows") is rows)
        assert rules_focus(app) == "d1"
        # The survivors' countdown text follows the clock: a1's 5m
        # verdict, decided_at 200, reads off the controller clock
        # the test now owns.
        clock = {"now": 300.0}
        page.link.controller._clock = lambda: clock["now"]
        cp.tick()
        await wait_for(lambda: "3m left" in rule_row_text(app, 0))  # 500-300
        clock["now"] = 360.0
        cp.tick()
        await wait_for(lambda: "2m left" in rule_row_text(app, 0))  # 500-360
        await wait_for(lambda: cp.query_one("#rule-rows") is rows)
        # A same-membership frame (a mode switch lands in it): the
        # status line follows the mode, the rows never swap.
        page.link.controller.apply_frame(same_rows_frame("allow"))
        cp.tick()
        await wait_for(lambda: "mode allow" in status_line(app))
        await wait_for(lambda: cp.query_one("#rule-rows") is rows)
        assert rules_focus(app) == "d1"
        # A membership change (a1 revoked): the fresh-list swap.
        page.link.controller.apply_frame(empty_rules_frame("allow"))
        cp.tick()
        # Pin the swap itself, not a row count: a count of 0 also
        # reads on the old list mid-swap under load (#322).
        await wait_for(
            lambda: (
                rules_children(app) == 0
                and cp.query_one("#rule-rows") is not rows
            )
        )


async def test_rules_rebuild_self_heals_without_an_old_list() -> None:
    """A rebuild that finds no list (one that died mid-swap) mounts
    the fresh one anew — the swap is also the heal."""
    factory = FakeFactory([FakeWS([rules_frame()]), FakeWS([])])
    app, page, _data = make_page(factory)
    async with app.run_test() as pilot:
        cp = await open_consent(pilot, app, page)
        await wait_for(lambda: rules_children(app) == 2)
        await cp.query_one("#rule-rows").remove()
        cp.rule_rebuilds.request()
        await wait_for(lambda: rules_children(app) == 2)


async def test_a_swap_without_an_old_list_mounts_the_fresh_one() -> None:
    """The swap's no-old-list arc, driven directly: the self-heal
    test above reaches it through the rebuild loop, whose lookup
    can still find a dying list on a starved loop and repaint in
    place instead (#468's race) — under CI load that left the arc
    uncovered and the coverage gate red. The direct call pins the
    swap's own contract: no old list mounts the fresh one."""
    factory = FakeFactory([FakeWS([rules_frame()]), FakeWS([])])
    app, page, _data = make_page(factory)
    async with app.run_test() as pilot:
        cp = await open_consent(pilot, app, page)
        await wait_for(lambda: rules_children(app) == 2)
        old = cp.query_one("#rule-rows")
        await old.remove()
        ordered = consent_ui.rule_rows(cp.controller.rules)
        await cp.swap_rule_rows(cp.query_one("#rules-zone"), None, ordered)
        assert cp.query_one("#rule-rows") is not old
        await wait_for(lambda: rules_children(app) == 2)


async def test_the_verdicts_refresh_on_frames() -> None:
    """The verdicts repaint while a picker sits above the page: a
    frame landing repaints the rows without a visit, and `m`
    reaches the host's picker from the page."""
    factory = FakeFactory([FakeWS([rules_frame()]), FakeWS([])])
    app, page, _data = make_page(factory)
    async with app.run_test() as pilot:
        cp = await open_consent(pilot, app, page)
        await wait_for(lambda: rules_children(app) == 2)
        page.link.controller.apply_frame(empty_rules_frame("allow"))
        cp.tick()
        await wait_for(lambda: rules_children(app) == 0)
        await open_screen(pilot, app, "m", "ModeScreen")
        await open_screen(pilot, app, "escape", "ConsentPage")


async def test_a_swap_reclaims_focus_from_its_dying_rules_list() -> None:
    """A focus grab landing inside a swap's remove window leaves
    the screen focused on the removed list — the page's own
    ``started`` can process there on a starved loop (its zone
    query still finds the dying list), and keys then die on the
    removed widget's closed pump for good: 'm' never took effect
    under the parallel suite (#468, #476). The swap's tail
    reclaims the focus its removal orphaned, and the page's keys
    live again."""
    factory = FakeFactory([FakeWS([rules_frame()]), FakeWS([])])
    app, page, _data = make_page(factory)
    async with app.run_test() as pilot:
        cp = await open_consent(pilot, app, page)
        await wait_for(lambda: rules_children(app) == 2)
        await pilot.pause()  # started() lands; it must not mask the race
        old = cp.query_one("#rule-rows")
        real_remove = old.remove

        async def remove_under_a_grab():
            removal = real_remove()  # the prune repairs synchronously here
            cp.set_focus(old)  # the grab: started() landing inside the window
            return await removal

        old.remove = remove_under_a_grab
        cp.set_focus(None)  # the swap's head reads held_focus False
        page.link.controller.apply_frame(empty_rules_frame("allow"))
        cp.tick()
        # The children count crosses zero while the dying list's
        # rows prune, and the page's own once-a-second tick can
        # land its own swap beside this one — wait for the end
        # state itself: the fresh list owns the id, and the focus
        # the grab left on the corpse has been reclaimed onto it.
        await wait_for(
            lambda: (
                cp.rule_rows() is not old
                and cp.focused is not old
                and cp.focused is cp.rule_rows()
            )
        )
        await open_screen(pilot, app, "m", "ModeScreen")


async def test_a_zone_swap_keeps_the_other_zones_focus() -> None:
    """A holds-list rebuild (a new hold arriving beside a standing
    one) moves no focus into the holds zone while the operator
    reads the verdicts — the growth edge is the empty queue alone."""
    factory = FakeFactory(
        [
            FakeWS([request_frame("r1"), rules_frame()]),
            FakeWS([]),
        ]
    )
    app, page, _data = make_page(factory)
    async with app.run_test() as pilot:
        await open_consent(pilot, app, page)
        await wait_for(lambda: hold_children(app) == 1)
        await wait_for(lambda: rules_children(app) == 2)
        # Walk into the verdicts once both zones stand and the holds
        # zone holds the focus (the growth swap has landed).
        await wait_for(lambda: focused_zone(app) == "holds")
        await press_until(pilot, "down", lambda: focused_zone(app) == "rules")
        assert focused_zone(app) == "rules"
        factory.made[0].push(request_frame("r2"))
        await wait_for(lambda: hold_children(app) == 2)
        await pilot.pause()
        assert focused_zone(app) == "rules"  # no steal into the holds
        assert rules_focus(app) is not None  # a row keeps its highlight


# -- the sightings' flash (#201 over #358) ---------------------------------


async def test_a_sighting_flashes_the_page_line() -> None:
    """An off-allowlist sighting interrupts wherever the operator
    is: with the consent page closed, the workspace page's consent
    line carries the alarm for its TTL."""
    factory = FakeFactory([FakeWS([rules_frame()]), FakeWS([])])
    app, page, _data = make_page(factory)
    async with app.run_test() as pilot:
        await open_page(pilot, app, page)
        factory.made[0].push(secret_frame("sighting", host="evil.example"))
        await wait_for(lambda: page.link.sightings)
        page.tick()
        assert "! sighting" in page_consent(app, page)
        assert "evil.example" in page_consent(app, page)


async def test_a_sighting_flashes_the_consent_page_while_it_is_up() -> None:
    factory = FakeFactory([FakeWS([rules_frame()]), FakeWS([])])
    app, page, _data = make_page(factory)
    async with app.run_test() as pilot:
        await open_consent(pilot, app, page)
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


# -- the mode picker over the page -----------------------------------------


async def test_the_mode_picker_opens_from_the_consent_page() -> None:
    """`m` on the consent page opens the workspace page's picker
    directly (#301 over #454): the operator watching holds
    escalates or relaxes the posture without leaving the page —
    the switch's one path since the workspace page's own mode
    action left (#460) — the current mode starts highlighted, the
    pick goes through the data seam, and `m` again under the open
    picker stacks nothing."""
    factory = FakeFactory([FakeWS([rules_frame()]), FakeWS([])])
    app, page, data = make_page(factory)
    async with app.run_test() as pilot:
        await open_consent(pilot, app, page)
        await wait_for(lambda: "mode interactive" in status_line(app))
        await open_screen(pilot, app, "m", "ModeScreen")
        options = app.screen.query_one("#pick-options")
        assert options.highlighted == 2  # interactive, the snapshot's
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
        await wait_for(lambda: on_consent(app))


async def test_the_status_line_names_the_link_state() -> None:
    """The empty line and the status line stay honest about the
    link: a drop names itself, a rejected registration names its
    reason — silence is never data — and the held count drops to
    zero off a live link for the header's own reason (a dead
    socket's snapshot may carry holds the server already
    resolved), a truncated closing tag in the reason included."""
    factory = FakeFactory([FakeWS([rules_frame()]), FakeWS([])])
    app, page, _data = make_page(factory)
    async with app.run_test() as pilot:
        cp = await open_consent(pilot, app, page)
        await wait_for(lambda: "mode interactive" in status_line(app))
        page.link.state = "reconnecting"
        cp.sync_empty([])
        assert "reconnecting" in str(cp.query_one("#holds-empty").content)
        factory.made[0].push(request_frame("r1"))
        await wait_for(lambda: len(page.link.controller.pending) == 1)
        cp.update_status()
        assert "0 held" in status_line(app)  # the drop folds the count
        page.link.state = "rejected"
        page.link.reject_reason = "unknown workspace [/dev"
        cp.update_status()
        assert "unknown workspace" in status_line(app)  # parses, renders
        # The workspace page beneath ticks on its own timer and
        # repaints its #consent line with the same reason — the
        # load-bound crash sat here (#476): drive the repaint by
        # hand so the poisoned reason meets the markup parser on
        # every run, not only when the timer lands in the window.
        page.paint_consent()
        assert "unknown workspace" in str(
            page.query_one("#consent", Static).content
        )
        await pilot.pause()


async def test_the_status_line_shows_the_mode() -> None:
    """The page's status line names the current mode at all times
    (#301): `—` until the first rules frame lands, the snapshot's
    mode after, and a mode switch's reply repaints it."""
    factory = FakeFactory([FakeWS([]), FakeWS([])])
    app, page, _data = make_page(factory)
    async with app.run_test() as pilot:
        cp = await open_consent(pilot, app, page)
        await pilot.pause()
        cp.update_status()
        assert "mode —" in status_line(app)
        page.link.controller.apply_frame(rules_frame())
        cp.tick()
        await wait_for(lambda: "mode interactive" in status_line(app))
        page.link.controller.apply_frame(mode_frame("allow"))
        cp.tick()
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
        await open_consent(pilot, app, page)
        await wait_for(lambda: hold_children(app) == 1)
        await open_screen(pilot, app, "A", "DurationScreen")
        await pilot.press("m")
        await pilot.pause()
        assert not [
            s for s in app.screen_stack if type(s).__name__ == "ModeScreen"
        ]
        await press_until(
            pilot,
            "escape",
            lambda: type(app.screen).__name__ != "DurationScreen",
        )
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
        await open_consent(pilot, app, page)
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
        await open_consent(pilot, app, page)
        await open_screen(pilot, app, "m", "ModeScreen")
        await pilot.press("up")  # the row's interactive -> static
        await pilot.press("enter")
        await pilot.press("y")  # the confirmation: taken
        await wait_for(lambda: len(data.modes) == 1)
        assert data.modes == [(WS, "static", True)]


async def test_the_picker_with_an_unknown_current() -> None:
    """A current mode the picker does not know (a row without a
    recorded mode and no snapshot yet) leaves nothing highlighted
    — the widget's constructor pre-highlights the first row, and a
    bare Enter must not take a posture the page has not seen
    (#465 review)."""
    factory = FakeFactory([FakeWS([]), FakeWS([])])
    app, page, _data = make_page(factory)
    async with app.run_test() as pilot:
        await open_page(pilot, app, page)
        page.row = dict(PAGE_ROW, egress_mode=None)
        page.open_mode_picker()

        def highlight():
            try:
                return app.screen.query_one("#pick-options").highlighted
            except Exception:
                return "pending"  # the compose stream settles async

        # The constructor's own highlight (row 0) is transient: the
        # mount settles on nothing highlighted.
        await wait_for(lambda: highlight() is None)
        await pilot.press("enter")
        await asyncio.sleep(0.1)
        # Enter decided nothing: the picker still stands, nothing
        # was picked.
        assert type(app.screen).__name__ == "ModeScreen"


async def test_mode_picker_escape_cancels() -> None:
    """Escape closes the picker deciding nothing."""
    factory = FakeFactory([FakeWS([rules_frame()]), FakeWS([])])
    app, page, data = make_page(factory)
    async with app.run_test() as pilot:
        await open_consent(pilot, app, page)
        await open_screen(pilot, app, "m", "ModeScreen")
        await press_until(
            pilot,
            "escape",
            lambda: not isinstance(app.screen, consent_ui.ModeScreen),
        )
        assert data.modes == []


async def test_mode_switch_failure_flashes() -> None:
    """A failed switch (the daemon's named refusal among them)
    flashes on the consent page's own status line — the surface
    that owns the terminal while the switch stands (#460) — and
    never crashes the tree."""
    factory = FakeFactory([FakeWS([rules_frame()]), FakeWS([])])
    app, page, data = make_page(factory)
    data.fail.add("mode")
    async with app.run_test() as pilot:
        await open_consent(pilot, app, page)
        await open_screen(pilot, app, "m", "ModeScreen")
        await pilot.press("up")  # interactive -> static
        await pilot.press("enter")
        await wait_for(lambda: len(data.modes) == 1)
        await wait_for(lambda: "mode switch failed" in status_line(app))
        assert on_consent(app)  # the page kept the terminal


async def test_modal_keys_do_not_reach_the_hidden_page() -> None:
    """The verdict keys stay inert under a pushed screen (the
    screen-separation rule #358): `a` decides nothing while the
    picker is open over the consent page, and `q` closes the modal
    instead of the page."""
    factory = FakeFactory([FakeWS([request_frame("r1")]), FakeWS([])])
    app, page, data = make_page(factory)
    async with app.run_test() as pilot:
        await open_consent(pilot, app, page)
        await wait_for(lambda: hold_children(app) == 1)
        await open_screen(pilot, app, "m", "ModeScreen")
        await pilot.press("a")
        await pilot.press("d")
        await asyncio.sleep(0.05)
        assert data.decided == []  # the hidden hold stays undecided
        await open_screen(
            pilot, app, "q", "ConsentPage"
        )  # the modal's own binding
        assert app.is_running  # q closed the modal, not the tree


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
        cp = await open_consent(pilot, app, page)
        await wait_for(lambda: hold_children(app) == 2)
        rows = cp.query_one("#hold-rows")
        ordered = page.link.controller.ordered()

        def row_text(index: int) -> str:
            """One row's text; empty while its inner Static still
            mounts (the compose stream lags the row count — the
            repaint reads the Statics, so it waits for them)."""
            try:
                return str(rows.children[index].query_one(Static).content)
            except Exception:
                return ""

        await wait_for(lambda: row_text(0) and row_text(1))

        class Gone:
            """A hold whose row left between the membership read and
            the pass — the skip the guard exists for."""

            id = "gone"

        cp.repaint_countdowns(rows, ordered)  # both survivors
        cp.repaint_countdowns(rows, [*ordered, Gone()])
        await pilot.pause()
        assert hold_children(app) == 2


async def test_a_tick_survives_widgets_that_left_under_it() -> None:
    """A tick whose widgets left under it (a teardown race, a
    mid-swap death) is noise, not a crash: the tick swallows the
    missing query and the next one repaints what stands."""
    factory = FakeFactory([FakeWS([rules_frame()]), FakeWS([])])
    app, page, _data = make_page(factory)
    async with app.run_test() as pilot:
        cp = await open_consent(pilot, app, page)
        await wait_for(lambda: cp.hold_rows() is not None)
        await cp.query_one("#holds-empty").remove()
        cp.tick()  # the empty line's query raises inside: swallowed
        await wait_for(lambda: cp.hold_rows() is not None)


async def test_a_header_line_that_left_under_the_paint() -> None:
    """A header line removed under the paint (a teardown race)
    is noise, not a crash: the paint swallows the missing query
    (one try wraps both lines, so the meta line's repaint skips
    with the missing name line)."""
    factory = FakeFactory([FakeWS([rules_frame()]), FakeWS([])])
    app, page, _data = make_page(factory)
    async with app.run_test() as pilot:
        cp = await open_consent(pilot, app, page)
        await wait_for(lambda: "dev" in header_line(app))
        await cp.query_one("#header").remove()
        cp.paint_header()  # swallowed: the name line is gone
        await pilot.pause()


async def test_hold_rows_returns_none_in_a_swap_window() -> None:
    factory = FakeFactory([FakeWS([request_frame("r1")]), FakeWS([])])
    app, page, _data = make_page(factory)
    async with app.run_test() as pilot:
        cp = await open_consent(pilot, app, page)
        await wait_for(lambda: cp.hold_rows() is not None)
        rows = cp.hold_rows()
        await rows.remove()
        assert cp.hold_rows() is None
        cp.tick()  # the missing list self-heals
        await wait_for(lambda: cp.hold_rows() is not None)


async def test_action_revoke_in_a_swap_window() -> None:
    """`x` pressed inside a rebuild's swap window reads as nothing
    focused and revokes nothing."""
    factory = FakeFactory([FakeWS([rules_frame()]), FakeWS([])])
    app, page, data = make_page(factory)
    async with app.run_test() as pilot:
        cp = await open_consent(pilot, app, page)
        await wait_for(lambda: rules_children(app) == 2)
        rows = cp.query_one("#rule-rows")
        await rows.remove()
        await cp.action_revoke()
        assert data.revoked == []


async def test_pick_duration_and_decide_in_a_swap_window() -> None:
    """`A` pressed inside a rebuild's swap window reads as nothing
    focused: the picker stays closed, nothing flashes."""
    factory = FakeFactory([FakeWS([request_frame("r1")]), FakeWS([])])
    app, page, _data = make_page(factory)
    async with app.run_test() as pilot:
        cp = await open_consent(pilot, app, page)
        await wait_for(lambda: hold_children(app) == 1)
        rows = cp.hold_rows()
        await rows.remove()
        await cp.pick_duration("allow")
        await pilot.pause()
        assert type(app.screen).__name__ == "ConsentPage"
        assert "no hold focused" in status_line(app)


async def test_a_mid_swap_death_self_heals_on_the_next_tick() -> None:
    """A rebuild that died mid-swap leaves no list; the tick sees
    the membership differ from nothing and rebuilds — the zone
    self-heals instead of wedging blank."""
    factory = FakeFactory(
        [FakeWS([request_frame("r1"), request_frame("r2")]), FakeWS([])]
    )
    app, page, _data = make_page(factory)
    async with app.run_test() as pilot:
        cp = await open_consent(pilot, app, page)
        await wait_for(lambda: hold_children(app) == 2)
        rows = cp.hold_rows()
        await rows.remove()
        cp.tick()
        await wait_for(lambda: hold_children(app) == 2)


async def test_rebuild_re_arms_when_frames_land_mid_flight() -> None:
    """A frame landing while the rebuild is in flight re-arms: the
    fresh state applies the moment the flight lands."""
    factory = FakeFactory([FakeWS([request_frame("r1")]), FakeWS([])])
    app, page, _data = make_page(factory)
    async with app.run_test() as pilot:
        cp = await open_consent(pilot, app, page)
        await wait_for(lambda: hold_children(app) == 1)
        landing = asyncio.Event()

        real_rebuild = cp.hold_rebuilds._rebuild  # zero-arg: reads at call

        async def gated_rebuild() -> None:
            await real_rebuild()
            landing.set()

        cp.hold_rebuilds._rebuild = gated_rebuild
        factory.made[0].push(request_frame("r2"))
        await wait_for(lambda: hold_children(app) == 2)
        await wait_for(landing.is_set)


async def test_a_dying_rebuild_logs_and_re_arms() -> None:
    """A rebuild that dies mid-flight logs (on a live owner) and
    carries a request that armed while it was dying."""
    factory = FakeFactory([FakeWS([request_frame("r1")]), FakeWS([])])
    app, page, _data = make_page(factory)
    async with app.run_test() as pilot:
        cp = await open_consent(pilot, app, page)
        await wait_for(lambda: hold_children(app) == 1)
        # The open-time flight settles first: the mid-flight request
        # below must arm against a dying flight, not race whatever
        # the page's own startup still holds in the air (#322).
        await wait_for(lambda: not cp.hold_rebuilds.scheduled)
        armed_while_dying = {"late": False}

        async def dying() -> None:
            raise RuntimeError("boom")

        cp.hold_rebuilds._rebuild = dying

        original_request = cp.hold_rebuilds.request

        def spying_request() -> None:
            if cp.hold_rebuilds.scheduled:
                armed_while_dying["late"] = True
            original_request()

        cp.hold_rebuilds.request = spying_request  # type: ignore[method-assign]
        cp.hold_rebuilds.request()  # arms the dying flight
        # The mid-flight request, explicit: the arming call set
        # ``scheduled`` synchronously, so this one observably arms
        # while the flight is dying — no startup race to win.
        cp.hold_rebuilds.request()
        await wait_for(lambda: not cp.hold_rebuilds.scheduled)
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


# -- the harness's budgets (#468) -----------------------------------------


async def test_wait_for_credits_a_descheduled_park(monkeypatch) -> None:
    """A park that runs past its requested span (the runner was
    descheduled under the parallel suite) is not budget spent: the
    deadline extends by the overshoot, so a condition that lands
    after the wall-clock window still lands (#468). The budget
    here is 0.3s while every park runs 0.15s over — without the
    credit the poll gives up before the 0.8s condition."""
    real_sleep = asyncio.sleep

    def starved_sleep(delay: float):
        return real_sleep(delay + 0.15)

    lands_at = time.monotonic() + 0.8
    monkeypatch.setattr(asyncio, "sleep", starved_sleep)
    await wait_for(lambda: time.monotonic() >= lands_at, timeout=0.3)


async def test_wait_for_still_gives_up_without_a_landing(monkeypatch) -> None:
    """The credit buys back starved time, not a second chance: a
    condition that never lands still fails inside its (credited)
    budget — the credit must not make a genuine regression hang."""
    real_sleep = asyncio.sleep

    def starved_sleep(delay: float):
        return real_sleep(delay + 0.15)

    monkeypatch.setattr(asyncio, "sleep", starved_sleep)
    try:
        await wait_for(lambda: False, timeout=0.3)
    except AssertionError as exc:
        assert "never landed" in str(exc)
    else:
        raise AssertionError("the poll should have given up")


class SwapWindowRow:
    """A row caught in the rebuild's swap window: the row is mounted and
    highlighted, its ``Static`` answers only after ``shots`` reads
    (#489)."""

    def __init__(self, text: str, shots: int) -> None:
        self.text = text
        self.shots = shots

    def query_one(self, selector):
        if self.shots > 0:
            self.shots -= 1
            raise NoMatches(f"No nodes match {selector!r} on SwapWindowRow")
        return SimpleNamespace(content=self.text)


class SwapWindowPage:
    """The zone list as the row helpers see it during a rebuild: the
    container query answers only after its own ``shots`` reads — the
    #492 CI shape, where ``No nodes match '#hold-rows'`` fired while
    the count gates had already passed."""

    def __init__(self, rows, shots: int = 0) -> None:
        self.rows = rows
        self.shots = shots

    def query_one(self, selector):
        if self.shots > 0:
            self.shots -= 1
            raise NoMatches(f"No nodes match {selector!r} on SwapWindowPage")
        return SimpleNamespace(children=self.rows)


class PopulatingPage:
    """The zone list mid-populate: each read sees one more row mounted
    than the last, so an early read's index is past the end."""

    def __init__(self, rows) -> None:
        self.rows = rows
        self.reads = 0

    def query_one(self, selector):
        self.reads += 1
        return SimpleNamespace(children=self.rows[: self.reads - 1])


async def test_the_row_read_retries_through_the_swap_window() -> None:
    """#489: the row read was a single un-retried shot, so a read that
    landed inside a rebuild's swap window raised while the count gates
    had already passed. The read polls, so the window is a retry."""
    page = SwapWindowPage([SwapWindowRow("api.example:443", shots=3)])
    assert await poll(lambda: row_text(page, "hold-rows", 0)) == (
        "api.example:443"
    )


async def test_the_container_read_retries_through_the_swap_window() -> None:
    """The #492 CI shape: the zone's own id query raised inside the
    window — the row behind it was fine. The poll retries the whole
    read, container query first."""
    page = SwapWindowPage([SwapWindowRow("api.example:443", shots=0)], shots=3)
    assert await poll(lambda: row_text(page, "hold-rows", 0)) == (
        "api.example:443"
    )


async def test_a_read_past_the_rows_mounted_so_far_retries() -> None:
    """The list populates row by row: a read whose index is past the
    rows mounted so far raises, and the poll reads again once the row
    stands."""
    page = PopulatingPage([SwapWindowRow("api.example:443", shots=0)])
    assert await poll(lambda: row_text(page, "hold-rows", 0)) == (
        "api.example:443"
    )


async def test_a_window_then_wrong_content_reports_the_content() -> None:
    """A read that raises once and then answers with the wrong content
    must fail as the content mismatch it is: the window's error is
    cleared by the first answering read, so triage is not sent down
    the flake path this file just retired."""
    page = SwapWindowPage([SwapWindowRow("api.example:443", shots=1)])
    try:
        await poll(
            lambda: "forever" in row_text(page, "hold-rows", 0), timeout=0.3
        )
    except AssertionError as exc:
        assert "never landed" in str(exc)
    else:
        raise AssertionError("the read should have given up")


async def test_a_window_at_the_deadline_reports_the_content() -> None:
    """A read that answers wrong through the whole budget and then hits
    a window on the deadline read still fails as the content mismatch:
    a window opening at the deadline does not outrank the answers
    before it."""
    calls = []

    def answers_then_raises():
        calls.append(1)
        if len(calls) > 1:
            raise NoMatches("window opened at the deadline")
        return False

    try:
        await poll(answers_then_raises, timeout=0.05, delay=0.2)
    except AssertionError as exc:
        assert "never landed" in str(exc)
    else:
        raise AssertionError("the read should have given up")


async def test_a_read_with_no_page_open_names_the_page() -> None:
    """A poll with no consent page on the stack still fails with the
    guard's message, not a bare timeout."""
    try:
        await poll(lambda: row_text(None, "hold-rows", 0), timeout=0.3)
    except AssertionError as exc:
        assert "no consent page open" in str(exc)
    else:
        raise AssertionError("the read should have given up")


async def test_a_row_read_that_never_lands_reports_the_query_error() -> None:
    """The retry must not hide a genuine failure: a row that never
    answers still fails, with the query error, not a bare timeout."""
    page = SwapWindowPage([SwapWindowRow("api.example:443", shots=10_000)])
    try:
        await poll(lambda: row_text(page, "rule-rows", 0), timeout=0.3)
    except NoMatches as exc:
        assert "SwapWindowRow" in str(exc)
    else:
        raise AssertionError("the read should have given up")


async def test_press_until_credits_a_descheduled_cycle(monkeypatch) -> None:
    """A press cycle that runs past its requested sleep is not
    budget spent either (#468): the presses keep coming until the
    key lands, however long the OS starved the worker in between."""
    real_sleep = asyncio.sleep

    def starved_sleep(delay: float):
        return real_sleep(delay + 0.15)

    class CountingPilot:
        presses = 0

        async def press(self, key: str) -> None:
            CountingPilot.presses += 1

    lands_at = time.monotonic() + 0.8
    monkeypatch.setattr(asyncio, "sleep", starved_sleep)
    pilot = CountingPilot()
    await press_until(
        pilot, "m", lambda: time.monotonic() >= lands_at, timeout=0.3
    )
    assert pilot.presses >= 2  # the budget bought more than one cycle


# -- the hold flash's shape (#454) -----------------------------------------


def test_hold_flash_names_the_destination_and_the_key() -> None:
    """The flash carries the destination with its port (all ports
    for a portless flow) and the key in — the attention a hold
    gets while the consent page is closed."""

    class Req:
        dest_host = "api.example"
        dest_port = 443

    assert hold_flash(Req()) == ("egress to decide: api.example:443 — press e")

    class Portless:
        dest_host = "raw.example"
        dest_port = 0

    assert "raw.example (all ports)" in hold_flash(Portless())


def test_hold_flash_escapes_markup() -> None:
    """A destination carrying rich markup renders literally — the
    consent line parses markup at update time, and a truncated
    closing tag in a destination would raise there (#318): the
    flash's text parses clean, bracket and all."""
    from rich.text import Text

    class Hostile:
        dest_host = "[bold]x[/"
        dest_port = 443

    flashed = hold_flash(Hostile())
    Text.from_markup(flashed)  # parses, no MarkupError
    assert "x" in flashed


# -- the link (unchanged, beside the page) ----------------------------------


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


async def test_the_link_ends_on_a_token_the_handshake_cannot_carry() -> None:
    """A token outside the handshake's grammar never becomes callable
    by retrying: the loop ends with one reason on the state, no
    token echoed."""
    from msks.client.wsauth import UnusableToken

    def factory():
        raise UnusableToken("the token cannot ride the websocket handshake")

    link = DeciderLink(WS, ws_factory=factory)
    connected, refused, rejected = await link.pump_one()
    assert (connected, refused, rejected) == (False, False, True)
    assert link.state == "unusable token"
    assert "cannot ride" in link.reject_reason


async def test_the_link_refuses_an_auth_refusal_close() -> None:
    """A daemon that does not hold the token closes 4401 (#216):
    the link marks itself refused, sends nothing further, and takes
    the slow refused retry."""
    ws = FakeWS(close_code=4401)
    link = DeciderLink(WS, ws_factory=FakeFactory([ws]))
    connected, refused, rejected = await link.pump_one()
    assert (connected, refused, rejected) == (True, True, False)
    assert link.state == "refused — bad token?"


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
    the shared context. The token rides the handshake's
    Authorization header (#216), never the URL."""
    import ssl

    ctx = ssl.create_default_context()
    monkeypatch.setattr(consent_ui, "_SHARED_SSL", [ctx])
    kwargs = consent_ui.ws_connect_kwargs("http://d", "t", ctx)
    assert kwargs["ssl"] is None
    kwargs = consent_ui.ws_connect_kwargs("https://d", "t", ctx)
    assert kwargs["ssl"] is ctx
    assert kwargs["uri"].endswith("/api/v1/events")
    assert "token" not in kwargs["uri"]
    assert kwargs["additional_headers"] == [("Authorization", "Bearer t")]
    assert consent_ui.shared_ssl() is ctx


# -- the design pass (#470): the verdict keys' affordances ---------------


async def test_mis_zoned_verdict_keys_name_their_zone() -> None:
    """#470 C2: a verdict key pressed outside its zone names the
    zone it acts on — the letters with a hold waiting but the
    verdicts focused, x with the holds focused. The presses decide
    and revoke nothing."""
    factory = FakeFactory(
        [FakeWS([request_frame("r1"), rules_frame()]), FakeWS([])]
    )
    app, page, data = make_page(factory)
    async with app.run_test() as pilot:
        await open_consent(pilot, app, page)
        await wait_for(lambda: hold_children(app) == 1)
        await wait_for(lambda: rules_children(app) == 2)
        await press_until(pilot, "down", lambda: focused_zone(app) == "rules")
        await press_until(
            pilot, "a", lambda: "a decides a held request" in status_line(app)
        )
        await press_until(
            pilot,
            "A",
            lambda: "A picks a duration on a held request" in status_line(app),
        )
        await press_until(
            pilot, "d", lambda: "d decides a held request" in status_line(app)
        )
        await press_until(
            pilot,
            "D",
            lambda: "D picks a duration on a held request" in status_line(app),
        )
        assert data.decided == []
        await press_until(pilot, "up", lambda: focused_zone(app) == "holds")
        await press_until(
            pilot, "x", lambda: "x revokes a verdict row" in status_line(app)
        )
        assert data.revoked == []


async def test_a_teardown_prune_under_the_highlight_repaint_stops_quiet() -> (
    None
):
    """#470 S3: the highlight repaint walks the rows that stand —
    a row whose Static teardown pruned mid-walk skips alone, the
    walk stops quiet."""
    factory = FakeFactory(
        [FakeWS([request_frame("r1"), rules_frame()]), FakeWS([])]
    )
    app, page, _data = make_page(factory)
    async with app.run_test() as pilot:
        cp = await open_consent(pilot, app, page)
        await wait_for(lambda: hold_children(app) == 1)
        await wait_for(lambda: rules_children(app) == 2)
        await cp.query_one("#rule-rows").children[0].query_one(Static).remove()
        cp.on_list_view_highlighted(None)  # the pruned row skips, no raise


async def test_a_flash_before_the_labels_land_names_nothing() -> None:
    """The status-line update's own guard (#470 C6, the review
    round): a flash routed to the page while its compose still
    settles — a sighting drained from the workspace page beneath
    — finds the labels absent and stops quiet, noise not a
    crash."""
    factory = FakeFactory(
        [FakeWS([request_frame("r1"), rules_frame()]), FakeWS([])]
    )
    app, page, _data = make_page(factory)
    async with app.run_test() as pilot:
        cp = await open_consent(pilot, app, page)
        await wait_for(lambda: hold_children(app) == 1)
        await cp.query_one("#holds-label").remove()
        cp.update_status()  # the label gone: the update stops quiet


async def test_the_focused_rows_carry_the_bold_cue() -> None:
    """#470 S3: the focused hold's destination and the focused
    verdict's host render bold — the focus cue that survives a
    theme whose highlight bar reads weakly."""
    factory = FakeFactory(
        [FakeWS([request_frame("r1"), rules_frame()]), FakeWS([])]
    )
    app, page, _data = make_page(factory)

    def bold_cue(list_id: str) -> bool:
        """Whether the focused row of the named zone carries the
        bold span — False while its Static still mounts (the
        compose stream lags the highlight)."""
        try:
            item = cp.query_one(list_id).highlighted_child
            if item is None:
                return False
            return any(
                span.style == "$text bold"
                for span in item.query_one(Static).content.spans
            )
        except Exception:
            return False

    async with app.run_test() as pilot:
        cp = await open_consent(pilot, app, page)
        await wait_for(lambda: hold_children(app) == 1)
        await wait_for(lambda: focused_zone(app) == "holds")
        await wait_for(lambda: bold_cue("#hold-rows"))
        await press_until(pilot, "down", lambda: focused_zone(app) == "rules")
        await wait_for(lambda: bold_cue("#rule-rows"))


async def test_a_decide_key_inside_a_swap_window_flashes() -> None:
    """The decide path's honest nothing (#454, #470 C2): a verdict
    key whose hold list stands in a rebuild's swap window reads
    as nothing focused and names it — the guard the zone gates
    stand in front of."""
    factory = FakeFactory([FakeWS([request_frame("r1")]), FakeWS([])])
    app, page, data = make_page(factory)
    async with app.run_test() as pilot:
        cp = await open_consent(pilot, app, page)
        await wait_for(lambda: hold_children(app) == 1)
        await wait_for(lambda: focused_zone(app) == "holds")
        await cp.query_one("#hold-rows").remove()
        await cp.decide_focused("allow", "15m")  # the swap window
        await wait_for(lambda: "no hold focused" in status_line(app))
        assert data.decided == []


async def test_the_verdict_keys_leave_when_the_queue_empties() -> None:
    """#470 C3 (the review round's insurance): the verdict keys'
    gate follows the queue's membership wherever the focus stands
    — the last hold resolving under the rules zone's focus takes
    the keys out of the page's active bindings, and the holds
    rebuild hands the footer the fresh answer."""
    factory = FakeFactory(
        [FakeWS([request_frame("r1"), rules_frame()]), FakeWS([])]
    )
    app, page, data = make_page(factory)
    async with app.run_test() as pilot:
        cp = await open_consent(pilot, app, page)
        await wait_for(lambda: hold_children(app) == 1)
        await wait_for(lambda: rules_children(app) == 2)
        await press_until(pilot, "down", lambda: focused_zone(app) == "rules")

        def verdict_keys() -> list[str]:
            bindings = cp.active_bindings
            return sorted(k for k in ("a", "A", "d", "D") if k in bindings)

        await wait_for(lambda: verdict_keys() == ["A", "D", "a", "d"])
        await decide_the(page.link.controller, "r1")
        cp.tick()  # the rebuild lands the gate's fresh answer
        await wait_for(lambda: verdict_keys() == [])
        assert data.decided == []
