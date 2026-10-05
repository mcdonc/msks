"""The tmux consent-terminal launcher (#379, #467).

``terminal_open_cmd`` names a command prefix and the TUI appends
the ``msks ssh`` invocation after it (#341). This module ships
the suffix half: ``msks-term-popup`` runs inside whatever terminal
window the prefix opens — ``konsole -e msks-term-popup``, ``xterm
-e msks-term-popup``, any terminal that runs a command — and runs
the appended command as a local tmux session. When the appended
command names a workspace, the launch also starts the standalone
consent-decider app (#467) in a hidden session on the same
socket: the app holds the workspace's decider registration for
the window's whole life, and a hold arriving with the popup
closed raises a ``display-popup`` viewer over the shell that
attaches to it. The viewer hides (``q``/``Esc``/``C-b``) without
stopping the app — the queue lives on, holds leave it as the
daemon resolves them, and ``C-b p`` (the launch's binding) or the
next hold brings the popup back. The bindings map rides the
app's footer at the popup's bottom. The terminal choice stays
where it already lives: in ``terminal_open_cmd``'s own prefix.

Two roles share this module, spelled as the first argument:

``launch``
    The console entry the operator's prefix runs. Validates tmux,
    names the window per ``MSKSC_TERMINAL_TITLE`` when the setting
    is in place (#445), resolves the workspace's name for the
    session's status bar (#455), names a fresh session after the
    workspace on a dedicated socket (a server of its own, so the
    pane inherits this process's environment — an operator's
    already-running tmux server would otherwise substitute its
    own — and the session stays out of their window list), starts
    the hidden consent session beside it, and becomes the attached
tmux client (``tmux -L <socket> new-session ... pane ...``).
``pane``
    The tmux session's first process: drop the new-window marker
    (#445) and become the appended command.

The launcher runs detached with stdio on devnull (#341's spawn
contract), so it keeps its own diagnostics to a log file under
the tmp dir; the consent app owns the only interactive surface.
"""

import asyncio
import os
import shlex
import shutil
import subprocess
import sys

from .env import env_token, env_url
from .rest import workspace_row
from .wintitle import TITLE_MARKER, configured_title, set_window_title

#: The popup geometry: fixed cells, sized for the 80-column window
#: the consent screens already target; tmux clips it to a smaller
#: client window. The hidden session is born at this size too, so
#: the app never reflows when a viewer attaches.
POPUP_COLS = 76
POPUP_ROWS = 16

#: The hidden session's name (#467): one launch owns one socket,
#: so a fixed name is unique on it — the shell session carries the
#: workspace id beside it.
CONSENT_SESSION = "consent"

#: The standalone consent app's module (#467) — the hidden
#: session's command runs it with the interpreter already running
#: this launcher.
DECIDE_MODULE = "msks.client.tui.decide_app"

#: The reopen key (#467): ``C-b p`` on the launch's own server
#: (the shell session's prefix is the default, and no inner tmux
#: competes for it) shows the popup viewer again.
REOPEN_KEY = "p"

#: How long a tmux query may take before the caller stops waiting
#: on it: the app's show blocks while the popup stands (the kill
#: ends the wait, not the popup), and the launch never waits at
#: all — it only builds argv.
TMUX_TIMEOUT_S = 3.0

#: The modules this package reaches itself through: the pane
#: re-invokes this module by name with the interpreter that is
#: already running it.
MODULE = "msks.client.term_popup"

#: The pane's scrollback depth (#434): the session runs in tmux's
#: alternate screen, which leaves the terminal window's own
#: scrollbar nothing to scroll — the wheel and copy mode scroll
#: the session's history instead, and this is how deep that
#: history reaches. The option must sit on the server before the
#: session exists (a pane adopts its history limit only at
#: creation), which is why the launch line starts the server and
#: sets its options ahead of ``new-session``. The cost is memory
#: that grows with use, not with the setting: tmux allocates a
#: history line only when output scrolls one off the screen, so
#: the depth is the ceiling, not the starting footprint.
HISTORY_LINES = 10000

