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
from test_consent_tui import request_frame, rules_frame
from textual.widgets import Static


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


async def fake_decide(seams, workspace, request_id, decision, duration):
    seams["decided"].append((workspace, request_id, decision, duration))
    if seams.get("fail_decide"):
        raise RuntimeError("daemon away")


async def fake_revoke(seams, workspace, request_id):
    seams["revoked"].append((workspace, request_id))
    if seams.get("fail_revoke"):
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
        reconnect_delays=(0.01, 0.01, 0.01),
    )
    return app, seams


def held_row(app, index: int) -> str:
    rows = app.query_one("#requests")
    return str(rows.children[index].query_one(Static).content)


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
        await wait_for(lambda: len(app.query_one("#requests").children) == 2)
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
        await wait_for(lambda: len(app.query_one("#requests").children) == 1)
        # d denies the survivor.
        await pilot.press("d")
        await wait_for(lambda: len(seams["decided"]) == 2)
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
        await wait_for(lambda: len(app.controller.pending) == 2)
        await pilot.press("D")
        await wait_for(lambda: type(app.screen).__name__ == "DurationScreen")
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
        await wait_for(
            lambda: len(app.screen.query_one("#rule-rows").children) == 2
        )
        header = str(app.screen.query_one("#allowlist").content)
        assert "mode interactive" in header
        assert ".debian.org" in header
        rows = app.screen.query_one("#rule-rows")
        assert len(rows.children) == 2
        assert "allowed" in str(rows.children[0].query_one(Static).content)
        assert "forever" in str(rows.children[1].query_one(Static).content)
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
    assert await app.pump_one() is False
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
    assert await app2.pump_one() is False
    # A clean server close ends the connection (False, not refused).
    clean = FakeWS()
    await clean.close()
    app3, _ = make_app(FakeFactory([]))
    app3._ws_factory = lambda: Entering(clean)
    assert await app3.pump_one() is False

    # close() swallowing a dead peer.
    class DeadClose(FakeWS):
        async def close(self):
            raise OSError("already gone")

    await consent_app.close_ws(DeadClose())


async def test_ws_loop_cycles_until_stopped(monkeypatch) -> None:
    app, _ = make_app(FakeFactory([]))
    outcomes = iter([True, False])
    stop_after_clean = {"clean": False}

    async def scripted_pump() -> bool:
        outcome = next(outcomes)
        if outcome is False:
            stop_after_clean["clean"] = True
            app._stop = True
        return outcome

    monkeypatch.setattr(app, "pump_one", scripted_pump)
    monkeypatch.setattr(consent_app, "REFUSED_RETRY_INTERVAL", 0.01)
    await app.ws_loop()  # refused -> continue -> clean -> stop-return
    assert stop_after_clean["clean"]
    # Pre-stopped: the loop never pumps.
    app2, _ = make_app(FakeFactory([]))
    app2._stop = True
    pumps = {"n": 0}

    async def counting() -> bool:
        pumps["n"] += 1
        return False

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
    assert calls[0][0] == "POST" and calls[0][1].endswith("/r1")
    assert calls[1][0] == "DELETE"
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
        await wait_for(lambda: app.query_one("#requests").children)
        await pilot.press("a")
        await wait_for(lambda: "decide failed" in status_line(app))
        assert "404" in status_line(app)
        app.action_quit_screen()


async def test_the_rules_screen_refreshes_on_frames(monkeypatch) -> None:
    """A fresh egress.rules frame repaints the open rules screen:
    a revoked row leaves on the frame, countdowns tick."""

    factory = FakeFactory([FakeWS([rules_frame()]), FakeWS([])])
    app, seams = make_app(factory)
    async with app.run_test() as pilot:
        await pilot.press("r")
        await wait_for(
            lambda: len(app.screen.query_one("#rule-rows").children) == 2
        )
        # Revoke succeeds; the frame drops the row; the screen
        # repaints without backing out.
        await pilot.press("x")
        await wait_for(lambda: len(seams["revoked"]) == 1)
        app.controller.apply_frame(
            json.dumps(
                {
                    "event": "egress.rules",
                    "data": {
                        "workspace_id": "ws",
                        "mode": "interactive",
                        "allow_list": [".debian.org"],
                        "allowed": [],
                        "denied": [],
                    },
                }
            )
        )
        app.safe_repaint()
        await wait_for(
            lambda: len(app.screen.query_one("#rule-rows").children) == 0
        )
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


async def test_rules_refresh_preserves_the_focused_rule() -> None:
    """A per-tick repaint must not move `x`'s target: the focused
    rule id survives the clear+rebuild."""
    factory = FakeFactory([FakeWS([rules_frame()]), FakeWS([])])
    app, seams = make_app(factory)
    async with app.run_test() as pilot:
        await pilot.press("r")
        await wait_for(
            lambda: len(app.screen.query_one("#rule-rows").children) == 2
        )
        await pilot.press("down")  # d1, the denied row
        await pilot.pause()
        from msks.client.tui.consent_app import focused_rule_id

        assert focused_rule_id(app.screen.query_one("#rule-rows")) == "d1"
        app.safe_repaint()  # the tick's repaint
        await wait_for(
            lambda: focused_rule_id(app.screen.query_one("#rule-rows")) == "d1"
        )
        await pilot.press("x")
        await wait_for(lambda: len(seams["revoked"]) == 1)
        assert seams["revoked"] == [("ws-dev", "d1")]  # the right rule
        app.action_quit_screen()


async def test_the_picker_decides_the_hold_it_opened_on() -> None:
    """The focused row resolving mid-pick must not redirect Enter's
    verdict to the neighbor that inherits focus."""
    factory = FakeFactory([FakeWS([request_frame("r1")]), FakeWS([])])
    app, seams = make_app(factory)
    factory.made and None
    async with app.run_test() as pilot:
        await wait_for(lambda: app.query_one("#requests").children)
        factory.made[0].push(request_frame("r2"))
        await wait_for(lambda: len(app.controller.pending) == 2)
        await pilot.press("A")
        await wait_for(lambda: type(app.screen).__name__ == "DurationScreen")
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
        await wait_for(lambda: "already resolved" in status_line(app))
        assert seams["decided"] == []  # nothing decided, nobody harmed
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
