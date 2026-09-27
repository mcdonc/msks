"""The tmux consent-terminal launcher tests (#379).

Every tmux/terminal boundary is a recorded subprocess call or a
stubbed function — the suite pins the argv contracts (the
session's tmux line, the popup's display-popup line, the decide
role's POST), the watcher's frame handling, and the liveness
retirement, never opening a window.
"""

import asyncio
import io
import os
import pty
import shlex
import subprocess
import threading
from types import SimpleNamespace

import pytest
import websockets
from msks.client import term_popup as tp
from msks.client import wsauth
from websockets.frames import Close

SSH_CHILD = [
    "/venv/bin/python",
    "-m",
    "msks.client.cli",
    "ssh",
    "a1b2c3d4e5",
]


def client_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MSKSC_URL", "https://daemon")
    monkeypatch.setenv("MSKSC_TOKEN", "tok")


# --- the launch argv contracts ---------------------------------------------


def test_workspace_from_argv_finds_the_ssh_target() -> None:
    assert tp.workspace_from_argv(SSH_CHILD) == "a1b2c3d4e5"
    # The last ssh token wins (an operator-chained invocation).
    assert tp.workspace_from_argv(["ssh", "x", "ssh", "y"]) == "y"
    # A trailing ssh with no target, or no ssh at all, ships no
    # watcher.
    assert tp.workspace_from_argv(["msks", "ssh"]) is None
    assert tp.workspace_from_argv(["/venv/bin/python", "-m", "cli"]) is None


def test_session_and_socket_names_carry_the_workspace() -> None:
    # One server per launch: the socket carries the discriminator
    # (a server of its own carries the launcher's env to the pane),
    # so the session name is the plain workspace id.
    assert tp.session_name("a1b2c3d4e5") == "a1b2c3d4e5"
    assert tp.session_name(None) == "shell"
    assert tp.socket_name("a1b2c3d4e5", 4242) == "msks-a1b2c3d4e5-4242"
    assert tp.socket_name(None, 7) == "msks-shell-7"


def test_session_argv_names_the_socket_the_pane_and_the_workspace() -> None:
    argv = tp.session_argv(
        SSH_CHILD, session="a1b2c3d4e5", workspace_id="a1b2c3d4e5"
    )
    assert argv[0:2] == ["tmux", "-L"]
    assert argv[2].startswith("msks-a1b2c3d4e5-")
    assert argv[3:6] == ["new-session", "-s", "a1b2c3d4e5"]
    assert argv[7:] == [";", "set-option", "destroy-unattached", "on"]
    joined = argv[6]
    assert joined.startswith(f"{tp.sys.executable} -m msks.client.term_popup")
    # The pane argv carries the workspace: the watcher is the
    # feature, and nothing else can re-derive the id.
    assert (
        " pane -s a1b2c3d4e5 -w a1b2c3d4e5 -- /venv/bin/python -m"
        " msks.client.cli ssh a1b2c3d4e5" in joined
    )


def test_session_argv_without_a_workspace_ships_no_watcher() -> None:
    argv = tp.session_argv(["top"], session="shell", workspace_id=None)
    assert " pane -s shell -- top" in argv[6]
    assert " -w " not in argv[6]


def test_take_option_stops_at_the_separator() -> None:
    assert tp.take_option("-s", ["a", "-s", "b", "c"]) == ("b", ["a", "c"])
    assert tp.take_option("-s", ["a"]) == (None, ["a"])
    # The child's own -w belongs to the child, whatever follows --.
    argv = ["-s", "sess", "--", "python", "-m", "foo", "-w", "other"]
    assert tp.take_option("-w", argv) == (None, argv)
    assert tp.take_option("-s", argv) == ("sess", argv[2:])
    with pytest.raises(SystemExit, match="needs a value"):
        tp.take_option("-w", ["-s", "sess", "-w"])


def test_child_argv() -> None:
    assert tp.child_argv(["-w", "ws", "--", "a", "b"]) == ["a", "b"]
    assert tp.child_argv(["a"]) == ["a"]


# --- the roles' dispatch and validation ------------------------------------