#: The status bar's left-side width (#455): tmux clips the left
#: side at ten cells by default, too short for a workspace name,
#: so the launch raises the budget the label needs rather than
#: clip the name it just resolved.
STATUS_LEFT_LENGTH = 64

#: How long the launch waits on the workspace-name lookup for the
#: status bar (#455): the REST read budget is sized for boots, and
#: this wait sits between the operator and their window — a
#: daemon that hangs delays the window by these seconds, then the
#: id stands in.
LABEL_WAIT_S = 5.0

#: The popup's page-scroll bindings (#444), the keyboard twin of
#: the wheel path above: each command pages one page — up on the
#: shifted page-up key, down on its twin — through the same
#: history the wheel scrolls, entering copy mode armed with the
#: wheel's exit-at-bottom rule (``-e``). The vehicle is the stock
#: wheel binding's own: an ``if -F`` whose quoted branches ride
#: the mode check — already in copy mode, the plain page command;
#: not yet, the compound that enters and pages. Two properties of
#: that shape are load-bearing: tmux parses the chain's argv with
#: quotes and separators, so the embedded ``;`` must sit inside
#: the quoted branch or it would split the launch chain itself;
#: and ``bind-key`` validates its command at bind time, so the
#: man page's newer ``copy-mode -u``/``-d`` pair — ``-d`` arrived
#: in tmux 3.5 — would abort the whole launch (no window at all)
#: on the 3.2–3.4 tmux a stock distribution ships; every piece
#: here runs on the documented 3.2 floor. The shifted page keys
#: are the pair terminals reserve for their own scrollback, and
#: this session's alternate screen leaves that scrollback empty:
#: a terminal that passes the shifted keys through (their
#: well-known sequences — Konsole's keytab can send them with an
#: ``AppScreen``-scoped rule) pages the history with them, while
#: one that keeps the keys for its own view still has the wheel.
#: The bare page keys carry no binding here, so they reach the
#: shell untouched.
PAGE_SCROLL_COMMANDS = (
    (
        "S-PgUp",
        "if -F '#{pane_in_mode}' 'send-keys -X page-up'"
        " 'copy-mode -e; send-keys -X page-up'",
    ),
    (
        "S-PgDn",
        "if -F '#{pane_in_mode}' 'send-keys -X page-down'"
        " 'copy-mode -e; send-keys -X page-down'",
    ),
)

ROLES = ("launch", "pane")


def main(argv: list[str] | None = None) -> int:
    """The console entry: dispatch on the first word — a role name
    for the internal invocations, anything else (the appended
    ``msks ssh`` argv) is the launch role's child."""
    args = sys.argv[1:] if argv is None else list(argv)
    role, rest = split_role(args)
    if not rest:
        raise SystemExit("usage: msks-term-popup <command the TUI appends>")
    return RUNNERS[role](rest)


def split_role(args: list[str]) -> tuple[str, list[str]]:
    """The first word when it spells a role; else the launch role
    with the whole argv as its child."""
    if args and args[0] in ROLES:
        return args[0], args[1:]
    return "launch", args


def run_launch(argv: list[str]) -> int:
    """The launcher (#379): a missing tmux names itself and stops
    before a window could open half-way — its title included; a
    configured title (``MSKSC_TERMINAL_TITLE``) names the window
    only once tmux is there to fill it (#445). Then resolve the
    workspace's name for the status bar (#455) — last of the
    pre-exec steps, so a slow lookup delays only the client — and
    become the tmux client attached to a fresh session (one that
    ends with this window) whose pane runs this module's pane role
    with the appended command. The terminal window itself is
    whatever the operator's prefix opened — this process already
    runs inside it."""
    workspace_id = workspace_from_argv(argv)
    if shutil.which("tmux") is None:
        raise SystemExit("msks-term-popup: tmux is not on PATH")
    title = configured_title(workspace_id)
    if title is not None:
        set_window_title(title)
    label = status_label(workspace_id)
    os.execvp(
        "tmux",
        session_argv(argv, session_name(workspace_id), workspace_id, label),
    )
    return 0  # pragma: no cover — execvp replaces the process


