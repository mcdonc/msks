"""The tmux consent-terminal launcher tests (#379).

Every tmux/terminal boundary is a recorded subprocess call or a
stubbed function — the suite pins the argv contracts (the
session's tmux line, the popup's display-popup line, the decide
role's POST), the watcher's frame handling, and the liveness
retirement, never opening a window.
"""

import asyncio
import fcntl
import io
import json
import os
import pty
import shlex
import shutil
import struct
import subprocess
import termios
import threading
import time
import tty
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


def no_daemon_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Drop the daemon presets an ambient shell may carry: the
    launch role now resolves the status label over REST, and a
    preset pointing at a live (or wedged) dev daemon would have
    every launch test dial it — the token's absence stops the
    lookup before a dial, the same fallback the window ships."""
    monkeypatch.delenv("MSKSC_TOKEN", raising=False)


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
        SSH_CHILD,
        session="a1b2c3d4e5",
        workspace_id="a1b2c3d4e5",
        label="project-x",
    )
    assert argv[0:2] == ["tmux", "-L"]
    assert argv[2].startswith("msks-a1b2c3d4e5-")
    # The joined pane command is the word after new-session's
    # session name; deriving it (not a positional index) keeps
    # this assert readable when the chain changes shape.
    i = argv.index("new-session")
    joined = argv[i + 3]
    assert argv[3:] == [
        # The scrollback options (#434) land on this launch's own
        # server ahead of the session: a pane adopts its history
        # limit only at creation, and mouse mode read live still
        # wants to be on the server the session is born from.
        # set-titles pins off for the configured window title
        # (#445): the fresh server reads the operator's tmux.conf,
        # and its set-titles on would take the title over.
        "start-server",
        ";",
        "set-option",
        "-g",
        "mouse",
        "on",
        ";",
        "set-option",
        "-g",
        "history-limit",
        str(tp.HISTORY_LINES),
        # The page-scroll bindings (#444) ride the same server:
        # the shifted page keys page the same history the wheel
        # scrolls, in the stock wheel binding's own vehicle — an
        # if -F whose quoted branches keep the embedded separator
        # from splitting the launch chain — with commands every
        # documented-supported tmux accepts at bind time (the man
        # page's -d is 3.5-only, and bind-time validation would
        # abort the whole launch on a 3.2–3.4 tmux).
        ";",
        "bind-key",
        "-n",
        "S-PgUp",
        "if -F '#{pane_in_mode}' 'send-keys -X page-up'"
        " 'copy-mode -e; send-keys -X page-up'",
        ";",
        "bind-key",
        "-n",
        "S-PgDn",
        "if -F '#{pane_in_mode}' 'send-keys -X page-down'"
        " 'copy-mode -e; send-keys -X page-down'",
        ";",
        "set-option",
        "-g",
        "set-titles",
        "off",
        # The status bar's left side takes the resolved name
        # (#455) — a label apart from the id, so the pin proves
        # the label lands, and the length budget keeps tmux's
        # ten-cell default from clipping it. The window-list
        # formats and the right side pin to empty (#458): the
        # default list would follow the label, the default right
        # side would trail it, and the label is the whole bar.
        ";",
        "set-option",
        "-g",
        "status-left",
        "[project-x] ",
        ";",
        "set-option",
        "-g",
        "status-left-length",
        str(tp.STATUS_LEFT_LENGTH),
        ";",
        "set-option",
        "-g",
        "window-status-format",
        "",
        ";",
        "set-option",
        "-g",
        "window-status-current-format",
        "",
        ";",
        "set-option",
        "-g",
        "status-right",
        "",
        ";",
        "new-session",
        "-s",
        "a1b2c3d4e5",
        joined,
        ";",
        "set-option",
        "destroy-unattached",
        "on",
    ]
    assert joined.startswith(f"{tp.sys.executable} -m msks.client.term_popup")
    # The pane argv carries the workspace: the watcher is the
    # feature, and nothing else can re-derive the id.
    assert (
        " pane -s a1b2c3d4e5 -w a1b2c3d4e5 -- /venv/bin/python -m"
        " msks.client.cli ssh a1b2c3d4e5" in joined
    )


def test_session_argv_escapes_a_hash_in_the_label() -> None:
    # A workspace name can carry the character tmux's formats
    # read — a `#[` pair would repaint the bar — so it doubles
    # into the literal form tmux's own escape is.
    argv = tp.session_argv(
        ["top"], session="s", workspace_id=None, label="a#b"
    )
    assert argv[argv.index("status-left") + 1] == "[a##b] "


def test_session_argv_without_a_workspace_ships_no_watcher() -> None:
    argv = tp.session_argv(
        ["top"], session="shell", workspace_id=None, label="shell"
    )
    joined = argv[argv.index("new-session") + 3]
    assert " pane -s shell -- top" in joined
    assert " -w " not in joined
    assert argv[argv.index("status-left") + 1] == "[shell] "


def test_page_keys_scroll_the_session_history() -> None:
    """The #444 boundary against a real tmux server: an attached
    client's tty takes the well-known shifted page sequences and
    the shipped bindings page the pane's history — up into copy
    mode, back down out of it — while the bare page key passes
    through to the pane's application. The session runs detached
    on its own socket and the client rides a local pty, so no
    window opens anywhere."""
    # The suite's devenv ships tmux; the guard covers foreign hosts.
    if shutil.which("tmux") is None:  # pragma: no cover
        pytest.skip("tmux is not on PATH")
    socket = f"msks-test-{os.getpid()}"
    session = "scrolltest"
    # The shipped server half, ahead of new-session: exactly the
    # options and bindings the launcher lays on its own server
    # (the slice already ends with the chain's separator).
    half = tp.session_argv(
        ["true"], session=session, workspace_id=None, label="shell"
    )
    half = half[: half.index("new-session")]

    def run(*words: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["tmux", "-L", socket, *words],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )

    def pane_flag(flag: str) -> str:
        proc = run("display", "-p", "-t", session, f"#{{{flag}}}")
        if proc.returncode != 0:
            raise AssertionError(
                f"tmux display -p #{{{flag}}} failed: {proc.stderr.strip()}"
            )
        return proc.stdout.strip()

    master, slave = pty.openpty()
    fcntl.ioctl(master, termios.TIOCSWINSZ, struct.pack("HHHH", 10, 80, 0, 0))

    def eventually(flag: str, want: str, deadline: float = 5.0) -> None:
        end = time.monotonic() + deadline
        while time.monotonic() < end:
            if pane_flag(flag) == want:
                return
            time.sleep(0.1)
        raise AssertionError(
            f"#{flag} stayed {pane_flag(flag)!r}, wanted {want!r}"
        )

    def eventually_at_least(
        flag: str, want: int, deadline: float = 5.0
    ) -> None:
        end = time.monotonic() + deadline
        while time.monotonic() < end:
            value = pane_flag(flag)
            if value.isdigit() and int(value) >= want:
                return
            time.sleep(0.1)
        raise AssertionError(
            f"#{flag} stayed {value!r}, wanted at least {want}"
        )

    client = None
    try:
        subprocess.run(
            [
                "tmux",
                "-L",
                socket,
                *half[3:],
                "new-session",
                "-d",
                "-s",
                session,
                "-x",
                "80",
                "-y",
                "10",
                "sh -c 'seq 1 50; exec cat'",
            ],
            timeout=10,
            check=True,
            capture_output=True,
        )
        client = subprocess.Popen(
            ["tmux", "-L", socket, "attach", "-t", session],
            stdin=slave,
            stdout=slave,
            stderr=slave,
            start_new_session=True,
            env={**os.environ, "TERM": "xterm"},
        )
        os.close(slave)
        # 50 lines printed on a 10-row window: the ones that left
        # the viewport are the history the page keys walk (the
        # count carries the pane's own startup lines, so the wait
        # asks for a depth of pages, not an exact figure).
        eventually_at_least("history_size", 30)
        os.write(master, b"\x1b[5;2~")  # S-PgUp pages into copy mode
        eventually("pane_in_mode", "1")
        os.write(master, b"\x1b[6;2~")  # S-PgDn pages back down; the
        os.write(master, b"\x1b[6;2~")  # bottom exits copy mode
        eventually("pane_in_mode", "0")
        os.write(master, b"\x1b[5~")  # the bare page key reaches
        # the pane's cat, not a binding: copy mode stays closed
        # across a window of checks, not one sampling of it.
        end = time.monotonic() + 1.0
        while time.monotonic() < end:
            assert pane_flag("pane_in_mode") == "0"
            time.sleep(0.1)
    finally:
        run("kill-server")  # the server's death takes the session
        if client is not None:
            client.kill()
            client.wait(timeout=10)
        os.close(master)


def test_the_status_bar_carries_the_workspace_name() -> None:
    """The #455/#458 boundary against a real tmux server: the
    shipped option chain writes the resolved name onto the bar —
    read back through the session it names, expanded the way the
    bar itself renders it — with the length budget raised past
    tmux's ten-cell default, and the window-list formats and the
    right side read back empty, so the label is the whole bar.
    The session runs detached on its own socket with a pane that
    stays alive (a pane that exits takes the last session — and
    its server — with it), so no window opens anywhere and the
    read-back never races the session's death."""
    if shutil.which("tmux") is None:  # pragma: no cover
        pytest.skip("tmux is not on PATH")
    socket = f"msks-test-{os.getpid()}"
    session = "barname"
    half = tp.session_argv(
        ["true"], session=session, workspace_id=None, label="project-x"
    )
    half = half[: half.index("new-session")]
    try:
        subprocess.run(
            [
                "tmux",
                "-L",
                socket,
                *half[3:],
                "new-session",
                "-d",
                "-s",
                session,
                "-x",
                "80",
                "-y",
                "10",
                "sh -c 'exec cat'",
            ],
            timeout=10,
            check=True,
            capture_output=True,
        )
        proc = subprocess.run(
            [
                "tmux",
                "-L",
                socket,
                "display",
                "-p",
                "-t",
                session,
                "#{E:status-left}|#{status-left-length}"
                "|#{window-status-format}|#{window-status-current-format}"
                "|#{status-right}",
            ],
            capture_output=True,
            text=True,
            timeout=10,
            check=True,
        )
        assert proc.stdout.strip() == (
            f"[project-x] |{tp.STATUS_LEFT_LENGTH}|||"
        )
    finally:
        subprocess.run(
            ["tmux", "-L", socket, "kill-server"],
            timeout=10,
            check=False,
            capture_output=True,
        )


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
    no_daemon_env(monkeypatch)
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


