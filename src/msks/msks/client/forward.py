"""``msks forward <workspace-id> <port>``: a workspace TCP port on stdio.

The connection is the daemon's forward websocket (#109) — TLS plus
the handshake's auth subprotocol (#116), the same mechanism as
every other websocket surface — bridged to the command's stdio, the
shape ssh's ProxyCommand expects. With ``--local PORT`` the command
binds loopback instead, and every accepted connection gets its own
forward websocket. A workspace the daemon reports as not running is
booted first, exactly as ``msks console`` does.

The forward is not a console: bytes pass unexamined both ways, so
binary protocols (ssh, rsync) ride it cleanly, and the command works
with pipes — a tty is not required on either end.
"""

import asyncio
import contextlib
import ssl
import sys
from urllib.parse import quote

import websockets

from . import wsauth
from .rest import (
    DEFAULT_URL,  # noqa: F401  (re-exported for callers/tests)
    ensure_running,
    env_token,
    env_url,
    ssl_context,
)

#: Input is read up to this many bytes per websocket frame.
READ_CHUNK = 4096

#: The receive budget — one frame from the daemon is bounded by the
#: pump's 4096-byte reads, this is the same ceiling the console client
#: allows.
MAX_FRAME = 2**22

CLOSE_CODE_REASONS = {
    4400: "bad forward request (port or parameters)",
    4401: wsauth.AUTH_FAILED_MESSAGE,
    4404: "no such workspace",
    4403: "forward not permitted for this token",
    4501: (
        "forward unavailable (no NIC, not running, or service not listening?)"
    ),
}


def ws_url(base_url: str, workspace_id: str, port: int) -> str:
    """The forward websocket's URL. The token travels in the
    handshake's auth subprotocol offer (#116), never the URL."""
    scheme, sep, rest = base_url.partition("://")
    if sep:
        scheme = "wss" if scheme == "https" else "ws"
    else:
        scheme, rest = "wss", base_url
    # The id is a path segment: quote with no safe chars (a space must
    # become %20, not + — the server percent-decodes paths only).
    return (
        f"{scheme}://{rest}/api/v1/workspaces/{quote(workspace_id, safe='')}"
        f"/forward/{port}"
    )


def connect(address: str, token: str, ssl_ctx):
    """The websocket connection, in hand for a clean close on failure.

    The token rides the handshake's auth subprotocol offer (#116).
    A plain-ws URL (http daemon) takes no ssl argument.
    """
    return websockets.connect(
        address,
        subprotocols=wsauth.subprotocols(token),
        ssl=None if address.startswith("ws://") else ssl_ctx,
        max_size=MAX_FRAME,
    )


def check_port(port: int) -> int:
    """A usable TCP port number, or a one-line exit."""
    if not 1 <= port <= 65535:
        raise SystemExit(f"msks: port {port} is out of range (1-65535)")
    return port


def close_code_message(closed: websockets.ConnectionClosed) -> str | None:
    """The one-line failure for a daemon close code, or None for a
    clean end (the guest service closed, stdin EOF — exit 0)."""
    if closed.rcvd is None or closed.rcvd.code not in CLOSE_CODE_REASONS:
        return None
    reason = closed.rcvd.reason.strip()
    detail = f": {reason}" if reason else ""
    return f"msks: {CLOSE_CODE_REASONS[closed.rcvd.code]}{detail}"


def report_close(closed: websockets.ConnectionClosed) -> None:
    """Name the daemon's close codes as a one-line exit (the stdio
    session); only the named refusals fail the client."""
    message = close_code_message(closed)
    if message is not None:
        raise SystemExit(message)


async def bridge(ws, reader, writer) -> None:
    """Pump bytes both ways until either side ends; the other task is
    cancelled. A ConnectionClosed from the websocket propagates, so
    the session can name the daemon's refusal close codes."""
    await settle_pump(
        {
            asyncio.create_task(ws_to_stream(ws, writer)),
            asyncio.create_task(stream_to_ws(reader, ws)),
        }
    )