def run_pane(argv: list[str]) -> int:
    """The tmux pane's first process: drop the new-window marker
    (#445) — the window is already titled, :func:`run_launch` wrote
    it before attaching tmux, and tmux owns the pane's escapes —
    and become the appended command."""
    os.environ.pop(TITLE_MARKER, None)
    child = child_argv(argv)
    os.execvp(child[0], child)
    return 0  # pragma: no cover — execvp replaces the process


# --- the consent popup's tmux edges (#467) -----------------------------------


def tmux_argv(socket: str, *args: str) -> list[str]:
    """One tmux command on the launch's own server."""
    return ["tmux", "-L", socket, *args]


def decide_app_command(
    workspace_id: str, socket: str, session: str = CONSENT_SESSION
) -> list[str]:
    """The hidden session's command: this interpreter running the
    standalone consent app for the workspace, pointed back at the
    launch's socket and its own session so it can raise and hide
    its popup viewer."""
    return [
        sys.executable,
        "-m",
        DECIDE_MODULE,
        "-w",
        workspace_id,
        "--socket",
        socket,
        "--session",
        session,
    ]


def viewer_command(socket: str, session: str = CONSENT_SESSION) -> str:
    """The shell command a popup viewer runs: a client attaching to
    the hidden session. ``env -u TMUX`` unsets TMUX so the nested
    attach is permitted — the popup itself runs inside a pane of
    the same server, where TMUX is set."""
    return (
        f"env -u TMUX tmux -L {shlex.quote(socket)}"
        f" attach -t {shlex.quote(session)}"
    )


def popup_command(socket: str, session: str = CONSENT_SESSION) -> str:
    """The ``display-popup`` command string the reopen binding
    carries: centered, the popup geometry, closing when its viewer
    exits (``-E``). The command must sit as one quoted word —
    ``display-popup`` treats its trailing command as absorbing
    every token after it."""
    return (
        f"display-popup -E -w {POPUP_COLS} -h {POPUP_ROWS}"
        f" {shlex.quote(viewer_command(socket, session))}"
    )


def show_popup_argv(
    socket: str, client: str, session: str = CONSENT_SESSION
) -> list[str]:
    """The argv that shows the popup viewer on one shell client —
    the app's own show path, run server-side so it names the
    target client with ``-c``. Blocks while the popup stands."""
    return tmux_argv(
        socket,
        "display-popup",
        "-c",
        client,
        "-E",
        "-w",
        str(POPUP_COLS),
        "-h",
        str(POPUP_ROWS),
        viewer_command(socket, session),
    )


def detach_argv(socket: str, session: str = CONSENT_SESSION) -> list[str]:
    """The argv that detaches the hidden session's viewer: the
    popup hides while the app inside the session keeps running."""
    return tmux_argv(socket, "detach-client", "-s", session)


def shell_clients(socket: str, session: str = CONSENT_SESSION) -> list[str]:
    """The clients to show a popup on: every client of this server
    attached outside the hidden session — the shell window's own
    client, never the viewer a popup already carries."""
    try:
        proc = subprocess.run(
            tmux_argv(
                socket,
                "list-clients",
                "-F",
                "#{client_name}\t#{client_session}",
            ),
            capture_output=True,
            text=True,
            timeout=TMUX_TIMEOUT_S,
            check=False,
        )
    except OSError, subprocess.SubprocessError:
        return []
    if proc.returncode != 0 or proc.stdout is None:
        return []
    return outside_clients(proc.stdout, session)


def outside_clients(out: str, session: str) -> list[str]:
    """The parsed client names attached outside the hidden
    session — the shell window's own client, never a viewer."""
    names = []
    for line in out.splitlines():
        name, _, attached = line.partition("\t")
        if name and attached != session:
            names.append(name)
    return names


