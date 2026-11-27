"""The client half of the bridge (#1102): hold a workspace's
forward proxies, carrying their connections into the guest.

The daemon owns the rows and their listeners; this process — the
holder — serves every connection they accept. Each one becomes an
ssh ``-W 127.0.0.1:<port>`` channel over the forward websocket
(``ProxyCommand msks forward <ws> 22``), the same transport
``msks ssh`` rides, signed with the workspace's own identity (the
operator's agent when it holds the key, else the configured
identity file — the ``session_identity`` ladder). The private half
stays in this process or the operator's agent; the daemon holds
no key material anywhere. The guest's host key pins in the same
per-workspace cache a session pins in, rotation suffix and all
(#1158): a renewed seed re-keys the guest, and the channel must
read the directory the session re-pinned, not the first boot's.

``msks start`` leaves a holder behind when the workspace has
forward rows, so the proxies serve after the command exits; the
holder exits when its websocket ends (the workspace stopped, the
daemon went away). ``msks proxy hold`` runs one on demand.

The re-hold watch (#1127) lives here too: an open client — the
TUI or the GUI — restarts a holder whose failure named the
missing one, through :class:`ReHolder`, the one policy both
surfaces share.
"""

import asyncio
import contextlib
import json
import signal
import sys
import time
from urllib.parse import quote

import websockets

from ..spec.owners import AMBIGUOUS_CLOSE
from ..spec.proxy import CLOSE, OPEN, pack_frame, unpack_frame
from . import wsdial
from .env import env_token, env_url, is_unix, ssl_context
from .forwarding import config_quote
from .hold import holder_due, spawn_holder  # noqa: F401  (spawn re-export)
from .identity import cache_key, known_hosts_path
from .rest import fetch_ssh_key
from .screenshot import pipe_from_env
from .ssh import proxy_command, session_identity, workspace_user
from .wsdial import ws_url

#: One ssh channel read's byte budget, the same chunk the daemon
#: side moves.
READ_CHUNK = 4096

#: The ssh binary the holder drives — the same one ``msks ssh``
#: shells out to.
SSH = "ssh"

CLOSE_CODE_REASONS = {
    4401: "bridge refused (bad or missing token)",
    4404: "no such workspace",
    AMBIGUOUS_CLOSE: "ambiguous workspace name",
    4501: "bridge unavailable (no NIC)",
}


def bridge_ws_url(base_url: str, workspace_id: str) -> str:
    """The bridge websocket's URL."""
    return ws_url(
        base_url,
        f"/api/v1/workspaces/{quote(workspace_id, safe='')}/bridge",
    )


#: The clean ends: the workspace stopped (the detach hook's
#: close) or the server is going away. A holder exits quietly on
#: these alone — every other end names itself (#1102 review).
CLEAN_CLOSES = (1000, 1001)


def close_code_message(closed: websockets.ConnectionClosed) -> str | None:
    """The one-line failure for how the daemon's websocket ended:
    None for a clean end, the named refusal for a mapped close
    code, and an unexpected-end line for everything else (a
    dropped connection, an unmapped code) — a repair path that
    answers success to those lies."""
    rcvd = closed.rcvd
    if rcvd is None:
        return unexpected_end(1006)
    if rcvd.code in CLEAN_CLOSES:
        return None
    return named_refusal(rcvd.code, rcvd.reason) or unexpected_end(rcvd.code)


def named_refusal(code: int, reason: str) -> str | None:
    """The one-line failure for a mapped refusal close code."""
    if code not in CLOSE_CODE_REASONS:
        return None
    detail = f": {reason.strip()}" if reason.strip() else ""
    return f"msks: {CLOSE_CODE_REASONS[code]}{detail}"


def unexpected_end(code: int) -> str:
    """The line an abnormal bridge end answers with — a repair
    path that exits quietly here lies (#1102 review)."""
    return (
        "msks: the bridge ended unexpectedly (close code "
        f"{code}) — 'msks proxy hold <workspace>' retries"
    )


