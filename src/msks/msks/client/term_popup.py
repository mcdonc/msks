"""The tmux consent-terminal launcher (#379).

``terminal_open_cmd`` names a command prefix and the TUI appends
the ``msks ssh`` invocation after it (#341). This module ships the
suffix half: ``msks-term-popup`` runs inside whatever terminal
window the prefix opens — ``konsole -e msks-term-popup``, ``xterm
-e msks-term-popup``, any terminal that runs a command — and runs
the appended command as a local tmux session. While the session
lives, a background watcher speaks the events websocket as a
consent decider for the workspace; an egress request needing a
verdict raises a tmux popup over the shell, one keypress decides
it, and the popup closes. The terminal choice stays where it
already lives: in ``terminal_open_cmd``'s own prefix.

Four roles share this module, spelled as the first argument:

``launch``
    The console entry the operator's prefix runs. Validates tmux,
    names the window per ``MSKSC_TERMINAL_TITLE`` when the setting
    is in place (#445), names a fresh session after the workspace
    on a dedicated socket (a server of its own, so the pane
    inherits this process's environment — an operator's
    already-running tmux server would otherwise substitute its
    own — and the session stays out of their window list), and
    becomes the attached tmux client (``tmux -L <socket>
    new-session ... pane ...``).
``pane``
    The tmux session's first process. Starts the watcher beside the
    shell (its own process group membership takes it down with the
    window), then becomes the appended command.
``watch``
    The consent watcher: connect as decider, raise the popup for
    each pending request, and exit once the session is gone.
``decide``
    The popup's command: show the held request, take one keypress,
    and post the verdict.

The launcher runs detached with stdio on devnull (#341's spawn
contract), so every role keeps its own diagnostics to a log file
under the tmp dir; the popup itself owns the only interactive
surface.
"""

import asyncio
import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import termios
import time
import tty

import websockets

from . import wsauth
from .config import TITLE_ENV_VAR
from .egress import DURATIONS, connect_args, dest_label, refused
from .env import env_token, env_url
from .rest import api_client, request

#: The popup geometry: fixed cells, sized for the 80-column window
#: the consent screens already target; tmux clips it to a smaller
#: client window.
POPUP_COLS = 76
POPUP_ROWS = 12

#: How long the outcome line stays up before ``-E`` closes the
#: popup — an instant close would eat the verdict's confirmation.
OUTCOME_LINGER_S = 1.2

#: The recv idle window the watcher sits in when no frames flow:
#: each wake re-checks that its tmux session still exists, so a
#: closed window retires the watcher within one tick.
LIVENESS_TICK_S = 5.0

#: The popup's quick verdicts: ``a`` allows until restart (the
#: common case), ``d`` denies now — and so does every other key,
#: the fail-fast default a popup left alone must take (the
#: ``--decide`` prompt's rule). The uppercase twins (``A``, ``D``)
#: open the duration chooser for the same verdict.
CHOICES = {
    "a": ("allow", "tilrestart"),
    "d": ("deny", "once"),
}

