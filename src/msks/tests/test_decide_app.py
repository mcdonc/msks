"""The standalone consent-decider app tests (#467).

The websocket is a scripted fake (the link's own ladder runs as
the app's worker), the verdict posts land on a recording seam,
and the popup's tmux edges are recorded subprocess calls — the
suite pins the queue's behavior, the verdict keys, the picker,
and the viewer's show/hide paths, never opening a window.
"""

import asyncio
import json
import re
import subprocess
import time

import pytest
from msks.client import term_popup as tp
from msks.client.tui import decide_app as da
from msks.client.tui.consent_ui import (
    ConfirmScreen,
    DurationScreen,
    ModeScreen,
)
from textual.widgets import OptionList, Static

SOCKET = "msks-ws1-7"
VIEWER = "/dev/pts/3"

#: One row's countdown cell: seconds, however many remain.
COUNTDOWN = re.compile(r"\(\d+s\)")


def rules_frame(
    mode: str = "interactive", allow_list: list[str] | None = None
) -> str:
    """The registration's first frame: the rules view that adopts
    the server's canonical workspace id (#297)."""
    return json.dumps(
        {
            "event": "egress.rules",
            "data": {
                "workspace_id": "ws1",
                "mode": mode,
                "allow_list": allow_list or [],
                "allowed": [],
                "denied": [],
            },
        }
    )


def request_frame(
    rid: str,
    host: str = "api.example",
    port: int = 443,
    requested_at: float = 10.0,
    expires_at: float | None = None,
) -> str:
    """One held request's frame, the daemon's snapshot shape."""
    row: dict = {
        "id": rid,
        "workspace_id": "ws1",
        "dest_host": host,
        "dest_port": port,
        "requested_at": requested_at,
    }
    if expires_at is not None:
        row["expires_at"] = expires_at
    return json.dumps(
        {
            "event": "egress.request",
            "data": {"workspace_id": "ws1", "request": row},
        }
    )


def resolved_frame(rid: str, decision: str = "allowed") -> str:
    """One resolution's frame."""
    return json.dumps(
        {
            "event": "egress.resolved",
            "data": {
                "workspace_id": "ws1",
                "request_id": rid,
                "decision": decision,
            },
        }
    )


def rejected_frame() -> str:
    """The daemon's registration refusal."""
    return json.dumps(
        {
            "event": "egress.decider_rejected",
            "data": {"reason": "unknown workspace"},
        }
    )


class FakeWS:
    """One scripted websocket: recv() yields the shared frame
    list, then parks — frames the test appends later still land,
    and the connection outlives the scenario (a stream that ends
    would take the link's reconnect-and-reset path instead)."""

    def __init__(self, frames: list[str]) -> None:
        self.frames = frames
        self.sent: list[str] = []

    async def send(self, raw: str) -> None:
        self.sent.append(raw)

    async def close(self) -> None:
        return None

    def __aiter__(self):
        return self

    async def __anext__(self) -> str:
        while not self.frames:
            await asyncio.sleep(0.02)
        return self.frames.pop(0)


class FakeConnect:
    """The ws_factory seam: every dial lands a fresh socket over
    the shared frame list."""

    def __init__(self, frames: list[str]) -> None:
        self.frames = frames
        self.sockets: list[FakeWS] = []

    def __call__(self, **kwargs):
        return self

    async def __aenter__(self):
        ws = FakeWS(self.frames)
        self.sockets.append(ws)
        return ws

    async def __aexit__(self, *exc):
        return False


async def until(predicate, timeout: float = 5.0) -> None:
    """Await a condition the app's own scheduling settles, or name
    the wait's failure. A poll that lands mid-mount (a row whose
    text widget has not joined the tree yet) reads as not-yet,
    not as failure."""
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        try:
            if predicate():
                return
        except Exception:  # noqa: BLE001 - mid-mount reads not-yet
            pass
        await asyncio.sleep(0.02)
    raise AssertionError("condition did not arrive")


def verdict_seam() -> tuple[list, object]:
    """A recording stand-in for the REST verdict contract."""
    posted = []

    async def seam(ws, rid, decision, duration):
        posted.append((ws, rid, decision, duration))

    return posted, seam


def mode_seam(reply: dict | None = None) -> tuple[list, object]:
    """A recording stand-in for the REST mode contract; the reply
    is the fresh rules frame the daemon returns, ``applied`` True
    unless the canned reply says otherwise."""
    sent = []

    async def seam(ws, mode, *, confirm_empty=False):
        sent.append((ws, mode, confirm_empty))
        if reply is None:
            return {
                "workspace_id": ws,
                "mode": mode,
                "allow_list": [],
                "allowed": [],
                "denied": [],
                "applied": True,
            }
        return dict(reply)

    return sent, seam


