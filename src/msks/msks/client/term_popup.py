"""The tmux consent-terminal launcher (#379).

``terminal_open_cmd`` names a command prefix and the TUI appends
the ``msks ssh`` invocation after it (#341). This module ships the
suffix half: ``msks-term-popup`` runs inside whatever terminal
window the prefix opens — ``konsole -e msks-term-popup``, ``xterm
-e msks-term-popup``, any terminal that runs a command — and runs
the appended command as a local tmux session. While the session
lives, a background watcher speaks the events websocket as a
consent decider for the workspace; an egress request needing a
verdict raises a tmux popup over the shell, and the popup lists
every current hold for the workspace (#461): the operator moves
between the holds with the arrow keys, allows or denies each one,
and a hold leaves the list the moment the daemon resolves it — a
verdict from another decider window, or the hold's own timeout.
The popup closes itself when the list empties; Esc closes it any
time, and the holds a closed popup leaves behind stay held, to
their timeout or a verdict from the consent TUI. The terminal
choice stays where it already lives: in ``terminal_open_cmd``'s
own prefix.

Four roles share this module, spelled as the first argument:

``launch``
    The console entry the operator's prefix runs. Validates tmux,
    names the window per ``MSKSC_TERMINAL_TITLE`` when the setting
    is in place (#445), resolves the workspace's name for the
    session's status bar (#455), names a fresh session after the
    workspace on a dedicated socket (a server of its own, so the
    pane inherits this process's environment — an operator's
    already-running tmux server would otherwise substitute its
    own — and the session stays out of their window list), and
    becomes the attached tmux client (``tmux -L <socket>
    new-session ... pane ...``).
``pane``
    The tmux session's first process. Starts the watcher beside the
    shell (its own process group membership takes it down with the
    window), then becomes the appended command.
``watch``
    The consent watcher: connect as decider, raise the popup when
    a request arrives and none stands, and exit once the session
    is gone.
``decide``
    The popup's command: speak the events websocket itself, show
    the live list of the workspace's holds, and post the verdicts
    the operator keys in.

The launcher runs detached with stdio on devnull (#341's spawn
contract), so every role keeps its own diagnostics to a log file
under the tmp dir; the popup itself owns the only interactive
surface.
"""

import asyncio
import fcntl
import json
import os
import shlex
import shutil
import struct
import subprocess
import sys
import tempfile
import termios
import time
import tty

import websockets

from . import wsauth
from .egress import DURATIONS, connect_args, dest_label, refused
from .env import env_token, env_url
from .rest import api_client, request, workspace_row
from .tui.consent import (
    ADDED,
    REJECTED,
    RESOLVED,
    RULES,
    ConsentController,
    ConsentRequest,
    fmt_duration,
)
from .wintitle import TITLE_MARKER, configured_title, set_window_title

#: The popup geometry: fixed cells, sized for the 80-column window
#: the consent screens already target; tmux clips it to a smaller
#: client window. The rows cover the daemon's pending cap (the
#: default rate limit holds eight) beside the chrome — header,
#: key map, status — so the common case lists every hold at once;
#: more holds than fit scroll a window around the selection.
POPUP_COLS = 76
POPUP_ROWS = 16

#: The popup's fixed chrome: the header row, the blank under it,
#: the blank over the key map, the key map's three rows, and the
#: status line — the list's rows are everything between.
CHROME_ROWS = 7

#: How long the outcome line stays up before the popup closes —
#: an instant close would eat the verdict's confirmation.
OUTCOME_LINGER_S = 1.2

#: The popup's repaint beat: the countdowns move once a second,
#: and an empty list past its grace closes on a tick.
TICK_S = 1.0

#: How long the popup waits after its registration's rules frame
#: before an empty list counts as final: the pending rows land
#: right behind that frame, and a blank beat between the two is a
#: slow link, not a workspace with nothing held.
SNAPSHOT_GRACE_S = 0.5

#: How long a lone Esc byte waits for a sequence tail before it
#: counts as the Esc key itself — an arrow's three bytes arrive
#: together, and only a bare Esc sits alone.
ESC_WAIT_S = 0.05

#: The cursor escapes the popup writes on its own tty: the cursor
#: hides while the list stands (the selection marker points) and
#: comes back on every exit path.
HIDE_CURSOR = "\x1b[?25l"
SHOW_CURSOR = "\x1b[?25h"

#: The recv idle window the watcher sits in when no frames flow:
#: each wake re-checks that its tmux session still exists, so a
#: closed window retires the watcher within one tick.
LIVENESS_TICK_S = 5.0

#: The popup's quick verdicts: ``a`` allows until restart (the
#: common case), ``d`` denies now. The uppercase twins (``A``,
#: ``D``) open the duration chooser for the same verdict. A key
#: outside the map changes nothing — the hold stays held, for its
#: timeout or a later verdict, and the map names every action.
CHOICES = {
    "a": ("allow", "tilrestart"),
    "d": ("deny", "once"),
}

#: The duration chooser's keys, in :data:`egress.DURATIONS` order.
DURATION_KEYS = {str(i): duration for i, duration in enumerate(DURATIONS, 1)}

