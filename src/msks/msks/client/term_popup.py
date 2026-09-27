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
    names a fresh session after the workspace on a dedicated
    socket (a server of its own, so the pane inherits this
    process's environment — an operator's already-running tmux
    server would otherwise substitute its own — and the session
    stays out of their window list), and becomes the attached
    tmux client (``tmux -L <socket> new-session ... pane ...``).
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
from .egress import connect_args, dest_label, refused
from .rest import api_client, env_token, env_url, request

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

#: One popup keypress's verdict: the five allow durations, ``n``
#: for deny. Enter, an unknown key, EOF — anything else — denies
#: now: a popup left alone fails the held connection fast instead
#: of waiting out the timeout (the ``--decide`` prompt's rule).
CHOICES = {
    "1": ("allow", "once"),
    "2": ("allow", "5m"),
    "3": ("allow", "15m"),
    "4": ("allow", "tilrestart"),
    "5": ("allow", "forever"),
    "n": ("deny", "once"),
}

#: The modules this package reaches itself through: the pane, the
#: watcher, and the popup's command all re-invoke this module by
#: name with the interpreter that is already running it.
MODULE = "msks.client.term_popup"

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
    before a window could open half-way; otherwise become the tmux
    client attached to a fresh session (one that ends with this
    window) whose pane runs this module's pane role with the
    appended command. The terminal window itself is whatever the
    operator's prefix opened — this process already runs inside
    it."""
    if shutil.which("tmux") is None:
        raise SystemExit("msks-term-popup: tmux is not on PATH")
    workspace_id = workspace_from_argv(argv)
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
    print(f"  destination: {dest}")
    print(f"  request:     {request_id}")
    key = read_key(prompt_text())
    decision, duration = verdict_for(key)
    try:
        asyncio.run(post_verdict(workspace_id, request_id, decision, duration))
    except SystemExit as exc:
        print(f"  {exc}")
        linger()
        return 1
    print(f"  {decision} ({duration})")
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
    stops the watcher with the log's one line."""
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
            f"decider registration refused: {data.get('workspace', '')}"
            " — no such workspace; stopping",
            flush=True,
        )
        raise SystemExit(1)


def raise_popup(row: dict, workspace_id: str, session: str) -> int:
    """Open the decider overlay over the session's window: one
    tmux client (the terminal window's own client) gets the popup,
    and it closes itself when the decide role exits (``-E``). No
    attached client — the window closed between the frame and here
    — reports its nonzero returncode; the caller moves on."""
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


def prompt_text() -> str:
    """The popup's key map, in the order the rows read."""
    return (
        "  allow for:  1 once   2 5m   3 15m   4 until restart"
        "   5 forever\n"
        "  deny now:   n (or any other key)\n"
        "  > "
    )


def verdict_for(key: str) -> tuple[str, str]:
    """One pressed key's ``(decision, duration)``: the five allow
    durations, ``n`` denies — and so does everything else, the
    fail-fast default a popup left alone must take."""
    return CHOICES.get(key, ("deny", "once"))


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
    client)."""
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
    return [
        "tmux",
        "-L",
        socket_name(workspace_id),
        "new-session",
        "-s",
        session,
        shlex.join(pane),
        ";",
        "set-option",
        "destroy-unattached",
        "on",
    ]


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
    server keeps the environment of whoever started it."""
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