def build(frames: list[str], seam, **kwargs) -> da.ConsentDeciderApp:
    """The app over a scripted connection with the verdict seam."""
    return da.ConsentDeciderApp(
        "ws1", ws_factory=FakeConnect(frames), decide=seam, **kwargs
    )


def row_texts(app: da.ConsentDeciderApp) -> list[str]:
    """The queue's rendered rows."""
    return [
        str(item.query_one(Static).content)
        for item in app.query_one("#requests").children
    ]


def status_text(app: da.ConsentDeciderApp) -> str:
    """The rendered status line."""
    return str(app.query_one("#status").content)


def record_tmux(monkeypatch: pytest.MonkeyPatch, viewers: int = 0) -> list:
    """Record every tmux subprocess the app runs; the hidden
    session reads as carrying ``viewers`` attached clients."""
    ran = []
    monkeypatch.setattr(
        tp, "shell_clients", lambda socket, session=None: [VIEWER]
    )
    monkeypatch.setattr(
        tp, "hidden_has_viewer", lambda socket, session=None: bool(viewers)
    )
    monkeypatch.setattr(
        da.subprocess,
        "run",
        lambda argv, **kwargs: (
            ran.append(list(argv)) or subprocess.CompletedProcess(argv, 0)
        ),
    )
    return ran


async def test_the_queue_lists_holds_and_the_status_counts() -> None:
    posted, seam = verdict_seam()
    frames = [
        rules_frame(),
        request_frame("r1", expires_at=time.time() + 45),
        request_frame("r2", host="cdn.example"),
    ]
    async with build(frames, seam).run_test() as pilot:
        await until(lambda: len(row_texts(pilot.app)) == 2)
        rows = row_texts(pilot.app)
        assert "api.example:443" in rows[0]
        assert COUNTDOWN.search(rows[0])
        assert "cdn.example:443" in rows[1]
        assert "2 held" in status_text(pilot.app)
    assert posted == []


async def test_the_verdict_keys_decide_the_focused_hold() -> None:
    posted, seam = verdict_seam()
    frames = [
        rules_frame(),
        request_frame("r1"),
        request_frame("r2", host="cdn.example"),
    ]
    async with build(frames, seam).run_test() as pilot:
        await until(lambda: len(row_texts(pilot.app)) == 2)
        await pilot.press("down")
        await pilot.press("a")
        await until(lambda: len(posted) == 1)
        assert posted == [("ws1", "r2", "allow", "tilrestart")]
        # The row stays until its resolution frame lands; the
        # flash owns the status line for its read.
        assert "allow (tilrestart)" in status_text(pilot.app)
        assert len(row_texts(pilot.app)) == 2
        frames.append(resolved_frame("r2"))
        await until(lambda: len(row_texts(pilot.app)) == 1)
        # The flash owns the status line for its read — the
        # verdict's confirmation beat.
        assert "allow (tilrestart)" in status_text(pilot.app)


async def test_the_duration_picker_decides_the_picked_window() -> None:
    posted, seam = verdict_seam()
    frames = [rules_frame(), request_frame("r1")]
    async with build(frames, seam).run_test() as pilot:
        await until(lambda: len(row_texts(pilot.app)) == 1)
        await pilot.press("D")
        assert isinstance(pilot.app.screen, DurationScreen)
        # The picker highlights the default (until restart);
        # down then enter picks the window under it.
        await pilot.press("down")
        await pilot.press("enter")
        await until(lambda: len(posted) == 1)
        assert posted == [("ws1", "r1", "deny", "forever")]
        # Escape cancels: nothing posts, the queue stands.
        await pilot.press("A")
        assert isinstance(pilot.app.screen, DurationScreen)
        await pilot.press("escape")
        await asyncio.sleep(0.1)
        assert len(posted) == 1


async def test_a_hold_that_lands_shows_the_popup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    posted, seam = verdict_seam()
    ran = record_tmux(monkeypatch)
    frames = [rules_frame(), request_frame("r1")]
    async with build(
        frames, seam, popup_socket=SOCKET, popup_session=tp.CONSENT_SESSION
    ).run_test():
        await until(lambda: ran)
    assert ran == [tp.show_popup_argv(SOCKET, VIEWER)]
    assert posted == []