#: The keys that open the duration chooser, and the verdict each
#: parks until its duration key lands.
CHOOSER_KEYS = {"A": "allow", "D": "deny"}

#: The popup's ANSI paint codes: bold labels, a bold-cyan
#: destination, a faint request id, green allow bindings, red deny
#: bindings. :func:`span` applies a code only while :func:`ansi`
#: says stdout takes paint (a terminal that did not opt out with
#: NO_COLOR).
PAINT_LABEL = "1"
PAINT_FACT = "1;36"
PAINT_ID = "2"
PAINT_KEY = "32"
PAINT_DKEY = "31"
PAINT_ALLOW = "1;32"
PAINT_DENY = "1;31"

#: The popup key map's quick-form column width: the widest cell
#: ("[a] until restart") plus a one-space gutter, so the duration
#: chooser column lines up under itself row over row.
KEY_COLUMN = 20

#: The modules this package reaches itself through: the pane, the
#: watcher, and the popup's command all re-invoke this module by
#: name with the interpreter that is already running it.
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

#: The escape sequences the popup's keys ride in: the arrows in
#: both spellings a terminal sends (CSI and SS3), mapped to the
#: names the popup speaks. Left and right arrive and count for
#: nothing — the list has one column.
KEY_SEQUENCES = (
    (b"\x1b[A", "up"),
    (b"\x1b[B", "down"),
    (b"\x1bOA", "up"),
    (b"\x1bOB", "down"),
    (b"\x1b[C", ""),
    (b"\x1b[D", ""),
    (b"\x1bOC", ""),
    (b"\x1bOD", ""),
)

ROLES = ("launch", "pane", "watch", "decide")


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
    """The tmux pane's first process (#379): start the consent
    watcher beside the shell, then become the appended command. The
    watcher stays in this pane's process group, so the window's
    teardown SIGHUP retires it with the shell; its own liveness
    check is the backstop. No workspace in the child (or no tmux
    environment — a hand-run pane) runs the command alone. The
    new-window marker (#445) is dropped before anything runs: the
    window is already titled — :func:`run_launch` wrote it before
    attaching tmux — and tmux owns the pane's escapes, so the
    session has no title of its own to write."""
    os.environ.pop(TITLE_MARKER, None)
    session, rest = take_option("-s", argv)
    workspace_id, rest = take_option("-w", rest)
    child = child_argv(rest)
    if workspace_id is not None and os.environ.get("TMUX"):
        start_watcher(session or session_name(workspace_id), workspace_id)
    os.execvp(child[0], child)
    return 0  # pragma: no cover — execvp replaces the process


def run_watch(argv: list[str]) -> int:
    """The consent watcher role: one loop over the events websocket
    until its tmux session is gone. Never returns early on a closed
    connection — the reconnect ladder keeps the decider registered
    through a daemon restart; the liveness check is the only exit."""
    session, rest = take_option("-s", argv)
    workspace_id, rest = take_option("-w", rest)
    if session is None or workspace_id is None or rest:
        raise SystemExit(
            "usage: msks.client.term_popup watch -s SESSION -w WORKSPACE"
        )
    return asyncio.run(watch_loop(workspace_id, session))


def run_decide(argv: list[str]) -> int:
    """The popup's command (#461): speak the events websocket as a
    decider of its own, show the live list of the workspace's
    holds, and post the verdicts the operator keys in. The tty
    stays in cbreak for the whole session — one keypress is one
    key — and the terminal's saved modes come back on every exit
    path. A stdin that is not a terminal (a test, a piped run)
    skips the key surface: the list still lives, and the popup
    still closes itself when it empties."""
    workspace_id, rest = take_option("-w", argv)
    if workspace_id is None or rest:
        raise SystemExit("usage: msks.client.term_popup decide -w WORKSPACE")
    saved = None
    if sys.stdin.isatty():
        saved = termios.tcgetattr(sys.stdin.fileno())
        tty.setcbreak(sys.stdin.fileno())
    try:
        return asyncio.run(decide_loop(workspace_id))
    finally:
        if saved is not None:
            termios.tcsetattr(sys.stdin.fileno(), termios.TCSADRAIN, saved)


# --- the watcher's frame handling ------------------------------------------


class WatchState:
    """The watcher's cross-frame state (#461): the ids this window
    already saw resolved, and the latch that keeps one popup up at
    a time — a request that lands while the popup stands shows in
    the popup's own list, so its frame needs no raise of its own.
    A popup the watcher cannot raise (tmux gone) sets the give-up
    flag: the watcher stays registered as the workspace's decider,
    and the diagnostics carry the one line naming the failure."""

    def __init__(self) -> None:
        self.resolved: set[str] = set()
        self.popup_up = False
        self.gave_up = False
        self.tasks: set[asyncio.Task] = set()


async def watch_loop(workspace_id: str, session: str) -> int:
    """The watcher's event loop (#379): register as decider on
    every connection until its tmux session is gone. Never returns
    early on a closed connection — the reconnect ladder keeps the
    decider registered through a daemon restart; the liveness
    check is the only exit."""
    state = WatchState()
    try:
        url, token = env_url(), env_token()
        async for sock in websockets.connect(**connect_args(url, token)):
            if not await watch_connection(sock, workspace_id, session, state):
                return 0
    except wsauth.UnusableToken as exc:
        # One line without the token in it — the websocket library's
        # own refusal embeds the credential whole (#116 review).
        raise SystemExit(f"msks: {exc}") from None
    return 0