def channel_argv(
    workspace_id: str,
    agent: str,
    identity: str,
    user: str,
    port: int,
    known_hosts: str,
    host: str = "127.0.0.1",
) -> list[str]:
    """One relayed connection's ssh argv: a ``-W`` channel to the
    guest's own loopback port, over the forward websocket, as the
    workspace's login user, signed through the same agent the
    session identity resolved."""
    return [
        SSH,
        "-o",
        proxy_command(workspace_id),
        "-o",
        f"UserKnownHostsFile={config_quote(known_hosts)}",
        "-o",
        "StrictHostKeyChecking=accept-new",
        "-o",
        "IdentitiesOnly=yes",
        "-o",
        f"IdentityAgent={config_quote(agent)}",
        "-i",
        identity,
        "-l",
        user,
        "-W",
        f"{host}:{port}",
        workspace_id,
    ]


def parse_control(message: str) -> tuple[str, int, int | None, str | None]:
    """(kind, cid, port, host) of one control text frame — the
    port and host ride an ``open`` alone — raising for a frame
    this side does not speak. A host beyond the default resolves
    inside the workspace."""
    body = json.loads(message)
    kind, cid = body["t"], int(body["cid"])
    if kind != OPEN:
        return kind, cid, None, None
    port = int(body["port"])
    host = body.get("host") or None
    return kind, cid, port, host


