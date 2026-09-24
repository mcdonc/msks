"""The consent decider app (Pilot-driven, fake seams — #195)."""

import asyncio
import json

import pytest
import websockets
from msks.client import cli
from msks.client.tui import consent_app
from msks.client.tui.consent_app import (
    ConsentDeciderApp,
    backoff,
    refused_close,
)
from test_consent_tui import (
    request_frame as shared_request_frame,
)
from test_consent_tui import (
    rules_frame as shared_rules_frame,
)
from textual.widgets import Static


def request_frame(rid, host="api.example", port=443):
    """The shared fixture, scoped to this app's workspace — the
    controller filters foreign frames (#280 review)."""
    return shared_request_frame(rid, host, port, workspace="ws-dev")


def rules_frame():
    """The shared fixture, scoped to this app's workspace."""
    return shared_rules_frame(workspace="ws-dev")


class FakeWS:
    """One scripted websocket connection that stays open like a
    real one: frames stream, then recv blocks until close() (or
    raises the scripted close code). An instant StopAsyncIteration
    would make the app reconnect and reset its state — the real
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
        """Deliver a frame to a parked connection (the app's recv is
        parked once the initial frames ran out — appending to the
        list alone would never wake it)."""
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
    """The async-context-manager half the app awaits."""

    def __init__(self, ws: FakeWS) -> None:
        self.ws = ws

    async def __aenter__(self) -> FakeWS:
        return self.ws

    async def __aexit__(self, *exc) -> None:
        return None


def recording(seams: dict, **kw) -> None:
    seams.setdefault("decided", [])
    seams.setdefault("revoked", [])
    seams.setdefault("modes", [])


async def fake_decide(seams, workspace, request_id, decision, duration):
    seams["decided"].append((workspace, request_id, decision, duration))
    if seams.get("fail_decide"):
        raise RuntimeError("daemon away")


async def fake_revoke(seams, workspace, request_id):
    seams["revoked"].append((workspace, request_id))
    if seams.get("fail_revoke"):
        raise RuntimeError("daemon away")


async def fake_set_mode(
    seams, workspace, mode, *, confirm_empty: bool = False
):
    seams["modes"].append((workspace, mode, confirm_empty))
    if seams.get("fail_mode"):
        raise RuntimeError("daemon away")


def make_app(factory, hold_timeout: float = 120.0):
    seams: dict = {}
    recording(seams)
    app = ConsentDeciderApp(
        "ws-dev",
        hold_timeout=hold_timeout,
        ws_factory=factory,
        decide=lambda *a: fake_decide(seams, *a),
        revoke=lambda *a: fake_revoke(seams, *a),
        set_mode=lambda *a, **kw: fake_set_mode(seams, *a, **kw),
        reconnect_delays=(0.01, 0.01, 0.01),
    )
    return app, seams


def held_row(app, index: int) -> str:
    rows = app.query_one("#requests")
    return str(rows.children[index].query_one(Static).content)


def queue_children(app) -> int:
    """The queue's row count; -1 inside a rebuild's swap window."""
    try:
        return len(app.query_one("#requests").children)
    except Exception:
        return -1


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


def secret_frame(kind: str, **data) -> str:
    """One interceptor audit frame as the daemon sends it (#201)."""
    payload = {"workspace_id": "ws-dev", "name": "api"}
    payload.update(data)
    return json.dumps({"event": f"secret.{kind}", "data": payload})


def focused_request_id_or_none(app) -> str | None:
    """The queue's focused id, or None in a swap window."""
    try:
        from msks.client.tui.consent_app import focused_request_id

        return focused_request_id(app.query_one("#requests"))
    except Exception:
        return None


def rules_focus(app) -> str | None:
    """The focused rule id, or None during a swap window."""
    try:
        from msks.client.tui.consent_app import focused_rule_id

        return focused_rule_id(app.screen.query_one("#rule-rows"))
    except Exception:
        return None


def status_line(app) -> str:
    return str(app.query_one("#status").content)


async def wait_for(condition, tries: int = 200, delay: float = 0.02) -> None:
    """Poll a render condition (UI updates land on the message pump,
    not synchronously with the worker's frames)."""
    for _ in range(tries):
        if condition():
            return
        await asyncio.sleep(delay)
    raise AssertionError("condition never landed")


async def press_until(pilot, key: str, landed, tries: int = 40) -> None:
    """Press a key until its effect lands — an action pressed inside
    a rebuild's swap window no-ops (by design), so the tests retry."""
    for _ in range(tries):
        await pilot.press(key)
        try:
            if landed():
                return
        except Exception:
            pass
        await asyncio.sleep(0.05)
    raise AssertionError(f"{key!r} never took effect")


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
    app, seams = make_app(factory)
    async with app.run_test() as pilot:
        await wait_for(lambda: queue_children(app) == 2)
        await pilot.pause()
        assert "api.example:443" in held_row(app, 0)
        assert "raw.example (all ports)" in held_row(app, 1)
        assert "ws-dev" in status_line(app)
        assert "connected" in status_line(app)
        # a allows the focused (first) hold with the default duration.
        await pilot.press("a")
        await wait_for(lambda: len(seams["decided"]) == 1)
        assert seams["decided"] == [("ws-dev", "r1", "allow", "tilrestart")]
        # A resolved frame drops its row.
        app.controller.apply_frame(
            json.dumps(
                {
                    "event": "egress.resolved",
                    "data": {"request_id": "r1", "decision": "allowed"},
                }
            )
        )
        app.safe_repaint()
        await wait_for(lambda: queue_children(app) == 1)
        # d denies the survivor (retried: the press can land in the
        # rebuild's swap window right after the resolve).
        await press_until(pilot, "d", lambda: len(seams["decided"]) == 2)
        assert seams["decided"][-1] == ("ws-dev", "r2", "deny", "tilrestart")
        app.action_quit_screen()
    assert factory.made[0].sent and factory.made[0].closed


async def test_the_duration_picker() -> None:
    factory = FakeFactory([FakeWS([request_frame("r1")]), FakeWS([])])
    app, seams = make_app(factory)
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("A")
        await wait_for(lambda: type(app.screen).__name__ == "DurationScreen")
        # Default highlight is tilrestart; two ups land on 5m.
        await pilot.press("up", "up", "enter")
        await wait_for(lambda: len(seams["decided"]) == 1)
        assert seams["decided"] == [("ws-dev", "r1", "allow", "5m")]
        # D + Escape cancels: nothing more decided.
        factory.made[0].push(request_frame("r3"))
        await wait_for(lambda: queue_children(app) == 2)
        await press_until(
            pilot, "D", lambda: type(app.screen).__name__ == "DurationScreen"
        )
        await pilot.press("escape")
        await pilot.pause()
        assert len(seams["decided"]) == 1
        app.action_quit_screen()


async def test_the_rules_screen_revokes() -> None:
    factory = FakeFactory([FakeWS([rules_frame()]), FakeWS([])])
    app, seams = make_app(factory)
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("r")
        await wait_for(lambda: type(app.screen).__name__ == "RulesScreen")
        await wait_for(lambda: rules_children(app) == 2)
        await wait_for(lambda: rules_children(app) >= 0)
        header = str(app.screen.query_one("#allowlist").content)
        assert "mode interactive" in header
        assert ".debian.org" in header
        await wait_for(lambda: rules_children(app) == 2)

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
        await wait_for(lambda: len(seams["revoked"]) == 1)
        assert seams["revoked"] == [("ws-dev", "a1")]
        # r (or escape) returns to the queue.
        await pilot.press("r")
        await pilot.pause()
        assert app.query_one("#requests") is not None
        # A rules screen with nothing focused revokes nothing (a
        # fresh controller: no rules, no rows, no focus).
        from msks.client.tui import consent as consent_mod
        from msks.client.tui.consent_app import RulesScreen

        empty = consent_mod.ConsentController()
        app.push_screen(RulesScreen(empty, app.revoke_rule))
        await wait_for(lambda: type(app.screen).__name__ == "RulesScreen")
        await pilot.press("x")
        await pilot.pause()
        assert seams["revoked"] == [("ws-dev", "a1")]  # unchanged
        app.action_quit_screen()


async def test_reconnect_after_a_drop() -> None:
    dropped = FakeWS([], close_code=1006)
    revived = FakeWS([request_frame("r9")])
    factory = FakeFactory([dropped, revived, FakeWS([])])
    app, _seams = make_app(factory)
    async with app.run_test() as pilot:
        await pilot.pause()
        # The first connection dropped: backoff, then the second
        # registers and serves its snapshot row.
        for _ in range(100):
            if len(factory.made) >= 2:
                break
            await asyncio.sleep(0.02)
        for _ in range(100):
            if app.query_one("#requests").children:
                break
            await asyncio.sleep(0.02)
        await pilot.pause()
        assert "r9" in app.controller.pending
        assert revived.sent  # the re-registration frame
        app.action_quit_screen()


async def test_refused_token_retries_slowly() -> None:
    refused = FakeWS([], close_code=4401)
    factory = FakeFactory([refused, FakeWS([])])
    app, _seams = make_app(factory)
    async with app.run_test() as pilot:
        await pilot.pause()
        for _ in range(100):
            if "refused" in app._conn_state:
                break
            await asyncio.sleep(0.02)
        await pilot.pause()
        assert "refused" in status_line(app)
        app.action_quit_screen()


async def test_decide_and_revoke_failures_flash() -> None:
    factory = FakeFactory([FakeWS([request_frame("r1")]), FakeWS([])])
    app, seams = make_app(factory)
    seams["fail_decide"] = True
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("a")
        await wait_for(lambda: "decide failed" in status_line(app))
        # A failing revoke flashes the same way.
        seams["fail_decide"] = False
        seams["fail_revoke"] = True
        await app.revoke_rule("r1")
        await wait_for(lambda: "revoke failed" in status_line(app))
        app.action_quit_screen()


async def test_verdict_keys_without_a_focused_row() -> None:
    factory = FakeFactory([FakeWS([]), FakeWS([])])
    app, seams = make_app(factory)
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("a")
        await pilot.press("d")
        await pilot.pause()
        assert seams["decided"] == []  # nothing focused: no verdict
        app.action_quit_screen()


def test_backoff_and_refused_close() -> None:
    assert backoff((), 3) == 0.0
    assert backoff((1.0, 2.0, 5.0), 1) == 1.0
    assert backoff((1.0, 2.0, 5.0), 9) == 5.0
    refused = websockets.ConnectionClosed(
        websockets.frames.Close(4401, "bad token"), None
    )
    dropped = websockets.ConnectionClosed(
        websockets.frames.Close(1006, "gone"), None
    )
    assert refused_close(refused)
    assert not refused_close(dropped)


def test_row_and_label_helpers() -> None:
    from msks.client.tui import consent as consent_mod
    from msks.client.tui.consent_app import (
        allowlist_text,
        dest_line,
        duration_label,
        mode_label,
        rule_line,
        rule_rows,
    )

    request = consent_mod.ConsentRequest(
        id="r",
        workspace_id="ws",
        dest_host="h",
        dest_port=0,
        requested_at=0.0,
    )
    assert "h (all ports)  (45s)" in dest_line(request, 45.0)
    rule = consent_mod.ConsentRule(
        id="a",
        dest_host="h",
        dest_port=443,
        decision="allowed",
        duration="5m",
        decided_at=None,
        decided_by=None,
    )
    assert duration_label(rule, None) == "5m"
    assert duration_label(rule, 30.0) == "30s left"
    assert "allowed" in rule_line(rule, 30.0)
    assert allowlist_text(None) == "no rules snapshot yet"
    assert rule_rows(None) == []
    rules = consent_mod.EgressRules(
        workspace_id="ws",
        mode="allow",
        allow_list=("x",),
        allowed=(),
        denied=(),
    )
    assert allowlist_text(rules) == "mode allow   allowlist: x"
    assert mode_label(None) == "—"
    assert mode_label(rules) == "allow"


class RaisingEnter:
    """A connection whose entry fails (the daemon is unreachable)."""

    async def __aenter__(self):
        raise OSError("dial refused")

    async def __aexit__(self, *exc) -> None:
        return None


async def test_connect_failures_and_clean_closes() -> None:
    # An unreachable daemon: the entry failure reconnects (False).
    app, _ = make_app(FakeFactory([]))
    app._ws_factory = lambda: RaisingEnter()
    assert await app.pump_one() == (False, False)  # never connected
    # A send that blows up mid-handshake: generic arm, reconnect.
    boom = FakeWS()
    boom_sent = {"go": True}

    async def bad_send(_text):
        if boom_sent["go"]:
            boom_sent["go"] = False
            raise RuntimeError("netlink storm")

    boom.send = bad_send
    app2, _ = make_app(FakeFactory([]))
    app2._ws_factory = lambda: Entering(boom)
    assert await app2.pump_one() == (True, False)  # connected, dropped
    # A clean server close ends the connection (not refused).
    clean = FakeWS()
    await clean.close()
    app3, _ = make_app(FakeFactory([]))
    app3._ws_factory = lambda: Entering(clean)
    assert await app3.pump_one() == (True, False)

    # close() swallowing a dead peer.
    class DeadClose(FakeWS):
        async def close(self):
            raise OSError("already gone")

    await consent_app.close_ws(DeadClose())


async def test_ws_loop_cycles_until_stopped(monkeypatch) -> None:
    app, _ = make_app(FakeFactory([]))
    outcomes = iter([True, False])
    stop_after_clean = {"clean": False}
    ladder = {"attempt": None}

    async def scripted_pump() -> tuple[bool, bool]:
        outcome = next(outcomes)
        if outcome is False:
            stop_after_clean["clean"] = True
            app._stop = True
        return False, outcome

    real_backoff = consent_app.backoff

    def spying_backoff(delays, attempt):
        ladder["attempt"] = attempt
        return real_backoff(delays, attempt)

    monkeypatch.setattr(app, "pump_one", scripted_pump)
    monkeypatch.setattr(consent_app, "REFUSED_RETRY_INTERVAL", 0.01)
    monkeypatch.setattr(consent_app, "backoff", spying_backoff)
    await app.ws_loop()  # refused -> continue -> clean -> stop-return
    assert stop_after_clean["clean"]
    # Pre-stopped: the loop never pumps.
    app2, _ = make_app(FakeFactory([]))
    app2._stop = True
    pumps = {"n": 0}

    async def counting() -> tuple[bool, bool]:
        pumps["n"] += 1
        return False, False

    monkeypatch.setattr(app2, "pump_one", counting)
    await app2.ws_loop()
    assert pumps["n"] == 0


async def test_the_seams_and_entry_point(monkeypatch, capsys) -> None:
    # The REST seams hit the decide/revoke endpoints.
    import os

    os.environ.setdefault("MSKSC_TOKEN", "t")
    calls: list[tuple] = []

    async def fake_api(method, url, token, path, json_body=None, **_kw):
        calls.append((method, path, json_body))
        return {"id": "r", "ok": True}

    monkeypatch.setattr(consent_app, "api_call", fake_api)
    assert await consent_app.rest_decide("ws", "r1", "allow", "5m") == {
        "id": "r",
        "ok": True,
    }
    await consent_app.rest_revoke("ws", "r1")
    await consent_app.rest_set_mode("ws", "static")
    await consent_app.rest_set_mode("ws", "allow", confirm_empty=True)
    assert calls[0][0] == "POST" and calls[0][1].endswith("/r1")
    assert calls[1][0] == "DELETE"
    # The mode seam PUTs the policy; the confirmation rides only
    # when set.
    assert calls[2] == (
        "PUT",
        "/api/v1/workspaces/ws/egress/policy",
        {"mode": "static"},
    )
    assert calls[3][2] == {"mode": "allow", "confirm_empty": True}
    # The default factory builds the events connection.
    monkeypatch.setattr(consent_app, "env_url", lambda: "https://d:1")
    monkeypatch.setattr(consent_app, "env_token", lambda: "t")
    factory = consent_app.default_ws_factory()
    assert "events?token=t" in factory.uri
    # The entry point runs the app and returns success.
    ran = []

    class FakeApp:
        def __init__(self, workspace_id):
            self.workspace_id = workspace_id

        def run(self):
            ran.append(self.workspace_id)

    monkeypatch.setattr(consent_app, "ConsentDeciderApp", FakeApp)
    assert consent_app.run_consent_tui("ws-dev") == 0
    assert ran == ["ws-dev"]


async def test_repaint_survives_render_bugs(monkeypatch) -> None:
    app, _ = make_app(FakeFactory([]))

    def broken():
        raise RuntimeError("view bug")

    monkeypatch.setattr(app, "repaint", broken)
    app.safe_repaint()  # swallowed: the transport never tears down


def test_the_tui_subcommand_dispatches(monkeypatch) -> None:
    """`msks egress tui <ws>` reaches run_consent_tui (the subparser
    alone once left the command table without the key — a KeyError
    traceback to the user)."""
    launched = []

    def fake_run(ws):
        launched.append(ws)
        return 0

    monkeypatch.setattr(cli, "run_consent_tui", fake_run)
    rc = cli.main(["egress", "tui", "ws-dev"])
    assert rc == 0
    assert launched == ["ws-dev"]


async def test_a_verdict_failure_through_the_rest_seam(monkeypatch) -> None:
    """The REST seam reports failures as SystemExit (one readable
    line); a flash, not a dead app — the common case is the hold
    timing out mid-deliberation and the daemon answering 404."""
    factory = FakeFactory([FakeWS([request_frame("r1")]), FakeWS([])])
    app, seams = make_app(factory)

    async def gone(*_args):
        raise SystemExit("msks: 404: no held request with that id")

    app._decide = gone
    async with app.run_test() as pilot:
        await wait_for(lambda: queue_children(app) == 1)
        await press_until(
            pilot, "a", lambda: "decide failed" in status_line(app)
        )
        assert "404" in status_line(app)
        app.action_quit_screen()


async def test_a_resolved_hold_above_focus_never_retargets() -> None:
    """The destructive-key retarget the third review proved: a hold
    resolving ABOVE the focused one shifts every index; with the
    fresh-list rebuild the focused id keeps the verdict on the row
    the operator sees lit."""
    from msks.client.tui.consent_app import focused_request_id

    factory = FakeFactory([FakeWS([request_frame("r1")]), FakeWS([])])
    app, seams = make_app(factory)
    async with app.run_test() as pilot:
        await wait_for(lambda: queue_children(app) == 1)
        factory.made[0].push(request_frame("r2"))
        factory.made[0].push(request_frame("r3"))
        await wait_for(lambda: queue_children(app) == 3)
        await pilot.press("down")  # r2
        await pilot.pause()
        rows = app.query_one("#requests")
        assert focused_request_id(rows) == "r2"
        # r1 (above) resolves: the rebuild must keep r2 focused.
        app.controller.apply_frame(
            json.dumps(
                {
                    "event": "egress.resolved",
                    "data": {"request_id": "r1", "decision": "expired"},
                }
            )
        )
        app.safe_repaint()
        # The rebuild is async: the row count lands first, the
        # restored focus a beat later — wait on the focus itself.
        await wait_for(
            lambda: (
                queue_children(app) == 2
                and focused_request_id_or_none(app) == "r2"
            )
        )
        await press_until(pilot, "a", lambda: len(seams["decided"]) == 1)
        assert seams["decided"] == [("ws-dev", "r2", "allow", "tilrestart")]
        app.action_quit_screen()


async def test_the_focused_hold_leaving_falls_to_a_live_target() -> None:
    """The focused hold resolving must not leave the keys inert or
    aimed at a phantom: the rebuild falls back to the top."""

    factory = FakeFactory([FakeWS([request_frame("r1")]), FakeWS([])])
    app, seams = make_app(factory)
    async with app.run_test() as pilot:
        await wait_for(lambda: app.query_one("#requests").children)
        factory.made[0].push(request_frame("r2"))
        await wait_for(lambda: len(app.controller.pending) == 2)
        app.controller.apply_frame(
            json.dumps(
                {
                    "event": "egress.resolved",
                    "data": {"request_id": "r1", "decision": "expired"},
                }
            )
        )
        app.safe_repaint()
        await wait_for(
            lambda: (
                queue_children(app) == 1
                and focused_request_id_or_none(app) == "r2"
            )
        )
        await press_until(pilot, "d", lambda: len(seams["decided"]) == 1)
        assert seams["decided"][0][1] == "r2"
        app.action_quit_screen()


async def test_rules_refresh_survives_a_shifted_snapshot() -> None:
    """The rules rebuild pins the shifted case the third review
    proved (a row above the focus leaving): the focused id keeps `x`
    on the rule the operator sees, and a focused rule that left
    falls to the top."""
    factory = FakeFactory([FakeWS([rules_frame()]), FakeWS([])])
    app, seams = make_app(factory)
    async with app.run_test() as pilot:
        await pilot.press("r")
        await wait_for(lambda: rules_children(app) == 2)
        await pilot.press("down")  # d1
        await pilot.pause()
        assert rules_focus(app) == "d1"
        # a1 revoked + a new deny e1: d1 shifts to the top.
        app.controller.apply_frame(
            json.dumps(
                {
                    "event": "egress.rules",
                    "data": {
                        "workspace_id": "ws-dev",
                        "mode": "interactive",
                        "allow_list": [],
                        "allowed": [],
                        "denied": [
                            {
                                "id": "d1",
                                "dest_host": "203.0.113.7",
                                "dest_port": 0,
                                "decision": "denied",
                                "duration": "forever",
                                "decided_at": 201.0,
                                "decided_by": "token",
                            },
                            {
                                "id": "e1",
                                "dest_host": "198.51.100.9",
                                "dest_port": 8443,
                                "decision": "denied",
                                "duration": "5m",
                                "decided_at": 205.0,
                                "decided_by": "token",
                            },
                        ],
                    },
                }
            )
        )
        app.safe_repaint()
        await wait_for(lambda: rules_focus(app) == "d1")
        await press_until(pilot, "x", lambda: len(seams["revoked"]) == 1)
        assert seams["revoked"] == [("ws-dev", "d1")]  # still the right rule
        # And the focused rule leaving falls to the top.
        app.controller.apply_frame(
            json.dumps(
                {
                    "event": "egress.rules",
                    "data": {
                        "workspace_id": "ws-dev",
                        "mode": "interactive",
                        "allow_list": [],
                        "allowed": [],
                        "denied": [
                            {
                                "id": "e1",
                                "dest_host": "198.51.100.9",
                                "dest_port": 8443,
                                "decision": "denied",
                                "duration": "5m",
                                "decided_at": 205.0,
                                "decided_by": "token",
                            }
                        ],
                    },
                }
            )
        )
        app.safe_repaint()
        await wait_for(lambda: rules_focus(app) == "e1")
        app.action_quit_screen()


async def test_the_rules_screen_refreshes_on_frames(monkeypatch) -> None:
    """A fresh egress.rules frame repaints the open rules screen:
    a revoked row leaves on the frame, countdowns tick."""

    factory = FakeFactory([FakeWS([rules_frame()]), FakeWS([])])
    app, seams = make_app(factory)
    async with app.run_test() as pilot:
        await pilot.press("r")
        await wait_for(lambda: rules_children(app) == 2)
        # Revoke succeeds; the frame drops the row; the screen
        # repaints without backing out.
        await pilot.press("x")
        await wait_for(lambda: len(seams["revoked"]) == 1)
        app.controller.apply_frame(
            json.dumps(
                {
                    "event": "egress.rules",
                    "data": {
                        "workspace_id": "ws-dev",
                        "mode": "interactive",
                        "allow_list": [".debian.org"],
                        "allowed": [],
                        "denied": [],
                    },
                }
            )
        )
        app.safe_repaint()
        await wait_for(lambda: rules_children(app) == 0)
        app.action_quit_screen()


async def test_verdict_keys_do_not_bleed_from_the_rules_screen() -> None:
    factory = FakeFactory([FakeWS([request_frame("r1")]), FakeWS([])])
    app, seams = make_app(factory)
    async with app.run_test() as pilot:
        await wait_for(lambda: app.query_one("#requests").children)
        await pilot.press("r")
        await wait_for(lambda: type(app.screen).__name__ == "RulesScreen")
        await pilot.press("a")
        await pilot.press("d")
        await pilot.press("A")
        await pilot.press("D")
        await pilot.pause()
        assert seams["decided"] == []  # nothing decided behind the screen
        app.action_quit_screen()


async def test_the_events_screen() -> None:
    """`e` opens the interceptor audit screen (#201): rows newest
    first, the sighting row highlighted with its marker, arrows move
    the list, and `r` returns to the queue."""
    frames = [
        secret_frame("mint", dests=["api.example"], ts=100.0),
        secret_frame("swap", host="api.example", ts=101.0),
        secret_frame("sighting", host="evil.example", ts=102.0),
    ]
    factory = FakeFactory([FakeWS(frames), FakeWS([])])
    app, _ = make_app(factory)
    async with app.run_test() as pilot:
        await wait_for(lambda: len(app.controller.events) == 3)
        await pilot.press("e")
        await wait_for(lambda: events_children(app) == 3)
        screen = app.screen
        rows = screen.query_one("#event-rows")
        first, second, third = rows.children
        assert "sighting" in first.classes  # the exfil signal, on top
        line = str(first.query_one(Static).content)
        assert line.startswith("! ") and "sighting" in line
        assert "evil.example" in line
        assert "swap" in str(second.query_one(Static).content)
        assert "mint" in str(third.query_one(Static).content)
        assert screen.query_one("#events-note") is not None
        await pilot.press("down")  # arrows move the list, no trap
        await pilot.pause()
        assert rows.index == 1
        depth = len(app.screen_stack)
        await pilot.press("e")  # `e` again returns, not stacks
        await pilot.pause()
        assert type(app.screen).__name__ != "EventsScreen"
        assert len(app.screen_stack) == depth - 1
        await pilot.press("r")  # r from the queue opens the rules screen
        await wait_for(lambda: type(app.screen).__name__ == "RulesScreen")
        await pilot.press("r")  # and r there returns — same toggle shape
        await wait_for(lambda: type(app.screen).__name__ != "RulesScreen")
        app.action_quit_screen()


async def test_the_events_screen_refreshes_and_keeps_focus() -> None:
    """An event landing while the screen is open appears on top,
    and the focused row keeps its seq — a repaint must not move a
    row out from under a reading operator."""
    factory = FakeFactory(
        [FakeWS([secret_frame("swap", host="a.example")]), FakeWS([])]
    )
    app, _ = make_app(factory)
    async with app.run_test() as pilot:
        await wait_for(lambda: len(app.controller.events) == 1)
        await pilot.press("e")
        from msks.client.tui.consent_app import EventsScreen, focused_event_id

        def settled_on(target: int, count: int) -> bool:
            try:
                rows = app.screen.query_one("#event-rows")
                return (
                    len(rows.children) == count
                    and focused_event_id(rows) == target
                )
            except Exception:
                return False

        await wait_for(lambda: settled_on(1, 1))
        await pilot.pause()  # let the first flight settle its focus
        app.controller.apply_frame(
            secret_frame("sighting", host="evil.example")
        )
        app.safe_repaint()
        await wait_for(lambda: settled_on(1, 2))
        rows = app.screen.query_one("#event-rows")
        assert rows.children[0].event_seq == 2  # newest first
        assert isinstance(app.screen, EventsScreen)
        app.action_quit_screen()


async def test_verdict_keys_do_not_bleed_from_the_events_screen() -> None:
    factory = FakeFactory([FakeWS([request_frame("r1")]), FakeWS([])])
    app, seams = make_app(factory)
    async with app.run_test() as pilot:
        await wait_for(lambda: app.query_one("#requests").children)
        await pilot.press("e")
        await wait_for(lambda: type(app.screen).__name__ == "EventsScreen")
        await pilot.press("a")
        await pilot.press("d")
        await pilot.press("A")
        await pilot.press("D")
        await pilot.pause()
        assert seams["decided"] == []  # nothing decided behind the screen
        app.action_quit_screen()


async def test_a_sighting_flashes_on_the_queue_screen() -> None:
    """The exfil signal surfaces wherever the operator is: a
    sighting frame takes the queue's status line (#201)."""
    factory = FakeFactory(
        [
            FakeWS(
                [
                    request_frame("r1"),
                    secret_frame("sighting", host="evil.example"),
                ]
            ),
            FakeWS([]),
        ]
    )
    app, _ = make_app(factory)
    async with app.run_test():
        await wait_for(lambda: queue_children(app) == 1)
        await wait_for(lambda: "! sighting:" in status_line(app))
        app.action_quit_screen()


async def test_a_foreign_sighting_never_flashes() -> None:
    """Another workspace's sighting plants nothing (#280 rule,
    carried to secret events): the log stays empty and the status
    line stays quiet — a foreign sighting flashing here would be a
    false exfil alarm."""
    factory = FakeFactory(
        [
            FakeWS(
                [
                    request_frame("r1"),
                    json.dumps(
                        {
                            "event": "secret.sighting",
                            "data": {
                                "workspace_id": "ws-other",
                                "name": "api",
                                "host": "evil.example",
                            },
                        }
                    ),
                ]
            ),
            FakeWS([]),
        ]
    )
    app, _ = make_app(factory)
    async with app.run_test() as pilot:
        await wait_for(lambda: queue_children(app) == 1)
        await pilot.pause()
        await pilot.pause()
        assert app.controller.events == []
        assert "! sighting:" not in status_line(app)
        app.action_quit_screen()


async def test_the_events_screen_skips_rebuilds_when_unchanged() -> None:
    """The per-tick repaint costs the events screen a fingerprint
    compare, not a list swap: an unchanged log takes no rebuild, a
    new event takes its change-driven rebuild, and the gate closes
    behind it (a tick landing mid-rebuild may re-arm the in-flight
    loop — that is OneFlight's mechanism, not this test's subject —
    so the settled assertion is stability, not a call count)."""
    factory = FakeFactory(
        [FakeWS([secret_frame("swap", host="a.example")]), FakeWS([])]
    )
    app, _ = make_app(factory)
    async with app.run_test() as pilot:
        await wait_for(lambda: len(app.controller.events) == 1)
        await pilot.press("e")
        await wait_for(lambda: events_children(app) == 1)
        await pilot.pause()  # the first flight settles its paint
        screen = app.screen
        calls: list[int] = []
        real = screen.rebuild_rows

        async def counting() -> None:
            calls.append(1)
            await real()

        screen.rebuild_rows = counting
        app.safe_repaint()  # nothing changed: no rebuild
        await pilot.pause()
        await pilot.pause()
        assert calls == []
        app.controller.apply_frame(secret_frame("mint", dests=["api.example"]))
        app.safe_repaint()  # the log moved: the change rebuild runs
        await wait_for(lambda: events_children(app) == 2)
        await wait_for(lambda: screen._built == screen.log_fingerprint())
        settled = len(calls)
        assert settled >= 1  # the change drove its rebuild
        await pilot.pause()
        await pilot.pause()
        await asyncio.sleep(0.05)  # a tick here must add nothing
        assert len(calls) == settled  # the gate closed behind the paint
        app.action_quit_screen()


async def test_events_rebuild_self_heals_without_an_old_list() -> None:
    """The events twin of the queue's mid-swap heal: a rebuild with
    no #event-rows to remove (a died-mid-swap predecessor) mounts
    the fresh list anew."""
    from msks.client.tui.consent_app import EventsScreen

    factory = FakeFactory(
        [FakeWS([secret_frame("swap", host="a.example")]), FakeWS([])]
    )
    app, _ = make_app(factory)
    async with app.run_test() as pilot:
        await wait_for(lambda: len(app.controller.events) == 1)
        await pilot.press("e")
        await wait_for(lambda: events_children(app) == 1)
        screen = app.screen

        async def die_mid_swap():
            old = screen.query_one("#event-rows")
            await old.remove()
            raise RuntimeError("died before the mount")

        screen.rebuild_rows = die_mid_swap
        screen.schedule_refresh()
        await wait_for(lambda: events_children(app) == -1)  # gone
        screen.rebuild_rows = EventsScreen.rebuild_rows.__get__(screen)
        screen.schedule_refresh()
        await wait_for(lambda: events_children(app) == 1)  # healed
        app.action_quit_screen()


async def test_rules_refresh_preserves_the_focused_rule() -> None:
    """A per-tick repaint must not move `x`'s target: the focused
    rule id survives the clear+rebuild."""
    factory = FakeFactory([FakeWS([rules_frame()]), FakeWS([])])
    app, seams = make_app(factory)
    async with app.run_test() as pilot:
        await pilot.press("r")
        await wait_for(lambda: rules_children(app) == 2)
        await pilot.press("down")  # d1, the denied row
        await pilot.pause()
        assert rules_focus(app) == "d1"
        app.safe_repaint()  # the tick's repaint
        await wait_for(lambda: rules_focus(app) == "d1")
        await press_until(pilot, "x", lambda: len(seams["revoked"]) == 1)
        assert seams["revoked"] == [("ws-dev", "d1")]  # the right rule
        app.action_quit_screen()


async def test_the_picker_decides_the_hold_it_opened_on() -> None:
    """The focused row resolving mid-pick must not redirect Enter's
    verdict to the neighbor that inherits focus."""
    factory = FakeFactory([FakeWS([request_frame("r1")]), FakeWS([])])
    app, seams = make_app(factory)
    async with app.run_test() as pilot:
        await wait_for(lambda: queue_children(app) == 1)
        factory.made[0].push(request_frame("r2"))
        await wait_for(lambda: queue_children(app) == 2)
        await press_until(
            pilot, "A", lambda: type(app.screen).__name__ == "DurationScreen"
        )
        # The hold the picker opened on resolves mid-pick.
        app.controller.apply_frame(
            json.dumps(
                {
                    "event": "egress.resolved",
                    "data": {"request_id": "r1", "decision": "allowed"},
                }
            )
        )
        app.safe_repaint()
        await pilot.pause()
        await pilot.press("enter")
        # The verdict goes to the hold the picker OPENED on — the
        # resolved r1, never retargeted to r2. The server is the
        # source of truth for staleness (it 404s resolved ids).
        await wait_for(lambda: len(seams["decided"]) == 1)
        assert seams["decided"][0][1] == "r1"
        app.action_quit_screen()


def test_run_consent_tui_fails_cleanly_without_a_token(
    monkeypatch, capsys
) -> None:
    """A missing MSKSC_TOKEN exits before the screen draws — the
    one readable line every sibling prints, not a teardown."""

    monkeypatch.delenv("MSKSC_TOKEN", raising=False)
    monkeypatch.delenv("MSKSC_URL", raising=False)
    monkeypatch.setenv("MSKSC_URL", "https://d")
    with pytest.raises(SystemExit, match="MSKSC_TOKEN"):
        consent_app.run_consent_tui("ws-dev")


async def test_a_connect_failure_names_itself_once() -> None:
    """The first dial failure flashes its cause; identical retries
    stay quiet."""
    app, _ = make_app(FakeFactory([]))
    calls = {"n": 0}

    class Failing:
        async def __aenter__(self):
            calls["n"] += 1
            raise OSError("dial refused")

        async def __aexit__(self, *exc):
            return None

    app._ws_factory = lambda: Failing()
    async with app.run_test() as pilot:
        await wait_for(lambda: calls["n"] >= 2)
        await pilot.pause()
        assert "connect failed: dial refused" in status_line(app)
        assert calls["n"] >= 2  # retried; the identical cause stays quiet
        app.action_quit_screen()


def test_ws_connect_kwargs_and_shared_ssl(monkeypatch) -> None:
    from msks.client.tui.consent_app import ws_connect_kwargs

    kwargs = ws_connect_kwargs("https://d:1", "tok", "ctx")
    assert kwargs["uri"].endswith("events?token=tok")
    assert kwargs["ssl"] == "ctx"
    plain = ws_connect_kwargs("http://d:1", "tok", "ctx")
    assert plain["ssl"] is None
    monkeypatch.setattr(consent_app, "_SHARED_SSL", [None])
    first = consent_app.shared_ssl()
    assert consent_app.shared_ssl() is first  # one build, then cached


async def test_rebuild_re_arms_when_frames_land_mid_flight() -> None:
    """A frame arriving while a rebuild is in flight is applied the
    moment that flight lands (the pending re-arm), not on the next
    tick."""
    factory = FakeFactory([FakeWS([rules_frame()]), FakeWS([])])
    app, _ = make_app(factory)
    async with app.run_test() as pilot:
        await pilot.press("r")
        await wait_for(lambda: rules_children(app) == 2)
        screen = app.screen
        calls = []

        async def counting():
            calls.append(1)
            if len(calls) == 1:  # a frame lands mid-first-rebuild
                screen.schedule_refresh()

        screen.rebuild_rows = counting
        screen.schedule_refresh()
        await pilot.pause()
        await pilot.pause()
        assert len(calls) == 2  # the re-arm looped, then settled
        assert screen.rebuilds.scheduled is False
        app.action_quit_screen()


async def test_schedule_rebuild_pending_rearm() -> None:
    """The queue's rebuild re-arms the same way: a request landing
    mid-rebuild loops the flight instead of waiting a tick."""
    factory = FakeFactory([FakeWS([request_frame("r1")]), FakeWS([])])
    app, _ = make_app(factory)
    async with app.run_test() as pilot:
        await wait_for(lambda: queue_children(app) == 1)
        calls = []

        async def counting(ordered):
            calls.append(1)
            if len(calls) == 1:  # a request lands mid-first-rebuild
                app.schedule_rebuild()

        app.rebuild_queue = counting
        app.schedule_rebuild()
        await pilot.pause()
        await pilot.pause()
        assert len(calls) == 2
        assert app.rebuilds.scheduled is False
        app.action_quit_screen()


async def test_queue_rows_returns_none_in_a_swap_window() -> None:
    """queue_rows reads a mid-swap window as no list (None), the
    queue paths' signal for "nothing focused right now"."""
    factory = FakeFactory([FakeWS([request_frame("r1")]), FakeWS([])])
    app, _ = make_app(factory)
    async with app.run_test():
        await wait_for(lambda: queue_children(app) == 1)
        real_query = app.query_one

        def boom(*a, **k):
            raise RuntimeError("swap window")

        app.query_one = boom
        assert app.queue_rows() is None
        app.query_one = real_query
        assert app.queue_rows() is not None
        app.action_quit_screen()


async def test_action_revoke_in_a_swap_window() -> None:
    """x pressed while the rules list is mid-swap revokes nothing and
    raises nothing."""
    factory = FakeFactory([FakeWS([rules_frame()]), FakeWS([])])
    app, seams = make_app(factory)
    async with app.run_test() as pilot:
        await pilot.press("r")
        await wait_for(lambda: rules_children(app) == 2)
        screen = app.screen
        # Simulate the swap window: the query for #rule-rows fails.
        screen.query_one = lambda *a, **k: (_ for _ in ()).throw(
            RuntimeError("swap window")
        )
        await screen.action_revoke()
        assert seams["revoked"] == []
        app.action_quit_screen()


async def test_a_dying_rebuild_logs_and_re_arms() -> None:
    """A rebuild that raises mid-swap is logged and never wedges the
    single-flight flag: the next tick re-arms."""
    factory = FakeFactory([FakeWS([request_frame("r1")]), FakeWS([])])
    app, _ = make_app(factory)
    async with app.run_test() as pilot:
        await wait_for(lambda: queue_children(app) == 1)

        # Make the next rebuild blow up, arm it, and let it die.
        async def boom(ordered):
            raise RuntimeError("swap exploded")

        app.rebuild_queue = boom
        app.schedule_rebuild()
        await pilot.pause()
        assert app.rebuilds.scheduled is False  # finally: cleared
        # A dying rules rebuild clears its flag too.
        factory_rules = FakeFactory([FakeWS([rules_frame()]), FakeWS([])])
        app2, _ = make_app(factory_rules)
        async with app2.run_test() as pilot2:
            await pilot2.press("r")
            await wait_for(lambda: rules_children(app2) == 2)
            screen = app2.screen

            async def rules_boom():
                raise RuntimeError("rules swap exploded")

            screen.rebuild_rows = rules_boom
            screen.schedule_refresh()
            await pilot2.pause()
            assert screen.rebuilds.scheduled is False
            app2.action_quit_screen()
        app.action_quit_screen()


async def test_pick_duration_and_decide_in_a_swap_window() -> None:
    """A key pressed while the queue list is mid-swap reads as
    nothing focused: the picker flashes instead of deciding the
    neighbor."""

    factory = FakeFactory([FakeWS([request_frame("r1")]), FakeWS([])])
    app, seams = make_app(factory)
    async with app.run_test():
        await wait_for(lambda: queue_children(app) == 1)
        # Simulate the swap window: no list to read focus from.
        app.queue_rows = lambda: None
        await app.action_allow_duration()
        assert type(app.screen).__name__ != "DurationScreen"
        assert "no hold focused" in status_line(app)
        await app.decide_focused("allow", "forever")  # also nothing focused
        assert "no hold focused" in status_line(app)
        assert seams["decided"] == []
        app.action_quit_screen()


async def test_paint_rows_skips_a_row_that_left_mid_tick() -> None:
    """The in-place repaint (same-set tick) skips an ordered request
    whose row is already gone — the loop moves to the next item."""

    class Ghost:
        id = "gone"

    factory = FakeFactory([FakeWS([request_frame("r1")]), FakeWS([])])
    app, _ = make_app(factory)
    async with app.run_test():
        await wait_for(lambda: queue_children(app) == 1)
        rows = app.query_one("#requests")
        live = app.controller.ordered()[0]
        app.repaint_countdowns(
            rows, [Ghost(), live]
        )  # ghost skipped, live repainted
        app.action_quit_screen()


async def test_a_mid_swap_death_self_heals_on_the_next_tick() -> None:
    """The pass-4 wedge: a rebuild dying between the old list's
    removal and the fresh one's mount left no #requests, and every
    later tick died on the missing query — a blank UI until restart.
    Now the missing list schedules a rebuild and the queue comes
    back."""
    factory = FakeFactory([FakeWS([request_frame("r1")]), FakeWS([])])
    app, _ = make_app(factory)
    async with app.run_test():
        await wait_for(lambda: queue_children(app) == 1)

        async def die_mid_swap(ordered):
            old = app.query_one("#requests")
            await old.remove()  # the swap window opens…
            raise RuntimeError("died before the mount")

        app.rebuild_queue = die_mid_swap
        app.schedule_rebuild()
        await wait_for(lambda: queue_children(app) == -1)  # gone
        # The real rebuild_queue returns for the next tick.
        del app.rebuild_queue
        from msks.client.tui.consent_app import ConsentDeciderApp

        app.rebuild_queue = ConsentDeciderApp.rebuild_queue.__get__(app)
        app.safe_repaint()  # a tick
        await wait_for(lambda: queue_children(app) == 1)  # healed
        app.action_quit_screen()


async def test_registration_rejection_exits() -> None:
    """An egress.decider_rejected frame stops the loop and flashes
    the reason — no silent promptless wait on a typo'd workspace."""
    factory = FakeFactory([FakeWS([]), FakeWS([])])
    app, _ = make_app(factory)
    async with app.run_test():
        await wait_for(lambda: len(factory.made) == 1)
        factory.made[0].push(
            json.dumps(
                {
                    "event": "egress.decider_rejected",
                    "data": {"reason": "unknown workspace"},
                }
            )
        )
        await wait_for(lambda: app._stop is True)
        await wait_for(
            lambda: (
                "registration rejected: unknown workspace" in status_line(app)
            )
        )
        app.action_quit_screen()


async def test_backoff_resets_after_a_healthy_connection() -> None:
    """A connection that reached serve_connection was healthy: the
    drop after it starts the backoff ladder over (attempt 0), it
    does not climb for a lifetime of cumulative disconnects."""
    app, _ = make_app(FakeFactory([]))
    outcomes = iter([(False, True), (True, False), (False, False)])
    ladder = {"attempt": None, "n": 0}

    async def scripted_pump() -> tuple[bool, bool]:
        outcome = next(outcomes)
        if outcome == (False, False):  # after the reset's backoff
            app._stop = True
        return outcome

    import msks.client.tui.consent_app as consent_app_mod

    real_backoff = consent_app_mod.backoff

    def spying_backoff(delays, attempt):
        ladder["n"] += 1
        ladder["attempt"] = attempt
        return real_backoff(delays, attempt)

    from unittest.mock import patch

    with (
        patch.object(app, "pump_one", scripted_pump),
        patch.object(consent_app_mod, "REFUSED_RETRY_INTERVAL", 0.01),
        patch.object(consent_app_mod, "backoff", spying_backoff),
    ):
        await app.ws_loop()
    # The refusal sleeps its fixed interval (no backoff call); the
    # healthy connection's drop then backs off from attempt 0 — the
    # ladder restarted, not climbed to the cap.
    assert ladder["n"] == 1 and ladder["attempt"] == 0


async def test_rules_rebuild_self_heals_without_an_old_list() -> None:
    """The rules twin of the queue's mid-swap heal: a rebuild with no
    #rule-rows to remove (a died-mid-swap predecessor) mounts the
    fresh list anew."""
    factory = FakeFactory([FakeWS([rules_frame()]), FakeWS([])])
    app, _ = make_app(factory)
    async with app.run_test() as pilot:
        await pilot.press("r")
        await wait_for(lambda: rules_children(app) == 2)
        screen = app.screen

        async def die_mid_swap():
            old = screen.query_one("#rule-rows")
            await old.remove()
            raise RuntimeError("died before the mount")

        screen.rebuild_rows = die_mid_swap
        screen.schedule_refresh()
        await wait_for(lambda: rules_children(app) == -1)  # gone
        from msks.client.tui.consent_app import RulesScreen

        screen.rebuild_rows = RulesScreen.rebuild_rows.__get__(screen)
        screen.schedule_refresh()
        await wait_for(lambda: rules_children(app) == 2)  # healed
        app.action_quit_screen()


def test_schedule_rebuild_on_a_stopped_app_is_a_noop() -> None:
    """A rebuild armed after (or during) teardown runs zero loop
    iterations and clears its flag — no zombie flight, no
    traceback."""

    async def scenario() -> None:
        factory = FakeFactory([FakeWS([request_frame("r1")]), FakeWS([])])
        app, _ = make_app(factory)  # never run_test: not running
        app.schedule_rebuild()
        await asyncio.sleep(0.05)  # the flight lands in the finally
        assert app.rebuilds.scheduled is False

    asyncio.run(scenario())


async def test_a_rules_flight_settling_after_teardown() -> None:
    """The rules flight when the app has stopped: the while check
    exits with zero iterations (no zombie flight), and a rebuild
    that raises anyway logs nothing — teardown unmounted the tree
    under it, which is not a bug."""

    from msks.client.tui.consent_app import RulesScreen

    class StoppedApp:
        is_running = False

    class FlippingApp:
        """Reads running once (the loop check) then stopped (the
        except check): teardown happened mid-flight."""

        def __init__(self):
            self.reads = 0

        @property
        def is_running(self):
            self.reads += 1
            return self.reads == 1

    state: dict = {"app": StoppedApp()}

    def app_prop(_self):
        return state["app"]

    factory = FakeFactory([FakeWS([rules_frame()]), FakeWS([])])
    app, _ = make_app(factory)
    async with app.run_test() as pilot:
        await pilot.press("r")
        await wait_for(lambda: rules_children(app) == 2)
        screen = app.screen
        real_app_prop = RulesScreen.app
        # A stopped app, patched in for exactly one scheduler slice
        # per flight (the flight's own first step) — never long
        # enough for a render to see the stub.
        RulesScreen.app = property(app_prop)
        screen.schedule_refresh()  # stopped: zero iterations
        await asyncio.sleep(0)  # the flight runs to its finally
        RulesScreen.app = real_app_prop
        assert screen.rebuilds.scheduled is False

        async def dying():
            raise RuntimeError("died after teardown")

        flip = FlippingApp()
        state["app"] = flip
        RulesScreen.app = property(app_prop)  # re-patch for this arm
        screen.rebuild_rows = dying
        screen.schedule_refresh()  # raises into a quiet except
        await asyncio.sleep(0)  # the flight runs to its finally
        await asyncio.sleep(0)  # …and the except arm settles
        RulesScreen.app = real_app_prop
        assert screen.rebuilds.scheduled is False
        assert flip.reads == 2  # while (True), except (False)
        app.action_quit_screen()


async def test_a_flight_dying_at_teardown_stays_quiet() -> None:
    """A rebuild that raises after the app stopped logs nothing (the
    except arm's is_running check): teardown unmounts the tree under
    a mid-swap flight and that is not a bug."""
    factory = FakeFactory([FakeWS([request_frame("r1")]), FakeWS([])])
    app, _ = make_app(factory)
    async with app.run_test():
        await wait_for(lambda: queue_children(app) == 1)
        gate = asyncio.Event()

        async def gated(ordered):
            await gate.wait()
            raise RuntimeError("died after teardown")

        app.rebuild_queue = gated
        app.schedule_rebuild()  # the flight parks inside the gate
    # The app stopped (the with-block exited); release the flight.
    gate.set()
    await asyncio.sleep(0.05)
    assert app.rebuilds.scheduled is False


# --- the mode picker (#280) -------------------------------------------


def empty_rules_frame() -> str:
    """A rules snapshot with nothing effectively allowed: an empty
    allowlist under a mode that holds nothing allowed."""
    return json.dumps(
        {
            "event": "egress.rules",
            "data": {
                "workspace_id": "ws-dev",
                "mode": "allow",
                "allow_list": [],
                "allowed": [],
                "denied": [],
            },
        }
    )


def mode_frame(mode: str) -> str:
    """One rules frame carrying only the mode — the refresh a live
    mode switch sends (#301)."""
    return json.dumps(
        {
            "event": "egress.rules",
            "data": {
                "workspace_id": "ws-dev",
                "mode": mode,
                "allow_list": [],
                "allowed": [],
                "denied": [],
            },
        }
    )


def same_rows_frame(mode: str) -> str:
    """The fixture snapshot's rows under another mode — the
    same-membership refresh a mode switch sends while verdicts
    stand."""
    return json.dumps(
        {
            "event": "egress.rules",
            "data": {
                "workspace_id": "ws-dev",
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


async def test_the_mode_picker_opens_from_the_queue() -> None:
    """`m` on the queue opens the picker directly (#301): the
    operator watching holds escalates or relaxes the posture
    without detouring through the rules screen; the pick goes
    through the seam, and `m` again under the open picker stacks
    nothing (the shadow rule). The picker also opens over the
    events screen — every screen carries the toggle."""
    factory = FakeFactory([FakeWS([rules_frame()]), FakeWS([])])
    app, seams = make_app(factory)
    async with app.run_test() as pilot:
        await wait_for(lambda: "mode interactive" in status_line(app))
        await pilot.press("m")
        await wait_for(lambda: type(app.screen).__name__ == "ModeScreen")
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
        await wait_for(lambda: len(seams["modes"]) == 1)
        assert seams["modes"] == [("ws-dev", "static", False)]
        await pilot.press("e")
        await wait_for(lambda: type(app.screen).__name__ == "EventsScreen")
        await pilot.press("m")
        await wait_for(lambda: type(app.screen).__name__ == "ModeScreen")
        await pilot.press("escape")
        await wait_for(lambda: type(app.screen).__name__ == "EventsScreen")
        app.action_quit_screen()


async def test_the_status_line_shows_the_mode() -> None:
    """The queue's status line names the current mode at all times
    (#301): `—` until the first rules frame lands, the snapshot's
    mode after, and a mode switch's refreshed frame repaints it."""
    factory = FakeFactory([FakeWS([]), FakeWS([])])
    app, _ = make_app(factory)
    async with app.run_test() as pilot:
        await pilot.pause()
        assert "mode —" in status_line(app)
        app.controller.apply_frame(rules_frame())
        app.safe_repaint()
        await wait_for(lambda: "mode interactive" in status_line(app))
        app.controller.apply_frame(mode_frame("allow"))
        app.safe_repaint()
        await wait_for(lambda: "mode allow" in status_line(app))
        app.action_quit_screen()


async def test_m_stays_inert_under_a_modal() -> None:
    """`m` under the duration picker, the confirmation, and the
    picker itself opens nothing (#301, the shadow rule): a mode
    picker stacked under a modal would be a screen nobody can see
    deciding a posture."""
    factory = FakeFactory(
        [FakeWS([request_frame("r1"), empty_rules_frame()]), FakeWS([])]
    )
    app, _ = make_app(factory)
    async with app.run_test() as pilot:
        await wait_for(lambda: queue_children(app) == 1)
        await pilot.press("A")
        await wait_for(lambda: type(app.screen).__name__ == "DurationScreen")
        await pilot.press("m")
        await pilot.pause()
        assert not [
            s for s in app.screen_stack if type(s).__name__ == "ModeScreen"
        ]
        await pilot.press("escape")
        await wait_for(lambda: type(app.screen).__name__ != "DurationScreen")
        # The empty-static confirmation: m under it stays inert too.
        await pilot.press("m")
        await wait_for(lambda: type(app.screen).__name__ == "ModeScreen")
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
        app.action_quit_screen()


async def test_the_rules_screen_repaints_in_place() -> None:
    """An unchanged row set takes an in-place countdown repaint, not
    the remove-and-mount swap — the once-a-second flash #301
    reports. The list keeps its identity (and with it the focus and
    indexes) across a tick and across a same-membership frame; a
    membership change still swaps in a fresh list."""
    factory = FakeFactory([FakeWS([rules_frame()]), FakeWS([])])
    app, _ = make_app(factory)
    async with app.run_test() as pilot:
        await pilot.press("r")
        await wait_for(lambda: rules_children(app) == 2)
        rows = app.screen.query_one("#rule-rows")
        await pilot.press("down")  # focus d1
        await pilot.pause()
        # A tick on the same snapshot: in place, identity kept.
        app.safe_repaint()
        await pilot.pause()
        current = app.screen.query_one("#rule-rows")
        assert current is rows
        assert rules_focus(app) == "d1"
        # The survivors' countdown text follows the clock (the
        # repaint's other half — a no-op repaint would freeze it):
        # a1's 5m verdict, decided_at 200, reads off the controller
        # clock the test now owns.
        clock = {"now": 300.0}
        app.controller._clock = lambda: clock["now"]

        def row_text(i: int) -> str:
            return str(
                app.screen.query_one("#rule-rows")
                .children[i]
                .query_one(Static)
                .content
            )

        app.safe_repaint()
        await wait_for(lambda: "3m left" in row_text(0))  # 500-300
        clock["now"] = 360.0
        app.safe_repaint()
        await wait_for(lambda: "2m left" in row_text(0))  # 500-360
        assert app.screen.query_one("#rule-rows") is rows  # still in place
        # A same-membership frame (a mode switch lands in it): the
        # header follows the mode, the rows never swap.
        app.controller.apply_frame(same_rows_frame("allow"))
        app.safe_repaint()
        await wait_for(
            lambda: (
                "mode allow" in str(app.screen.query_one("#allowlist").content)
            )
        )
        assert app.screen.query_one("#rule-rows") is rows
        assert rules_focus(app) == "d1"
        # A membership change (a1 revoked): the fresh-list swap.
        app.controller.apply_frame(mode_frame("allow"))
        app.safe_repaint()
        await wait_for(lambda: rules_children(app) == 0)
        assert app.screen.query_one("#rule-rows") is not rows
        app.action_quit_screen()


async def test_the_events_screen_shows_the_mode() -> None:
    """The audit screen's header names the mode (#301 — visible on
    every screen), and a mode switch's frame repaints it without an
    event landing: the repaint fingerprint carries the mode."""
    factory = FakeFactory([FakeWS([rules_frame()]), FakeWS([])])
    app, _ = make_app(factory)
    async with app.run_test() as pilot:
        await wait_for(lambda: app.controller.rules is not None)
        await pilot.press("e")
        await wait_for(
            lambda: (
                "mode interactive"
                in str(app.screen.query_one("#events-note").content)
            )
        )
        app.controller.apply_frame(mode_frame("allow"))
        app.safe_repaint()
        await wait_for(
            lambda: (
                "mode allow"
                in str(app.screen.query_one("#events-note").content)
            )
        )
        app.action_quit_screen()


async def test_mode_picker_switches_through_the_seam(monkeypatch) -> None:
    """`m` opens the picker over the rules screen; a picked mode
    goes through the set_mode seam with no confirmation when
    something is effectively allowed (the allowlist is enough)."""
    factory = FakeFactory([FakeWS([rules_frame()]), FakeWS([])])
    app, seams = make_app(factory)
    async with app.run_test() as pilot:
        await pilot.press("r")
        await wait_for(lambda: rules_children(app) == 2)
        await pilot.press("m")
        await pilot.press("up")  # interactive -> static
        await pilot.press("enter")
        await wait_for(lambda: len(seams["modes"]) == 1)
        assert seams["modes"] == [("ws-dev", "static", False)]
        app.action_quit_screen()


async def test_mode_picker_confirms_an_empty_static_switch(
    monkeypatch,
) -> None:
    """static with nothing effectively allowed asks first: a no
    decides nothing, a yes sends the confirmed switch
    (confirm_empty — the daemon's escape from its own refusal)."""
    factory = FakeFactory([FakeWS([empty_rules_frame()]), FakeWS([])])
    app, seams = make_app(factory)
    async with app.run_test() as pilot:
        await pilot.press("r")
        await wait_for(lambda: rules_children(app) == 0)
        await pilot.press("m")
        await pilot.press("down")  # allow -> static
        await pilot.press("enter")
        await pilot.press("n")  # the confirmation: declined
        await wait_for(
            lambda: not isinstance(app.screen, consent_app.ConfirmScreen)
        )
        assert seams["modes"] == []
        await pilot.press("m")
        await pilot.press("down")
        await pilot.press("enter")
        await pilot.press("y")  # the confirmation: taken
        await wait_for(lambda: len(seams["modes"]) == 1)
        assert seams["modes"] == [("ws-dev", "static", True)]
        app.action_quit_screen()


async def test_mode_picker_escape_cancels(monkeypatch) -> None:
    """Escape closes the picker deciding nothing."""
    factory = FakeFactory([FakeWS([rules_frame()]), FakeWS([])])
    app, seams = make_app(factory)
    async with app.run_test() as pilot:
        await pilot.press("r")
        await wait_for(lambda: rules_children(app) == 2)
        await pilot.press("m")
        await pilot.press("escape")
        await wait_for(
            lambda: not isinstance(app.screen, consent_app.ModeScreen)
        )
        assert seams["modes"] == []
        app.action_quit_screen()


async def test_mode_switch_failure_flashes(monkeypatch) -> None:
    """A failed switch (the daemon's named refusal among them)
    flashes on the status line, never crashes the app."""
    factory = FakeFactory([FakeWS([rules_frame()]), FakeWS([])])
    app, seams = make_app(factory)
    seams["fail_mode"] = True
    async with app.run_test() as pilot:
        await pilot.press("r")
        await wait_for(lambda: rules_children(app) == 2)
        await pilot.press("m")
        await pilot.press("up")  # interactive -> static
        await pilot.press("enter")
        await wait_for(lambda: len(seams["modes"]) == 1)
        await wait_for(lambda: "mode switch failed" in app_status(app))
        app.action_quit_screen()


def app_status(app) -> str:
    """The status line's current text."""
    return str(app.query_one("#status", Static).content)


async def test_mode_picker_without_a_snapshot_confirms_static() -> None:
    """`m` before any rules frame lands: the picker highlights
    nothing (an unknown current mode), and a static pick with no
    snapshot asks the offline question — nothing effectively
    allowed is the reading of no rules at all."""
    factory = FakeFactory([FakeWS([]), FakeWS([])])
    app, seams = make_app(factory)
    async with app.run_test() as pilot:
        await pilot.press("r")
        await wait_for(lambda: type(app.screen).__name__ == "RulesScreen")
        await pilot.press("m")
        await pilot.press("down")  # allow -> static
        await pilot.press("enter")
        await pilot.press("y")  # the confirmation: taken
        await wait_for(lambda: len(seams["modes"]) == 1)
        assert seams["modes"] == [("ws-dev", "static", True)]
        app.action_quit_screen()


async def test_modal_keys_do_not_reach_the_hidden_queue() -> None:
    """The verdict/quit keys stay inert under a modal (#280
    review, RulesScreen's precedent): `a` decides nothing while
    the picker is open, and `q` closes the modal instead of the
    app."""
    factory = FakeFactory([FakeWS([request_frame("r1")]), FakeWS([])])
    app, seams = make_app(factory)
    async with app.run_test() as pilot:
        await wait_for(lambda: queue_children(app) == 1)
        await pilot.press("r")
        await wait_for(lambda: type(app.screen).__name__ == "RulesScreen")
        await pilot.press("m")
        await wait_for(lambda: type(app.screen).__name__ == "ModeScreen")
        await pilot.press("a")
        await pilot.press("d")
        await pilot.press("e")  # the events screen stays stacked nowhere
        await asyncio.sleep(0.05)
        assert seams["decided"] == []  # the hidden hold stays undecided
        await pilot.press("q")  # the modal's own binding
        await wait_for(lambda: type(app.screen).__name__ == "RulesScreen")
        assert app.is_running  # q closed the modal, not the app
        assert not [
            s for s in app.screen_stack if type(s).__name__ == "EventsScreen"
        ]
        app.action_quit_screen()