async def watch_connection(
    sock, workspace_id: str, session: str, state: WatchState
) -> bool:
    """One connection's lifetime: announce as decider, then idle
    no longer than one liveness tick so a closed window retires
    the watcher even with no traffic flowing. False means retired;
    True means the server closed the connection — at the announce
    or mid-stream — and the loop reconnects."""
    try:
        await sock.send(
            json.dumps({"type": "egress.decider", "workspace": workspace_id})
        )
        while True:
            try:
                raw = await asyncio.wait_for(
                    sock.recv(), timeout=LIVENESS_TICK_S
                )
            except TimeoutError:
                if not await asyncio.to_thread(session_alive, session):
                    return False
                continue
            await watch_frame(json.loads(raw), workspace_id, session, state)
    except websockets.ConnectionClosed as closed:
        return closed_connection(closed)


def closed_connection(closed: websockets.ConnectionClosed) -> bool:
    """A closed connection's verdict: the auth refusal exits the
    process (a token the daemon does not hold cannot become valid
    by reconnecting — one line, no spin); any other close tells
    the loop to reconnect — the server re-sends the snapshot."""
    if refused(closed):
        raise SystemExit(f"msks: {wsauth.AUTH_FAILED_MESSAGE}") from None
    return True


async def watch_frame(
    frame: dict, workspace_id: str, session: str, state: WatchState
) -> None:
    """One events frame (#461): a fresh request raises the list
    popup while none stands — and every resolution records its id,
    so neither a reconnect's re-sent snapshot nor a queued frame
    re-raises what this window already saw decided. A refused
    decider registration — the workspace id names nothing — stops
    the watcher with the log's one line naming the id and the
    daemon's reason."""
    event = frame.get("event")
    data = frame.get("data", {})
    if event == "egress.request":
        watch_request(data, workspace_id, session, state)
    elif event == "egress.resolved":
        watch_resolution(data, state)
    elif event == "egress.decider_rejected":
        stop_refused(workspace_id, data)


def watch_request(
    data: dict, workspace_id: str, session: str, state: WatchState
) -> None:
    """A request frame's raise: the ids this window saw decided
    and the popup latch both stand in front of it."""
    row = data.get("request")
    rid = row.get("id") if isinstance(row, dict) else None
    if rid and rid not in state.resolved and not state.popup_up:
        raise_later(workspace_id, session, state)


def watch_resolution(data: dict, state: WatchState) -> None:
    """A resolution frame's record: the id never raises again."""
    rid = data.get("request_id")
    if isinstance(rid, str):
        state.resolved.add(rid)


def stop_refused(workspace_id: str, data: dict) -> None:
    """The daemon's refusal of this watcher's registration: one
    line in the log, and the watcher stops."""
    print(
        f"decider registration refused: {workspace_id}"
        f" ({data.get('reason', 'no reason given')}); stopping",
        flush=True,
    )
    raise SystemExit(1)


def raise_later(workspace_id: str, session: str, state: WatchState) -> None:
    """Latch the popup up and raise it off the frame path: the
    recv loop keeps flowing while the popup stands (a resolution
    that lands mid-popup records before a queued request frame
    could re-raise it), and the latch opens when the popup's
    process ends. A raise that cannot run — tmux gone — gives up
    on popups with one line in the watcher's log and leaves the
    registration standing."""
    if state.gave_up:
        return
    state.popup_up = True

    async def cycle() -> None:
        try:
            await asyncio.to_thread(raise_popup, workspace_id, session)
        except OSError as exc:
            state.gave_up = True
            print(f"popup unavailable: {exc}; giving up on popups", flush=True)
        finally:
            state.popup_up = False

    task = asyncio.create_task(cycle())
    state.tasks.add(task)
    task.add_done_callback(state.tasks.discard)


def raise_popup(workspace_id: str, session: str) -> int:
    """Open the decider overlay over the session's window: one
    tmux client (the terminal window's own client) gets the popup,
    and it closes itself when the decide role exits (``-E``). No
    attached client — the window closed between the frame and here
    — returns 1; the caller moves on."""
    client = popup_client(session)
    if client is None:
        return 1
    proc = subprocess.run(
        [
            "tmux",
            "display-popup",
            "-t",
            client,
            "-E",
            "-w",
            str(POPUP_COLS),
            "-h",
            str(POPUP_ROWS),
            decide_command(workspace_id),
        ],
        check=False,
    )
    return proc.returncode