async def settle_pump(tasks: set) -> None:
    """End a two-way pump: cancel the loser, then surface a websocket
    close from the finished task (other outcomes end the session
    without an error — the survivor's ending is the ending)."""
    done, pending = await asyncio.wait(
        tasks, return_when=asyncio.FIRST_COMPLETED
    )
    for task in pending:
        task.cancel()
    for task in pending:
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await task
    raise_if_closed(done)


def raise_if_closed(done: set) -> None:
    """Re-raise a ConnectionClosed from a finished pump task, so the
    session can name the daemon's refusal close codes."""
    for task in done:
        try:
            task.result()
        except websockets.ConnectionClosed:
            raise
        except Exception:
            pass


async def ws_to_stream(ws, writer) -> None:
    """Websocket bytes to the stream; returns when the ws closes."""
    while True:
        message = await ws.recv()
        if isinstance(message, str):
            message = message.encode()
        writer.write(message)
        await writer.drain()


async def stream_to_ws(reader, ws) -> None:
    """Stream bytes to the websocket; returns on stream EOF."""
    while True:
        data = await reader.read(READ_CHUNK)
        if not data:
            return
        await ws.send(data)


async def dial(address: str, token: str, ssl_ctx):
    """The forward websocket connection, or the one-line exit for
    every dial-time failure: a token the handshake cannot carry
    (#116, message without the credential in it), a daemon that
    cannot be reached, a TLS mismatch, a rejected upgrade."""
    try:
        connection = connect(address, token, ssl_ctx)
        return await connection
    except wsauth.UnusableToken as exc:
        raise SystemExit(f"msks: {exc}") from None
    except (OSError, ssl.SSLError, websockets.InvalidStatus) as exc:
        raise SystemExit(f"msks: cannot reach {address}: {exc}") from exc


async def stdio_session(address: str, token: str, ssl_ctx) -> int:
    """One forward bridged to this process's stdio (the ProxyCommand
    shape); returns on either end's EOF, and exits nonzero on the
    daemon's named refusals."""
    ws = await dial(address, token, ssl_ctx)
    async with ws:
        # The handshake's echo check (#116): a daemon that did not
        # select the auth subprotocol is closing with its refusal or
        # a middlebox rewrote the handshake — either way named here,
        # never pumped.
        await wsauth.require_echo(ws)
        transport, stdin = await stdin_transport()
        try:
            await bridge(ws, stdin, _StdoutWriter())
        except websockets.ConnectionClosed as closed:
            report_close(closed)
        else:
            # Stdin EOF can end the pump a beat before the daemon's
            # refusal close frame lands — the cancelled receiver would
            # drop it (#113 review). Give it a moment, then name it.
            await drain_close(ws)
        finally:
            if transport is not None:
                transport.close()
    return 0


async def drain_close(ws, timeout_s: float = 1.0) -> None:
    """Report a close frame that arrives just after the local end
    ended; a connection that simply stays quiet is a clean end."""
    try:
        await asyncio.wait_for(ws.recv(), timeout_s)
    except websockets.ConnectionClosed as closed:
        report_close(closed)
    except TimeoutError:
        return


async def stdin_transport():
    """(transport, reader) for this process's stdin.

    A stdin with no usable fd simply means no input: the session still
    carries output. The fd is checked before the transport is built —
    asyncio's half-initialized pipe transport warns from its
    deallocator otherwise.
    """
    stdin = asyncio.StreamReader()
    if pipe_fileno(sys.stdin) is None:
        stdin.feed_eof()
        return None, stdin
    try:
        protocol = asyncio.StreamReaderProtocol(stdin)
        transport, _ = await asyncio.get_running_loop().connect_read_pipe(
            lambda: protocol, _StdinPipe(sys.stdin)
        )
        return transport, stdin
    except OSError, NotImplementedError:
        stdin.feed_eof()
        return None, stdin


