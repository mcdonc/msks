"""The console websocket (#21, #63): the interactive shell
bridge, its identity negotiation, the #123 refusal scan, and the
#103 echo watchdog."""

import asyncio
import contextlib
import re

from fastapi import APIRouter, WebSocket

from ...identity import LOGIN_NAME_RE
from ...imagestore import images_dir, load_record
from ...microvm.errors import MicrovmError
from .deps import authed_accept
from .streams import close_reason, pump_streams

#: The console protocol the daemon negotiates (#63); manifest-keyed.
CONSOLE_PROTOCOL_PRELUDE = "prelude-v1"
#: The wire charset for a console user name — the same shape the
#: guest helper's prelude accepts, checked before anything is
#: forwarded. The login name's charset is the same one (#248's
#: create field, the seed's interpolation guard): one pattern, the
#: helper's own rule.
USER_NAME_RE = LOGIN_NAME_RE
#: The wire charset for a TERM value: printable ASCII minus space
#: (every terminfo name fits), matching the guest helper's check.
TERM_RE = re.compile(r"^[!-~]{1,32}$")


def console_request(params) -> tuple[str, int, int, str, str | None]:
    """The console websocket's user/rows/cols/term, or the refusal
    reason.

    The daemon validates what it can before opening the vsock stream:
    free-form strings never reach the guest-side parser, and the
    window size is bounded to what a pty can carry.
    """
    user = params.get("user", "root")
    if not USER_NAME_RE.fullmatch(user):
        return user, 24, 80, "xterm", f"invalid console user {user!r}"
    term = params.get("term", "xterm")
    if not TERM_RE.fullmatch(term):
        return user, 24, 80, "xterm", f"invalid console term {term!r}"
    rows, cols, problem = console_dimensions(params)
    return user, rows, cols, term, problem


def console_dimensions(params) -> tuple[int, int, str | None]:
    """rows/cols from the query string, or the refusal reason."""
    rows = console_dimension("rows", params.get("rows"), 24)
    cols = console_dimension("cols", params.get("cols"), 80)
    for value in (rows, cols):
        if isinstance(value, str):
            return 24, 80, value
    return rows, cols, None


def console_dimension(name: str, raw: str | None, default: int) -> int | str:
    """One window dimension: absent means the default, anything else
    must be an integer within a pty's range."""
    if raw is None:
        return default
    try:
        value = int(raw)
    except ValueError:
        return f"{name} must be an integer, got {raw!r}"
    if not 1 <= value <= 65535:
        return f"{name}={value} out of range"
    return value


def console_image_policy(
    app, row: dict
) -> tuple[str, tuple[str, ...], str | None]:
    """The workspace image's console protocol, served users, and the
    refusal reason when the record cannot be read.

    A workspace bound to a catalog image gets that image's markers;
    anything booted from explicit artifacts is legacy (root only) —
    the raw root shell those images serve is exactly today's
    behavior. A bound image whose record is unreadable (corrupt
    image.json, out-of-band deletion) is refused loudly: piping a raw
    stream at a prelude guest yields an opaque refusal, and silently
    treating it as legacy would mask the corruption.
    """
    state_dir = app.state.settings.vmm.state_dir
    image_hash = row.get("image_hash")
    if not image_hash:
        return "legacy", ("root",), None
    cache = images_dir(state_dir) / image_hash
    if not (cache / "image.json").is_file():
        return (
            "legacy",
            ("root",),
            f"image record unreadable: {image_hash[:12]}",
        )
    record = load_record(cache)
    if record is None:
        return (
            "legacy",
            ("root",),
            f"image record unreadable: {image_hash[:12]}",
        )
    return record.console_protocol, record.console_users, None


def scan_guest_protocol(
    tail: bytes, at_start: bool
) -> tuple[bool, str | None]:
    """(auth_ok, refusal_text) for the buffered guest output.

    The helper's protocol lines read at a line start; ``at_start``
    is true only while the buffer still holds the stream's first
    bytes (a slid tail makes every buffer start mid-stream). AUTH
    OK wins over a refusal in the same buffer: authenticated output
    is not a refusal even inside one read.
    """
    anchor = rb"(?:\A|\n)" if at_start else rb"\n"
    if re.search(anchor + rb"AUTH OK", tail):
        return True, None
    found = re.search(anchor + rb"(MSKS ERR[^\n]*)", tail)
    if found is not None:
        return False, found.group(1).decode(errors="replace").strip()
    return False, None