def hidden_has_viewer(socket: str, session: str = CONSENT_SESSION) -> bool:
    """Whether the hidden session has a viewer client attached —
    the popup standing open."""
    try:
        proc = subprocess.run(
            tmux_argv(socket, "list-clients", "-t", session),
            capture_output=True,
            text=True,
            timeout=TMUX_TIMEOUT_S,
            check=False,
        )
    except OSError, subprocess.SubprocessError:
        return False
    return bool(proc.stdout and proc.stdout.strip())


def session_argv(
    child: list[str], session: str, workspace_id: str | None, label: str
) -> list[str]:
    """The tmux argv: one client on this launch's dedicated socket,
    attached to a fresh session whose pane runs this module's pane
    role with the appended command, and — when the command names a
    workspace — the hidden consent session beside it (see
    :func:`consent_chain`), whose app retires when this window
    has been gone past its grace. The pane command is
    shell-joined as tmux
    takes its command, so a child path with spaces survives the
    join; the client owns the terminal window the operator's
    prefix already opened, and ``destroy-unattached`` tears the
    session down with that window — the shell ends with its window
    (the bare ``msks ssh`` behavior) and the consent watcher
    retires with it (the prefix's own hold flags — ``konsole
    --hold``, xterm's ``-hold`` — still apply around the tmux
    client).

    The server starts and takes its scrollback options (#434)
    before the session exists, because a pane adopts its history
    limit only at creation: mouse mode turns the wheel into
    scrolling, and the raised history line count is how far back
    it reaches. The page-scroll bindings (#444) land with them:
    the shifted page keys page that same history through tmux's
    copy mode, the keyboard twin of the wheel. ``set-titles``
    pins off for the configured window title (#445): a fresh
    server still reads the operator's own tmux.conf, whose
    ``set-titles on`` would otherwise hand the outer window's
    title to tmux the moment the client attaches. The status
    bar's left side takes the workspace label (#455): tmux's
    default shows the session name — the workspace id — and the
    label is the name an operator reads; a ``#`` in it doubles
    for the format tmux leaves literal, so a name cannot write
    format escapes into the bar. The window-list formats pin to
    empty (#458): tmux's default bar follows the left side with
    the window list — an index, the pane command's name, a
    current-window flag — and a single-window session names
    itself enough with the label. The right side pins to empty
    with them: tmux's default paints the pane's title, the clock,
    and the date there, and the label is the whole bar.
    Everything
    lands on this launch's own server (the dedicated socket
    carries it), so the operator's own tmux server, when one
    runs, keeps its own settings."""
    socket = socket_name(workspace_id)
    pane = [
        sys.executable,
        "-m",
        MODULE,
        "pane",
        "--",
        *child,
    ]
    argv = [
        "tmux",
        "-L",
        socket,
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
        str(HISTORY_LINES),
    ]
    for key, command in PAGE_SCROLL_COMMANDS:
        argv += [";", "bind-key", "-n", key, command]
    argv += consent_chain(workspace_id, socket)
    argv += [
        ";",
        "set-option",
        "-g",
        "set-titles",
        "off",
        ";",
        "set-option",
        "-g",
        "status-left",
        f"[{label.replace('#', '##')}] ",
        ";",
        "set-option",
        "-g",
        "status-left-length",
        str(STATUS_LEFT_LENGTH),
        # The window-list formats pin to empty (#458): tmux's
        # default bar follows the left side with the window list,
        # and a single-window session names itself enough with
        # the label. The right side pins to empty with them —
        # tmux's default paints the pane title, the clock, and
        # the date there — so the label is the whole bar.
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
        session,
        shlex.join(pane),
        ";",
        "set-option",
        "destroy-unattached",
        "on",
    ]
    return argv