def report_close_stderr(closed: websockets.ConnectionClosed) -> None:
    """Name a daemon refusal on stderr (the local listener's
    per-connection form: one line and EOF, the listener stays up);
    a clean close stays quiet."""
    message = close_code_message(closed)
    if message is not None:
        print(message, file=sys.stderr)


def pipe_fileno(stream) -> int | None:
    """The stream's fd when it exists, else None (no input possible)."""
    try:
        return stream.fileno()
    except OSError, ValueError:
        return None


class _StdinPipe:
    """A connect_read_pipe target over stdin whose close() is a no-op.

    The transport closes whatever file object it is handed — but this
    process's stdin belongs to its caller (ssh's ProxyCommand reads it
    to the end), so closing it here would break the caller's cleanup.
    """

    def __init__(self, stream) -> None:
        self._stream = stream

    def fileno(self) -> int:
        return self._stream.fileno()

    def readable(self) -> bool:
        return True

    def close(self) -> None:
        pass


class _StdoutWriter:
    """The stream side of the stdio bridge: blocking writes to
    stdout's buffer — the ProxyCommand caller reads promptly."""

    def __init__(self) -> None:
        self._buffer = sys.stdout.buffer

    def write(self, data) -> None:
        self._buffer.write(data)
        self._buffer.flush()

    async def drain(self) -> None:
        return

    def close(self) -> None:
        pass

    async def wait_closed(self) -> None:
        return


async def local_listener(address: str, token: str, ssl_ctx, local_port: int):
    """The loopback listener for ``--local``: every accepted connection
    opens its own forward websocket and is bridged to it independently
    (#108's one websocket per TCP connection). A refused forward is one
    stderr line and a closed connection — the listener stays up.
    Returns the server, not yet serving."""

    async def handle(reader, writer):
        try:
            connection = connect(address, token, ssl_ctx)
            async with await connection as ws:
                await wsauth.require_echo(ws)
                await bridge(ws, reader, writer)
        except (OSError, ssl.SSLError, websockets.InvalidStatus) as exc:
            print(f"msks: cannot reach forward: {exc}", file=sys.stderr)
        except websockets.ConnectionClosed as closed:
            report_close_stderr(closed)
        except wsauth.UnusableToken as refusal:
            # One stderr line without the token in it; the listener
            # stays up for the next connection either way.
            print(f"msks: {refusal}", file=sys.stderr)
        except SystemExit as refusal:
            # The echo check's refusal, one stderr line — the listener
            # stays up for the next connection either way.
            print(str(refusal), file=sys.stderr)
        finally:
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()

    server = await asyncio.start_server(handle, "127.0.0.1", local_port)
    print(f"msks: 127.0.0.1:{local_port} -> {address}", file=sys.stderr)
    return server


async def local_server(
    address: str, token: str, ssl_ctx, local_port: int
) -> int:
    """Serve the loopback listener until cancelled (Ctrl-C ends the
    command through the CLI's interrupt handling)."""
    server = await local_listener(address, token, ssl_ctx, local_port)
    async with server:
        await server.serve_forever()
    return 0


def run_workspace_forward(
    workspace_id: str, port: int, local: int | None
) -> int:
    """One forward session, from env/TLS setup to a clean end.

    The pre-flight boot check runs before any listening socket opens:
    a not-running workspace boots with its notices on stderr first.
    """
    check_port(port)
    if local is not None:
        check_port(local)
    token = env_token()
    url = env_url()
    ssl_ctx = ssl_context()
    asyncio.run(ensure_running(workspace_id, url, token, ssl_ctx=ssl_ctx))
    address = ws_url(url, workspace_id, port)
    if local is not None:
        try:
            return asyncio.run(local_server(address, token, ssl_ctx, local))
        except OSError as exc:
            # A refused bind (the port is taken) is operator-shaped:
            # one readable line, not a traceback.
            raise SystemExit(
                f"msks: cannot bind 127.0.0.1:{local}: {exc}"
            ) from exc
    return asyncio.run(stdio_session(address, token, ssl_ctx))