#: The duration chooser's keys, in :data:`egress.DURATIONS` order.
DURATION_KEYS = {str(i): duration for i, duration in enumerate(DURATIONS, 1)}

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
    only once tmux is there to fill it (#445). Then become the
    tmux client attached to a fresh session (one that ends with
    this window) whose pane runs this module's pane role with the
    appended command. The terminal window itself is whatever the
    operator's prefix opened — this process already runs inside
    it."""
    workspace_id = workspace_from_argv(argv)
    if shutil.which("tmux") is None:
        raise SystemExit("msks-term-popup: tmux is not on PATH")
    title = configured_title(workspace_id)
    if title is not None:
        set_window_title(title)
    os.execvp(
        "tmux",
        session_argv(argv, session_name(workspace_id), workspace_id),
    )
    return 0  # pragma: no cover — execvp replaces the process


def run_pane(argv: list[str]) -> int:
    """The tmux pane's first process (#379): start the consent
    watcher beside the shell, then become the appended command. The
    watcher stays in this pane's process group, so the window's
    teardown SIGHUP retires it with the shell; its own liveness
    check is the backstop. No workspace in the child (or no tmux
    environment — a hand-run pane) runs the command alone."""
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
    """The popup's command (#379): show the held request, take one
    keypress, post the verdict. A post that cannot land (the
    request resolved in another window, the daemon unreachable)
    names its reason from :func:`request`'s one-line contract; the
    outcome lingers so ``-E``'s instant close cannot eat it."""
    workspace_id, rest = take_option("-w", argv)
    request_id, rest = take_option("-r", rest)
    dest, rest = take_option("-d", rest)
    if None in (workspace_id, request_id, dest) or rest:
        raise SystemExit(
            "usage: msks.client.term_popup decide "
            "-w WORKSPACE -r REQUEST -d DESTINATION"
        )
    on = ansi()
    print(
        f"  {span(on, PAINT_LABEL, 'destination:')}"
        f" {span(on, PAINT_FACT, dest)}"
    )
    print(
        f"  {span(on, PAINT_LABEL, 'request:')}"
        f"     {span(on, PAINT_ID, request_id)}"
    )
    decision, duration = read_verdict(on)
    try:
        asyncio.run(post_verdict(workspace_id, request_id, decision, duration))
    except SystemExit as exc:
        print(f"  {span(on, PAINT_DENY, str(exc))}")
        linger()
        return 1
    paint = PAINT_ALLOW if decision == "allow" else PAINT_DENY
    print(f"  {span(on, paint, f'{decision} ({duration})')}")
    linger()
    return 0


async def watch_loop(workspace_id: str, session: str) -> int:
    """The watcher's event loop (#379): register as decider on
    every connection until its tmux session is gone. Never returns
    early on a closed connection — the reconnect ladder keeps the
    decider registered through a daemon restart; the liveness
    check is the only exit."""
    resolved: set[str] = set()
    try:
        url, token = env_url(), env_token()
        async for sock in websockets.connect(**connect_args(url, token)):
            if not await watch_connection(
                sock, workspace_id, session, resolved
            ):
                return 0
    except wsauth.UnusableToken as exc:
        # One line without the token in it — the websocket library's
        # own refusal embeds the credential whole (#116 review).
        raise SystemExit(f"msks: {exc}") from None
    return 0


async def watch_connection(
    sock, workspace_id: str, session: str, resolved: set[str]
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
            await watch_frame(json.loads(raw), workspace_id, session, resolved)
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
    frame: dict, workspace_id: str, session: str, resolved: set[str]
) -> None:
    """One events frame (#379): a fresh request raises the decider
    popup over the shell — the blocking popup queues a second
    request behind the first — and every resolution records its id,
    so a reconnect's re-sent snapshot does not re-raise a request
    this window already decided. A request resolved while its
    popup is up answers at the POST with the daemon's one-line
    reason; a request resolved between its frame and its popup
    still pops (the resolution frame sits unread behind it). A
    refused decider registration — the workspace id names nothing —
    stops the watcher with the log's one line naming the id and the
    daemon's reason."""
    event = frame.get("event")
    data = frame.get("data", {})
    if event == "egress.request":
        row = data["request"]
        if row["id"] not in resolved:
            await asyncio.to_thread(raise_popup, row, workspace_id, session)
    elif event == "egress.resolved":
        resolved.add(data["request_id"])
    elif event == "egress.decider_rejected":
        print(
            f"decider registration refused: {workspace_id}"
            f" ({data.get('reason', 'no reason given')}); stopping",
            flush=True,
        )
        raise SystemExit(1)


def raise_popup(row: dict, workspace_id: str, session: str) -> int:
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
            decide_command(row, workspace_id),
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


def read_verdict(on: bool) -> tuple[str, str]:
    """The popup's keypress exchange: one key takes its quick
    verdict; an uppercase twin reads a second key and takes that
    duration for the same verdict."""
    key = read_key(prompt_text(on))
    verdict = verdict_for(key)
    if verdict is None:
        choice = read_key(duration_text(on))
        return ("allow" if key == "A" else "deny", duration_for(choice))
    return verdict


def read_key(prompt: str) -> str:
    """Read one keypress without waiting for Enter — the popup's
    single-key contract. A stdin that is not a terminal (a test, a
    piped run) falls back to a whole line; EOF reads empty."""
    sys.stdout.write(prompt)
    sys.stdout.flush()
    if not sys.stdin.isatty():
        return sys.stdin.readline().strip()
    fd = sys.stdin.fileno()
    saved = termios.tcgetattr(fd)
    try:
        tty.setcbreak(fd)
        return sys.stdin.read(1)
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, saved)