def test_main_defaults_to_launch_for_the_appended_child(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(tp.shutil, "which", lambda tool: "/bin/" + tool)
    seen = {}
    monkeypatch.setattr(
        tp.os, "execvp", lambda binname, argv: seen.update(argv=argv)
    )
    assert tp.main(list(SSH_CHILD)) == 0
    assert seen["argv"][0] == "tmux"


def test_main_spelled_roles_dispatch(monkeypatch) -> None:
    runs = []

    def fake_launch(argv):
        runs.append(argv)
        return 0

    # The dispatch table holds the bound roles; patch the entry,
    # or the real run_launch would exec a live tmux from the test.
    monkeypatch.setattr(tp, "RUNNERS", {"launch": fake_launch})
    assert tp.main(["launch", "cmd"]) == 0
    assert runs == [["cmd"]]
    with pytest.raises(SystemExit, match="usage"):
        tp.main([])


def test_run_launch_names_a_missing_tmux(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(tp.shutil, "which", lambda tool: None)
    with pytest.raises(SystemExit, match="tmux is not on PATH"):
        tp.run_launch(list(SSH_CHILD))


def test_run_launch_execs_the_tmux_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(tp.shutil, "which", lambda tool: "/bin/" + tool)
    seen = {}
    monkeypatch.setattr(
        tp.os,
        "execvp",
        lambda binname, argv: seen.update(binname=binname, argv=argv),
    )
    assert tp.run_launch(list(SSH_CHILD)) == 0
    assert seen["binname"] == "tmux"
    assert seen["argv"][:2] == ["tmux", "-L"]
    assert seen["argv"][3] == "new-session"
    assert seen["argv"][2].startswith("msks-a1b2c3d4e5-")
    assert seen["argv"][5] == "a1b2c3d4e5"


def test_the_launch_line_meets_the_pane_role(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The boundary the shipped chain crosses: the pane argv that
    # session_argv shell-joins is exactly what the pane role
    # parses, and it starts the watcher for the appended command's
    # workspace (strip the leading interpreter invocation, then
    # spell the pane role the way tmux's sh would hand it over).
    monkeypatch.setenv("TMUX", "/tmp/tmux-0/default,1,sess")
    started = {}
    monkeypatch.setattr(
        tp,
        "start_watcher",
        lambda session, ws: started.update(session=session, ws=ws),
    )
    monkeypatch.setattr(tp.os, "execvp", lambda binname, argv: None)
    joined = tp.session_argv(
        SSH_CHILD, session="a1b2c3d4e5", workspace_id="a1b2c3d4e5"
    )[6]
    words = shlex.split(joined)[3:]  # past python -m msks.client.term_popup
    assert words[0] == "pane"
    tp.main(["pane", *words[1:]])
    assert started == {"session": "a1b2c3d4e5", "ws": "a1b2c3d4e5"}


def test_run_watch_validates_and_runs_the_loop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with pytest.raises(SystemExit, match="usage"):
        tp.run_watch(["-s", "only"])
    calls = []

    async def fake_loop(workspace_id: str, session: str) -> int:
        calls.append((workspace_id, session))
        return 7

    monkeypatch.setattr(tp, "watch_loop", fake_loop)
    assert tp.run_watch(["-s", "sess", "-w", "ws"]) == 7
    assert calls == [("ws", "sess")]


def test_run_pane_starts_the_watcher_then_execs_the_child(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TMUX", "/tmp/tmux-0/default,1,sess")
    started = {}
    monkeypatch.setattr(
        tp,
        "start_watcher",
        lambda session, ws: started.update(session=session, ws=ws),
    )
    seen = {}
    monkeypatch.setattr(
        tp.os, "execvp", lambda binname, argv: seen.update(argv=argv)
    )
    rc = tp.run_pane(["-s", "sess", "-w", "ws1", "--", *SSH_CHILD])
    assert rc == 0
    assert started == {"session": "sess", "ws": "ws1"}
    assert seen["argv"] == SSH_CHILD
    # No workspace in the child: the pane runs the command alone.
    # Without -s the session name derives from the workspace; with
    # no tmux environment (a hand-run pane) it runs alone too.
    started.clear()
    tp.run_pane(["-s", "sess", "--", *SSH_CHILD])
    assert started == {}
    tp.run_pane(["-w", "ws1", "--", *SSH_CHILD])
    assert started["ws"] == "ws1"
    assert started["session"] == "ws1"
    started.clear()
    monkeypatch.delenv("TMUX")
    tp.run_pane(["-s", "sess", "-w", "ws1", "--", *SSH_CHILD])
    assert started == {}


def test_start_watcher_popens_this_module(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spawned = {}

    def fake_popen(argv, **kwargs):
        spawned.update(argv=argv, kwargs=kwargs)
        return "proc"

    monkeypatch.setattr(tp.subprocess, "Popen", fake_popen)
    logs = []

    def fake_log(**kwargs):
        logs.append(
            SimpleNamespace(name="/tmp/consent.log", close=lambda: None)
        )
        return logs[-1]

    monkeypatch.setattr(tp.tempfile, "NamedTemporaryFile", fake_log)
    tp.start_watcher("sess", "ws1")
    assert spawned["argv"] == [
        tp.sys.executable,
        "-m",
        "msks.client.term_popup",
        "watch",
        "-s",
        "sess",
        "-w",
        "ws1",
    ]
    assert spawned["kwargs"]["stdin"] == subprocess.DEVNULL
    # The child inherits the open log file object, not a path —
    # the fd stays valid whatever happens to the file.
    assert spawned["kwargs"]["stdout"] is logs[0]


# --- the watcher's frame handling ------------------------------------------


async def test_watch_frame_raises_popups_and_records_resolutions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    popped = []
    monkeypatch.setattr(
        tp, "raise_popup", lambda row, ws, session: popped.append(row["id"])
    )
    request = {
        "event": "egress.request",
        "data": {
            "request": {
                "id": "r1",
                "workspace_id": "ws1",
                "dest_host": "api.example",
                "dest_port": 443,
            }
        },
    }
    resolved = {"event": "egress.resolved", "data": {"request_id": "r1"}}
    await tp.watch_frame(request, "ws1", "sess", set())
    assert popped == ["r1"]
    seen: set[str] = set()
    await tp.watch_frame(resolved, "ws1", "sess", seen)
    await tp.watch_frame(request, "ws1", "sess", seen)
    assert seen == {"r1"}
    assert popped == ["r1"]  # decided elsewhere: no second popup
    other = {"event": "egress.rules", "data": {}}
    await tp.watch_frame(other, "ws1", "sess", seen)


async def test_watch_frame_stops_on_a_refused_registration(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture,
) -> None:
    # A workspace id that names nothing: the server refuses the
    # decider frame with its reason, and the watcher stops instead
    # of sitting connected and useless. The frame shape is the
    # daemon's own (api.py): a reason, no workspace key — the
    # watcher's own id parameter is what names the typo.
    rejected = {
        "event": "egress.decider_rejected",
        "data": {"reason": "unknown workspace"},
    }
    monkeypatch.setattr(tp, "raise_popup", lambda *a: None)
    with pytest.raises(SystemExit):
        await tp.watch_frame(rejected, "nope", "sess", set())
    out = capsys.readouterr().out
    assert "decider registration refused: nope" in out
    assert "unknown workspace" in out


class FakeWS:
    """One scripted websocket: recv() yields the frames, then
    idles until the caller's liveness tick retires the loop."""

    def __init__(self, frames: list[str] | None = None) -> None:
        self.frames = frames or []
        self.sent: list[str] = []

    async def send(self, text: str) -> None:
        self.sent.append(text)

    async def recv(self) -> str:
        if self.frames:
            return self.frames.pop(0)
        await asyncio.sleep(30)


class FakeConnect:
    """The websockets.connect surface: scripted connections, then
    done."""

    def __init__(self, connections: list[FakeWS]) -> None:
        self.connections = connections

    def __call__(self, **kwargs):
        self.kwargs = kwargs
        return self

    def __aiter__(self):
        return self

    async def __anext__(self):
        if not self.connections:
            raise StopAsyncIteration
        return self.connections.pop(0)


async def test_watch_loop_registers_decides_and_retires(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client_env(monkeypatch)
    monkeypatch.setattr(tp, "LIVENESS_TICK_S", 0.05)
    monkeypatch.setattr(tp, "session_alive", lambda session: False)
    popped = []
    monkeypatch.setattr(
        tp,
        "raise_popup",
        lambda row, ws, session: popped.append((row["id"], ws, session)),
    )
    sock = FakeWS(
        [
            '{"event": "egress.request", "data": {"request": {'
            '"id": "r1", "workspace_id": "ws1", '
            '"dest_host": "api.example", "dest_port": 443}}}',
        ]
    )
    connect = FakeConnect([sock])
    monkeypatch.setattr(tp.websockets, "connect", connect)
    assert await tp.watch_loop("ws1", "sess") == 0
    announce = '{"type": "egress.decider", "workspace": "ws1"}'
    assert sock.sent == [announce]
    assert popped == [("r1", "ws1", "sess")]
    assert connect.kwargs["uri"].endswith("/api/v1/events")


async def test_watch_loop_reconnects_after_a_close(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client_env(monkeypatch)
    monkeypatch.setattr(tp, "LIVENESS_TICK_S", 0.05)
    monkeypatch.setattr(tp, "session_alive", lambda session: False)
    monkeypatch.setattr(tp, "raise_popup", lambda *a: None)

    def broken_recv() -> str:
        raise websockets.ConnectionClosed(None, Close(1000, "bye"))

    first = FakeWS([])
    first.recv = broken_recv  # type: ignore[method-assign]
    monkeypatch.setattr(tp.websockets, "connect", FakeConnect([first]))
    assert await tp.watch_loop("ws1", "sess") == 0


async def test_watch_loop_idles_while_the_session_lives(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # No frames flowing: each liveness tick re-checks the session;
    # the first pass keeps watching, the second retires.
    client_env(monkeypatch)
    monkeypatch.setattr(tp, "LIVENESS_TICK_S", 0.05)
    checks = iter([True, False])
    monkeypatch.setattr(tp, "session_alive", lambda session: next(checks))
    monkeypatch.setattr(tp.websockets, "connect", FakeConnect([FakeWS()]))
    assert await tp.watch_loop("ws1", "sess") == 0


async def test_watch_loop_exits_on_a_token_the_handshake_cannot_carry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # One line, no reconnect spin, and the token stays off the
    # message (#116 review).
    client_env(monkeypatch)
    monkeypatch.setenv("MSKSC_TOKEN", "a b")
    monkeypatch.setattr(tp.websockets, "connect", FakeConnect([]))
    with pytest.raises(SystemExit) as caught:
        await tp.watch_loop("ws1", "sess")
    assert "cannot ride" in str(caught.value)
    assert "a b" not in str(caught.value)


async def test_watch_loop_exits_on_an_auth_refusal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client_env(monkeypatch)
    monkeypatch.setattr(tp, "LIVENESS_TICK_S", 0.05)

    def refused_recv() -> str:
        raise websockets.ConnectionClosed(
            Close(wsauth.CLOSE_AUTH_FAILED, "no"), None
        )

    sock = FakeWS([])
    sock.recv = refused_recv  # type: ignore[method-assign]
    monkeypatch.setattr(tp.websockets, "connect", FakeConnect([sock]))
    with pytest.raises(SystemExit, match="authentication failed"):
        await tp.watch_loop("ws1", "sess")


# --- the popup and the tmux boundaries -------------------------------------


def test_raise_popup_targets_the_sessions_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(tp, "popup_client", lambda session: "/dev/pts/3")
    ran = {}

    def fake_run(argv, **kwargs):
        ran.update(argv=argv, kwargs=kwargs)
        return subprocess.CompletedProcess(argv, 0)

    monkeypatch.setattr(tp.subprocess, "run", fake_run)
    row = {"id": "r1", "dest_host": "api.example", "dest_port": 443}
    assert tp.raise_popup(row, "ws1", "sess") == 0
    argv = ran["argv"]
    assert argv[:4] == ["tmux", "display-popup", "-t", "/dev/pts/3"]
    assert argv[4] == "-E"
    assert argv[5:9] == ["-w", "76", "-h", "12"]
    assert "decide -w ws1 -r r1 -d api.example:443" in argv[9]
    # No attached client — the window is gone — reports nonzero.
    monkeypatch.setattr(tp, "popup_client", lambda session: None)
    assert tp.raise_popup(row, "ws1", "sess") == 1


def test_popup_client_resolves_the_attached_tty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("TMUX", raising=False)

    def fake_run(argv, **kwargs):
        return subprocess.CompletedProcess(argv, 0, stdout="/dev/pts/4\n\n")

    monkeypatch.setattr(tp.subprocess, "run", fake_run)
    assert tp.popup_client("sess") == "/dev/pts/4"

    def dead(argv, **kwargs):
        return subprocess.CompletedProcess(argv, 1)

    monkeypatch.setattr(tp.subprocess, "run", dead)
    assert tp.popup_client("sess") is None

    def nobody(argv, **kwargs):
        return subprocess.CompletedProcess(argv, 0, stdout="\n")

    monkeypatch.setattr(tp.subprocess, "run", nobody)
    assert tp.popup_client("sess") is None

    def explode(argv, **kwargs):
        raise OSError("no tmux")

    monkeypatch.setattr(tp.subprocess, "run", explode)
    assert tp.popup_client("sess") is None


def test_session_alive_reads_has_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("TMUX", raising=False)
    assert tp.session_alive("msks-no-such-session") is False

    monkeypatch.setattr(
        tp.subprocess,
        "run",
        lambda argv, **kwargs: subprocess.CompletedProcess(argv, 0),
    )
    assert tp.session_alive("sess") is True

    def explode(argv, **kwargs):
        raise OSError("no tmux")

    monkeypatch.setattr(tp.subprocess, "run", explode)
    assert tp.session_alive("sess") is False


# --- the decide role --------------------------------------------------------


def test_verdict_for_maps_the_keys() -> None:
    # The quick forms: a allows until restart, d denies now.
    assert tp.verdict_for("a") == ("allow", "tilrestart")
    assert tp.verdict_for("d") == ("deny", "once")
    # The uppercase twins hand their duration to the chooser.
    assert tp.verdict_for("A") is None
    assert tp.verdict_for("D") is None
    # Any other key — Enter, EOF, a stray letter — denies now.
    assert tp.verdict_for("") == ("deny", "once")
    assert tp.verdict_for("x") == ("deny", "once")
    assert tp.verdict_for("1") == ("deny", "once")


def test_duration_for_maps_the_chooser_keys() -> None:
    assert tp.duration_for("1") == "once"
    assert tp.duration_for("2") == "5m"
    assert tp.duration_for("3") == "15m"
    assert tp.duration_for("4") == "tilrestart"
    assert tp.duration_for("5") == "forever"
    # A stray key keeps until restart, the chooser's common case.
    assert tp.duration_for("x") == "tilrestart"


def test_span_paints_only_when_on() -> None:
    assert tp.span(True, "32", "go") == "\x1b[32mgo\x1b[0m"
    assert tp.span(False, "32", "go") == "go"


def test_ansi_follows_the_tty_and_no_color(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Tty(io.StringIO):
        def isatty(self) -> bool:
            return True

    monkeypatch.setattr(tp.sys, "stdout", Tty())
    assert tp.ansi() is True
    monkeypatch.setenv("NO_COLOR", "1")
    assert tp.ansi() is False
    monkeypatch.delenv("NO_COLOR")
    monkeypatch.setattr(tp.sys, "stdout", io.StringIO())
    assert tp.ansi() is False


def test_prompt_text_plains_and_paints() -> None:
    plain = tp.prompt_text(on=False)
    assert "[a] until restart" in plain
    assert "[A] choose duration" in plain
    assert "[d] now" in plain
    assert "[D] choose duration" in plain
    assert "(any other key denies)" in plain
    assert "\x1b" not in plain
    painted = tp.prompt_text(on=True)
    assert painted != plain
    assert "\x1b[32m[a]" in painted
    assert "\x1b[31m[d]" in painted


def test_duration_text_lists_the_durations() -> None:
    plain = tp.duration_text(on=False)
    assert "[1] once" in plain
    assert "[5] forever" in plain
    assert "\x1b" not in plain


def test_read_key_falls_back_to_a_line_when_not_a_tty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(tp.sys, "stdin", io.StringIO("4\n"))
    monkeypatch.setattr(tp.sys, "stdout", io.StringIO())
    assert tp.read_key("choose: ") == "4"


def test_read_key_reads_one_key_from_a_tty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A real pty pair: cbreak reads the one key without Enter, and
    # the terminal's saved modes come back. The key is written
    # mid-read (a timer thread) — a byte sitting in the pty's
    # queue before the cbreak switch is dropped by the termios
    # change, while the popup's own keys always arrive after it.
    master, slave = pty.openpty()
    reader = os.fdopen(slave, "r")
    monkeypatch.setattr(tp.sys, "stdin", reader)
    monkeypatch.setattr(tp.sys, "stdout", io.StringIO())
    timer = threading.Timer(0.1, os.write, args=(master, b"4"))
    timer.start()
    assert tp.read_key("choose: ") == "4"
    timer.join()
    timer = threading.Timer(0.1, os.write, args=(master, b"n"))
    timer.start()
    assert tp.read_key("again: ") == "n"
    timer.join()
    os.close(master)
    reader.close()


def test_linger_holds_the_outcome_line(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(tp, "OUTCOME_LINGER_S", 0)
    tp.linger()


def test_run_decide_posts_the_verdict(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture,
) -> None:
    client_env(monkeypatch)
    monkeypatch.setattr(tp, "linger", lambda: None)
    posted = {}

    class FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return None

    monkeypatch.setattr(tp, "api_client", lambda *_a, **_k: FakeClient())

    async def fake_request(client, method, path, json_body=None):
        posted.update(method=method, path=path, body=json_body)

    monkeypatch.setattr(tp, "request", fake_request)
    monkeypatch.setattr(tp, "read_key", lambda prompt: "a")
    rc = tp.run_decide(["-w", "ws1", "-r", "r1", "-d", "api.example:443"])
    assert rc == 0
    assert posted["method"] == "POST"
    assert posted["path"] == "/api/v1/workspaces/ws1/egress/requests/r1"
    assert posted["body"] == {"decision": "allow", "duration": "tilrestart"}
    out = capsys.readouterr().out
    assert "destination: api.example:443" in out
    assert "allow (tilrestart)" in out


def test_run_decide_uppercase_opens_the_duration_chooser(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture,
) -> None:
    client_env(monkeypatch)
    monkeypatch.setattr(tp, "linger", lambda: None)
    posted = {}

    class FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return None

    monkeypatch.setattr(tp, "api_client", lambda *_a, **_k: FakeClient())

    async def fake_request(client, method, path, json_body=None):
        posted.update(method=method, path=path, body=json_body)

    monkeypatch.setattr(tp, "request", fake_request)
    keys = iter(["A", "2"])
    monkeypatch.setattr(tp, "read_key", lambda prompt: next(keys))
    rc = tp.run_decide(["-w", "ws1", "-r", "r1", "-d", "api.example:443"])
    assert rc == 0
    assert posted["body"] == {"decision": "allow", "duration": "5m"}
    out = capsys.readouterr().out
    assert "allow (5m)" in out


def test_run_decide_names_a_post_that_cannot_land(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture,
) -> None:
    client_env(monkeypatch)
    monkeypatch.setattr(tp, "linger", lambda: None)

    class FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return None

    monkeypatch.setattr(tp, "api_client", lambda *_a, **_k: FakeClient())

    async def refuse(client, method, path, json_body=None):
        raise SystemExit("msks: 409: resolved elsewhere")

    monkeypatch.setattr(tp, "request", refuse)
    monkeypatch.setattr(tp, "read_key", lambda prompt: "y")
    rc = tp.run_decide(["-w", "ws1", "-r", "r1", "-d", "d:443"])
    assert rc == 1
    assert "resolved elsewhere" in capsys.readouterr().out


def test_run_decide_validates_its_arguments() -> None:
    with pytest.raises(SystemExit, match="usage"):
        tp.run_decide(["-w", "ws1"])


def test_decide_command_spells_the_popup_role() -> None:
    row = {"id": "r1", "dest_host": "api.example", "dest_port": 0}
    line = tp.decide_command(row, "ws1")
    assert line.startswith(tp.sys.executable)
    assert line.endswith(
        "-m msks.client.term_popup decide -w ws1 -r r1"
        " -d 'api.example (all ports)'"
    )
