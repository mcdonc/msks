"""The tmux consent-terminal launcher tests (#379).

Every tmux/terminal boundary is a recorded subprocess call or a
stubbed function — the suite pins the argv contracts (the
session's tmux line, the popup's display-popup line, the decide
role's POST), the watcher's frame handling, and the liveness
retirement, never opening a window.
"""

import asyncio
import errno
import fcntl
import io
import os
import pty
import select
import shlex
import shutil
import struct
import subprocess
import termios
import threading
import time

import pytest
from msks.client import term_popup as tp

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
    socket = argv[2]
    assert socket.startswith("msks-a1b2c3d4e5-")
    # The joined commands ride the chain as single words; deriving
    # them from the builders (not positional indexes) keeps this
    # assert readable when the chain changes shape.
    consent_command = shlex.join(tp.decide_app_command("a1b2c3d4e5", socket))
    binding_command = tp.popup_command(socket)
    # The pane command rides the chain's last new-session — the
    # consent session's detached one lands ahead of it.
    i = len(argv) - 1 - argv[::-1].index("new-session")
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
        # The drag copy's clipboard half (#434's mouse mode):
        # set-clipboard on emits the OSC 52 write, and the
        # appended feature claims Konsole, whose terminfo carries
        # no Ms for tmux to read — without the pair, a paste in
        # another window delivered whatever last wrote the
        # system clipboard, not the drag just made.
        ";",
        "set-option",
        "-g",
        "set-clipboard",
        "on",
        ";",
        "set-option",
        "-ga",
        "terminal-features",
        tp.CLIPBOARD_FEATURES,
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
        # The hidden consent session (#467): the decider app at
        # the popup's geometry, its bar and prefix and lease each
        # pinned, and the reopen binding — all ahead of the shell
        # session, so the decider stands before the window reads.
        ";",
        "new-session",
        "-d",
        "-s",
        tp.CONSENT_SESSION,
        "-x",
        str(tp.POPUP_COLS - 2),
        "-y",
        str(tp.POPUP_ROWS - 2),
        consent_command,
        ";",
        "set-option",
        "-t",
        tp.CONSENT_SESSION,
        "status",
        "off",
        ";",
        "set-option",
        "-t",
        tp.CONSENT_SESSION,
        "prefix",
        "None",
        ";",
        "set-option",
        "-t",
        tp.CONSENT_SESSION,
        "destroy-unattached",
        "off",
        ";",
        "bind-key",
        tp.REOPEN_KEY,
        binding_command,
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
        tp.status_right(),
        ";",
        "set-option",
        "-g",
        "status-right-length",
        str(tp.STATUS_RIGHT_LENGTH),
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
    # The pane argv is bare — the marker cleanup and the exec; the
    # workspace rides the consent session's own command instead.
    assert " pane -- /venv/bin/python -m msks.client.cli ssh a1b2c3d4e5" in (
        joined
    )


def test_session_argv_escapes_a_hash_in_the_label() -> None:
    # A workspace name can carry the character tmux's formats
    # read — a `#[` pair would repaint the bar — so it doubles
    # into the literal form tmux's own escape is.
    argv = tp.session_argv(
        ["top"], session="s", workspace_id=None, label="a#b"
    )
    assert argv[argv.index("status-left") + 1] == "[a##b] "


def test_status_right_carries_the_hint_then_the_mode() -> None:
    """#764: the row's right half. The launch-time form is the
    hint alone (the mode is unknown until the decider's first
    frame), and the decider's write puts the mode in front."""
    assert tp.status_right() == f"· {tp.STATUS_HINT}"
    assert tp.STATUS_HINT == "C-b p popup"
    assert tp.status_right("interactive") == (
        f"interactive · {tp.STATUS_HINT}"
    )


def test_mode_argv_writes_the_row_on_the_launch_socket() -> None:
    argv = tp.mode_argv("msks-ws1-7", "static")
    assert argv == [
        "tmux",
        "-L",
        "msks-ws1-7",
        "set-option",
        "-g",
        "status-right",
        f"static · {tp.STATUS_HINT}",
    ]