def prompt_text(on: bool = True) -> str:
    """The popup's key map, one verdict per row: the quick form and
    the duration chooser each sit in their own column. A key
    outside the map still denies now — :func:`verdict_for`'s
    fail-fast rule — but the map no longer says so."""
    return (
        f"  {span(on, PAINT_LABEL, 'allow:')}  "
        f"{key_cell(on, PAINT_KEY, '[a]', 'until restart')}"
        f"{span(on, PAINT_KEY, '[A]')} choose duration\n"
        f"  {span(on, PAINT_LABEL, 'deny:')}   "
        f"{key_cell(on, PAINT_DKEY, '[d]', 'now')}"
        f"{span(on, PAINT_DKEY, '[D]')} choose duration\n"
        f"  {span(on, PAINT_LABEL, '> ')}"
    )


def key_cell(on: bool, code: str, key: str, label: str) -> str:
    """One key-map cell: the painted key, its label, and the pad to
    :data:`KEY_COLUMN` — computed on the plain width, so paint
    never shifts the columns."""
    cell = f"{span(on, code, key)} {label}"
    pad = KEY_COLUMN - len(key) - 1 - len(label)
    return f"{cell}{' ' * max(pad, 1)}"


def duration_text(on: bool = True) -> str:
    """The duration chooser's key map, in :data:`egress.DURATIONS`
    order."""
    keys = "  ".join(
        f"{span(on, PAINT_KEY, f'[{i}]')} {duration}"
        for i, duration in enumerate(DURATIONS, 1)
    )
    return (
        f"  {span(on, PAINT_LABEL, 'duration:')} {keys}\n"
        f"  {span(on, PAINT_LABEL, '> ')}"
    )


def verdict_for(key: str) -> tuple[str, str] | None:
    """One pressed key's ``(decision, duration)``: the lowercase
    quick forms, or None for the uppercase forms (their verdict's
    duration comes from the chooser). Every key outside the map
    denies now — the fail-fast default a popup left alone must
    take."""
    if key in CHOICES:
        return CHOICES[key]
    if key in ("A", "D"):
        return None
    return ("deny", "once")


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


def configured_title(workspace_id: str | None) -> str | None:
    """The window title the operator configured (#445):
    ``MSKSC_TERMINAL_TITLE``'s template with ``{workspace}``
    resolved to the session's name — the workspace's id, or
    ``shell`` for a child naming none. None (the setting unset, or
    blank) leaves the terminal emulator's own title in place."""
    template = os.environ.get(TITLE_ENV_VAR, "")
    if not template.strip():
        return None
    return template.replace("{workspace}", session_name(workspace_id))


def set_window_title(title: str) -> None:
    """Name the terminal window this launcher runs in (#445): one
    OSC 0 sequence, the title-setting escape every terminal that
    runs a command honors. Control characters drop out of the
    title first — a BEL would end the OSC early and an ESC would
    start a live sequence. Written before the tmux client takes
    the screen — the launch's own server pins ``set-titles`` off
    (:func:`session_argv`), so the title stays for the window's
    lifetime — and a stdout that is not a terminal (a piped
    hand-run) stays clean."""
    if not sys.stdout.isatty():
        return
    clean = "".join(c for c in title if c >= " " and c != "\x7f")
    sys.stdout.write(f"\x1b]0;{clean}\x07")
    sys.stdout.flush()


def session_argv(
    child: list[str], session: str, workspace_id: str | None
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
    title to tmux the moment the client attaches. Everything
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


def decide_command(row: dict, workspace_id: str) -> str:
    """The one-line command the popup runs: this module's decide
    role with the request's id and destination — everything the
    prompt shows, so the popup needs no websocket of its own."""
    return shlex.join(
        [
            sys.executable,
            "-m",
            MODULE,
            "decide",
            "-w",
            workspace_id,
            "-r",
            row["id"],
            "-d",
            dest_label(row),
        ]
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


def linger() -> None:
    """Hold the outcome line up for the read it needs before the
    popup closes."""
    time.sleep(OUTCOME_LINGER_S)


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