def consent_chain(workspace_id: str | None, socket: str) -> list[str]:
    """The hidden consent session's slice of the launch chain
    (#467), empty for a child naming no workspace: the session
    itself — born detached at the popup's geometry, so the app
    never reflows when a viewer attaches — its three options, and
    the reopen binding, all ahead of the shell's own
    ``new-session`` so the decider stands before the window the
    operator reads. The status bar pins off (the popup shows the
    app alone), the prefix pins off (every key reaches the app,
    and ``C-b`` closes the viewer from inside it), and
    ``destroy-unattached`` pins off: the popup closing leaves the
    decider standing while the shell session's own ``on`` — set
    once both sessions exist, so it never touches this one —
    tears the shell session down with its window. The app itself
    carries the window-gone retirement (see
    :mod:`msks.client.tui.decide_app`)."""
    if workspace_id is None:
        return []
    return [
        ";",
        "new-session",
        "-d",
        "-s",
        CONSENT_SESSION,
        # Born at the viewer's inner size — ``display-popup``'s
        # -w/-h carry its border — so a viewer's attach resizes
        # nothing (#467 review).
        "-x",
        str(POPUP_COLS - 2),
        "-y",
        str(POPUP_ROWS - 2),
        shlex.join(decide_app_command(workspace_id, socket)),
        ";",
        "set-option",
        "-t",
        CONSENT_SESSION,
        "status",
        "off",
        ";",
        "set-option",
        "-t",
        CONSENT_SESSION,
        "prefix",
        "None",
        ";",
        "set-option",
        "-t",
        CONSENT_SESSION,
        "destroy-unattached",
        "off",
        ";",
        "bind-key",
        REOPEN_KEY,
        popup_command(socket),
    ]


def workspace_from_argv(argv: list[str]) -> str | None:
    """The workspace an appended msks invocation names: the token
    after the last ``ssh`` (the form :func:`ssh_child_argv` in the
    TUI appends). A child naming no workspace still runs — its
    window simply ships no consent watcher."""
    for i in range(len(argv) - 1, 0, -1):
        if argv[i - 1] == "ssh":
            return argv[i]
    return None


def status_label(workspace_id: str | None) -> str:
    """The status bar's workspace label (#455): the daemon's name
    for the workspace — the session name tmux would otherwise
    show is the id, an opaque token to read. The window opens
    whatever the daemon answers: a lookup that cannot land (no
    token, a daemon down, a workspace gone) falls back to the id,
    a row naming nothing keeps the id too, and a child naming no
    workspace carries ``shell`` like its session. The wait stays
    short so a daemon that hangs delays the window by seconds,
    not by the REST read budget."""
    if workspace_id is None:
        return session_name(workspace_id)
    try:
        row = asyncio.run(
            asyncio.wait_for(
                workspace_row(workspace_id, env_url(), env_token()),
                LABEL_WAIT_S,
            )
        )
    except Exception, SystemExit:
        return workspace_id
    return row.get("name") or workspace_id


def session_name(workspace_id: str | None) -> str:
    """The tmux session name on this launch's socket: the
    workspace, or ``shell`` for a child naming none."""
    return workspace_id or "shell"


def socket_name(workspace_id: str | None, token: int | None = None) -> str:
    """This launch's dedicated tmux socket name: the workspace plus
    a per-launch discriminator, so a second window on the same
    workspace opens its own server (two operators, two shells)
    instead of sharing a session. A server of its own is what
    carries the launcher's environment to the pane — a shared
    server keeps the environment of whoever started it. The
    server's socket file lands in the tmp tmux dir and outlives
    the server by whatever the tmp reapers take; one small file
    per window is the accepted cost."""
    suffix = os.getpid() if token is None else token
    return f"msks-{workspace_id or 'shell'}-{suffix}"


def child_argv(argv: list[str]) -> list[str]:
    """The appended command after the ``--`` separator (the empty
    form is a usage error the caller's exec refuses)."""
    if "--" in argv:
        argv = argv[argv.index("--") + 1 :]
    return argv


RUNNERS = {
    "launch": run_launch,
    "pane": run_pane,
}


if __name__ == "__main__":  # pragma: no cover — the -m entry the
    # session command and the popup run; the console entry
    # (pyproject's msks-term-popup) calls main() directly.
    raise SystemExit(main())