def test_a_missing_tmux_leaves_the_window_title_alone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The refusal path stops before the window could open
    # half-way — its title included: a configured template never
    # reaches a window tmux is not there to fill.
    monkeypatch.setattr(tp.shutil, "which", lambda tool: None)
    monkeypatch.setenv("MSKSC_TERMINAL_TITLE", "msks — {workspace}")
    out = Tty()
    monkeypatch.setattr(tp.sys, "stdout", out)
    with pytest.raises(SystemExit, match="tmux is not on PATH"):
        tp.run_launch(list(SSH_CHILD))
    assert out.getvalue() == ""


def test_run_launch_execs_the_tmux_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(tp.shutil, "which", lambda tool: "/bin/" + tool)
    no_daemon_env(monkeypatch)
    seen = {}
    monkeypatch.setattr(
        tp.os,
        "execvp",
        lambda binname, argv: seen.update(binname=binname, argv=argv),
    )
    assert tp.run_launch(list(SSH_CHILD)) == 0
    assert seen["binname"] == "tmux"
    assert seen["argv"][:2] == ["tmux", "-L"]
    argv = seen["argv"]
    i = argv.index("new-session")
    # The socket sits at a fixed position ahead of the chain; the
    # session name is the word after new-session itself.
    assert argv[2].startswith("msks-a1b2c3d4e5-")
    assert argv[i + 2] == "a1b2c3d4e5"


