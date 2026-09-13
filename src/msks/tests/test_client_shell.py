"""Client unit tests: env/URL handling, tty guards, pump semantics.

The interactive loop runs against fakes; the live path is the
appliance smoke test's shell drive.
"""

import asyncio
import contextlib
import io
import os
import ssl
import sys
import termios
from pathlib import Path

import pytest
from msks.client.shell import (
    DEFAULT_URL,
    DETACH,
    env_token,
    env_url,
    main,
    pump,
    require_tty,
    ws_url,
)


class FakeWs:
    """Records sends; yields queued messages, then stays quiet."""

    def __init__(self, incoming: list | None = None) -> None:
        self.sent: list[bytes] = []
        self._incoming = list(incoming or [])

    async def recv(self):
        if self._incoming:
            return self._incoming.pop(0)
        await asyncio.sleep(3600)

    async def send(self, data: bytes) -> None:
        self.sent.append(data)


class FakeStdout:
    def __init__(self) -> None:
        self.buffer = io.BytesIO()

    def flush(self) -> None:
        pass


def feed_stdin(data: bytes) -> asyncio.StreamReader:
    reader = asyncio.StreamReader()
    reader.feed_data(data)
    reader.feed_eof()
    return reader


def test_env_url_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MSKSC_URL", raising=False)
    assert env_url() == DEFAULT_URL


def test_env_url_overridden_and_slash_trimmed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MSKSC_URL", "https://10.0.0.9:8660/")
    assert env_url() == "https://10.0.0.9:8660"


