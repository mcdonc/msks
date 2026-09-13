"""``msks shell <workspace-id>``: an interactive shell in a workspace.

The connection is the daemon's console websocket (TLS + token, the
same authentication as the REST surface) bridged to the local tty in
raw mode. Ctrl-] detaches: it closes the client session — the
workspace keeps running, and the shell process inside the guest ends
when the stream closes.

Window-size changes are not propagated v1: the guest's pty is fixed
at its creation size, and applying a resize needs a guest-side
helper that does not exist yet.
"""

import argparse
import asyncio
import contextlib
import os
import ssl
import sys
import termios
import tty

import websockets

DEFAULT_URL = "https://127.0.0.1:8660"

# The detach escape (like telnet/ssh -e): Ctrl-], byte 0x1d. Ctrl-C
# and Ctrl-D belong to the guest.
DETACH = b"\x1d"


def env_url() -> str:
    return os.environ.get("MSKSC_URL", DEFAULT_URL).rstrip("/")


def env_token() -> str:
    token = os.environ.get("MSKSC_TOKEN", "")
    if not token:
        raise SystemExit(
            "msks: set MSKSC_TOKEN to a daemon token "
            "(MSKSC_URL for a non-default daemon)"
        )
    return token


def ssl_context() -> ssl.SSLContext:
    """Verify against MSKSC_CAFILE when set; otherwise TOFU-blind v1.

    The daemon's certificate is self-signed; pinning it with
    MSKSC_CAFILE gives verification, and without it the client
    proceeds unverified with a warning to stderr.
    """
    cafile = os.environ.get("MSKSC_CAFILE", "")
    if cafile:
        return ssl.create_default_context(cafile=cafile)
    print(
        "msks: MSKSC_CAFILE not set; the daemon certificate is NOT verified",
        file=sys.stderr,
    )
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


def ws_url(base_url: str, workspace_id: str, token: str) -> str:
    scheme, sep, rest = base_url.partition("://")
    if sep:
        scheme = "wss" if scheme == "https" else "ws"
    else:
        scheme, rest = "wss", base_url
    return f"{scheme}://{rest}/api/v1/workspaces/{workspace_id}/console?token={token}"


async def pump(stdin: asyncio.StreamReader, ws, stdout) -> None:
    """Local tty to daemon to guest, until detach or session end."""
    stdin_task = asyncio.create_task(stdin.read(1))
    ws_task = asyncio.create_task(ws.recv())
    while True:
        done, _ = await asyncio.wait(
            {stdin_task, ws_task}, return_when=asyncio.FIRST_COMPLETED
        )
        if stdin_task in done:
            stdin_task = await _stdin_step(stdin_task.result(), stdin, ws)
            if stdin_task is None:
                return
        if ws_task in done:
            ws_task = await _ws_step(ws_task.result(), ws, stdout)


async def _stdin_step(byte, stdin, ws):
    """Send one input byte; the next read task, or None to detach."""
    if not byte or byte == DETACH:
        # stdin EOF or the escape: detach either way.
        return None
    await ws.send(byte)
    return asyncio.create_task(stdin.read(1))


async def _ws_step(message, ws, stdout):
    """Write one daemon message out; the next receive task."""
    if isinstance(message, str):
        message = message.encode()
    stdout.buffer.write(message)
    stdout.buffer.flush()
    return asyncio.create_task(ws.recv())


async def run_shell(workspace_id: str, url: str, token: str, ssl_ctx) -> int:
    """One interactive session; 0 on clean detach or session end."""
    async with websockets.connect(
        ws_url(url, workspace_id, token), ssl=ssl_ctx, max_size=2**22
    ) as ws:
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
        except websockets.ConnectionClosed:
            pass  # the daemon ended the session; a clean exit
        finally:
            for task in asyncio.all_tasks(loop) - {asyncio.current_task()}:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
            transport.close()
    return 0


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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="msks", description="msks client: workspace microvms over the daemon API"
    )
    sub = parser.add_subparsers(dest="command", required=True)
    shell = sub.add_parser("shell", help="interactive shell in a workspace")
    shell.add_argument("workspace_id", help="the workspace to attach to")
    # With one subcommand, parse_args guarantees command == "shell"
    # and workspace_id is present.
    args = parser.parse_args(argv)

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
    try:
        if old is not None:
            tty.setraw(sys.stdin.fileno())
        return asyncio.run(run_shell(args.workspace_id, url, token, ssl_ctx))
    finally:
        restore(old, old is not None)


if __name__ == "__main__":
    raise SystemExit(main())