def test_run_launch_puts_the_resolved_name_on_the_status_bar(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The launcher resolves the label and hands it to the argv
    # builder — the seam the unit tests below fill, joined here
    # to the chain the client execs.
    monkeypatch.setattr(tp.shutil, "which", lambda tool: "/bin/" + tool)
    monkeypatch.setattr(tp, "status_label", lambda ws: "project-x")
    seen = {}
    monkeypatch.setattr(
        tp.os, "execvp", lambda binname, argv: seen.update(argv=argv)
    )
    assert tp.run_launch(list(SSH_CHILD)) == 0
    assert seen["argv"][seen["argv"].index("status-left") + 1] == (
        "[project-x] "
    )


# --- the status bar's workspace label (#455) --------------------------------


def test_status_label_uses_the_daemons_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client_env(monkeypatch)
    seen = {}

    async def row(workspace_id: str, url: str, token: str) -> dict:
        seen.update(workspace_id=workspace_id, url=url, token=token)
        return {"name": "project-x"}

    monkeypatch.setattr(tp, "workspace_row", row)
    assert tp.status_label("a1b2c3d4e5") == "project-x"
    assert seen == {
        "workspace_id": "a1b2c3d4e5",
        "url": "https://daemon",
        "token": "tok",
    }
    # A child naming no workspace is a plain shell, its label its
    # session's own name.
    assert tp.status_label(None) == "shell"


def test_status_label_falls_back_to_the_id_when_the_lookup_cannot_land(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client_env(monkeypatch)

    async def refused(workspace_id: str, url: str, token: str) -> dict:
        raise SystemExit("msks: 404: no such workspace")

    monkeypatch.setattr(tp, "workspace_row", refused)
    assert tp.status_label("a1b2c3d4e5") == "a1b2c3d4e5"

    async def unnamed(workspace_id: str, url: str, token: str) -> dict:
        return {"name": ""}

    monkeypatch.setattr(tp, "workspace_row", unnamed)
    assert tp.status_label("a1b2c3d4e5") == "a1b2c3d4e5"

    # A token the environment never carried stops the lookup
    # before a dial: the id stands in, no window waits on it.
    monkeypatch.delenv("MSKSC_TOKEN", raising=False)
    assert tp.status_label("a1b2c3d4e5") == "a1b2c3d4e5"


def test_status_label_times_out_to_the_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client_env(monkeypatch)
    monkeypatch.setattr(tp, "LABEL_WAIT_S", 0.05)

    async def hang(workspace_id: str, url: str, token: str) -> dict:
        await asyncio.sleep(30)
        return {"name": "late"}

    monkeypatch.setattr(tp, "workspace_row", hang)
    assert tp.status_label("a1b2c3d4e5") == "a1b2c3d4e5"


# --- the configured window title (#445) -----------------------------------


class Tty(io.StringIO):
    """A stdout the title writer accepts — a pipe answers
    isatty() False and stays clean. The emission itself lives in
    :mod:`msks.client.wintitle` and is tested there."""

    def isatty(self) -> bool:
        return True


def test_run_launch_titles_the_window_before_the_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(tp.shutil, "which", lambda tool: "/bin/" + tool)
    no_daemon_env(monkeypatch)
    monkeypatch.setattr(tp.os, "execvp", lambda binname, argv: None)
    monkeypatch.setenv("MSKSC_TERMINAL_TITLE", "msks — {workspace}")
    out = Tty()
    monkeypatch.setattr(tp.sys, "stdout", out)
    assert tp.run_launch(list(SSH_CHILD)) == 0
    assert out.getvalue() == "\x1b]0;msks — a1b2c3d4e5\x07"


def test_run_launch_without_a_title_writes_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(tp.shutil, "which", lambda tool: "/bin/" + tool)
    no_daemon_env(monkeypatch)
    monkeypatch.setattr(tp.os, "execvp", lambda binname, argv: None)
    monkeypatch.setenv("MSKSC_TERMINAL_TITLE", "")
    out = Tty()
    monkeypatch.setattr(tp.sys, "stdout", out)
    assert tp.run_launch(list(SSH_CHILD)) == 0
    assert out.getvalue() == ""


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
    argv = tp.session_argv(
        SSH_CHILD,
        session="a1b2c3d4e5",
        workspace_id="a1b2c3d4e5",
        label="a1b2c3d4e5",
    )
    joined = argv[argv.index("new-session") + 3]
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
    # The new-window marker (#445) is gone before the child runs:
    # the window is already titled (the launch role wrote it) and
    # tmux owns the pane's escapes.
    monkeypatch.setenv(tp.TITLE_MARKER, "1")
    tp.run_pane(["-s", "sess", "-w", "ws1", "--", *SSH_CHILD])
    assert tp.TITLE_MARKER not in os.environ
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


async def until(predicate, timeout: float = 2.0) -> None:
    """Await a condition the loop's own scheduling settles — a
    task's start, a thread's record — or name the wait's failure."""
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if predicate():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("condition did not arrive")


def rules_frame() -> str:
    """The registration's first frame: the rules view that adopts
    the server's canonical workspace id (#297)."""
    return json.dumps(
        {
            "event": "egress.rules",
            "data": {
                "workspace_id": "ws1",
                "mode": "interactive",
                "allow_list": [],
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


async def test_watch_frame_raises_one_popup_and_latches_the_rest(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gate = threading.Event()
    popped = []

    def slow_popup(workspace_id: str, session: str) -> int:
        popped.append((workspace_id, session))
        assert gate.wait(5)
        return 0

    monkeypatch.setattr(tp, "raise_popup", slow_popup)
    state = tp.WatchState()
    await tp.watch_frame(json.loads(request_frame("r1")), "ws1", "sess", state)
    await until(lambda: len(popped) == 1)
    # The popup stands: a second request joins its list, and its
    # frame raises nothing of its own.
    await tp.watch_frame(json.loads(request_frame("r2")), "ws1", "sess", state)
    assert state.popup_up is True
    gate.set()
    await until(lambda: not state.popup_up)
    assert popped == [("ws1", "sess")]
    # The latch open again: the next request raises.
    await tp.watch_frame(json.loads(request_frame("r2")), "ws1", "sess", state)
    await until(lambda: len(popped) == 2)
    gate.set()
    await until(lambda: not state.popup_up)


async def test_watch_frame_skips_the_decided_and_the_shapeless(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    popped = []
    monkeypatch.setattr(tp, "raise_popup", lambda *a: popped.append(a))
    state = tp.WatchState()
    state.resolved = {"r1"}
    await tp.watch_frame(json.loads(request_frame("r1")), "ws1", "sess", state)
    await tp.watch_frame(
        {"event": "egress.request", "data": {}}, "ws1", "sess", state
    )
    await tp.watch_frame(
        {"event": "egress.request", "data": {"request": "junk"}},
        "ws1",
        "sess",
        state,
    )
    await asyncio.sleep(0.05)
    assert popped == []
    # A resolution's id records whatever raised it; a frame that
    # names none, and a frame the watcher does not know, both
    # pass it by.
    await tp.watch_frame(
        json.loads(resolved_frame("r2")), "ws1", "sess", state
    )
    assert state.resolved == {"r1", "r2"}
    await tp.watch_frame(
        {"event": "egress.resolved", "data": {"request_id": None}},
        "ws1",
        "sess",
        state,
    )
    await tp.watch_frame(
        {"event": "egress.rules", "data": {}}, "ws1", "sess", state
    )
    assert state.resolved == {"r1", "r2"}


async def test_watch_frame_gives_up_when_tmux_is_gone(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    def boom(workspace_id: str, session: str) -> int:
        raise OSError("no tmux")

    monkeypatch.setattr(tp, "raise_popup", boom)
    state = tp.WatchState()
    await tp.watch_frame(json.loads(request_frame("r1")), "ws1", "sess", state)
    await until(lambda: state.gave_up)
    await until(lambda: not state.popup_up)
    assert "popup unavailable: no tmux" in capsys.readouterr().out
    # The watcher stays registered: a later request raises nothing
    # and breaks nothing.
    await tp.watch_frame(json.loads(request_frame("r2")), "ws1", "sess", state)
    await asyncio.sleep(0.05)
    assert state.popup_up is False


async def test_watch_frame_stops_on_a_refused_registration(
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
    with pytest.raises(SystemExit):
        await tp.watch_frame(rejected, "nope", "sess", tp.WatchState())
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
        lambda ws, session: popped.append((ws, session)),
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
    await until(lambda: popped == [("ws1", "sess")])
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
    assert tp.raise_popup("ws1", "sess") == 0
    argv = ran["argv"]
    assert argv[:4] == ["tmux", "display-popup", "-t", "/dev/pts/3"]
    assert argv[4] == "-E"
    assert argv[5:9] == ["-w", "76", "-h", "16"]
    assert "decide -w ws1" in argv[9]
    # No attached client — the window is gone — reports nonzero.
    monkeypatch.setattr(tp, "popup_client", lambda session: None)
    assert tp.raise_popup("ws1", "sess") == 1


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


def test_chooser_line_lists_the_durations() -> None:
    plain = tp.chooser_line(on=False)
    assert "[1] once" in plain
    assert "[5] forever" in plain
    assert "\x1b" not in plain


def test_clip_cuts_long_labels() -> None:
    assert tp.clip("abc", 5) == "abc"
    assert tp.clip("abcdef", 5) == "abcd…"
    assert tp.clip("x", 0) == ""


def test_popup_size_reads_the_pty_and_falls_back(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    master, slave = pty.openpty()
    out = os.fdopen(slave, "w")
    monkeypatch.setattr(tp.sys, "stdout", out)
    fcntl.ioctl(master, termios.TIOCSWINSZ, struct.pack("HHHH", 24, 100, 0, 0))
    assert tp.popup_size() == (100, 24)
    # A pty that answers nothing keeps the shipped geometry.
    fcntl.ioctl(master, termios.TIOCSWINSZ, struct.pack("HHHH", 0, 0, 0, 0))
    assert tp.popup_size() == (tp.POPUP_COLS, tp.POPUP_ROWS)
    os.close(master)
    out.close()
    # So does a stdout with no fd at all (a pipe, a test).
    monkeypatch.setattr(tp.sys, "stdout", io.StringIO())
    assert tp.popup_size() == (tp.POPUP_COLS, tp.POPUP_ROWS)


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


# --- the popup's key surface ------------------------------------------------


def test_key_decoder_maps_sequences_and_singles() -> None:
    decoder = tp.KeyDecoder()
    assert decoder.feed(b"\x1b[Ba") == ["down", "a"]
    assert decoder.feed(b"\x1bOA") == ["up"]
    assert decoder.feed(b"\r") == ["enter"]
    assert decoder.feed(b"\x04") == ["esc"]
    # Control bytes drop; left and right count for nothing.
    assert decoder.feed(b"\x00\x7f") == []
    assert decoder.feed(b"\x1b[D") == []
    # An unknown CSI body drops its leader and keeps scanning.
    assert decoder.feed(b"\x1b[Zx") == ["x"]
    # A parameterized sequence drops whole, parameters and all.
    assert decoder.feed(b"\x1b[1;5Aa") == ["a"]
    # A control byte inside a body ends it: the tail survives.
    assert decoder.feed(b"\x1b[\x08x") == ["x"]
    # An unknown SS3 pair drops whole; a partial one parks.
    assert decoder.feed(b"\x1bOPa") == ["a"]
    parked = tp.KeyDecoder()
    assert parked.feed(b"\x1bO") == []
    assert parked.parked is True
    # An Esc pair (Alt+key) degrades to the key it carried.
    assert decoder.feed(b"\x1bx") == ["x"]


def test_key_decoder_lone_esc_counts_after_the_wait() -> None:
    decoder = tp.KeyDecoder()
    assert decoder.feed(b"\x1b") == []
    assert decoder.parked is True
    assert decoder.lapse() == ["esc"]
    # A partial sequence had its tail lost: it drops silently.
    partial = tp.KeyDecoder()
    assert partial.feed(b"\x1b[") == []
    assert partial.lapse() == []
    # A tail that arrives in time still completes the sequence.
    split = tp.KeyDecoder()
    assert split.feed(b"\x1b") == []
    assert split.feed(b"[B") == ["down"]


async def test_key_source_pumps_a_pty() -> None:
    master, slave = pty.openpty()
    saved = termios.tcgetattr(slave)
    tty.setcbreak(slave)  # the popup runs the pty in cbreak; a
    # canonical line buffer would sit on the keys until Enter.
    source = tp.KeySource(slave)
    try:
        os.write(master, b"\x1b[Ba")
        assert await asyncio.wait_for(source.keys.get(), 2) == "down"
        assert await asyncio.wait_for(source.keys.get(), 2) == "a"
        # A lone Esc waits out its tail-window before it counts.
        os.write(master, b"\x1b")
        assert await asyncio.wait_for(source.keys.get(), 2) == "esc"
    finally:
        source.close()
        termios.tcsetattr(slave, termios.TCSADRAIN, saved)
        os.close(master)
        os.close(slave)


async def test_key_source_settles_and_closes_its_timer() -> None:
    master, slave = pty.openpty()
    source = tp.KeySource(slave)
    source.decoder.buf += b"\x1b"
    source.settle()
    source.settle()  # a second park re-arms the wait
    assert source.timer is not None
    source.lapse()
    assert source.timer is None
    assert source.keys.get_nowait() == "esc"
    source.decoder.buf += b"\x1b"
    source.settle()
    source.close()  # an armed timer dies with the source
    assert source.timer is None
    os.close(master)
    os.close(slave)


async def test_key_source_survives_a_dead_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    master, slave = pty.openpty()
    saved = termios.tcgetattr(slave)
    tty.setcbreak(slave)
    source = tp.KeySource(slave)

    def gone(fd: int, count: int) -> bytes:
        raise OSError("fd closed")

    monkeypatch.setattr(tp.os, "read", gone)
    os.write(master, b"a")  # the readable edge meets the dead read
    await asyncio.sleep(0.05)
    monkeypatch.undo()
    source.close()
    termios.tcsetattr(slave, termios.TCSADRAIN, saved)
    os.close(master)
    os.close(slave)


# --- the popup UI -----------------------------------------------------------


def ui(now: dict | None = None) -> tp.PopupUI:
    """A plain-paint UI on a movable clock — render() touches no
    terminal, so the assertions read the frames as text."""
    clock = (lambda: now["t"]) if now is not None else (lambda: 1000.0)
    return tp.PopupUI("ws1", clock=clock, on=False)


def test_the_list_renders_rows_and_countdowns() -> None:
    view = ui()
    assert view.frame(rules_frame()) is False
    view.frame(request_frame("r1", expires_at=1045.0))
    view.frame(
        request_frame(
            "r2", host="cdn.example", requested_at=11.0, expires_at=1041.0
        )
    )
    lines = view.render(76, 16).split("\n")
    assert lines[0].strip() == "pending egress requests: 2"
    assert "> api.example:443" in lines[2]
    assert lines[2].endswith("45s")
    assert "  cdn.example:443" in lines[3]
    assert lines[3].endswith("41s")
    # The key map's three rows and the status row follow the list.
    assert "[a] until restart" in lines[5]
    assert "[d] now" in lines[6]
    assert "[↑]/[↓] move" in lines[7]
    assert lines[8] == ""
    assert "\x1b" not in "\n".join(lines)


def test_the_list_clips_long_rows_and_deep_lists() -> None:
    view = ui()
    view.frame(rules_frame())
    view.frame(
        request_frame("r1", host="a-very-long-hostname.example.internal")
    )
    narrow = view.render(40, 16).splitlines()
    assert "…" in narrow[2]
    assert len(narrow[2]) <= 40
    # More holds than the window: the header counts the hidden.
    deep = ui()
    deep.frame(rules_frame())
    for i in range(7):
        deep.frame(request_frame(f"r{i}", host=f"h{i}.example"))
    short = deep.render(76, 12).splitlines()
    assert short[0].strip().startswith("pending egress requests: 7")
    assert "(+2 more)" in short[0]


async def test_keys_navigate_and_decide(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    posted = []

    async def fake_post(ws, rid, decision, duration):
        posted.append((ws, rid, decision, duration))

    monkeypatch.setattr(tp, "post_verdict", fake_post)
    view = ui()
    view.frame(rules_frame())
    view.frame(request_frame("r1"))
    view.frame(request_frame("r2", host="cdn.example"))
    assert view.key("down") is False
    assert view.index == 1
    view.key("up")
    view.key("up")  # clamps at the top
    assert view.index == 0
    view.key("x")  # outside the map: nothing changes
    assert not view.posts
    view.key("a")
    await asyncio.gather(*view.posts)
    assert posted == [("ws1", "r1", "allow", "tilrestart")]
    assert view.status == (
        "allow api.example:443 (tilrestart)",
        tp.PAINT_ALLOW,
    )
    assert view.exited is False


async def test_the_chooser_takes_a_duration_and_esc_backs_out(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    posted = []

    async def fake_post(ws, rid, decision, duration):
        posted.append((ws, rid, decision, duration))

    monkeypatch.setattr(tp, "post_verdict", fake_post)
    view = ui()
    view.frame(rules_frame())
    view.frame(request_frame("r1"))
    view.key("D")
    assert "duration:" in view.render(76, 16)
    view.key("esc")  # the chooser backs out, nothing posts
    assert view.chooser is None
    view.key("D")
    view.key("up")  # arrows hold the chooser open
    assert view.chooser == "deny"
    view.key("3")
    await asyncio.gather(*view.posts)
    assert posted == [("ws1", "r1", "deny", "15m")]
    assert view.status == ("deny api.example:443 (15m)", tp.PAINT_DENY)


async def test_a_post_that_cannot_land_names_its_reason(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def refuse(ws, rid, decision, duration):
        raise SystemExit("msks: 409: resolved elsewhere")

    monkeypatch.setattr(tp, "post_verdict", refuse)
    view = ui()
    view.frame(rules_frame())
    view.frame(request_frame("r1"))
    view.key("a")
    await asyncio.gather(*view.posts)
    assert view.status == (
        "msks: 409: resolved elsewhere",
        tp.PAINT_DENY,
    )
    # The hold stays for its own exit: the popup does not.
    assert view.exited is False


def test_frames_drop_resolved_holds_and_clamp_the_selection() -> None:
    view = ui()
    view.frame(rules_frame())
    for rid in ("r1", "r2", "r3"):
        view.frame(request_frame(rid))
    view.move(1)
    view.move(1)
    assert view.index == 2
    view.frame(resolved_frame("r3"))
    assert view.index == 1
    view.frame(resolved_frame("r1"))
    assert view.index == 0
    assert [hold.id for hold in view.rows()] == ["r2"]


def test_the_empty_list_closes_after_the_grace() -> None:
    now = {"t": 1000.0}
    view = ui(now)
    view.frame(rules_frame())
    view.frame(request_frame("r1"))
    view.frame(resolved_frame("r1"))
    now["t"] = 1000.4
    assert view.tick() is False  # inside the grace window
    now["t"] = 1000.6
    assert view.tick() is True  # past it, and the list is empty
    assert view.exit_code == 0
    # The frames never landed: no close, whatever the clock says.
    assert ui(now).tick() is False


def test_a_refused_registration_exits_with_the_reason() -> None:
    view = ui()
    rejected = json.dumps(
        {
            "event": "egress.decider_rejected",
            "data": {"reason": "unknown workspace"},
        }
    )
    assert view.frame(rejected) is True
    assert view.exit_code == 1
    assert view.status == (
        "decider registration refused: unknown workspace",
        tp.PAINT_DENY,
    )


async def test_session_result_reraises_the_unexpected() -> None:
    async def bug() -> None:
        raise ValueError("bug")

    broken = asyncio.create_task(bug())
    await asyncio.sleep(0)
    with pytest.raises(ValueError, match="bug"):
        tp.session_result([broken])
    # A cancelled task carries no verdict of its own.
    stopped = asyncio.create_task(asyncio.sleep(30))
    stopped.cancel()
    await asyncio.gather(stopped, return_exceptions=True)
    assert tp.session_result([stopped]) is None


def test_paint_marks_the_exit_when_the_pty_dies(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Dead(io.StringIO):
        def write(self, text):
            raise OSError("gone")

        def flush(self) -> None:
            return None

    monkeypatch.setattr(tp.sys, "stdout", Dead())
    view = ui()
    view.frame(rules_frame())
    view.frame(request_frame("r1"))
    assert view.exited is True


def test_cursor_writes_only_to_a_terminal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    out = Tty()
    monkeypatch.setattr(tp.sys, "stdout", out)
    tp.cursor(tp.HIDE_CURSOR)
    tp.cursor(tp.SHOW_CURSOR)
    assert out.getvalue() == "\x1b[?25l\x1b[?25h"
    plain = io.StringIO()
    monkeypatch.setattr(tp.sys, "stdout", plain)
    tp.cursor(tp.HIDE_CURSOR)
    assert plain.getvalue() == ""


async def test_the_edges_of_an_empty_popup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    view = ui()
    view.move(1)  # nothing to move
    view.decide("allow", "once")  # nothing to decide
    assert view.posts == set()
    assert view.frame(rules_frame()) is False
    junk = json.dumps({"event": "junk", "data": {}})
    assert view.frame(junk) is False  # an unhandled frame changes nothing
    monkeypatch.setattr(tp, "OUTCOME_LINGER_S", 5)
    # A close without a line to read skips the beat entirely.
    await asyncio.wait_for(view.close_out(), 0.1)


def test_a_resync_clears_stale_rows() -> None:
    view = ui()
    view.frame(rules_frame())
    view.frame(request_frame("r1"))
    view.resync = True  # a reconnect's registration
    view.frame(rules_frame())  # its snapshot lands nothing
    assert view.rows() == []
    view.frame(request_frame("r2"))
    assert [hold.id for hold in view.rows()] == ["r2"]
    # A mid-connection rules refresh clears nothing.
    view.frame(rules_frame())
    assert [hold.id for hold in view.rows()] == ["r2"]


# --- the decide loop --------------------------------------------------------


class ClosingWS(FakeWS):
    """A scripted websocket that closes once its frames run out —
    the reconnect path's connection."""

    async def recv(self) -> str:
        if self.frames:
            return self.frames.pop(0)
        raise websockets.ConnectionClosed(None, Close(1000, "bye"))


async def scripted(
    monkeypatch: pytest.MonkeyPatch, connections: list
) -> FakeConnect:
    """Wire the decide loop to scripted connections on a fast
    clock, with stdin a non-terminal (the keys arrive by queue
    or not at all)."""
    client_env(monkeypatch)
    monkeypatch.setattr(tp.sys, "stdin", io.StringIO())
    monkeypatch.setattr(tp, "SNAPSHOT_GRACE_S", 0.05)
    monkeypatch.setattr(tp, "TICK_S", 0.02)
    monkeypatch.setattr(tp, "OUTCOME_LINGER_S", 0)
    connect = FakeConnect(connections)
    monkeypatch.setattr(tp.websockets, "connect", connect)
    return connect


async def fake_posts(monkeypatch: pytest.MonkeyPatch) -> list:
    """Record the verdict posts in place of the REST contract."""
    posted = []

    async def fake_post(ws, rid, decision, duration):
        posted.append((rid, decision, duration))

    monkeypatch.setattr(tp, "post_verdict", fake_post)
    return posted


async def test_decide_loop_lists_decides_and_leaves(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    posted = await fake_posts(monkeypatch)
    sock = FakeWS(
        [
            rules_frame(),
            request_frame("r1"),
            request_frame("r2", host="cdn.example"),
        ]
    )
    await scripted(monkeypatch, [sock])
    keys: asyncio.Queue[str] = asyncio.Queue()
    task = asyncio.create_task(tp.decide_loop("ws1", keys))
    await asyncio.sleep(0.1)  # the frames land before the keys
    keys.put_nowait("down")
    await asyncio.sleep(0.05)
    keys.put_nowait("a")
    await until(lambda: len(posted) == 1)
    keys.put_nowait("esc")
    assert await task == 0
    assert posted == [("r2", "allow", "tilrestart")]
    assert sock.sent == ['{"type": "egress.decider", "workspace": "ws1"}']
    out = capsys.readouterr().out
    assert "pending egress requests: 2" in out
    assert "api.example:443" in out
    assert "cdn.example:443" in out


async def test_decide_loop_closes_on_an_empty_snapshot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await scripted(monkeypatch, [FakeWS([rules_frame()])])
    assert await tp.decide_loop("ws1") == 0


async def test_decide_loop_drops_a_hold_that_expires(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    posted = await fake_posts(monkeypatch)
    await scripted(
        monkeypatch,
        [FakeWS([rules_frame(), request_frame("r1"), resolved_frame("r1")])],
    )
    assert await tp.decide_loop("ws1") == 0
    assert posted == []
    out = capsys.readouterr().out
    assert "api.example:443" in out
    assert "pending egress requests: 0" in out


async def test_decide_loop_resyncs_after_a_reconnect(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    # The first connection delivers a hold and drops; the hold
    # resolves during the outage, and the second connection's
    # snapshot lands nothing — the resync clears the stale row
    # instead of pinning it to the list forever.
    first = ClosingWS([rules_frame(), request_frame("r1")])
    second = FakeWS([rules_frame()])
    await scripted(monkeypatch, [first, second])
    assert await tp.decide_loop("ws1") == 0
    assert "pending egress requests: 0" in capsys.readouterr().out


async def test_decide_loop_exits_on_a_refused_registration(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    await scripted(
        monkeypatch,
        [
            FakeWS(
                [
                    json.dumps(
                        {
                            "event": "egress.decider_rejected",
                            "data": {"reason": "unknown workspace"},
                        }
                    )
                ]
            )
        ],
    )
    assert await tp.decide_loop("ws1") == 1
    out = capsys.readouterr().out
    assert "decider registration refused: unknown workspace" in out


async def test_decide_loop_exits_on_a_token_the_handshake_cannot_carry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client_env(monkeypatch)
    monkeypatch.setenv("MSKSC_TOKEN", "a b")
    monkeypatch.setattr(tp.websockets, "connect", FakeConnect([]))
    with pytest.raises(SystemExit) as caught:
        await tp.decide_loop("ws1")
    assert "cannot ride" in str(caught.value)
    assert "a b" not in str(caught.value)


async def test_decide_loop_exits_on_an_auth_refusal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client_env(monkeypatch)

    def refused_recv() -> str:
        raise websockets.ConnectionClosed(
            Close(wsauth.CLOSE_AUTH_FAILED, "no"), None
        )

    sock = FakeWS([])
    sock.recv = refused_recv  # type: ignore[method-assign]
    monkeypatch.setattr(tp.websockets, "connect", FakeConnect([sock]))
    with pytest.raises(SystemExit, match="authentication failed"):
        await tp.decide_loop("ws1")


async def test_decide_loop_returns_when_the_connections_run_out(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The connect ladder done — the daemon gone for good — is a
    # clean exit, not a spin.
    await scripted(monkeypatch, [ClosingWS([])])
    assert await tp.decide_loop("ws1") == 0


async def test_post_verdict_posts_the_rest_contract(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client_env(monkeypatch)
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
    await tp.post_verdict("ws1", "r1", "allow", "5m")
    assert posted["method"] == "POST"
    assert posted["path"] == "/api/v1/workspaces/ws1/egress/requests/r1"
    assert posted["body"] == {"decision": "allow", "duration": "5m"}


def test_run_decide_validates_its_arguments() -> None:
    with pytest.raises(SystemExit, match="needs a value"):
        tp.run_decide(["-w"])
    with pytest.raises(SystemExit, match="usage"):
        tp.run_decide(["-w", "ws1", "extra"])


def test_run_decide_runs_the_loop(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(tp.sys, "stdin", io.StringIO())
    calls = []

    async def fake_loop(workspace_id, keys=None):
        calls.append((workspace_id, keys))
        return 7

    monkeypatch.setattr(tp, "decide_loop", fake_loop)
    assert tp.run_decide(["-w", "ws1"]) == 7
    assert calls == [("ws1", None)]


def test_run_decide_takes_keys_from_a_tty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The full key path against a real pty pair: cbreak holds for
    # the session, the readable edge decodes the keys, and the
    # terminal's saved modes come back. The frames arrive over a
    # scripted websocket; the keys arrive from the pty's master.
    posted = []

    async def fake_post(ws, rid, decision, duration):
        posted.append((rid, decision, duration))

    monkeypatch.setattr(tp, "post_verdict", fake_post)
    client_env(monkeypatch)
    monkeypatch.setattr(tp, "SNAPSHOT_GRACE_S", 5.0)
    monkeypatch.setattr(tp, "TICK_S", 0.1)
    monkeypatch.setattr(
        tp.websockets,
        "connect",
        FakeConnect([FakeWS([rules_frame(), request_frame("r1")])]),
    )
    master, slave = pty.openpty()
    before = termios.tcgetattr(slave)
    monkeypatch.setattr(tp.sys, "stdin", os.fdopen(slave, "r"))
    allow = threading.Timer(0.2, os.write, args=(master, b"a"))
    leave = threading.Timer(0.6, os.write, args=(master, b"\x1b"))
    allow.start()
    leave.start()
    assert tp.run_decide(["-w", "ws1"]) == 0
    allow.join()
    leave.join()
    assert posted == [("r1", "allow", "tilrestart")]
    assert list(termios.tcgetattr(slave)) == list(before)
    os.close(master)


def test_decide_command_spells_the_popup_role() -> None:
    line = tp.decide_command("ws1")
    assert line.startswith(tp.sys.executable)
    assert line.endswith("-m msks.client.term_popup decide -w ws1")
