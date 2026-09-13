"""``msks shell <workspace-id>``: an interactive shell in a workspace.

The connection is the daemon's console websocket (TLS + token, the
same authentication as the REST surface) bridged to the local tty in
raw mode. A workspace the daemon reports as not running is booted
first. Ctrl-] detaches: it closes the client session — the workspace
keeps running, and the shell process inside the guest ends when the
stream closes. Doubling the escape (Ctrl-] Ctrl-], the second press
within a short window) sends one literal 0x1d to the guest instead.
Input is read in chunks and sent one frame per chunk: interactive
typing is unchanged, while a large paste becomes a few frames
instead of one per byte.

Window-size changes are not propagated v1: the guest's pty is fixed
at its creation size, and applying a resize needs a guest-side
helper that does not exist yet.
"""

import asyncio
import contextlib
import ssl
import sys
import termios
import tty
from urllib.parse import quote_plus

import websockets

from .rest import (  # noqa: F401
    DEFAULT_URL,
    ensure_running,
    env_token,
    env_url,
    ssl_context,
)

# Re-exported for the tests and for callers that expect the client's
# env/TLS helpers on the shell module (they moved to rest.py).

# The detach escape (like telnet/ssh -e): Ctrl-], byte 0x1d. Ctrl-C
# and Ctrl-D belong to the guest. A doubled escape — Ctrl-] Ctrl-],
# the second press inside ESCAPE_WINDOW — sends one literal 0x1d
# instead of detaching.
DETACH = b"\x1d"

#: Input is read up to this many bytes per websocket frame: typing
#: still lands one byte per frame, a paste lands a bounded few.
READ_CHUNK = 4096

#: How long a lone escape waits for its doubling before detaching.
ESCAPE_WINDOW = 0.05


def ws_url(base_url: str, workspace_id: str, token: str) -> str:
    scheme, sep, rest = base_url.partition("://")
    if sep:
        scheme = "wss" if scheme == "https" else "ws"
    else:
        scheme, rest = "wss", base_url
    # Minted tokens are urlsafe today; quoting keeps the query string
    # well-formed for any charset a future minting scheme produces.
    return (
        f"{scheme}://{rest}/api/v1/workspaces/{quote_plus(workspace_id)}"
        f"/console?token={quote_plus(token)}"
    )


async def pump(stdin: asyncio.StreamReader, ws, stdout) -> None:
    """Local tty to daemon to guest, until detach or session end."""
    stdin_task = asyncio.create_task(stdin.read(READ_CHUNK))
    ws_task = asyncio.create_task(ws.recv())
    while True:
        done, _ = await asyncio.wait(
            {stdin_task, ws_task}, return_when=asyncio.FIRST_COMPLETED
        )
        # Output first: a message that completed in the same round as
        # a detach must still be written, and the stdin branch below
        # returns from the whole loop on detach.
        if ws_task in done:
            ws_task = await _ws_step(ws_task.result(), ws, stdout)
        if stdin_task in done:
            stdin_task = await _stdin_step(stdin_task.result(), stdin, ws)
            if stdin_task is None:
                return


async def _stdin_step(chunk, stdin, ws):
    """Send one input chunk; the next read task, or None to detach.

    The escape convention: a Ctrl-] whose next byte — in this chunk or
    within ESCAPE_WINDOW — is another Ctrl-] sends one literal 0x1d;
    a lone Ctrl-], or one followed by a different byte, detaches (the
    following byte is consumed with it). Bytes read before the
    escape still belong to the guest: they are sent before the
    detach closes the session.
    """
    if not chunk:
        # stdin EOF: detach.
        return None
    out, detach = await scan_chunk(chunk, stdin)
    if out:
        await ws.send(out)
    if detach:
        return None
    return asyncio.create_task(stdin.read(READ_CHUNK))


async def scan_chunk(chunk, stdin) -> tuple[bytes, bool]:
    """Escape-scan one input chunk: (bytes to send, detach)."""
    out = bytearray()
    i = 0
    while i < len(chunk):
        byte = chunk[i : i + 1]
        if byte != DETACH:
            out += byte
            i += 1
            continue
        nxt = chunk[i + 1 : i + 2]
        if not nxt:
            # The escape is the chunk's last byte: its doubling may
            # still arrive within the window.
            nxt = await doubling_byte(stdin)
        if nxt != DETACH:
            # EOF, window expiry, or escape-then-other-byte: detach.
            return bytes(out), True
        out += DETACH  # doubled: one literal escape to the guest
        i += 2
    return bytes(out), False