def test_env_token_missing_exits(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MSKSC_TOKEN", raising=False)
    with pytest.raises(SystemExit, match="MSKSC_TOKEN"):
        env_token()


def test_env_token_present(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MSKSC_TOKEN", "sekrit")
    assert env_token() == "sekrit"


def test_ws_url_schemes() -> None:
    assert (
        ws_url("https://h:1", "wid", "tok")
        == "wss://h:1/api/v1/workspaces/wid/console?token=tok"
    )
    assert (
        ws_url("http://h:1", "wid", "tok")
        == "ws://h:1/api/v1/workspaces/wid/console?token=tok"
    )
    # A bare host:port (no scheme) means the TLS shape.
    assert ws_url("h:1", "wid", "tok").startswith("wss://h:1/")


def test_ssl_context_unverified_warns(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from msks.client import shell

    monkeypatch.delenv("MSKSC_CAFILE", raising=False)
    ctx = shell.ssl_context()
    assert ctx.verify_mode == ssl.CERT_NONE
    assert "NOT verified" in capsys.readouterr().err


def test_ssl_context_cafile(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    from msks.client import shell

    cafile = tmp_path / "ca.pem"
    cafile.write_bytes(b"")
    monkeypatch.setenv("MSKSC_CAFILE", str(cafile))
    with pytest.raises(ssl.SSLError):
        # An empty pem fails to load: proof the file was used.
        shell.ssl_context()


def test_require_tty_rejects_pipes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class NotATty(io.StringIO):
        def isatty(self) -> bool:
            return False

    monkeypatch.setattr(sys, "stdin", NotATty())
    monkeypatch.setattr(sys, "stdout", NotATty())
    with pytest.raises(SystemExit, match="interactive tty"):
        require_tty()


def test_main_requires_subcommand() -> None:
    with pytest.raises(SystemExit):
        main([])


def test_main_shell_needs_tty(monkeypatch: pytest.MonkeyPatch) -> None:
    class NotATty(io.StringIO):
        def isatty(self) -> bool:
            return False

    monkeypatch.setattr(sys, "stdin", NotATty())
    monkeypatch.setattr(sys, "stdout", NotATty())
    monkeypatch.setattr(sys, "stderr", io.StringIO())
    with pytest.raises(SystemExit, match="interactive tty"):
        main(["shell", "wid"])


async def _cancel_orphans() -> None:
    """Pump can return with its last recv task still pending."""
    for task in asyncio.all_tasks() - {asyncio.current_task()}:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


async def test_pump_sends_input_and_detaches() -> None:
    ws = FakeWs(incoming=[b"prompt> "])
    stdout = FakeStdout()
    try:
        await asyncio.wait_for(pump(feed_stdin(b"l" + DETACH), ws, stdout), timeout=5)
    finally:
        await _cancel_orphans()
    assert ws.sent == [b"l"]
    assert stdout.buffer.getvalue() == b"prompt> "


async def test_pump_stdin_eof_detaches() -> None:
    ws = FakeWs()
    try:
        # A byte first (ws stays quiet), then EOF: exercises the
        # loop iteration where only stdin completed.
        await asyncio.wait_for(pump(feed_stdin(b"x"), ws, FakeStdout()), timeout=5)
    finally:
        await _cancel_orphans()
    assert ws.sent == [b"x"]


def test_require_tty_passes_on_tty(monkeypatch: pytest.MonkeyPatch) -> None:
    class Tty(io.StringIO):
        def isatty(self) -> bool:
            return True

    monkeypatch.setattr(sys, "stdin", Tty())
    monkeypatch.setattr(sys, "stdout", Tty())
    assert require_tty() is None


async def test_pump_decodes_text_messages() -> None:
    ws = FakeWs(incoming=["text-frame"])
    stdout = FakeStdout()

    async def detach_later() -> None:
        await asyncio.sleep(0.05)
        ws._incoming = None
        # Feed the escape through a fresh read cycle.
        ws.recv = _recv_raise  # type: ignore[method-assign]

    def _recv_raise():
        raise AssertionError("unused")

    # Simpler deterministic shape: text arrives, THEN stdin EOF ends
    # the session (EOF is the other detach path).
    stdin = asyncio.StreamReader()
    stdin.feed_data(b"x")

    async def finish_stdin() -> None:
        await asyncio.sleep(0.05)
        stdin.feed_eof()

    asyncio.create_task(finish_stdin())
    try:
        await asyncio.wait_for(pump(stdin, ws, stdout), timeout=5)
    finally:
        await _cancel_orphans()
    assert stdout.buffer.getvalue() == b"text-frame"
    assert ws.sent == [b"x"]


class ConnectStub:
    """websockets.connect() stand-in yielding a scripted ws."""

    def __init__(self, ws) -> None:
        self._ws = ws

    def __call__(self, url, ssl=None, max_size=None):
        outer = self

        class _Ctx:
            async def __aenter__(self):
                return outer._ws

            async def __aexit__(self, *exc):
                return False

        return _Ctx()


class PipeStdin:
    """A stdin stand-in: a real pipe the event loop can read."""

    def __init__(self) -> None:
        fd, w = os.pipe()
        os.set_blocking(w, False)
        self._file = os.fdopen(fd, "rb", closefd=False)
        self._w = w

    def fileno(self) -> int:
        return self._file.fileno()

    def readable(self) -> bool:
        return True

    def close(self) -> None:
        self._file.close()  # idempotent, like a real file

    def feed(self, data: bytes) -> None:
        os.write(self._w, data)


async def test_run_shell_detaches_on_escape(monkeypatch: pytest.MonkeyPatch) -> None:
    from msks.client import shell

    ws = FakeWs(incoming=[b"hello\n"])
    pipe = PipeStdin()
    monkeypatch.setattr(sys, "stdin", pipe)
    monkeypatch.setattr(shell.websockets, "connect", ConnectStub(ws))
    monkeypatch.setenv("MSKSC_CAFILE", "")
    monkeypatch.delenv("MSKSC_CAFILE", raising=False)
    stdout = FakeStdout()
    monkeypatch.setattr(sys, "stdout", stdout)
    loop = asyncio.get_running_loop()
    loop.run_in_executor(None, lambda: (pipe.feed(b"l"), pipe.feed(DETACH)))
    result = await asyncio.wait_for(run_shell_via(shell), 5)
    assert result == 0
    assert ws.sent == [b"l"]
    assert stdout.buffer.getvalue() == b"hello\n"


async def run_shell_via(shell):
    return await shell.run_shell("wid", "u", "t", None)


async def test_run_shell_survives_server_close(monkeypatch: pytest.MonkeyPatch) -> None:
    from msks.client import shell

    class ClosingWs:
        def __init__(self) -> None:
            self._first = True

        async def recv(self):
            if self._first:
                self._first = False
                await asyncio.sleep(0)
                close = shell.websockets.frames.Close(1000, "bye")
                raise shell.websockets.ConnectionClosed(None, close)
            raise AssertionError("unused")

        async def send(self, data):
            raise AssertionError("unused")

    pipe = PipeStdin()
    monkeypatch.setattr(sys, "stdin", pipe)
    monkeypatch.setattr(shell.websockets, "connect", ConnectStub(ClosingWs()))
    monkeypatch.setattr(sys, "stdout", FakeStdout())
    assert await shell.run_shell("wid", "u", "t", None) == 0


class FdOnly:
    """A stdin stand-in: pytest capture replaces sys.stdin with a
    pseudofile that has no fileno()."""

    def fileno(self) -> int:
        return 0


def test_main_raw_mode_cycle(monkeypatch: pytest.MonkeyPatch) -> None:
    from msks.client import shell

    monkeypatch.setattr(sys, "stdin", FdOnly())
    monkeypatch.setenv("MSKSC_TOKEN", "t")
    monkeypatch.setattr(shell, "require_tty", lambda: None)
    restored: list = []

    async def fake_run(wid, url, token, ssl_ctx):
        return 7

    monkeypatch.setattr(shell, "run_shell", fake_run)
    monkeypatch.setattr(shell.termios, "tcgetattr", lambda fd: ["old"], raising=True)
    monkeypatch.setattr(
        shell.termios, "tcsetattr", lambda fd, when, attrs: restored.append(attrs)
    )
    monkeypatch.setattr(shell.tty, "setraw", lambda fd: None)
    assert main(["shell", "wid"]) == 7
    assert restored == [["old"]]


def test_main_without_a_terminal(monkeypatch: pytest.MonkeyPatch) -> None:
    from msks.client import shell

    monkeypatch.setattr(sys, "stdin", FdOnly())
    monkeypatch.setenv("MSKSC_TOKEN", "t")
    monkeypatch.setattr(shell, "require_tty", lambda: None)
    monkeypatch.setattr(
        shell.termios, "tcgetattr", lambda fd: (_ for _ in ()).throw(termios.error())
    )

    async def fake_run(wid, url, token, ssl_ctx):
        return 0

    monkeypatch.setattr(shell, "run_shell", fake_run)
    assert main(["shell", "wid"]) == 0


def test_module_entry_runs(monkeypatch: pytest.MonkeyPatch) -> None:
    from msks.client import shell

    monkeypatch.setattr(sys, "argv", ["msks"])
    source = Path(shell.__file__).read_text()
    # Executing the source in a fresh namespace avoids runpy's
    # already-imported RuntimeWarning while still running the module
    # entry block.
    with pytest.raises(SystemExit):
        exec(compile(source, shell.__file__, "exec"), {"__name__": "__main__"})


def _closed(code: int, reason: str = ""):
    from msks.client import shell

    close = shell.websockets.Close(code, reason)
    return shell.websockets.ConnectionClosed(close, None)


def test_report_close_4401() -> None:
    from msks.client import shell

    with pytest.raises(SystemExit, match="authentication failed"):
        shell._report_close(_closed(4401))


def test_report_close_carries_reason() -> None:
    from msks.client import shell

    with pytest.raises(SystemExit, match="workspace stopped"):
        shell._report_close(_closed(4501, "workspace stopped"))


def test_report_close_clean_end_is_quiet() -> None:
    from msks.client import shell

    assert shell._report_close(_closed(1000)) is None


def test_stdin_pipe_passthrough() -> None:
    from msks.client import shell

    pipe = shell._StdinPipe(sys.stdin)
    assert pipe.close() is None
    assert pipe.readable() is True