class Holder:
    """The serving loop's state: the websocket, one ssh channel
    per connection id, the workspace's login user, and the
    known_hosts file its channels pin the guest's host key under.
    The spawn seam lets the tests drive a fake ssh."""

    def __init__(
        self, socket, workspace_id: str, user: str, known_hosts: str
    ) -> None:
        self.socket = socket
        self.workspace_id = workspace_id
        self.user = user
        self.known_hosts = known_hosts
        self.channels: dict[int, object] = {}
        self.spawner = asyncio.create_subprocess_exec

    async def open(
        self,
        cid: int,
        port: int,
        agent: str,
        ident: str,
        host: str = "127.0.0.1",
    ):
        """Serve one relayed connection: an ssh channel piped to
        the websocket both ways, until either end closes it. The
        far end is ``host:port`` inside the workspace — the
        loopback by default, or the name the row carries, which
        the guest's sshd resolves."""
        argv = channel_argv(
            self.workspace_id,
            agent,
            ident,
            self.user,
            port,
            self.known_hosts,
            host,
        )
        process = await self.spawner(
            *argv,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        pump = asyncio.create_task(self.pump_out(cid, process))
        self.channels[cid] = (process, pump)

    async def pump_out(self, cid: int, process) -> None:
        """The ssh channel's stdout to the websocket until it
        ends; the connection closes with it."""
        while True:
            data = await process.stdout.read(READ_CHUNK)
            if not data:
                break
            await self.socket.send(pack_frame(cid, data))
        await self.socket.send(json.dumps({"t": CLOSE, "cid": cid}))
        await process.wait()
        if process.returncode != 0:
            print(
                f"msks: bridge channel {cid} ended"
                f" (ssh exited {process.returncode})",
                file=sys.stderr,
            )
        self.channels.pop(cid, None)

    async def feed(self, data: bytes) -> None:
        """One binary frame from the daemon to its ssh channel's
        stdin; a frame this side cannot place is dropped (the
        channel's own end closes the connection)."""
        try:
            cid, payload = unpack_frame(data)
        except ValueError:
            return
        held = self.channels.get(cid)
        if held is None or held[0].stdin is None:
            return
        with contextlib.suppress(BrokenPipeError, OSError):
            held[0].stdin.write(payload)
            await held[0].stdin.drain()

    def close(self, cid: int) -> None:
        """One relayed connection ended: the ssh channel stops."""
        held = self.channels.pop(cid, None)
        if held is None:
            return
        process, pump = held
        with contextlib.suppress(ProcessLookupError):
            process.terminate()
        pump.cancel()

    async def sweep(self) -> None:
        """End every ssh channel (the websocket's end)."""
        for cid in list(self.channels):
            self.close(cid)


async def dispatch_control(
    holder: Holder, message: str, agent: str, ident: str
) -> None:
    """One control frame's effect: an ``open`` starts an ssh
    channel, a ``close`` ends one; a frame this side does not
    speak acts on nothing."""
    try:
        kind, cid, port, host = parse_control(message)
    except ValueError, KeyError, TypeError:
        return  # a frame this side does not speak acts on nothing
    if kind == OPEN and port is not None:
        await guarded_open(holder, cid, port, agent, ident, host)
    elif kind == CLOSE:
        holder.close(cid)


async def guarded_open(
    holder: Holder,
    cid: int,
    port: int,
    agent: str,
    ident: str,
    host: str | None = None,
) -> None:
    """One ``open`` with its spawn failure isolated: the channel
    that cannot start answers its own close, and the holder keeps
    serving the rest."""
    try:
        await holder.open(cid, port, agent, ident, host or "127.0.0.1")
    except OSError as exc:
        print(
            f"msks: bridge channel {cid} did not start ({exc})",
            file=sys.stderr,
        )
        await holder.socket.send(json.dumps({"t": CLOSE, "cid": cid}))


async def serve(socket, holder: Holder, agent: str, ident: str) -> None:
    """Answer the daemon's frames until the websocket ends: a
    text frame dispatches, a binary frame feeds its channel."""
    while True:
        message = await socket.recv()
        if isinstance(message, str):
            await dispatch_control(holder, message, agent, ident)
            continue
        await holder.feed(message)


async def hold(
    workspace_id: str, url: str, token: str, ssl_ctx, key: dict, holder
) -> None:
    """The holder's session: connect the bridge websocket and
    serve it until it ends, naming the daemon's refusals."""
    address = bridge_ws_url(url, workspace_id)
    connection = wsdial.dial(address, token, ssl_ctx, max_size=2**22)
    async with await connection as socket:
        # The session's own cache key (#697, #803): the row's disk
        # and seed epochs rotate the directory the guest's fresh
        # host keys re-pin under (#1158).
        cache_ref, instance = cache_key(key, workspace_id)
        user = workspace_user(key)
        hosts = known_hosts_path(cache_ref, instance=instance)
        server = Holder(socket, workspace_id, user, hosts)
        # The pipe rides the holder (#1194): the same identity and
        # the same known_hosts its channels pin under, serving the
        # guest's capture listener while this websocket lives.
        pipe = pipe_from_env(
            workspace_id,
            holder.server_address,
            holder.identity_path,
            user,
            hosts,
            key.get("name"),
        )
        try:
            await serve(
                socket, server, holder.server_address, holder.identity_path
            )
        except websockets.ConnectionClosed as closed:
            message = close_code_message(closed)
            if message is not None:
                raise SystemExit(message) from None
        finally:
            await server.sweep()
            if pipe is not None:
                pipe.stop()


def run_hold(workspace_id: str, transport=None) -> int:
    """Hold the workspace's bridge from this client: resolve the
    identity exactly as a session would, serve the rows'
    connections until the websocket ends. A stop signal — SIGTERM,
    or the terminal a manual holder runs in closing — answers
    through the loop's own teardown: the cancellation runs
    hold()'s finally blocks, the bridge sweeps its channels, and
    the screenshot pipe stops with its forward child (#1194),
    instead of orphaning them on a killed holder."""
    token = env_token()
    url = env_url()
    ssl_ctx = None if is_unix(url) else ssl_context()
    key = asyncio.run(
        fetch_ssh_key(url, token, workspace_id, transport=transport)
    )
    with session_identity(key, workspace_id) as resolved:
        with holder_sigterm():
            asyncio.run(hold(workspace_id, url, token, ssl_ctx, key, resolved))
    return 0


def terminating_exit(signum, frame) -> None:
    """The SIGTERM answer inside a holder: SystemExit, so the event
    loop's teardown unwinds the session instead of killing the
    process where it stands."""
    raise SystemExit(128 + signum)


@contextlib.contextmanager
def holder_sigterm():
    """One holder's stop signals, restored when the session leaves:
    SIGTERM (a kill, a shell's job control) and SIGHUP (the
    terminal a manual holder runs in closing) both answer through
    the loop's teardown, and the shell's own settings are not the
    holder's to keep."""
    previous = {
        signum: signal.signal(signum, terminating_exit)
        for signum in (signal.SIGTERM, signal.SIGHUP)
    }
    try:
        yield
    finally:
        for signum, handler in previous.items():
            signal.signal(signum, handler)


#: The stable opening of the cause a failed forward row carries
#: while no holder serves it — the daemon's own line, matched by
#: its opening so this side needs no daemon import (the client
#: isolation rule keeps the daemon's bridge module out of here).
NO_HOLDER_CAUSE = "no client holds the bridge"


def rehold_due(rows: list[dict]) -> bool:
    """Whether a refreshed rows read still owes the workspace a
    holder (#1127): the start hook's own gate
    (:func:`msks.client.hold.holder_due` — forward rows, none
    of them reading ``listening``, #1102's no-standby rule),
    narrowed to the cause a holder can serve: a failed forward
    row whose reason names the missing holder."""
    return holder_due(rows) and any(row_lost_holder(row) for row in rows)


def row_lost_holder(row: dict) -> bool:
    """Whether one forward row reads failed with the
    missing-holder cause — a bind refusal reads failed with
    another cause, and a holder cannot serve a port the host
    never bound."""
    return row.get("status") == "failed" and str(
        row.get("reason") or ""
    ).startswith(NO_HOLDER_CAUSE)


def frame_id(data: dict) -> str | None:
    """The workspace one frame's data names, when it names
    one."""
    return str(data.get("id")) if data.get("id") is not None else None


def holder_lost(frame: dict) -> str | None:
    """The workspace one events frame names as bridge-lost
    (#1127): a ``proxy.failed`` whose cause is the missing
    holder — the holder died, or the start left none. None for
    every other frame (a bind's own failure carries another
    cause, and another remedy)."""
    if frame.get("event") != "proxy.failed":
        return None
    return lost_with_cause(frame.get("data") or {})


def lost_with_cause(data: dict) -> str | None:
    """The named half of :func:`holder_lost`: the cause matches
    the missing-holder line, and the workspace it names is the
    answer."""
    if not str(data.get("reason") or "").startswith(NO_HOLDER_CAUSE):
        return None
    return frame_id(data)


def became_running(frame: dict) -> str | None:
    """The workspace one events frame names as newly running
    (#1127): the watcher's ``workspace.status`` transition. A
    start that left no holder (a raw API call, a client without
    the key) shows here first — the rows it left read failed
    with the bridge cause."""
    data = frame.get("data") or {}
    if frame.get("event") != "workspace.status":
        return None
    if data.get("status") != "running":
        return None
    return frame_id(data)


def bridge_serving(frame: dict) -> str | None:
    """The workspace one events frame names as bridge-served
    (#1127): a ``proxy.listening`` transition — the hub's own
    up-publish among the binds' own. The watch reads it as its
    spawn's outcome alone (:meth:`ReHolder.serving`). The binds'
    own frames are safe only because the attach publishes them
    before the ``running`` word: a bind landing after it would
    resolve a boot's deferred signal (:meth:`ReHolder.note_boot`)
    the way a holder would."""
    if frame.get("event") != "proxy.listening":
        return None
    return frame_id(frame.get("data") or {})


#: One bridge-failure signal's ruling (:meth:`ReHolder.signal`):
#: the surfaces follow WEIGH with a rows read, and GAVE_UP names
#: the spent budget.
WEIGH = "weigh"
GAVE_UP = "gave up"


class ReHolder:
    """The session's bridge re-hold policy (#1127): an open
    client — the TUI or the GUI — restarts a workspace's holder
    when its failure names the missing one, so the operator with
    a client open never reads that cause and runs the hold
    command by hand.

    One instance per client session; the shared data seam
    (:class:`~msks.client.data.RestData`) owns it, the start
    path marks it (:meth:`note_start`), and a boot another
    client makes opens the same window (:meth:`note_boot`). The
    surfaces feed it the frames their subscriptions land and the
    rows their reads serve, and poll :meth:`take_due` on their
    own cadence; it owns the rest — when to retry, how often,
    and when to give the workspace up to the row's own cause for
    the session.
    The spawn is the same detached child ``msks start`` leaves
    (:func:`spawn_holder`); no key material moves anywhere
    new.
    """

    #: Spawns per workspace per session: the failure that lands
    #: after the last one ends the watch — the row's cause
    #: stands, one line says the watch gave up, and a
    #: crash-looping holder cannot spin the client past it.
    MAX_TRIES = 3

    #: Seconds between handled signals for one workspace: the
    #: burst one failure publishes per row lands as one, and a
    #: holder that dies as fast as it connects retries at this
    #: pace, not at the frame rate.
    RETRY_AFTER_S = 30.0

    #: Seconds a spawn's outcome stays awaited: a listener that
    #: lands inside the window is the spawn's recovery, and the
    #: surfaces say so once; one that lands later — an
    #: operator's manual hold among them — is not this watch's
    #: work. The clock is monotonic, so a suspend never expires
    #: it early.
    AWAIT_S = 30.0

    def __init__(self, spawn=None, clock=time.monotonic) -> None:
        self.spawn = spawn or spawn_holder
        self.clock = clock
        #: Spawns spent per workspace this session.
        self.spent: dict[str, int] = {}
        #: The clock each workspace's backoff window closes at — a
        #: signal inside it defers (:attr:`pending`), and one
        #: past it weighs.
        self.window_until: dict[str, float] = {}
        #: The clock at each spawn — the outcome window.
        self.awaited: dict[str, float] = {}
        #: The workspaces holding a deferred signal: the failure
        #: landed inside the window (or over a spawn whose fate
        #: was still unknown), and :meth:`take_due` re-rules it
        #: once the window closes. Failure frames are
        #: edge-triggered — nothing periodic re-delivers them —
        #: so the deferral is a mark, not a drop.
        self.pending: set[str] = set()
        #: The workspaces whose give-up line was said.
        self.said: set[str] = set()

    def note_start(self, workspace_id: str) -> None:
        """A holder the start path itself spawned (#1102): the
        running transition the boot publishes must not stack a
        second child beside it, so the start's own spawn takes
        the window a signal would. The start marks before its
        POST flies (the daemon publishes ``running`` before the
        response), and a start that fails leaves the window to
        lapse — the rows read a due signal takes decides
        whether a holder is owed at all."""
        self.open_window(workspace_id)

    def note_spawn(self, workspace_id: str) -> None:
        """A holder spawned outside the watch (the start path's
        own, #1102): the outcome window opens with it — the
        listener that follows is that spawn's, and a failure
        under it defers exactly as the watch's own would."""
        self.awaited[workspace_id] = self.clock()

    def note_boot(self, workspace_id: str) -> None:
        """A boot this client did not make (#1198): the daemon's
        ``running`` transition opens the backoff window, the one
        :meth:`note_start` opens before its POST flies. The booting
        client's start path — a session child's ``hold_if_due``,
        the CLI start's hook — spawns its holder after that frame,
        and the holder registers inside the window: its listener
        resolves the deferred signal (:meth:`serving`), and a
        signal the window outlives is read at the close
        (:meth:`take_due`), when a listening row owes nothing. A
        watch that read the rows at the frame stacked a standby
        beside the holder on its way. A start that left no holder
        (a raw API call) is re-held one window late."""
        self.open_window(workspace_id)

    def open_window(self, workspace_id: str) -> None:
        """Start the backoff window now."""
        self.window_until[workspace_id] = self.clock() + self.RETRY_AFTER_S

    def signal(self, workspace_id: str) -> str | None:
        """One bridge-failure signal's ruling (#1127): WEIGH —
        follow with a rows read and :meth:`rehold`; GAVE_UP —
        the session's budget is spent, said once, and the
        surfaces flash the line; None — deferred or collapsed.
        A signal inside the backoff window defers (the burst one
        failure publishes per row lands as one), and so does one
        over a spawn whose outcome window still stands (the
        spawn's own holder may be connecting; its listener, or
        the window's lapse at :meth:`take_due`, resolves the
        deferral) — a deferred signal is re-ruled when the
        window closes, never dropped."""
        return self.defer_or_rule(workspace_id)

    def defer_or_rule(self, workspace_id: str) -> str | None:
        """The ruling half of :meth:`signal`, shared with
        :meth:`take_due`: the budget check comes last, after
        the deferrals, so a failure landing while the last
        spawn's holder is still connecting neither spends a
        give-up line early nor weighs a second child beside
        it."""
        if self.deferred(workspace_id):
            self.pending.add(workspace_id)
            return None
        return self.rule(workspace_id)

    def deferred(self, workspace_id: str) -> bool:
        """Whether a signal for this workspace waits: inside the
        backoff window, or over a spawn whose outcome window
        still stands."""
        until = self.window_until.get(workspace_id)
        if until is not None and self.clock() < until:
            return True
        return self.outcome_stands(workspace_id)

    def outcome_stands(self, workspace_id: str) -> bool:
        """Whether a spawn's outcome window still stands — its
        holder's fate is unknown, and a listener or the window's
        lapse resolves it."""
        at = self.awaited.get(workspace_id)
        return at is not None and (self.clock() - at) <= self.AWAIT_S

    def rule(self, workspace_id: str) -> str | None:
        """The fresh ruling: the give-up line once past the
        budget, else a weigh over a newly opened window. Either
        arm closes the standing outcome window — the spawn it
        watched is superseded (a weigh spawns a fresh one, and
        the give-up line names the row's cause instead)."""
        if self.spent.get(workspace_id, 0) >= self.MAX_TRIES:
            if workspace_id in self.said:
                return None
            self.said.add(workspace_id)
            self.awaited.pop(workspace_id, None)
            return GAVE_UP
        self.awaited.pop(workspace_id, None)
        self.open_window(workspace_id)
        return WEIGH

    def take_due(self) -> list[tuple[str, str]]:
        """The deferred signals whose windows closed, taken with
        their rulings (#1127): the surfaces poll this on their
        own cadence — the failure frames are edge-triggered, and
        nothing else re-delivers a deferred signal. Each taken
        signal re-rules exactly as a fresh one would: a weigh,
        or the give-up line the spent budget owes."""
        now = self.clock()
        due = []
        for workspace_id in sorted(self.pending):
            if self.signal_due(workspace_id, now):
                self.pending.discard(workspace_id)
                due.append((workspace_id, self.rule(workspace_id)))
        return due

    def signal_due(self, workspace_id: str, now: float) -> bool:
        """Whether one deferred signal's windows closed: the
        backoff elapsed, and the spawn it stood over resolved or
        lapsed with it."""
        until = self.window_until.get(workspace_id)
        if until is not None and now < until:
            return False
        at = self.awaited.get(workspace_id)
        return at is None or (now - at) > self.AWAIT_S

    def serving(self, workspace_id: str) -> bool:
        """A listener came up: whether it answers a spawn this
        watch made inside the outcome window — the caller says
        the recovery once. The take consumes the window and any
        deferred signal with it: the bridge stands, and the
        failure those frames carried is resolved. A listener
        past the window (an operator's manual hold) answers
        False."""
        at = self.awaited.pop(workspace_id, None)
        self.pending.discard(workspace_id)
        return at is not None and (self.clock() - at) <= self.AWAIT_S

    def rehold(self, workspace_id: str, rows: list[dict]) -> bool:
        """The refreshed rows read's ruling (#1127): rows that
        still owe the workspace a holder take one spawn, the
        same detached child ``msks start`` leaves. The attempt
        spends the budget; rows that read listening (a holder
        stands, #1102's no-standby rule) or failed with another
        cause (a bind refusal) spawn nothing and spend nothing.
        A spawn whose outcome window still stands answers False
        too: its holder's fate is unknown, and a second child
        beside it is the standby the rule forbids — a slow rows
        read racing a second signal lands here, on either
        surface."""
        if self.spent.get(workspace_id, 0) >= self.MAX_TRIES:
            return False
        if self.outcome_stands(workspace_id):
            return False
        if not rehold_due(rows):
            return False
        self.spent[workspace_id] = self.spent.get(workspace_id, 0) + 1
        if not self.spawn(workspace_id):
            return False
        self.awaited[workspace_id] = self.clock()
        return True