async def doubling_byte(stdin) -> bytes | None:
    """The byte after a chunk-final escape; None when the window expired."""
    try:
        return await asyncio.wait_for(stdin.read(1), ESCAPE_WINDOW)
    except TimeoutError:
        return None


async def _ws_step(message, ws, stdout):
    """Write one daemon message out; the next receive task."""
    if isinstance(message, str):
        message = message.encode()
    stdout.buffer.write(message)
    stdout.buffer.flush()
    return asyncio.create_task(ws.recv())


def _connect(address: str, ssl_ctx):
    """The websocket connection, in hand for a clean close on failure.

    A plain-ws URL (http daemon) takes no ssl argument.
    """
    return websockets.connect(
        address,
        ssl=None if address.startswith("ws://") else ssl_ctx,
        max_size=2**22,
    )


async def run_shell(workspace_id: str, url: str, token: str, ssl_ctx) -> int:
    """One interactive session; 0 on clean detach or session end."""
    address = ws_url(url, workspace_id, token)
    connection = _connect(address, ssl_ctx)
    try:
        ws = await connection
    except (OSError, ssl.SSLError, websockets.InvalidStatus) as exc:
        # Daemon down, TLS mismatch, or a rejected upgrade: one line,
        # not a traceback.
        raise SystemExit(f"msks: cannot reach {url}: {exc}") from exc
    async with ws:
        loop = asyncio.get_running_loop()
        stdin = asyncio.StreamReader()
        reader_protocol = asyncio.StreamReaderProtocol(stdin)
        # connect_read_pipe(factory, pipe): the factory is first and
        # the pipe is the file object itself, not a bare fd. The
        # transport is closed deterministically: leaving it to the
        # deallocator surfaces an unraisable double-close warning.
        transport, _ = await loop.connect_read_pipe(
            lambda: reader_protocol, _StdinPipe(sys.stdin)
        )
        try:
            await pump(stdin, ws, sys.stdout)
        except websockets.ConnectionClosed as closed:
            _report_close(closed)
        finally:
            for task in asyncio.all_tasks(loop) - {asyncio.current_task()}:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
            transport.close()
    return 0


CLOSE_CODE_REASONS = {
    4401: "authentication failed (bad token?)",
    4404: "no such workspace",
    4501: "console unavailable (is the workspace running?)",
}


def _report_close(closed: websockets.ConnectionClosed) -> None:
    """Name the daemon's close codes; anything else is a clean end.

    A clean detach or session end must stay exit 0 — only the named
    refusals fail the client.
    """
    if closed.rcvd is None or closed.rcvd.code not in CLOSE_CODE_REASONS:
        return
    reason = closed.rcvd.reason.strip()
    detail = f": {reason}" if reason else ""
    raise SystemExit(f"msks: {CLOSE_CODE_REASONS[closed.rcvd.code]}{detail}")


class _StdinPipe:
    """A connect_read_pipe target over stdin whose close() is a no-op.

    The transport closes whatever file object it is handed — but the
    client owns its tty: closing the real stdin would break the
    termios restore that runs after the session ends (#21).
    """

    def __init__(self, stream) -> None:
        self._stream = stream

    def fileno(self) -> int:
        return self._stream.fileno()

    def readable(self) -> bool:
        return True

    def close(self) -> None:
        pass


def require_tty() -> None:
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        raise SystemExit("msks shell: needs an interactive tty on stdin and stdout")


def restore(old, had: bool) -> None:
    if had:
        termios.tcsetattr(sys.stdin.fileno(), termios.TCSADRAIN, old)


def run_workspace_shell(workspace_id: str) -> int:
    """One interactive shell session, from tty setup to restore.

    Argument dispatch (``msks shell`` vs the other subcommands) lives
    in :mod:`msks.client.cli`; this is the shell command's body.
    """
    require_tty()
    token = env_token()
    url = env_url()
    try:
        old = termios.tcgetattr(sys.stdin.fileno())
    except termios.error:
        old = None
    # The TLS context (and its unverified-mode warning) is built
    # BEFORE raw mode: setraw clears OPOST, so a plain \n printed
    # mid-session would leave the cursor mid-column.
    ssl_ctx = ssl_context()
    # Same reason for the pre-flight REST call: a not-running
    # workspace is booted here, with its notices on stderr, before
    # the tty goes raw.
    asyncio.run(ensure_running(workspace_id, url, token, ssl_ctx=ssl_ctx))
    try:
        if old is not None:
            tty.setraw(sys.stdin.fileno())
        return asyncio.run(run_shell(workspace_id, url, token, ssl_ctx))
    finally:
        restore(old, old is not None)