class RefusalScan:
    """The #123 refusal line in guest output (``MSKS ERR ...``).

    A guest whose console helper rejects the session says so with
    one line and exits; without this scan the refusal reaches the
    client as transport death — the stream EOF ends the bridge
    with no websocket close at all (#217).

    The scan watches ALL guest output until the helper's ``AUTH
    OK`` line (the refusal can trail echoed input and split reads,
    so it cannot simply watch the first bytes) and then stands
    down — a logged or printed ``MSKS ERR`` line in post-auth
    shell output must not read as a refusal. The ``^`` anchor is
    trusted only while the stream's first bytes are still in the
    tail: once the tail slides, a mid-stream chunk boundary is
    indistinguishable from a line start. Matched, the bridge
    closes the websocket with
    :data:`CONSOLE_AUTH_REFUSED_CLOSE_CODE` and the guest's own
    text as the reason.
    """

    def __init__(self) -> None:
        self.text: str | None = None
        self.matched = asyncio.Event()
        self._tail = b""
        self._at_start = True
        self._armed = True

    def _append(self, data: bytes) -> None:
        """Grow the bounded tail; note when the stream's start slid
        out of it (the buffer start stops meaning a line start)."""
        self._tail = (self._tail + data)[-256:]
        if len(self._tail) < len(data):
            self._at_start = False

    def feed(self, data: bytes) -> None:
        """One relayed guest chunk; records the refusal once."""
        if self.text is not None or not self._armed:
            return
        self._append(data)
        auth_ok, refusal = scan_guest_protocol(self._tail, self._at_start)
        if auth_ok:
            # Authenticated: shell output from here on, not protocol.
            self._armed = False
        elif refusal is not None:
            self.text = refusal
            self.matched.set()


#: Close code for a console the guest refused (#123, #217): the
#: helper's ``MSKS ERR`` line names the reason in the close frame,
#: so a client can tell refusal apart from transport death. The
#: forward endpoint carries its own, unrelated 4403 ("not
#: permitted") in a separate table — the two must not merge.
CONSOLE_AUTH_REFUSED_CLOSE_CODE = 4403


async def bridge_console(
    socket: WebSocket, reader, writer, stall_timeout_s: float = 60.0
) -> None:
    """The console's pump: :func:`pump_streams` plus the echo
    watchdog.

    The guest pty echoes every input byte, so client input that draws
    zero guest bytes for ``stall_timeout_s`` names a wedged stream —
    the bridge closes the websocket with 4502 instead of hanging open
    and silent. An idle session (no input in flight) never trips it,
    and ``stall_timeout_s <= 0`` switches the watchdog off.

    Every ending is protocol-clean (#217): a refused session closes
    with 4403 and the guest's refusal text, a guest stream that ends
    (the helper exits, the shell logs out) closes with 1000, and only
    a client disconnect ends without a close frame — the client is
    gone; there is nothing to tell.
    """
    clock = StallClock()
    refusal = RefusalScan()

    def on_output(data: bytes) -> None:
        clock.disarm()
        refusal.feed(data)

    watchdog = asyncio.create_task(echo_watchdog(socket, clock))
    pump = asyncio.create_task(
        pump_streams(
            socket,
            reader,
            writer,
            on_input=lambda: clock.arm(stall_timeout_s),
            on_output=on_output,
        )
    )
    refused = asyncio.create_task(refusal.matched.wait())
    done, pending = await asyncio.wait(
        {pump, watchdog, refused}, return_when=asyncio.FIRST_COMPLETED
    )
    ended = None
    if pump in done:
        with contextlib.suppress(Exception):
            ended = pump.result()
    for task in pending:
        task.cancel()
    for task in pending:
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await task
    for task in done:
        with contextlib.suppress(Exception):
            task.result()
    await close_console_session(socket, ended, refusal)


async def close_console_session(socket, ended: str | None, refusal) -> None:
    """The bridge's protocol-clean ending (#217).

    Refusal first: a matched refusal closes with
    :data:`CONSOLE_AUTH_REFUSED_CLOSE_CODE` and the guest's text —
    even if the watchdog also fired. Then a guest stream that ended
    (shell exit, helper shutdown) closes with 1000. A client
    disconnect (``ended == "socket"``) and a watchdog close
    (``ended is None``, the watchdog having already closed 4502)
    attempt nothing. The suppress covers the races where the close
    already happened or the client vanished mid-close.
    """
    if refusal.text is not None:
        with contextlib.suppress(Exception):
            await socket.close(
                code=CONSOLE_AUTH_REFUSED_CLOSE_CODE,
                reason=close_reason(refusal.text),
            )
    elif ended == "stream":
        with contextlib.suppress(Exception):
            await socket.close(
                code=1000, reason=close_reason("console closed")
            )