def test_session_argv_without_a_workspace_ships_no_consent_session() -> None:
    argv = tp.session_argv(
        ["top"], session="shell", workspace_id=None, label="shell"
    )
    joined = argv[argv.index("new-session") + 3]  # one session alone
    assert " pane -- top" in joined
    assert tp.CONSENT_SESSION not in argv
    assert tp.REOPEN_KEY not in argv
    assert argv[argv.index("status-left") + 1] == "[shell] "
    # #764: no workspace means no reopen binding, and the row
    # advertises no hint — `C-b p` there is stock tmux's
    # previous-window, and a hint naming it would lie.
    assert argv[argv.index("status-right") + 1] == ""


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
    """The #455/#458/#764 boundary against a real tmux server: the
    shipped option chain writes the resolved name onto the bar —
    read back through the session it names, expanded the way the
    bar itself renders it — with the length budget raised past
    tmux's ten-cell default, the window-list formats reading
    back empty, and the right side empty too: this launch names
    no workspace, so no reopen binding ships and the hint stays
    off the bar (#764). The launch-time hint on a
    workspace-named launch is the argv unit tests' claim.
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


def test_main_spelled_roles_dispatch(monkeypatch, tmp_path) -> None:
    runs = []

    def fake_launch(argv):
        runs.append(argv)
        return 0

    # The dispatch table holds the bound roles; patch the entry,
    # or the real run_launch would exec a live tmux from the test.
    # The usage refusal logs like every role failure (#1143).
    log = log_at(monkeypatch, tmp_path)
    monkeypatch.setattr(tp, "RUNNERS", {"launch": fake_launch})
    assert tp.main(["launch", "cmd"]) == 0
    assert runs == [["cmd"]]
    with pytest.raises(SystemExit, match="usage"):
        tp.main([])
    assert "launch: usage" in log.read_text()


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
    # half-way — its title included: the title never reaches a
    # window tmux is not there to fill.
    monkeypatch.setattr(tp.shutil, "which", lambda tool: None)
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
    # The attached session is the chain's last new-session — the
    # consent session's detached one lands ahead of it.
    i = len(argv) - 1 - argv[::-1].index("new-session")
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
    """The window the launcher runs in takes the msks title (#445,
    #858) before the client starts — no session record exists
    yet here, so the token half drops."""
    monkeypatch.setattr(tp.shutil, "which", lambda tool: "/bin/" + tool)
    no_daemon_env(monkeypatch)
    monkeypatch.setattr(tp.os, "execvp", lambda binname, argv: None)
    out = Tty()
    monkeypatch.setattr(tp.sys, "stdout", out)
    assert tp.run_launch(list(SSH_CHILD)) == 0
    assert out.getvalue() == "\x1b]0;msks a1b2c3d4e5\x07"


def test_the_launch_line_meets_the_pane_role(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The boundary the shipped chain crosses: the pane argv that
    # session_argv shell-joins is exactly what the pane role
    # takes — the marker cleanup and the run, nothing beside
    # (strip the leading interpreter invocation, then spell the
    # pane role the way tmux's sh would hand it over).
    monkeypatch.setenv("TMUX", "/tmp/tmux-0/default,1,sess")
    monkeypatch.setattr(
        tp.subprocess,
        "run",
        lambda child, **kwargs: subprocess.CompletedProcess(child, 0),
    )
    argv = tp.session_argv(
        SSH_CHILD,
        session="a1b2c3d4e5",
        workspace_id="a1b2c3d4e5",
        label="a1b2c3d4e5",
    )
    last = len(argv) - 1 - argv[::-1].index("new-session")
    joined = argv[last + 3]
    words = shlex.split(joined)[3:]  # past python -m msks.client.term_popup
    assert words[0] == "pane"
    monkeypatch.setenv(tp.TITLE_MARKER, "1")
    tp.main(["pane", *words[1:]])
    assert tp.TITLE_MARKER not in os.environ


# --- the consent popup's tmux edges (#467) -----------------------------------


def test_decide_app_command_spells_the_hidden_session() -> None:
    command = tp.decide_app_command("ws1", "msks-ws1-7")
    assert command[0] == tp.sys.executable
    assert command[1:] == [
        "-m",
        "msks.client.tui.decide_app",
        "-w",
        "ws1",
        "--socket",
        "msks-ws1-7",
        "--session",
        tp.CONSENT_SESSION,
    ]


def test_viewer_and_popup_commands_carry_the_geometry() -> None:
    viewer = tp.viewer_command("msks-ws1-7")
    assert viewer == (
        f"env -u TMUX tmux -L msks-ws1-7 attach -t {tp.CONSENT_SESSION}"
    )
    # The binding's command quotes the viewer as one word —
    # display-popup absorbs every token after its command.
    assert tp.popup_command("msks-ws1-7") == (
        f"display-popup -E -w {tp.POPUP_COLS} -h {tp.POPUP_ROWS}"
        f" {shlex.quote(viewer)}"
    )
    # The show path names its client; the detach path the session.
    assert tp.show_popup_argv("msks-ws1-7", "/dev/pts/3") == [
        "tmux",
        "-L",
        "msks-ws1-7",
        "display-popup",
        "-c",
        "/dev/pts/3",
        "-E",
        "-w",
        str(tp.POPUP_COLS),
        "-h",
        str(tp.POPUP_ROWS),
        viewer,
    ]
    assert tp.detach_argv("msks-ws1-7") == [
        "tmux",
        "-L",
        "msks-ws1-7",
        "detach-client",
        "-s",
        tp.CONSENT_SESSION,
    ]


def test_consent_chain_gates_on_the_workspace() -> None:
    assert tp.consent_chain(None, "msks-shell-7") == []
    chain = tp.consent_chain("ws1", "msks-ws1-7")
    assert chain[:6] == [
        ";",
        "new-session",
        "-d",
        "-s",
        tp.CONSENT_SESSION,
        "-x",
    ]
    # The session keeps its own life: the global default lands
    # after both sessions exist and must not take this one.
    assert chain[-2:] == [
        tp.REOPEN_KEY,
        tp.popup_command("msks-ws1-7"),
    ]


def test_shell_clients_skips_the_hidden_viewers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ran = {}

    def fake_run(argv, **kwargs):
        ran.update(argv=argv)
        return subprocess.CompletedProcess(
            argv, 0, stdout="/dev/pts/3\tws1\n/dev/pts/9\tconsent\n"
        )

    monkeypatch.setattr(tp.subprocess, "run", fake_run)
    assert tp.shell_clients("msks-ws1-7") == ["/dev/pts/3"]
    assert ran["argv"][1:4] == ["-L", "msks-ws1-7", "list-clients"]
    # A server that cannot answer names no client.
    monkeypatch.setattr(
        tp.subprocess,
        "run",
        lambda argv, **kwargs: subprocess.CompletedProcess(argv, 1),
    )
    assert tp.shell_clients("msks-ws1-7") == []

    def explode(argv, **kwargs):
        raise subprocess.SubprocessError("slow")

    monkeypatch.setattr(tp.subprocess, "run", explode)
    assert tp.shell_clients("msks-ws1-7") == []


def test_hidden_has_viewer_reads_the_sessions_clients(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        tp.subprocess,
        "run",
        lambda argv, **kwargs: subprocess.CompletedProcess(
            argv, 0, stdout="/dev/pts/9 (1)\n"
        ),
    )
    assert tp.hidden_has_viewer("msks-ws1-7") is True
    monkeypatch.setattr(
        tp.subprocess,
        "run",
        lambda argv, **kwargs: subprocess.CompletedProcess(argv, 0, stdout=""),
    )
    assert tp.hidden_has_viewer("msks-ws1-7") is False

    def explode(argv, **kwargs):
        raise OSError("no tmux")

    monkeypatch.setattr(tp.subprocess, "run", explode)
    assert tp.hidden_has_viewer("msks-ws1-7") is False


def test_run_pane_drops_the_marker_and_runs_the_child(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen = {}

    def run(argv, **kwargs):
        seen.update(argv=argv)
        return subprocess.CompletedProcess(argv, 0)

    monkeypatch.setattr(tp.subprocess, "run", run)
    monkeypatch.setenv(tp.TITLE_MARKER, "1")
    assert tp.run_pane(["--", *SSH_CHILD]) == 0
    assert seen["argv"] == SSH_CHILD
    assert tp.TITLE_MARKER not in os.environ


def test_run_pane_holds_the_window_open_on_a_refusal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A nonzero child keeps its pane (and so its window) alive:
    # the refusal it printed stays readable until Enter (#1143).
    held = []
    monkeypatch.setattr(
        tp.subprocess,
        "run",
        lambda argv, **kwargs: subprocess.CompletedProcess(argv, 1),
    )
    monkeypatch.setattr(tp, "hold_pane_open", held.append)
    assert tp.run_pane(["--", *SSH_CHILD]) == 1
    assert held == ["the command exited 1"]


def test_run_pane_holds_a_killed_command_and_answers_its_signal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A signal death holds too, and the pane answers the shell's
    # convention for it: 128 + the signal number.
    held = []
    monkeypatch.setattr(
        tp.subprocess,
        "run",
        lambda argv, **kwargs: subprocess.CompletedProcess(argv, -9),
    )
    monkeypatch.setattr(tp, "hold_pane_open", held.append)
    assert tp.run_pane(["--", *SSH_CHILD]) == 137
    assert held == ["the command exited on a signal"]


def test_run_pane_names_a_command_it_cannot_run(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    held = []

    def explode(argv, **kwargs):
        raise FileNotFoundError(2, "No such file or directory", argv[0])

    monkeypatch.setattr(tp.subprocess, "run", explode)
    monkeypatch.setattr(tp, "hold_pane_open", held.append)
    assert tp.run_pane(["--", "/nope/missing"]) == tp.NOT_FOUND
    assert "cannot run /nope/missing" in capsys.readouterr().err
    assert held == ["the command could not run"]


def test_run_pane_refuses_an_empty_child() -> None:
    with pytest.raises(SystemExit, match="usage"):
        tp.run_pane(["--"])


class FakeStdin:
    """A stdin the hold can read: one byte per read, or a raise."""

    def __init__(self, answer: bytes | KeyboardInterrupt) -> None:
        self.answer = answer
        self.reads: list[int] = []

    def read(self, size: int = -1) -> str:
        self.reads.append(size)
        if isinstance(self.answer, KeyboardInterrupt):
            raise self.answer
        return self.answer.decode()


def test_hold_pane_open_prints_the_prompt_and_waits(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    stdin = FakeStdin(b"\r")
    monkeypatch.setattr(tp.sys, "stdin", stdin)
    tp.hold_pane_open("the command exited 1")
    out = capsys.readouterr().out
    assert "the command exited 1" in out
    assert "Press Enter to close this window." in out
    # One byte, not a line: a child killed in raw mode leaves the
    # pane's tty raw, where Enter makes no line to read.
    assert stdin.reads == [1]


def test_hold_pane_open_ends_on_eof_and_interrupt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Both are the operator's close: the hold must never trap a
    # window shut (#1143).
    monkeypatch.setattr(tp.sys, "stdin", FakeStdin(b""))
    tp.hold_pane_open("the command exited 1")
    monkeypatch.setattr(tp.sys, "stdin", FakeStdin(KeyboardInterrupt()))
    tp.hold_pane_open("the command exited 1")


# --- the tmp-dir diagnostics log (#1143) -----------------------------------


def log_at(monkeypatch: pytest.MonkeyPatch, tmp_path):
    """Point the diagnostics log at the test's own tmp dir."""
    monkeypatch.setattr(tp.tempfile, "gettempdir", lambda: str(tmp_path))
    return tmp_path / tp.DIAGNOSTIC_LOG


def test_a_refused_role_logs_its_reason(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    log = log_at(monkeypatch, tmp_path)
    monkeypatch.setattr(tp.shutil, "which", lambda tool: None)
    with pytest.raises(SystemExit, match="tmux is not on PATH"):
        tp.main(list(SSH_CHILD))
    assert "launch: msks-term-popup: tmux is not on PATH" in (log.read_text())


def test_an_unexpected_failure_still_reaches_the_log(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    log = log_at(monkeypatch, tmp_path)

    def wedged(argv):
        raise RuntimeError("wedged")

    monkeypatch.setattr(tp, "RUNNERS", {"launch": wedged})
    with pytest.raises(RuntimeError, match="wedged"):
        tp.main(["launch", "cmd"])
    assert "launch: RuntimeError: wedged" in log.read_text()


def test_the_log_never_takes_the_failing_role_down(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    # The log's directory cannot exist (a file sits where it
    # would), and the role's own refusal still propagates — the
    # log is a courtesy record, never a dependency.
    blocker = tmp_path / "blocker"
    blocker.write_text("in the way")
    monkeypatch.setattr(tp.tempfile, "gettempdir", lambda: str(blocker))

    def refuse(argv):
        raise SystemExit("msks-term-popup: no")

    monkeypatch.setattr(tp, "RUNNERS", {"launch": refuse})
    with pytest.raises(SystemExit, match="no"):
        tp.main(["launch", "cmd"])


def test_the_log_refuses_a_planted_symlink(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    # A symlink at the log's name in a shared tmp dir would
    # redirect the append; the O_NOFOLLOW open leaves it alone
    # and the refusal still propagates (#1143).
    target = tmp_path / "target"
    target.write_text("")
    link = tmp_path / tp.DIAGNOSTIC_LOG
    link.symlink_to(target)
    monkeypatch.setattr(tp.tempfile, "gettempdir", lambda: str(tmp_path))

    def refuse(argv):
        raise SystemExit("msks-term-popup: no")

    monkeypatch.setattr(tp, "RUNNERS", {"launch": refuse})
    with pytest.raises(SystemExit, match="no"):
        tp.main(["launch", "cmd"])
    assert target.read_text() == ""


def test_a_write_that_fails_leaves_the_caller_its_own_failure(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    # A failed write after a successful open (the fdopen stands
    # in for the flush): the log takes itself down quietly — best
    # effort means the whole append, not just its first syscall
    # (#1143 review).
    log = log_at(monkeypatch, tmp_path)

    def refuse_fdopen(fd, mode, **kwargs):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(tp.os, "fdopen", refuse_fdopen)
    tp.log_diagnostic("anything")
    assert log.read_text() == ""


def test_the_log_refuses_to_wait_on_a_readerless_fifo(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """#1143 review: a FIFO planted at the log's name hangs a plain
    O_WRONLY open (no reader answers), wedging the failing role
    with nothing shown. The O_NONBLOCK flag must answer ENXIO at
    once — the daemon-thread join turns a regression into this
    test's failure instead of the suite's hang."""
    os.mkfifo(tmp_path / tp.DIAGNOSTIC_LOG)
    monkeypatch.setattr(tp.tempfile, "gettempdir", lambda: str(tmp_path))
    done: list[bool] = []

    def write_it() -> None:
        tp.log_diagnostic("anything")
        done.append(True)

    writer = threading.Thread(target=write_it, daemon=True)
    writer.start()
    writer.join(timeout=5)
    assert done == [True]


#: spawn_pane's fork fires CPython's multi-threaded-fork
#: DeprecationWarning under xdist (the worker's execnet threads make
#: the process multi-threaded; a single-process run stays quiet):
#: the child's post-fork work is the minimal setsid/ctty/dup/close/
#: exec ladder, the same shape stdlib pty.fork runs, so the warning
#: is noise here, not a deadlock the suite can hit (the GUI session
#: modules filter the same one, #951).
pytestmark = pytest.mark.filterwarnings(
    "ignore:This process.*multi-threaded.*fork:DeprecationWarning"
)


def spawn_pane(child: list[str]) -> tuple[int, int]:
    """The pane role on its own pty, the way tmux runs it (#1143
    review): a session leader whose controlling terminal is the
    pty, the pane wrapper exec'd onto it — writes to the master
    reach it the way a terminal's keys would."""
    master, slave = pty.openpty()
    pid = os.fork()
    if pid == 0:
        os.setsid()
        fcntl.ioctl(slave, termios.TIOCSCTTY, 0)
        os.dup2(slave, 0)
        os.dup2(slave, 1)
        os.dup2(slave, 2)
        if slave > 2:
            os.close(slave)
        if master > 2:
            os.close(master)
        os.execv(
            tp.sys.executable,
            [tp.sys.executable, "-m", tp.MODULE, "pane", "--", *child],
        )
    os.close(slave)
    return master, pid


def pane_read(master: int) -> bytes | None:
    """One read off a master select called readable: the bytes, or
    ``None`` at the pane's end. The two kernels end a pty differently
    once the wrapper's exit hangs it up: Linux answers EIO, macOS the
    empty read (the pair #951's ``read_once`` carries for the GUI
    session). Neither is a harness failure — the pane is simply
    over, and the callers fall through to their own answer."""
    try:
        data = os.read(master, 4096)
    except OSError as exc:
        if exc.errno != errno.EIO:
            raise
        return None
    return data or None


def read_pane(
    master: int, want: bytes, deadline: float = 10.0, buf: bytes = b""
) -> bytes:
    """Read the pane's pty until the wanted bytes arrive, and
    return everything seen. One chunk can carry the child's last
    words and the wrapper's close prompt together (under a loaded
    runner the first read lands after both wrote), so a later
    ``read_pane`` on the same pane starts from ``buf``, the buffer
    the previous one returned, instead of from empty."""
    end = time.monotonic() + deadline
    while want not in buf and time.monotonic() < end:
        ready, _, _ = select.select([master], [], [], 0.1)
        if not ready:
            continue
        data = pane_read(master)
        if data is None:
            break  # the pane ended first: the assertion below names it
        buf += data
    if want in buf:
        return buf
    raise AssertionError(f"{want!r} never arrived; saw {buf!r}")


def pane_alive(pid: int) -> bool:
    """Whether the wrapper still runs (a poll, not a reap)."""
    done, _ = os.waitpid(pid, os.WNOHANG)
    return done == 0


def wait_pane(master: int, pid: int, deadline: float = 10.0) -> int:
    """The wrapper's exit code, polled. The poll drains the pty's
    master as it goes: on a macOS pty a session leader that ran a
    child and then read its stdin finishes exit() only once the
    master is read (tmux's server polls its masters constantly,
    which is why the pane never sees this; a harness that stops
    reading wedges the wrapper in exit, past SIGTERM). The drain
    stops at the pane's end (``pane_read``'s ``None``, whichever way
    the kernel reports it) and the poll goes on to the reap."""
    end = time.monotonic() + deadline
    pane_open = True
    while time.monotonic() < end:
        if pane_open and select.select([master], [], [], 0.05)[0]:
            pane_open = pane_read(master) is not None
        done, status = os.waitpid(pid, os.WNOHANG)
        if done != 0:
            return os.waitstatus_to_exitcode(status)
    raise AssertionError("the pane wrapper did not exit")


def test_the_hold_keeps_the_pane_until_enter() -> None:
    """#1143 against a real pty: the refused child's output and
    the close prompt stay on the pane, a plain keypress does not
    close the canonical tty's read, and Enter does."""
    master, pid = spawn_pane(["/bin/sh", "-c", "echo refused; exit 3"])
    try:
        seen = read_pane(master, b"refused")
        read_pane(master, b"Press Enter to close this window.", buf=seen)
        os.write(master, b"x")  # a plain key: the hold stands
        time.sleep(0.5)
        assert pane_alive(pid)
        os.write(master, b"\r")  # Enter: the hold ends
        assert wait_pane(master, pid) == 3
    finally:
        os.close(master)


def test_the_hold_answers_enter_on_a_raw_tty() -> None:
    """#1143 review: the byte read, not a line read. A child that
    dies with the pane's tty raw (a killed ssh) leaves no line
    editing for ``input()`` to wait on — Enter must answer the
    single-byte read directly. This is the pin that separates
    ``sys.stdin.read(1)`` from an ``input()`` regression, which
    the canonical-tty test above cannot tell apart."""
    master, pid = spawn_pane(["/bin/sh", "-c", "stty raw; exit 5"])
    try:
        read_pane(master, b"Press Enter to close this window.")
        os.write(master, b"\r")
        assert wait_pane(master, pid) == 5
    finally:
        os.close(master)


def test_the_interrupt_key_kills_the_child_and_not_the_hold() -> None:
    """#1143 review against a real pty: the interrupt key is
    SIGINT to the pane's foreground process group. The child dies
    on it; the wrapper absorbs its own copy, holds the window
    open with the signal's name, and Enter closes."""
    master, pid = spawn_pane(["/bin/sh", "-c", "echo started; exec sleep 30"])
    try:
        # The child's own output is the proof the wrapper already
        # spawned it — the SIGINT handler is installed ahead of
        # the spawn, so the key cannot race the setup. The exec
        # pins the interrupted process to sleep itself: its
        # SIGINT disposition is the default on every shell, so
        # the death is a signal death everywhere the suite runs.
        seen = read_pane(master, b"started")
        os.write(master, b"\x03")  # Ctrl-C: ISIG delivers SIGINT
        # The reason and the close prompt print as one line.
        read_pane(master, b"exited on a signal. Press Enter", buf=seen)
        assert pane_alive(pid)  # the wrapper stood through it
        os.write(master, b"\r")
        # 128 + SIGINT(2), the shell convention the pane answers.
        assert wait_pane(master, pid) == 130
    finally:
        os.close(master)


# --- the consent chain against a real server (#467) --------------------------


def consent_server(socket: str) -> None:
    """The hidden half of the shipped chain on a real server: the
    consent session, born detached at the viewer's inner size
    with its own lease — a dummy command stands in for the app."""
    subprocess.run(
        [
            "tmux",
            "-L",
            socket,
            "start-server",
            ";",
            "new-session",
            "-d",
            "-s",
            tp.CONSENT_SESSION,
            "-x",
            str(tp.POPUP_COLS - 2),
            "-y",
            str(tp.POPUP_ROWS - 2),
            "sh -c 'sleep 60'",
            ";",
            "set-option",
            "-t",
            tp.CONSENT_SESSION,
            "destroy-unattached",
            "off",
        ],
        timeout=10,
        check=True,
        capture_output=True,
    )


def lease_shell_session(socket: str, master, slave) -> subprocess.Popen:
    """The shell half, as the launch creates it: the client's own
    new-session (attached, never detached), then the trailing
    option that ties the session to its window."""
    client = subprocess.Popen(
        [
            "tmux",
            "-L",
            socket,
            "new-session",
            "-s",
            "shell",
            "sh -c 'sleep 60'",
        ],
        stdin=slave,
        stdout=slave,
        stderr=slave,
        start_new_session=True,
        env={**os.environ, "TERM": "xterm"},
    )
    os.close(slave)
    eventually(lambda: lease_taken(socket))
    return client


def lease_taken(socket: str) -> bool:
    """The trailing option, polled: the client's own new-session
    races the session's existence on a loaded host, and the
    option lands the moment the session does."""
    proc = subprocess.run(
        [
            "tmux",
            "-L",
            socket,
            "set-option",
            "-t",
            "shell",
            "destroy-unattached",
            "on",
        ],
        timeout=5,
        check=False,
        capture_output=True,
    )
    return proc.returncode == 0


def pty_pair():
    """A pty the size of a small window, master and slave."""
    master, slave = pty.openpty()
    fcntl.ioctl(master, termios.TIOCSWINSZ, struct.pack("HHHH", 24, 80, 0, 0))
    return master, slave


def eventually(check, deadline: float = 15.0) -> None:
    """Await a server-side condition, polling the real server.
    The deadline breathes for a loaded parallel suite — a probe
    passes in well under a second on an idle host."""
    end = time.monotonic() + deadline
    while time.monotonic() < end:
        if check():
            return
        time.sleep(0.1)
    raise AssertionError("server condition did not arrive")


def session_up(socket: str, session: str) -> bool:
    proc = subprocess.run(
        ["tmux", "-L", socket, "has-session", "-t", session],
        capture_output=True,
        timeout=5,
        check=False,
    )
    return proc.returncode == 0


def test_the_sessions_carry_opposite_leases() -> None:
    """#467 against a real server: the shell session dies with its
    client (``destroy-unattached on``) while the consent session
    stands — its app carries the window-gone retirement, so a
    closed window leaves the session for the clock, not forever —
    and the server dies when the last session ends."""
    if shutil.which("tmux") is None:  # pragma: no cover
        pytest.skip("tmux is not on PATH")
    socket = f"msks-lease-{os.getpid()}"
    consent_server(socket)
    master, slave = pty_pair()
    client = lease_shell_session(socket, master, slave)
    try:
        eventually(lambda: session_up(socket, "shell"))
        eventually(lambda: session_up(socket, tp.CONSENT_SESSION))
        # The window closes: its client dies, and the shell
        # session follows it.
        client.kill()
        client.wait(timeout=10)
        eventually(lambda: not session_up(socket, "shell"))
        assert session_up(socket, tp.CONSENT_SESSION)
        # The last session's end takes the server with it.
        subprocess.run(
            ["tmux", "-L", socket, "kill-session", "-t", tp.CONSENT_SESSION],
            timeout=5,
            check=True,
            capture_output=True,
        )
        eventually(lambda: not session_up(socket, tp.CONSENT_SESSION))
    finally:
        subprocess.run(
            ["tmux", "-L", socket, "kill-server"],
            timeout=10,
            check=False,
            capture_output=True,
        )
        client.wait(timeout=10)
        os.close(master)


def test_the_viewer_show_and_hide_cycle() -> None:
    """#467 against a real server: the show path's display-popup
    puts a viewer on the shell's client (the blocking call
    outlives the test's wait — the popup stands), and the detach
    hides it while the consent session stands."""
    if shutil.which("tmux") is None:  # pragma: no cover
        pytest.skip("tmux is not on PATH")
    socket = f"msks-view-{os.getpid()}"
    consent_server(socket)
    master, slave = pty_pair()
    client = lease_shell_session(socket, master, slave)
    shown: list = []
    try:
        eventually(lambda: session_up(socket, "shell"))

        def find_client() -> str | None:
            clients = tp.shell_clients(socket, tp.CONSENT_SESSION)
            return clients[0] if clients else None

        eventually(lambda: find_client() is not None)
        target = find_client()
        assert target is not None
        # display-popup blocks while the popup stands: it runs on a
        # thread, and the viewer it parks is the thing asserted.
        shower = threading.Thread(
            target=lambda: shown.append(
                subprocess.run(
                    tp.show_popup_argv(socket, target),
                    capture_output=True,
                    timeout=tp.TMUX_TIMEOUT_S,
                    check=False,
                ).returncode
                == 0
            ),
            daemon=True,
        )
        shower.start()
        eventually(lambda: tp.hidden_has_viewer(socket, tp.CONSENT_SESSION))
        subprocess.run(
            tp.detach_argv(socket), timeout=5, check=True, capture_output=True
        )
        eventually(
            lambda: not tp.hidden_has_viewer(socket, tp.CONSENT_SESSION)
        )
        shower.join(timeout=10)
        assert shown == [True]
        assert session_up(socket, tp.CONSENT_SESSION)
    finally:
        subprocess.run(
            ["tmux", "-L", socket, "kill-server"],
            timeout=10,
            check=False,
            capture_output=True,
        )
        client.kill()
        client.wait(timeout=10)
        os.close(master)