def popup_client(session: str) -> str | None:
    """The session's attached client tty — the window the popup
    overlays. tmux addresses popup targets by client, and a session
    names its clients, so this resolves the one window the prefix
    opened even though the watcher itself is no client."""
    try:
        proc = subprocess.run(
            ["tmux", "list-clients", "-t", session, "-F", "#{client_tty}"],
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError:
        return None
    if proc.returncode != 0:
        return None
    for line in proc.stdout.splitlines():
        if line.strip():
            return line.strip()
    return None


def session_alive(session: str) -> bool:
    """Whether the tmux session still exists — False for a dead
    session and for a dead server alike (``has-session`` fails
    either way), which is the watcher's retirement condition."""
    try:
        proc = subprocess.run(
            ["tmux", "has-session", "-t", session],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
    except OSError:
        return False
    return proc.returncode == 0


def start_watcher(session: str, workspace_id: str) -> None:
    """Spawn the watcher beside the pane's command, its output to
    a log file under the tmp dir (the pane's own stdio belongs to
    the shell; the file is the watcher's one diagnostic surface —
    the refused-registration line, the auth refusal — and tmp
    reapers collect it). The child inherits the open file
    descriptor, and the same process group and $TMUX environment
    the pane holds are what the watcher rides to its grave and to
    the server socket."""
    log = tempfile.NamedTemporaryFile(
        prefix=f"msks-consent-{workspace_id}-", suffix=".log", delete=False
    )
    try:
        subprocess.Popen(
            [
                sys.executable,
                "-m",
                MODULE,
                "watch",
                "-s",
                session,
                "-w",
                workspace_id,
            ],
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=subprocess.STDOUT,
        )
    finally:
        log.close()


async def post_verdict(
    workspace_id: str, request_id: str, decision: str, duration: str
) -> None:
    """Post one verdict through the shared REST contract: failures
    raise SystemExit with the readable line the popup prints."""
    async with api_client(env_url(), env_token()) as client:
        await request(
            client,
            "POST",
            f"/api/v1/workspaces/{workspace_id}/egress/requests/{request_id}",
            {"decision": decision, "duration": duration},
        )


# --- the decide role: the live list ----------------------------------------


class SessionDone(Exception):
    """The popup's own exit, out of a session task: carries the
    process exit code and, for the exits that name their reason
    (the auth refusal), the one line to print — a SystemExit
    raised inside a task escapes the loop's own machinery, so the
    task hands the reason over and the main coroutine raises it."""

    def __init__(self, code: int, message: str | None = None) -> None:
        super().__init__(code)
        self.code = code
        self.message = message


async def decide_loop(
    workspace_id: str, keys: asyncio.Queue[str] | None = None
) -> int:
    """The popup's event loop (#461): register as a decider of its
    own on every connection, run the list until it empties or the
    operator leaves, and reconnect through a daemon restart — the
    registration re-sends the snapshot, and the resync clears what
    resolved while the connection was down."""
    keys, source = open_keys(keys)
    ui = PopupUI(workspace_id)
    cursor(HIDE_CURSOR)
    try:
        url, token = env_url(), env_token()
        async for sock in websockets.connect(**connect_args(url, token)):
            code = await popup_connection(sock, ui, keys, workspace_id)
            if code is not None:
                return code
    except wsauth.UnusableToken as exc:
        # One line without the token in it — the websocket library's
        # own refusal embeds the credential whole (#116 review).
        raise SystemExit(f"msks: {exc}") from None
    finally:
        if source is not None:
            source.close()
        cursor(SHOW_CURSOR)
    return 0


def open_keys(
    keys: asyncio.Queue[str] | None,
) -> tuple[asyncio.Queue[str] | None, KeySource | None]:
    """The popup's key surface: the pty's readable edge when stdin
    is a terminal, the caller's own queue when it hands one (the
    tests), and none at all on a piped stdin — the list still
    lives, and the popup still closes itself when it empties."""
    if keys is not None or not sys.stdin.isatty():
        return keys, None
    source = KeySource(sys.stdin.fileno())
    return source.keys, source


def cursor(code: str) -> None:
    """Hide or show the terminal's cursor: the selection marker
    points, so the cursor itself stays out of the list's face —
    and a stdout that is not a terminal (a pipe, a test) takes no
    escape at all."""
    if sys.stdout.isatty():
        sys.stdout.write(code)
        sys.stdout.flush()


async def popup_connection(
    sock, ui: PopupUI, keys: asyncio.Queue[str] | None, workspace_id: str
) -> int | None:
    """One connection from the popup's side: announce as a
    decider, mark the list for the resync, and run the session."""
    ui.resync = True
    await sock.send(
        json.dumps({"type": "egress.decider", "workspace": workspace_id})
    )
    done = await popup_session(sock, ui, keys)
    if done is None:
        return None
    if done.message:
        raise SystemExit(done.message)
    return done.code


async def popup_session(
    sock, ui: PopupUI, keys: asyncio.Queue[str] | None
) -> SessionDone | None:
    """One connection's session: the frames, the keys, and the
    repaint tick run side by side; the first to finish ends it. A
    SessionDone is the popup's own exit; None means the connection
    closed and the caller reconnects."""
    tasks = [asyncio.create_task(popup_reader(sock, ui))]
    if keys is not None:
        tasks.append(asyncio.create_task(popup_keyer(keys, ui)))
    tasks.append(asyncio.create_task(popup_ticker(ui)))
    try:
        await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    finally:
        await retire(tasks)
    return session_result(tasks)


async def retire(tasks: list[asyncio.Task]) -> None:
    """Cancel and reap the session's tasks: the first finisher
    ended the session, the rest stop where they stand."""
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)


def session_result(tasks: list[asyncio.Task]) -> SessionDone | None:
    """The session's verdict: the popup's own exit when a task
    carries one, None when the connection closed — and any other
    exception re-raises into the caller."""
    for task in tasks:
        if task.cancelled():
            continue
        exc = task.exception()
        if isinstance(exc, SessionDone):
            return exc
        if exc is not None:
            raise exc
    return None


async def popup_reader(sock, ui: PopupUI) -> None:
    """The frames task: every inbound frame lands on the UI; a
    closed connection returns (the caller reconnects), the auth
    refusal exits with its one line, and the UI's own exits — the
    emptied list, the refused registration — linger their last
    frame for the read it needs."""
    while True:
        try:
            raw = await sock.recv()
        except websockets.ConnectionClosed as closed:
            if refused(closed):
                raise SessionDone(
                    1, f"msks: {wsauth.AUTH_FAILED_MESSAGE}"
                ) from None
            return
        if ui.frame(raw):
            await ui.close_out()
            raise SessionDone(ui.exit_code)


async def popup_keyer(keys: asyncio.Queue[str], ui: PopupUI) -> None:
    """The keys task: every decoded key lands on the UI; the UI's
    own exits (Esc, the emptied list) leave without the linger —
    the operator's key already said goodbye."""
    while True:
        if ui.key(await keys.get()):
            raise SessionDone(ui.exit_code)


async def popup_ticker(ui: PopupUI) -> None:
    """The tick task: the per-second repaint that moves the
    countdowns and closes an emptied list past its grace."""
    while True:
        await asyncio.sleep(TICK_S)
        if ui.tick():
            raise SessionDone(ui.exit_code)


class KeyDecoder:
    """The pty's bytes into key names (#461): the arrows in both
    spellings a terminal sends, a lone Esc that waits briefly for
    a tail before it counts, and single printable keys. Anything
    else — a control byte, an unknown sequence, an Esc pair
    (Alt+key) — drops or degrades to the key it carried, so a
    terminal's odd emission never closes the popup by accident."""

    def __init__(self) -> None:
        self.buf = bytearray()

    @property
    def parked(self) -> bool:
        """Whether the buffer head waits on more bytes — an escape
        sequence mid-arrival."""
        return bool(self.buf)

    def feed(self, data: bytes) -> list[str]:
        """Drain arrived bytes into keys; a parked prefix stays for
        its tail or :meth:`lapse`."""
        self.buf += data
        keys = []
        while self.buf:
            key = self.take()
            if key is None:
                break
            if key:
                keys.append(key)
        return keys

    def take(self) -> str | None:
        """One key off the buffer head: a name, ``""`` for bytes
        consumed silently, or None while a prefix parks."""
        first = self.buf[0]
        if first == 0x1B:
            return self.take_escape()
        del self.buf[0]
        if first in (0x0A, 0x0D):
            return "enter"
        if first == 0x04:  # Ctrl-D leaves the way Esc does
            return "esc"
        if 0x20 <= first < 0x7F:
            return chr(first)
        return ""

    def take_escape(self) -> str | None:
        """The buffer's escape head: a known sequence's name, or
        whatever the unknown-escape rules make of it."""
        data = bytes(self.buf)
        for seq, key in KEY_SEQUENCES:
            if data.startswith(seq):
                del self.buf[: len(seq)]
                return key
        return self.take_unknown_escape(data)

    def take_unknown_escape(self, data: bytes) -> str | None:
        """The escape head no known sequence claims: a CSI or SS3
        body parks until its final byte and drops whole when it
        lands, a lone Esc parks for the lapse, and an Esc pair
        (Alt+key) sheds its modifier."""
        if data.startswith(b"\x1b["):
            return self.drop_csi(data)
        if data.startswith(b"\x1bO"):
            if len(data) < 3:
                return None
            del self.buf[:3]  # an unknown SS3 pair drops whole
            return ""
        if data == b"\x1b":
            return None  # a lone Esc: the lapse decides
        del self.buf[:1]
        return ""

    def drop_csi(self, data: bytes) -> str | None:
        """A CSI body: parked until its final byte arrives, then
        dropped whole."""
        span = csi_span(data)
        if span is None:
            return None
        del self.buf[:span]  # an unknown CSI: drop it whole
        return ""

    def lapse(self) -> list[str]:
        """The parked prefix after the wait: a lone Esc counts at
        last; a partial sequence had its tail lost, and drops."""
        lone = self.buf == b"\x1b"
        self.buf.clear()
        return ["esc"] if lone else []


def csi_span(data: bytes) -> int | None:
    """The length of one CSI sequence: the escape, the bracket,
    the parameter and intermediate bytes, and the final byte that
    closes it; None while that final byte has yet to arrive. A
    control byte inside the body ends the span early — the tail
    belongs to whatever comes next."""
    for i in range(2, len(data)):
        if data[i] < 0x20:
            return i
        if 0x40 <= data[i] <= 0x7E:
            return i + 1
    return None


class KeySource:
    """stdin's bytes onto the loop's key queue (#461): each
    readable edge feeds :class:`KeyDecoder`, and a parked escape
    prefix settles on a short timer — a lone Esc counts only once
    its tail fails to arrive. The queue is the same one a caller
    may hand :func:`decide_loop`, so the tests drive the key path
    with the pump switched off."""

    def __init__(self, fd: int) -> None:
        self.fd = fd
        self.decoder = KeyDecoder()
        self.timer = None
        self.keys: asyncio.Queue[str] = asyncio.Queue()
        asyncio.get_running_loop().add_reader(fd, self.readable)

    def readable(self) -> None:
        """One readable edge: drain what arrived and re-settle."""
        try:
            data = os.read(self.fd, 64)
        except OSError:
            return
        for name in self.decoder.feed(data):
            self.keys.put_nowait(name)
        self.settle()

    def settle(self) -> None:
        """(Re)arm the lapse timer while a prefix parks."""
        if self.timer is not None:
            self.timer.cancel()
            self.timer = None
        if self.decoder.parked:
            self.timer = asyncio.get_running_loop().call_later(
                ESC_WAIT_S, self.lapse
            )

    def lapse(self) -> None:
        """The timer's edge: settle the parked prefix."""
        self.timer = None
        for name in self.decoder.lapse():
            self.keys.put_nowait(name)
        self.settle()

    def close(self) -> None:
        """Take the reader and any armed timer off the loop."""
        if self.timer is not None:
            self.timer.cancel()
            self.timer = None
        asyncio.get_running_loop().remove_reader(self.fd)


class PopupUI:
    """The popup's screen state (#461): the pending list off the
    shared consent controller, the selection, the duration
    chooser, and the status line. :meth:`paint` is the only
    output edge; everything else is pure enough to drive straight
    from tests."""

    def __init__(
        self,
        workspace_id: str,
        *,
        clock=time.time,
        on: bool | None = None,
    ) -> None:
        self.workspace_id = workspace_id
        self.controller = ConsentController(
            clock=clock, workspace_id=workspace_id
        )
        self.clock = clock
        self.on = ansi() if on is None else on
        self.index = 0
        self.chooser: str | None = None
        self.status = ("connecting", "")
        self.posts: set[asyncio.Task] = set()
        self.exited = False
        self.exit_code = 0
        self.resync = True
        self.rules_at: float | None = None
        self.close_after: float | None = None

    def frame(self, raw: str) -> bool:
        """One events frame: apply it, repaint, and report the
        popup's exit."""
        outcome, payload = self.controller.apply_frame(raw)
        self.apply_outcome(outcome, payload)
        self.paint()
        return self.exited or self.closing()

    def apply_outcome(self, outcome: str, payload: object) -> None:
        """One frame's state change — the refused registration is
        the only frame that exits on its own."""
        if outcome == RULES:
            self.land_rules()
        elif outcome == ADDED:
            self.land_request()
        elif outcome == RESOLVED:
            self.index = min(
                self.index, max(len(self.controller.pending) - 1, 0)
            )
        elif outcome == REJECTED:
            self.land_refusal(payload)

    def land_request(self) -> None:
        """The list's first hold takes the selection."""
        if len(self.controller.pending) == 1:
            self.index = 0

    def land_refusal(self, payload: object) -> None:
        """The daemon's refusal of the popup's registration: the
        reason lands on the status line, and the exit code names
        the stop."""
        self.status = (
            f"decider registration refused: {payload or 'no reason given'}",
            PAINT_DENY,
        )
        self.exited = True
        self.exit_code = 1

    def key(self, name: str) -> bool:
        """One decoded key: the map owns every action, and a key
        outside it changes nothing — the hold stays held, for its
        timeout or a later verdict. Esc is the one way out ahead of
        the list emptying."""
        if name == "esc":
            return self.escape()
        self.act(name)
        self.paint()
        return self.exited

    def escape(self) -> bool:
        """The Esc key: out of the chooser when one stands, out of
        the popup otherwise."""
        if self.chooser:
            self.chooser = None
            return False
        self.exited = True
        self.exit_code = 0
        return True

    def act(self, name: str) -> None:
        """Every non-Esc key: the arrows move the selection, and
        the verdict keys act on it — the chooser takes the next
        key as its duration."""
        if name in ("up", "down"):
            self.step(name)
        elif self.chooser:
            self.choose(name)
        elif name in CHOICES:
            self.decide(*CHOICES[name])
        elif name in CHOOSER_KEYS:
            self.chooser = CHOOSER_KEYS[name]

    def step(self, name: str) -> None:
        """One arrow: the selection moves unless a chooser waits
        on the next key."""
        if not self.chooser:
            self.move(-1 if name == "up" else 1)

    def tick(self) -> bool:
        """The per-second beat: the countdowns move, and an empty
        list past its grace closes the popup."""
        self.paint()
        return self.closing()

    def closing(self) -> bool:
        """The emptied list's exit: only after the registration's
        frames had their grace — the pending rows land right behind
        the rules frame, and the blank beat between the two is a
        slow link, not a workspace with nothing held."""
        if self.rules_at is None or self.controller.pending:
            return False
        if self.close_after is not None and self.clock() < self.close_after:
            return False
        self.exited = True
        self.exit_code = 0
        return True

    async def close_out(self) -> None:
        """Hold the last frame up for the read it needs before the
        popup closes — a close without a line to read (a raise for
        a hold another window already decided) skips the beat."""
        if self.status[0]:
            await asyncio.sleep(OUTCOME_LINGER_S)

    def land_rules(self) -> None:
        """One rules frame: each connection's first precedes that
        connection's pending snapshot, so the grace window re-arms
        and a resync's stale rows clear — a hold that resolved
        while the connection was down never comes back, and the
        snapshot's rows re-land right behind the frame. The first
        one also drops the connecting line."""
        if self.resync:
            self.controller.pending.clear()
            self.index = 0
            self.resync = False
        self.close_after = self.clock() + SNAPSHOT_GRACE_S
        if self.rules_at is None:
            self.rules_at = self.clock()
            self.status = ("", "")

    def move(self, delta: int) -> None:
        """Move the selection, clamped to the list."""
        count = len(self.controller.pending)
        if count:
            self.index = min(max(self.index + delta, 0), count - 1)

    def choose(self, name: str) -> None:
        """The chooser's key: a duration for the parked verdict."""
        decision = self.chooser or "deny"
        self.chooser = None
        self.decide(decision, duration_for(name))

    def decide(self, decision: str, duration: str) -> None:
        """Post one verdict for the selected hold, off the key
        path: the row leaves when its resolution frame lands, not
        when the post returns."""
        rows = self.rows()
        if rows:
            task = asyncio.create_task(
                self.post(decision, duration, rows[self.index])
            )
            self.posts.add(task)
            task.add_done_callback(self.posts.discard)

    async def post(
        self, decision: str, duration: str, request: ConsentRequest
    ) -> None:
        """One verdict through the shared REST contract: failures
        name their reason on the status line (the request resolved
        in another window, the daemon unreachable) and the hold
        stays for its own exit."""
        label = dest_label(
            {
                "dest_host": request.dest_host,
                "dest_port": request.dest_port,
            }
        )
        try:
            await post_verdict(
                self.workspace_id, request.id, decision, duration
            )
        except SystemExit as exc:
            self.status = (str(exc), PAINT_DENY)
        else:
            paint = PAINT_ALLOW if decision == "allow" else PAINT_DENY
            self.status = (f"{decision} {label} ({duration})", paint)
        self.paint()

    def rows(self) -> list[ConsentRequest]:
        """The pending holds, oldest first — the stable order the
        selection rides."""
        return self.controller.ordered()

    def paint(self) -> None:
        """Repaint the popup: home, clear below, draw. A dead pty
        (the window closed under the popup) marks the exit instead
        of raising through the loop."""
        cols, height = popup_size()
        try:
            sys.stdout.write(f"\x1b[H\x1b[J{self.render(cols, height)}\n")
            sys.stdout.flush()
        except OSError:
            self.exited = True

    def render(self, width: int, height: int) -> str:
        """The popup's frame as text: the header, the list's window
        around the selection, the key map, and the status line."""
        rows = self.rows()
        fit = max(height - CHROME_ROWS, 1)
        start = 0
        if len(rows) > fit:
            start = min(max(self.index - fit // 2, 0), len(rows) - fit)
        window = rows[start : start + fit]
        lines = [self.header_line(len(rows), len(rows) - len(window)), ""]
        for offset, hold in enumerate(window):
            lines.append(
                self.hold_line(hold, start + offset == self.index, width)
            )
        lines.append("")
        lines.extend(self.keymap_lines())
        lines.append(self.status_line())
        return "\n".join(lines)

    def header_line(self, count: int, hidden: int) -> str:
        """The list's header: how many holds pend, and how many
        more sit past the window."""
        text = f"pending egress requests: {count}"
        if hidden:
            text += f" (+{hidden} more)"
        return f"  {span(self.on, PAINT_LABEL, text)}"

    def hold_line(
        self, request: ConsentRequest, selected: bool, width: int
    ) -> str:
        """One hold's row: the destination left, the remaining time
        right, the selection's marker pointing at the row the
        verdict keys act on."""
        label = dest_label(
            {
                "dest_host": request.dest_host,
                "dest_port": request.dest_port,
            }
        )
        remain = self.remaining(request)
        budget = max(width - 6, 12)
        if len(label) + len(remain) + 1 > budget:
            label = clip(label, budget - len(remain) - 1)
        pad = budget - len(label) - len(remain)
        left = f"{'> ' if selected else '  '}{label}"
        if selected:
            left = span(self.on, PAINT_FACT, left)
            remain = span(self.on, PAINT_LABEL, remain)
        else:
            remain = span(self.on, PAINT_ID, remain)
        return f"  {left}{' ' * pad} {remain}"

    def remaining(self, request: ConsentRequest) -> str:
        """The hold's time left, from the frame's honest deadline;
        a frame without one (an older daemon) shows none."""
        if request.expires_at is None:
            return ""
        return fmt_duration(max(0.0, request.expires_at - self.clock()))

    def keymap_lines(self) -> list[str]:
        """The key map's rows: the verdict keys, their duration
        choosers, and the list's own keys."""
        on = self.on
        return [
            f"  {span(on, PAINT_LABEL, 'allow:')}  "
            f"{key_cell(on, PAINT_KEY, '[a]', 'until restart')}"
            f"{span(on, PAINT_KEY, '[A]')} choose duration",
            f"  {span(on, PAINT_LABEL, 'deny:')}   "
            f"{key_cell(on, PAINT_DKEY, '[d]', 'now')}"
            f"{span(on, PAINT_DKEY, '[D]')} choose duration",
            f"  {span(on, PAINT_LABEL, 'select:')}"
            f" {span(on, PAINT_KEY, '[↑]/[↓]')} move"
            f"    {span(on, PAINT_KEY, '[Esc]')} close",
        ]

    def status_line(self) -> str:
        """The status row: the chooser's key map while a verdict
        parks, the last outcome line otherwise."""
        if self.chooser:
            return chooser_line(self.on)
        text, paint = self.status
        if not text:
            return ""
        marked = span(self.on, paint, text) if paint else text
        return f"  {marked}"


def key_cell(on: bool, code: str, key: str, label: str) -> str:
    """One key-map cell: the painted key, its label, and the pad to
    :data:`KEY_COLUMN` — computed on the plain width, so paint
    never shifts the columns."""
    cell = f"{span(on, code, key)} {label}"
    pad = KEY_COLUMN - len(key) - 1 - len(label)
    return f"{cell}{' ' * max(pad, 1)}"


def chooser_line(on: bool) -> str:
    """The duration chooser's key map, in :data:`egress.DURATIONS`
    order."""
    keys = "  ".join(
        f"{span(on, PAINT_KEY, f'[{i}]')} {duration}"
        for i, duration in enumerate(DURATIONS, 1)
    )
    return f"  {span(on, PAINT_LABEL, 'duration:')} {keys}"


def clip(text: str, width: int) -> str:
    """Text cut to width cells, an ellipsis marking the cut."""
    if width <= 0:
        return ""
    return text if len(text) <= width else text[: width - 1] + "…"


def popup_size() -> tuple[int, int]:
    """The popup pty's columns and rows — tmux sizes it to the
    popup geometry, clipped to the window — with the shipped
    geometry standing in when the query cannot run (a pipe, a
    test) or answers nothing."""
    try:
        packed = fcntl.ioctl(sys.stdout.fileno(), termios.TIOCGWINSZ, b" " * 8)
        rows, cols = struct.unpack("HHHH", packed)[:2]
    except OSError, ValueError:
        return POPUP_COLS, POPUP_ROWS
    if cols <= 0 or rows <= 0:
        return POPUP_COLS, POPUP_ROWS
    return cols, rows


def duration_for(key: str) -> str:
    """One duration-chooser keypress; a stray key keeps until
    restart, the chooser's common case."""
    return DURATION_KEYS.get(key, "tilrestart")


def ansi() -> bool:
    """Whether the popup's stdout takes paint: a terminal that did
    not opt out with NO_COLOR."""
    return sys.stdout.isatty() and "NO_COLOR" not in os.environ


def span(on: bool, code: str, text: str) -> str:
    """One painted span — code, text, reset — or the bare text when
    paint is off."""
    if not on:
        return text
    return f"\x1b[{code}m{text}\x1b[0m"


def session_argv(
    child: list[str], session: str, workspace_id: str | None, label: str
) -> list[str]:
    """The tmux argv: one client on this launch's dedicated socket,
    attached to a fresh session whose pane runs this module's pane
    role with the workspace id and the appended command — the
    watcher the pane starts is the feature, so the id rides the
    pane argv explicitly. The pane command is shell-joined as tmux
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
    pane = [
        sys.executable,
        "-m",
        MODULE,
        "pane",
        "-s",
        session,
        *(["-w", workspace_id] if workspace_id else []),
        "--",
        *child,
    ]
    argv = [
        "tmux",
        "-L",
        socket_name(workspace_id),
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


def decide_command(workspace_id: str) -> str:
    """The one-line command the popup runs: this module's decide
    role with the workspace — the holds arrive on the popup's own
    events connection, so the raise carries nothing else."""
    return shlex.join(
        [sys.executable, "-m", MODULE, "decide", "-w", workspace_id]
    )


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


def take_option(flag: str, argv: list[str]) -> tuple[str | None, list[str]]:
    """Pull ``flag VALUE`` out of argv wherever it sits before the
    ``--`` separator — the appended command's own ``-s``/``-w``
    words belong to it, and parsing past the separator would hand
    the role the wrong session or workspace. A trailing flag with
    no value refuses with a usage line."""
    head = argv[: argv.index("--")] if "--" in argv else argv
    if flag in head:
        i = head.index(flag)
        if i + 1 >= len(head):
            raise SystemExit(f"msks-term-popup: {flag} needs a value")
        return head[i + 1], argv[:i] + argv[i + 2 :]
    return None, argv


def child_argv(argv: list[str]) -> list[str]:
    """The appended command after the ``--`` separator (the empty
    form is a usage error the caller's exec refuses)."""
    if "--" in argv:
        argv = argv[argv.index("--") + 1 :]
    return argv


RUNNERS = {
    "launch": run_launch,
    "pane": run_pane,
    "watch": run_watch,
    "decide": run_decide,
}


if __name__ == "__main__":  # pragma: no cover — the -m entry the
    # session command and the popup run; the console entry
    # (pyproject's msks-term-popup) calls main() directly.
    raise SystemExit(main())