class StallClock:
    """The echo-deadline state (#103): armed by client input, cleared
    by any guest byte.

    A plain :class:`asyncio.Event` cannot carry the deadline: every
    input's ``set()`` resolves the watchdog's pending wait, and a
    later ``clear()`` from the guest-output task cannot cancel a
    timeout that ``wait_for`` already armed — the session would
    close one window after the last keystroke, echo or no echo.
    The clock holds the deadline itself; the watchdog sleeps until
    it and re-reads the state on every wake.
    """

    def __init__(self) -> None:
        self.deadline: float | None = None
        self.changed = asyncio.Event()

    def arm(self, timeout_s: float) -> None:
        """Input left the daemon: the guest has this long to answer.

        Armed only while no deadline is pending: the window runs from
        the FIRST unanswered input, not the last keystroke — a client
        that keeps sending into a wedged stream (the smoke probes'
        resend loop, a script pasting into a dead console) must not
        push its own close out of reach (#103).
        """
        if timeout_s > 0 and self.deadline is None:
            self.deadline = asyncio.get_running_loop().time() + timeout_s
            self.changed.set()

    def disarm(self) -> None:
        """Any guest byte: the stream is alive, no deadline pending."""
        self.deadline = None


#: Close code for a console stream that went silent with input in
#: flight (#103): input the guest never echoed means the stream (not
#: the workspace) is wedged; a reconnect gets a fresh session.
CONSOLE_STALLED_CLOSE_CODE = 4502


async def echo_watchdog(socket: WebSocket, clock: StallClock) -> None:
    """Close the session when the armed echo deadline expires."""
    loop = asyncio.get_running_loop()
    while True:
        # Clear before reading: an arm() racing this loop must leave
        # either a fresh deadline below or a set event to wake on —
        # clearing after the read could erase the wake and sleep
        # through an armed deadline.
        clock.changed.clear()
        deadline = clock.deadline
        if deadline is None:
            await clock.changed.wait()
            continue
        remaining = deadline - loop.time()
        if remaining <= 0:
            await socket.close(
                code=CONSOLE_STALLED_CLOSE_CODE,
                reason=close_reason(
                    "console stalled: no guest bytes after input; "
                    "reconnect for a fresh session"
                ),
            )
            return
        try:
            await asyncio.wait_for(clock.changed.wait(), remaining)
            clock.changed.clear()
        except TimeoutError:
            # The sleep ran out: loop around, re-read the deadline
            # (input may have re-armed it, output cleared it).
            continue


def router(app) -> APIRouter:
    """The console websocket."""
    api = APIRouter()

    @api.websocket("/api/v1/workspaces/{workspace_id}/console")
    async def console(socket: WebSocket, workspace_id: str) -> None:
        # Byte-stream bridge into a running workspace (#21): the
        # client gets an interactive shell over the same TLS + token
        # as the REST surface, the token riding the handshake's
        # Authorization header (#216). Closing the websocket closes
        # exactly one guest shell session; the workspace keeps
        # running. Auth accepts or closes 4401: the client sees a
        # specific close reason (4401/4404/4501) instead of a
        # generic HTTP 403 rejection.
        if not await authed_accept(app, socket):
            return
        row = await app.state.model.get_workspace(workspace_id)
        if row is None:
            await socket.close(code=4404)
            return
        # The ref (name or id, #246) resolved: the vsock dial and
        # every keyed surface below use the row's immutable id.
        workspace_id = row["id"]
        # Identity negotiation (#63): the daemon validates the request
        # against the image's served users before anything reaches the
        # guest, and only prelude images carry the user and window
        # size across the vsock link.
        user, rows, cols, term, problem = console_request(socket.query_params)
        if problem is not None:
            await socket.close(code=4400, reason=close_reason(problem))
            return
        protocol, served, unreadable = console_image_policy(app, row)
        if unreadable is not None:
            await socket.close(code=4501, reason=close_reason(unreadable))
            return
        # The workspace's recorded login user (#248) is served beside
        # the image's own console users: the first-boot seed
        # provisions the account, and the guest helper serves every
        # regular account passwd names — the manifest lists what the
        # IMAGE ships, the row adds what THIS workspace seeds.
        if user not in served and user != row.get("login_user"):
            refusal = f"console user {user!r} is not served"
            await socket.close(code=4400, reason=close_reason(refusal))
            return
        try:
            if protocol == CONSOLE_PROTOCOL_PRELUDE:
                reader, writer = await app.state.microvm.console(
                    workspace_id, user=user, rows=rows, cols=cols, term=term
                )
            else:
                reader, writer = await app.state.microvm.console(workspace_id)
        except MicrovmError as exc:
            # The client is token-authenticated by now: the cause is
            # not a secret, and the close reason is the only channel
            # an operator has for dead-VM vs refused vs deadline
            # (websocket close reasons cap at 123 bytes).
            await socket.close(code=4501, reason=close_reason(str(exc)))
            return
        try:
            await bridge_console(
                socket,
                reader,
                writer,
                app.state.settings.vmm.console_stall_timeout_s,
            )
        finally:
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()

    return api