async def test_the_empty_queue_retires_the_popup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    posted, seam = verdict_seam()
    ran = record_tmux(monkeypatch)
    frames = [rules_frame(), request_frame("r1")]
    async with build(
        frames, seam, popup_socket=SOCKET, popup_session=tp.CONSENT_SESSION
    ).run_test() as pilot:
        await until(lambda: len(row_texts(pilot.app)) == 1)
        frames.append(resolved_frame("r1"))
        await until(lambda: tp.detach_argv(SOCKET) in ran)
    assert posted == []


async def test_hide_never_quits_the_persistent_app(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    posted, seam = verdict_seam()
    ran = record_tmux(monkeypatch)
    frames = [rules_frame(), request_frame("r1")]
    async with build(
        frames, seam, popup_socket=SOCKET, popup_session=tp.CONSENT_SESSION
    ).run_test() as pilot:
        await until(lambda: len(row_texts(pilot.app)) == 1)
        await pilot.press("q")
        await until(lambda: tp.detach_argv(SOCKET) in ran)
        assert pilot.app.is_running
        # The footer's advertised toggle: C-b hides from inside.
        ran.clear()
        await pilot.press("ctrl+b")
        await until(lambda: ran)
        assert pilot.app.is_running  # a key never quits the decider


async def test_standalone_quits_on_q() -> None:
    posted, seam = verdict_seam()
    frames = [rules_frame(), request_frame("r1")]
    async with build(frames, seam).run_test() as pilot:
        await until(lambda: len(row_texts(pilot.app)) == 1)
        await pilot.press("q")
        await until(lambda: not pilot.app.is_running)


async def test_a_refused_registration_ends_the_app(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    posted, seam = verdict_seam()
    monkeypatch.setattr(da, "FLASH_TTL", 0.05)
    frames = [rejected_frame()]
    app = build(frames, seam)
    async with app.run_test():
        await until(lambda: not app.is_running)
        assert "unknown workspace" in app.link.reject_reason
    assert posted == []


def test_parse_args_takes_the_workspace_and_the_wiring() -> None:
    assert da.parse_args(["-w", "ws1"]) == ("ws1", None, None)
    assert da.parse_args(
        ["-w", "ws1", "--socket", SOCKET, "--session", "consent"]
    ) == ("ws1", SOCKET, "consent")
    with pytest.raises(SystemExit, match="unknown argument"):
        da.parse_args(["-w", "ws1", "--nope", "x"])
    with pytest.raises(SystemExit, match="needs a value"):
        da.parse_args(["-w"])
    with pytest.raises(SystemExit, match="usage"):
        da.parse_args([])


async def test_rest_decide_posts_the_verdict(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MSKSC_URL", "https://daemon")
    monkeypatch.setenv("MSKSC_TOKEN", "tok")
    posted = {}

    async def fake_call(method, path, *, json_body=None, **kwargs):
        posted.update(method=method, path=path, body=json_body)

    monkeypatch.setattr(da.context, "call", fake_call)
    await da.rest_decide("ws1", "r1", "allow", "5m")
    assert posted["method"] == "POST"
    assert posted["path"] == "/api/v1/workspaces/ws1/egress/requests/r1"
    assert posted["body"] == {"decision": "allow", "duration": "5m"}


async def test_rest_mode_puts_the_policy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MSKSC_URL", "https://daemon")
    monkeypatch.setenv("MSKSC_TOKEN", "tok")
    posted = {}

    async def fake_call(method, path, *, json_body=None, **kwargs):
        posted.update(method=method, path=path, body=json_body)

    monkeypatch.setattr(da.context, "call", fake_call)
    await da.rest_mode("ws1", "static", confirm_empty=True)
    assert posted["method"] == "PUT"
    assert posted["path"] == "/api/v1/workspaces/ws1/egress/policy"
    assert posted["body"] == {"mode": "static", "confirm_empty": True}
    await da.rest_mode("ws1", "allow")
    assert posted["body"] == {"mode": "allow"}


def test_the_footer_names_the_popup_bindings() -> None:
    # The persistent map carries the toggle the footer shows —
    # the verdict keys beside the reopen spelling, with the
    # hide keys present but unwritten.
    popup_app = da.ConsentDeciderApp(
        "ws1", popup_socket=SOCKET, popup_session=tp.CONSENT_SESSION
    )
    keys = {binding.key: binding for _key, binding in popup_app._bindings}
    assert keys["ctrl+b"].key_display == "C-b p"
    assert keys["ctrl+b"].action == "hide"
    assert all(keys[k].show is False for k in ("q", "Q", "escape"))
    standalone = da.ConsentDeciderApp("ws1")
    alone = {binding.key for _key, binding in standalone._bindings}
    assert "q" in alone and "ctrl+b" not in alone


async def test_the_mode_picker_switches_the_mode() -> None:
    """``m`` opens the shared picker (#465): the current mode
    starts highlighted, the pick goes straight through the REST
    seam when the gate holds nothing to ask, and the reply's
    rules frame lands — the status line names the new mode."""
    posted, seam = verdict_seam()
    sent, mode = mode_seam()
    frames = [rules_frame()]  # interactive, nothing allowed
    async with build(frames, seam, mode=mode).run_test() as pilot:
        await until(lambda: "mode interactive" in status_text(pilot.app))
        await pilot.press("m")
        assert isinstance(pilot.app.screen, ModeScreen)
        await pilot.press("up")  # static — the gate asks below
        await pilot.press("up")  # allow — straight through
        await pilot.press("enter")
        await until(lambda: sent == [("ws1", "allow", False)])
        assert not isinstance(pilot.app.screen, ConfirmScreen)
        await until(
            lambda: "mode allow (in effect now)" in status_text(pilot.app)
        )
    assert posted == []


async def test_the_mode_picker_cancels() -> None:
    posted, seam = verdict_seam()
    sent, mode = mode_seam()
    frames = [rules_frame()]
    async with build(frames, seam, mode=mode).run_test() as pilot:
        await until(lambda: "mode interactive" in status_text(pilot.app))
        await pilot.press("m")
        await pilot.press("escape")
        await asyncio.sleep(0.1)
        assert sent == []


async def test_static_with_nothing_allowed_asks_first() -> None:
    """The empty-static gate (#465): picking ``static`` while
    nothing is effectively allowed pushes the shared question —
    a no decides nothing, a yes sends the confirmed switch."""
    posted, seam = verdict_seam()
    sent, mode = mode_seam()
    frames = [rules_frame()]  # nothing allowed
    async with build(frames, seam, mode=mode).run_test() as pilot:
        await until(lambda: "mode interactive" in status_text(pilot.app))
        await pilot.press("m")
        await pilot.press("up")  # static, nothing allowed: ask
        await pilot.press("enter")
        assert isinstance(pilot.app.screen, ConfirmScreen)
        await pilot.press("n")
        await asyncio.sleep(0.1)
        assert sent == []
        await pilot.press("m")
        await pilot.press("up")
        await pilot.press("enter")
        await pilot.press("y")
        await until(lambda: sent == [("ws1", "static", True)])
    assert posted == []


async def test_static_with_something_allowed_skips_the_question() -> None:
    """An allowlist entry satisfies the gate: the static pick goes
    straight through, confirmation never asked."""
    posted, seam = verdict_seam()
    sent, mode = mode_seam()
    frames = [rules_frame(allow_list=["10.0.0.0/8"])]
    async with build(frames, seam, mode=mode).run_test() as pilot:
        await until(lambda: "mode interactive" in status_text(pilot.app))
        await pilot.press("m")
        await pilot.press("up")  # static — the allowlist carries it
        await pilot.press("enter")
        await until(lambda: sent == [("ws1", "static", False)])
        assert not isinstance(pilot.app.screen, ConfirmScreen)
    assert posted == []


async def test_a_switch_on_a_stopped_workspace_names_its_effect() -> None:
    """A reply without ``applied`` names the next-start effect —
    the CLI's own line."""
    posted, seam = verdict_seam()
    sent, mode = mode_seam(
        {
            "workspace_id": "ws1",
            "mode": "static",
            "allow_list": ["10.0.0.0/8"],
            "allowed": [],
            "denied": [],
            "applied": False,
        }
    )
    frames = [rules_frame(allow_list=["10.0.0.0/8"])]
    async with build(frames, seam, mode=mode).run_test() as pilot:
        await until(lambda: "mode interactive" in status_text(pilot.app))
        await pilot.press("m")
        await pilot.press("up")
        await pilot.press("enter")
        await until(
            lambda: "takes effect at next start" in status_text(pilot.app)
        )
    assert sent == [("ws1", "static", False)]


async def test_a_refused_mode_switch_names_its_reason() -> None:
    """A switch the daemon refuses owns the status line with its
    one-line reason — the verdict posts' shape."""
    posted, seam = verdict_seam()

    async def refuse(ws, mode_, *, confirm_empty=False):
        raise SystemExit("msks: 409: static needs confirmation")

    frames = [rules_frame(allow_list=["10.0.0.0/8"])]
    async with build(frames, seam, mode=refuse).run_test() as pilot:
        await until(lambda: "mode interactive" in status_text(pilot.app))
        await pilot.press("m")
        await pilot.press("up")
        await pilot.press("enter")
        await until(lambda: "mode switch failed" in status_text(pilot.app))
        assert "409" in status_text(pilot.app)
        assert pilot.app.is_running


async def test_the_picker_opens_before_any_rules_frame() -> None:
    """No rules frame yet: the picker opens with nothing
    highlighted — the widget's own focus would otherwise
    pre-highlight ``allow``, and a bare Enter must not take a
    posture the app has not seen — and the status line names no
    mode."""
    posted, seam = verdict_seam()
    sent, mode = mode_seam()
    frames = [request_frame("r1")]  # no rules frame
    async with build(frames, seam, mode=mode).run_test() as pilot:
        await until(lambda: len(row_texts(pilot.app)) == 1)
        await pilot.press("m")
        assert isinstance(pilot.app.screen, ModeScreen)
        options = pilot.app.screen.query_one(OptionList)
        # The constructor's own highlight (row 0) is transient: the
        # mount settles on nothing highlighted.
        await until(lambda: options.highlighted is None)
        await pilot.press("enter")
        await asyncio.sleep(0.1)
        assert sent == []
        await pilot.press("escape")
        await asyncio.sleep(0.1)
        assert sent == []
        assert "mode" not in status_text(pilot.app)


async def test_a_mode_switch_lands_after_the_exit() -> None:
    """A mode PUT that outlives the app (#465): the reply lands
    through the controller, the flash records, and the repaint's
    guard stands down — the verdict posts' own rule, driven
    without the race (an app that never ran)."""
    posted, seam = verdict_seam()
    sent, mode = mode_seam()
    app = build([rules_frame()], seam, mode=mode)
    assert not app.is_running
    await app.send_mode("allow")
    assert sent == [("ws1", "allow", False)]
    assert app.link.controller.rules is not None
    assert app.link.controller.rules.mode == "allow"


async def test_the_verdict_edges() -> None:
    """The verdict keys against an empty queue, a stale index,
    and the deny quick form: each changes only what it should."""
    posted, seam = verdict_seam()
    # An empty queue: the picker opens nothing, a quick verdict
    # posts nothing.
    async with build([rules_frame()], seam).run_test() as pilot:
        await pilot.press("A")
        assert not isinstance(pilot.app.screen, DurationScreen)
        await pilot.press("a")
        await asyncio.sleep(0.1)
        assert posted == []
    frames = [rules_frame(), request_frame("r1")]
    async with build(frames, seam).run_test() as pilot:
        await until(lambda: len(row_texts(pilot.app)) == 1)
        await pilot.press("d")
        await until(lambda: posted == [("ws1", "r1", "deny", "once")])


async def test_a_post_that_cannot_land_names_its_reason() -> None:
    async def refuse(ws, rid, decision, duration):
        raise SystemExit("msks: 409: resolved elsewhere")

    frames = [rules_frame(), request_frame("r1")]
    async with build(frames, refuse).run_test() as pilot:
        await until(lambda: len(row_texts(pilot.app)) == 1)
        await pilot.press("a")
        await until(lambda: "409" in status_text(pilot.app))

    async def bug(ws, rid, decision, duration):
        raise RuntimeError("bug")

    frames = [rules_frame(), request_frame("r1")]
    async with build(frames, bug).run_test() as pilot:
        await until(lambda: len(row_texts(pilot.app)) == 1)
        await pilot.press("a")
        await until(lambda: "verdict post failed" in status_text(pilot.app))


async def test_the_show_path_survives_a_dead_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    posted, seam = verdict_seam()
    record_tmux(monkeypatch)

    def dead(argv, **kw):
        raise subprocess.SubprocessError("gone")

    monkeypatch.setattr(da.subprocess, "run", dead)
    monkeypatch.setattr(da, "POPUP_SHOW_RETRY_DELAY", 0.01)
    monkeypatch.setattr(da, "POPUP_SHOW_ATTEMPTS", 1)
    frames = [rules_frame(), request_frame("r1")]
    async with build(
        frames, seam, popup_socket=SOCKET, popup_session=tp.CONSENT_SESSION
    ).run_test():
        # A show whose subprocess dies retries its budget, then
        # leaves the decider standing — the reopen key still
        # reaches it.
        await asyncio.sleep(0.4)
    assert posted == []


async def test_the_show_path_is_deduplicated(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    posted, seam = verdict_seam()
    record_tmux(monkeypatch, viewers=1)  # a viewer already stands
    ran = []
    monkeypatch.setattr(
        da.subprocess,
        "run",
        lambda argv, **kw: (
            ran.append(list(argv)) or subprocess.CompletedProcess(argv, 0)
        ),
    )
    frames = [rules_frame(), request_frame("r1"), request_frame("r2")]
    async with build(
        frames, seam, popup_socket=SOCKET, popup_session=tp.CONSENT_SESSION
    ).run_test():
        await asyncio.sleep(0.3)
    # The already-open viewer covers the burst: one show, and it
    # is the hidden-session check, not a subprocess.
    assert ran == []


def test_main_builds_the_app_from_the_argv(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    built = {}

    class FakeApp:
        def __init__(
            self, workspace, *, popup_socket=None, popup_session=None
        ):
            built.update(
                workspace=workspace,
                socket=popup_socket,
                session=popup_session,
            )

        def run(self) -> None:
            built["ran"] = True

    monkeypatch.setattr(da, "ConsentDeciderApp", FakeApp)
    assert da.main(["-w", "ws1", "--socket", SOCKET, "--session", "c"]) == 0
    assert built == {
        "workspace": "ws1",
        "socket": SOCKET,
        "session": "c",
        "ran": True,
    }


async def test_the_popup_guards() -> None:
    """The viewer paths' guards, driven straight: a standalone app
    takes show and hide as no-ops, a pending show dedupes, and a
    dead detach or missing wiring leaves the decider standing."""
    posted, seam = verdict_seam()
    frames = [rules_frame(), request_frame("r1")]
    async with build(frames, seam).run_test() as pilot:
        await until(lambda: len(row_texts(pilot.app)) == 1)
        app = pilot.app
        # No popup wiring: every viewer path is a no-op.
        app.schedule_hide()
        app.action_hide()
        app.action_quit()  # standalone: the quit path exits
        await until(lambda: not app.is_running)

    hid = []
    frames = [rules_frame(), request_frame("r1")]
    async with build(
        frames,
        seam,
        popup_socket=SOCKET,
        popup_session=tp.CONSENT_SESSION,
    ).run_test() as pilot:
        await until(lambda: len(row_texts(pilot.app)) == 1)
        pilot.app.schedule_hide = lambda: hid.append(True)
        pilot.app.action_quit()  # the persistent quit hides instead
        await until(lambda: pilot.app.is_running and hid == [True])


async def test_the_viewer_guards_without_wiring() -> None:
    posted, seam = verdict_seam()
    async with build([rules_frame()], seam).run_test() as pilot:
        app = pilot.app
        # No wiring: the show targets nothing, the hide detaches
        # nothing, and the scheduled paths never start tasks.
        assert app.show_popup() is False
        app.hide_viewer()
        app.schedule_show()
        app.schedule_hide()
        assert app.show_task is None
        assert app.hide_task is None
        # A dedupe: a pending show absorbs the next request.
        app.show_task = asyncio.ensure_future(asyncio.sleep(30))
        app.schedule_show()
        assert app.show_task is not None


async def test_a_dead_detach_changes_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    posted, seam = verdict_seam()
    monkeypatch.setattr(
        da.subprocess,
        "run",
        lambda argv, **kw: (_ for _ in ()).throw(subprocess.SubprocessError()),
    )
    frames = [rules_frame(), request_frame("r1")]
    async with build(
        frames, seam, popup_socket=SOCKET, popup_session=tp.CONSENT_SESSION
    ).run_test() as pilot:
        await until(lambda: len(row_texts(pilot.app)) == 1)
        pilot.app.hide_viewer()  # the dead detach changes nothing
        await asyncio.sleep(0.2)
        assert pilot.app.is_running
    assert posted == []


async def test_the_scheduler_dedupe_and_a_raising_show(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    posted, seam = verdict_seam()
    record_tmux(monkeypatch, viewers=1)
    frames = [rules_frame()]
    async with build(
        frames, seam, popup_socket=SOCKET, popup_session=tp.CONSENT_SESSION
    ).run_test() as pilot:
        app = pilot.app
        # A show already in flight absorbs the next request; a
        # hide already in flight absorbs the next key.
        app.show_task = asyncio.ensure_future(asyncio.sleep(30))
        app.schedule_show()
        assert app.show_task.done() is False
        app.hide_task = asyncio.ensure_future(asyncio.sleep(30))
        app.schedule_hide()
        assert app.hide_task.done() is False
        # A raising show ends its worker quietly — the reopen key
        # still reaches the decider.
        app.show_task.cancel()
        await until(lambda: app.show_task.done())

        def boom() -> bool:
            raise RuntimeError("show failed")

        monkeypatch.setattr(app, "show_popup", boom)
        app.schedule_show()
        await until(lambda: app.show_task.done())
        await asyncio.sleep(0.1)
        assert app.is_running
    assert posted == []


async def test_a_show_that_targets_nothing_retries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    posted, seam = verdict_seam()
    ran = record_tmux(monkeypatch)
    monkeypatch.setattr(tp, "shell_clients", lambda socket, session=None: [])
    monkeypatch.setattr(da, "POPUP_SHOW_RETRY_DELAY", 0.01)
    frames = [rules_frame(), request_frame("r1")]
    async with build(
        frames, seam, popup_socket=SOCKET, popup_session=tp.CONSENT_SESSION
    ).run_test() as pilot:
        # A window with no client to target (a race the shell
        # window lost for a moment): the worker spends its budget
        # and leaves the hold to the reopen key.
        await until(lambda: pilot.app.show_task is not None)
        await until(lambda: pilot.app.show_task.done())
        await asyncio.sleep(0.1)
        assert pilot.app.is_running
    assert ran == []
    assert posted == []


async def test_the_picker_decides_the_hold_it_was_keyed_on() -> None:
    """The picker captures its hold at open (#467 review): a row
    arriving while it stands must not move the verdict, and the
    queue's swap must not move the quick keys' target either."""
    posted, seam = verdict_seam()
    frames = [
        rules_frame(),
        request_frame("r1"),
        request_frame("r2", host="cdn.example"),
    ]
    async with build(frames, seam).run_test() as pilot:
        await until(lambda: len(row_texts(pilot.app)) == 2)
        await pilot.press("down")
        queue = pilot.app.query_one("#requests")
        assert queue.index == 1
        # A third hold lands while the picker stands.
        await pilot.press("D")
        assert isinstance(pilot.app.screen, DurationScreen)
        frames.append(request_frame("r3", host="late.example"))
        await until(lambda: len(row_texts(pilot.app)) == 3)
        await pilot.press("enter")
        await until(lambda: len(posted) == 1)
        assert posted == [("ws1", "r2", "deny", "tilrestart")]


async def test_the_selection_rides_its_hold_across_swaps() -> None:
    """A membership change keeps the highlighted hold where it
    stood (#467 review): an arrival or another row's resolution
    must not snap the selection to the top."""
    posted, seam = verdict_seam()
    frames = [
        rules_frame(),
        request_frame("r1"),
        request_frame("r2", host="cdn.example"),
    ]
    async with build(frames, seam).run_test() as pilot:
        await until(lambda: len(row_texts(pilot.app)) == 2)
        queue = pilot.app.query_one("#requests")
        await pilot.press("down")
        assert queue.index == 1
        # An arrival: the selection stays on its hold.
        frames.append(request_frame("r3", host="late.example"))
        await until(lambda: len(row_texts(pilot.app)) == 3)
        assert queue.index == 1
        await pilot.press("d")
        await until(lambda: posted == [("ws1", "r2", "deny", "once")])
        # Another row's resolution: the selection still stands.
        frames.append(resolved_frame("r1"))
        await until(lambda: len(row_texts(pilot.app)) == 2)
        # r2 — the selection's hold — not whatever moved up.
        await until(lambda: queue.index == 0 and len(queue.children) == 2)


async def test_the_decider_retires_with_its_window(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The window-gone clock (#467 review): a client seen once and
    then gone past the empty window retires the app; a client
    standing keeps it; a window that never attached retires past
    the launch grace."""
    posted, seam = verdict_seam()
    now = {"t": 1000.0}
    clients = {"list": [VIEWER]}
    monkeypatch.setattr(
        tp, "shell_clients", lambda socket, session=None: clients["list"]
    )
    frames = [rules_frame()]
    async with build(
        frames,
        seam,
        popup_socket=SOCKET,
        popup_session=tp.CONSENT_SESSION,
        clock=lambda: now["t"],
    ).run_test() as pilot:
        app = pilot.app
        now["t"] = 2000.0
        await app.retire_when_gone()  # a client stands: no retirement
        assert app.is_running
        clients["list"] = []
        now["t"] = 2008.0
        await app.retire_when_gone()  # inside the empty window
        assert app.is_running
        now["t"] = 2020.0
        await app.retire_when_gone()  # past it: the decider steps aside
        await until(lambda: not app.is_running)
    assert posted == []


async def test_a_window_that_never_attached_retires(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    posted, seam = verdict_seam()
    now = {"t": 1000.0}
    monkeypatch.setattr(tp, "shell_clients", lambda socket, session=None: [])
    frames = [rules_frame()]
    async with build(
        frames,
        seam,
        popup_socket=SOCKET,
        popup_session=tp.CONSENT_SESSION,
        clock=lambda: now["t"],
    ).run_test() as pilot:
        app = pilot.app
        now["t"] = 1020.0
        await app.retire_when_gone()  # inside the launch grace
        assert app.is_running
        now["t"] = 1031.0
        await app.retire_when_gone()  # no window ever came
        await until(lambda: not app.is_running)
    assert posted == []


async def test_daemon_text_cannot_crash_the_status_line(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A refusal or failure string with markup in it arrives
    escaped (#318's rule): the status line parses markup, and the
    hidden decider must survive hostile text."""

    async def refuse(ws, rid, decision, duration):
        raise SystemExit("msks: 409: gone [/dev/x]")

    frames = [rules_frame(), request_frame("r1")]
    async with build(frames, refuse).run_test() as pilot:
        await until(lambda: len(row_texts(pilot.app)) == 1)
        await pilot.press("a")
        await until(lambda: "409" in status_text(pilot.app))
        assert pilot.app.is_running


async def test_a_verdict_in_flight_through_the_exit() -> None:
    """A post that outlives the app: the flash records, the
    repaint stands down (#467 review)."""
    posted, seam = verdict_seam()
    landed = []

    async def slow(ws, rid, decision, duration):
        await asyncio.sleep(0.3)
        posted.append((rid, decision, duration))
        landed.append(True)

    frames = [rules_frame(), request_frame("r1")]
    async with build(frames, slow).run_test() as pilot:
        await until(lambda: len(row_texts(pilot.app)) == 1)
        await pilot.press("a")
        await pilot.press("q")  # the app quits under the post
        await until(lambda: not pilot.app.is_running)
        pilot.app.repaint()  # the teardown guard: no crash
        await until(lambda: landed == [True])
    assert posted == [("r1", "allow", "tilrestart")]


async def test_the_show_covers_every_shell_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two windows on the launch's socket: the show reaches each
    client's popup."""
    posted, seam = verdict_seam()
    ran = record_tmux(monkeypatch)
    monkeypatch.setattr(
        tp,
        "shell_clients",
        lambda socket, session=None: ["/dev/pts/3", "/dev/pts/9"],
    )
    frames = [rules_frame(), request_frame("r1")]
    async with build(
        frames, seam, popup_socket=SOCKET, popup_session=tp.CONSENT_SESSION
    ).run_test():
        await until(lambda: len(ran) == 2)
    assert ran == [
        tp.show_popup_argv(SOCKET, "/dev/pts/3"),
        tp.show_popup_argv(SOCKET, "/dev/pts/9"),
    ]


async def test_the_picker_decides_the_hold_even_if_it_resolved() -> None:
    """The keyed hold resolving mid-picker does not redirect the
    verdict (#467 review): the captured id posts — a resolved
    hold answers at the post, not by silently deciding another."""
    posted, seam = verdict_seam()
    frames = [
        rules_frame(),
        request_frame("r1"),
        request_frame("r2", host="cdn.example"),
    ]
    async with build(frames, seam).run_test() as pilot:
        await until(lambda: len(row_texts(pilot.app)) == 2)
        queue = pilot.app.query_one("#requests")
        await pilot.press("down")
        assert queue.index == 1
        await pilot.press("D")
        assert isinstance(pilot.app.screen, DurationScreen)
        # The keyed hold itself resolves while the picker stands.
        frames.append(resolved_frame("r2"))
        await until(lambda: len(row_texts(pilot.app)) == 1)
        await pilot.press("enter")
        await until(lambda: len(posted) == 1)
        assert posted == [("ws1", "r2", "deny", "tilrestart")]


async def test_the_quick_keys_read_the_rows_the_operator_sees() -> None:
    """A frame that reshaped the controller between repaints
    cannot move a quick key's target: the hold comes from the
    list's own row, not the fresh order mapped onto a stale
    index."""
    posted, seam = verdict_seam()
    frames = [rules_frame(), request_frame("r1"), request_frame("r2")]
    async with build(frames, seam).run_test() as pilot:
        await until(lambda: len(row_texts(pilot.app)) == 2)
        queue = pilot.app.query_one("#requests")
        await pilot.press("down")
        assert queue.index == 1
        # The controller drops r1 before the list swaps: the
        # highlighted row still says r2.
        del pilot.app.link.controller.pending["r1"]
        await pilot.press("d")
        await until(lambda: len(posted) == 1)
        assert posted == [("ws1", "r2", "deny", "once")]
