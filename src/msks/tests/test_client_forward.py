"""Forward client unit tests: URL/header conventions, port checks,
close-code reporting, and the pump — against fakes (the live dial path
is exercised by the daemon-side suite; no smoke drive exists yet)."""

import asyncio
import io
import sys

import pytest
import websockets
import websockets.frames
from msks.client import forward as fwd


class FakeStdout:
    def __init__(self) -> None:
        self.buffer = io.BytesIO()

    def flush(self) -> None:
        pass


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

    async def close(self) -> None:
        return None

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class FakeWriter:
    def __init__(self) -> None:
        self.buffer = b""

    def write(self, data) -> None:
        self.buffer += data

    async def drain(self) -> None:
        return


def fed_stream(data: bytes) -> asyncio.StreamReader:
    reader = asyncio.StreamReader()
    reader.feed_data(data)
    reader.feed_eof()
    return reader


def test_ws_url_carries_no_token() -> None:
    # The token rides the Authorization header, never the URL (#109):
    # URLs land in logs, headers do not.
    assert (
        fwd.ws_url("https://h:1", "ws 1", 22)
        == "wss://h:1/api/v1/workspaces/ws%201/forward/22"
    )
    assert fwd.ws_url("http://h:1", "ws", 8022).startswith("ws://h:1/")
    assert fwd.ws_url("h:1", "ws", 22).startswith("wss://h:1/")
    assert "token" not in fwd.ws_url("https://h:1", "ws", 22)


def test_auth_headers_is_the_rest_bearer_form() -> None:
    assert fwd.auth_headers("sekrit") == {"Authorization": "Bearer sekrit"}


def test_check_port_accepts_range_and_exits_outside() -> None:
    assert fwd.check_port(1) == 1
    assert fwd.check_port(65535) == 65535
    with pytest.raises(SystemExit, match="out of range"):
        fwd.check_port(0)
    with pytest.raises(SystemExit, match="out of range"):
        fwd.check_port(65536)


def closed_with(code: int, reason: str = "") -> websockets.ConnectionClosed:
    rcvd = websockets.frames.Close(code, reason)
    return websockets.ConnectionClosed(rcvd, None)


def test_report_close_names_the_refusals() -> None:
    with pytest.raises(SystemExit, match="no such workspace"):
        fwd.report_close(closed_with(4404))
    with pytest.raises(SystemExit, match=r"bad token"):
        fwd.report_close(closed_with(4401))
    with pytest.raises(SystemExit, match="no NIC"):
        fwd.report_close(closed_with(4501, "workspace ws has no NIC"))


def test_report_close_passes_clean_ends_through() -> None:
    # A guest service that closed the stream (1000/1005) is a clean
    # end: the client exits 0.
    fwd.report_close(closed_with(1000))
    fwd.report_close(closed_with(1005))


def test_close_code_message_names_what_it_knows() -> None:
    assert "forward unavailable" in fwd.close_code_message(closed_with(4501))
    assert fwd.close_code_message(closed_with(1000)) is None


def test_stream_to_ws_sends_chunks_until_eof() -> None:
    ws = FakeWs()

    async def run():
        await fwd.stream_to_ws(fed_stream(b"abc"), ws)

    asyncio.run(run())
    assert ws.sent == [b"abc"]


def test_ws_to_stream_forwards_text_and_bytes() -> None:
    ws = FakeWs(incoming=[b"bin", "text"])
    writer = FakeWriter()

    async def ws_then_close():
        # recv never ends on the fake after the queue empties; end the
        # pump by cancelling it once both messages landed.
        task = asyncio.create_task(fwd.ws_to_stream(ws, writer))
        while len(writer.buffer) < 7:
            await asyncio.sleep(0.01)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    asyncio.run(ws_then_close())
    assert writer.buffer == b"bintext"


def test_bridge_carries_bytes_and_ends_on_stream_eof() -> None:
    ws = FakeWs(incoming=[b"from-daemon\n"])
    writer = FakeWriter()

    async def run():
        await fwd.bridge(ws, fed_stream(b"to-daemon\n"), writer)

    asyncio.run(run())
    # Both directions flowed before the stream EOF ended the session.
    assert ws.sent == [b"to-daemon\n"]
    assert writer.buffer == b"from-daemon\n"


def test_bridge_raises_the_ws_close_for_the_session_to_name() -> None:
    class ClosingWs(FakeWs):
        async def recv(self):
            raise closed_with(4404)

    async def run():
        await fwd.bridge(ClosingWs(), fed_stream(b""), FakeWriter())

    with pytest.raises(websockets.ConnectionClosed):
        asyncio.run(run())


# --- The sessions (connect stubbed, stdin/stdout on real pipes) ---


class StdioPipe:
    """A stdin stand-in: a real pipe the event loop can read."""

    def __init__(self) -> None:
        import os

        fd, w = os.pipe()
        os.set_blocking(w, False)
        self._file = os.fdopen(fd, "rb", closefd=True)
        self._w = w

    def fileno(self) -> int:
        return self._file.fileno()

    def readable(self) -> bool:
        return True

    def close(self) -> None:
        self._file.close()

    def feed(self, data: bytes) -> None:
        import os

        os.write(self._w, data)

    def close_writer(self) -> None:
        import os

        os.close(self._w)


class ConnectStub:
    """websockets.connect() stand-in yielding a scripted ws."""

    def __init__(self, ws) -> None:
        self._ws = ws
        self.recorded: dict = {}

    def __call__(self, url, additional_headers=None, ssl=None, max_size=None):
        outer = self
        self.recorded = {
            "url": url,
            "headers": additional_headers,
            "ssl": ssl,
        }

        class _Ctx:
            async def __aenter__(self):
                return outer._ws

            def __await__(self):
                return self.__aenter__().__await__()

            async def __aexit__(self, *exc):
                return False

        return _Ctx()


def test_connect_passes_header_and_scheme_ssl(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stub = ConnectStub(FakeWs())
    monkeypatch.setattr(fwd.websockets, "connect", stub)
    fwd.connect("ws://h:1", "tok", "ctx")  # plain ws: no ssl argument
    assert stub.recorded["ssl"] is None
    fwd.connect("wss://h:1", "tok", "ctx")
    assert stub.recorded["ssl"] == "ctx"
    assert stub.recorded["headers"] == {"Authorization": "Bearer tok"}


async def test_stdio_session_carries_both_directions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ws = FakeWs(incoming=[b"from-daemon\n"])
    pipe = StdioPipe()
    monkeypatch.setattr(sys, "stdin", pipe)
    monkeypatch.setattr(sys, "stdout", FakeStdout())
    monkeypatch.setattr(fwd.websockets, "connect", ConnectStub(ws))
    pipe.feed(b"to-daemon\n")
    pipe.close_writer()  # EOF ends the session after both sides flow
    result = await asyncio.wait_for(fwd.stdio_session("ws://d", "t", None), 5)
    assert result == 0
    assert ws.sent == [b"to-daemon\n"]


async def test_stdio_session_survives_a_dead_stdin(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A stdin that cannot become a pipe (a redirect from a file) means
    # no input: the session still carries output to its end.
    class DeadStdin:
        def fileno(self) -> int:
            raise ValueError("not a pipe")

    ws = FakeWs(incoming=[b"bye\n"])
    monkeypatch.setattr(sys, "stdin", DeadStdin())
    monkeypatch.setattr(sys, "stdout", FakeStdout())
    monkeypatch.setattr(fwd.websockets, "connect", ConnectStub(ws))
    result = await asyncio.wait_for(fwd.stdio_session("ws://d", "t", None), 5)
    assert result == 0
    assert ws.sent == []


async def test_local_server_bridges_each_connection_privately(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    ws = FakeWs(incoming=[b"answer\n"])
    monkeypatch.setattr(fwd.websockets, "connect", ConnectStub(ws))
    server = await fwd.local_listener("ws://d", "t", None, 0)
    assert "127.0.0.1" in capsys.readouterr().err
    address = server.sockets[0].getsockname()
    reader, writer = await asyncio.open_connection(*address)
    writer.write(b"ask\n")
    await writer.drain()
    assert await reader.readline() == b"answer\n"
    assert ws.sent == [b"ask\n"]
    writer.close()
    server.close()


async def test_local_server_names_a_refused_connection(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    class Refusing:
        def __call__(self, *args, **kwargs):
            return self

        def __await__(self):
            raise OSError("dial refused")

    monkeypatch.setattr(fwd.websockets, "connect", Refusing())
    server = await fwd.local_listener("ws://d", "t", None, 0)
    address = server.sockets[0].getsockname()
    reader, writer = await asyncio.open_connection(*address)
    assert await reader.read() == b""  # refused: EOF, listener stays up
    assert "cannot reach forward" in capsys.readouterr().err
    server.close()


async def test_local_server_names_a_close_code(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    class ClosingWs(FakeWs):
        async def recv(self):
            raise closed_with(4404, "no such workspace")

    monkeypatch.setattr(fwd.websockets, "connect", ConnectStub(ClosingWs()))
    server = await fwd.local_listener("ws://d", "t", None, 0)
    address = server.sockets[0].getsockname()
    reader, writer = await asyncio.open_connection(*address)
    assert await reader.read() == b""
    assert "no such workspace" in capsys.readouterr().err
    server.close()


def test_run_workspace_forward_stdio_and_local(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MSKSC_TOKEN", "t")
    monkeypatch.setenv("MSKSC_URL", "https://d")
    monkeypatch.delenv("MSKSC_CAFILE", raising=False)
    calls: list[tuple] = []

    async def fake_ensure(*args, **kwargs):
        calls.append(("ensure", args[0]))

    monkeypatch.setattr(fwd, "ensure_running", fake_ensure)

    async def fake_session(address, token, ssl_ctx):
        calls.append(("stdio", address, token))
        return 7

    monkeypatch.setattr(fwd, "stdio_session", fake_session)
    assert fwd.run_workspace_forward("ws", 22, None) == 7

    async def fake_local(address, token, ssl_ctx, local_port):
        calls.append(("local", address, local_port))
        return 3

    monkeypatch.setattr(fwd, "local_server", fake_local)
    assert fwd.run_workspace_forward("ws", 8022, 8122) == 3
    assert calls == [
        ("ensure", "ws"),
        ("stdio", "wss://d/api/v1/workspaces/ws/forward/22", "t"),
        ("ensure", "ws"),
        ("local", "wss://d/api/v1/workspaces/ws/forward/8022", 8122),
    ]


def test_run_workspace_forward_rejects_a_bad_local_port(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MSKSC_TOKEN", "t")
    monkeypatch.setenv("MSKSC_URL", "https://d")
    monkeypatch.delenv("MSKSC_CAFILE", raising=False)
    with pytest.raises(SystemExit, match="out of range"):
        fwd.run_workspace_forward("ws", 22, 70000)


async def test_bridge_swallows_stream_errors_but_names_closes() -> None:
    class BrokenWs(FakeWs):
        async def recv(self):
            raise ValueError("guest stream broke")

    # A non-close failure ends the session quietly (the survivor's
    # ending is the ending); the close re-raise stays covered above.
    await asyncio.wait_for(
        fwd.bridge(BrokenWs(), fed_stream(b""), FakeWriter()), 5
    )


class ClosedFdStdin:
    """A stdin whose fd is already closed: connect_read_pipe refuses."""

    def __init__(self) -> None:
        import os

        self._fd = os.pipe()[0]
        os.close(self._fd)

    def fileno(self) -> int:
        return self._fd

    def readable(self) -> bool:
        return True

    def close(self) -> None:
        pass


async def test_stdio_session_treats_an_unusable_pipe_as_no_input(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ws = FakeWs(incoming=[b"ok\n"])
    monkeypatch.setattr(sys, "stdin", ClosedFdStdin())
    monkeypatch.setattr(sys, "stdout", FakeStdout())
    monkeypatch.setattr(fwd.websockets, "connect", ConnectStub(ws))
    assert (
        await asyncio.wait_for(fwd.stdio_session("ws://d", "t", None), 5) == 0
    )
    assert ws.sent == []


async def test_stdio_session_exits_nonzero_on_a_named_refusal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Parity with the console command and the CLI contract: a named
    # refusal is one line and a nonzero exit, not a quiet zero.
    class RefusedWs(FakeWs):
        async def recv(self):
            raise closed_with(4501, "workspace ws has no NIC")

    monkeypatch.setattr(sys, "stdin", ClosedFdStdin())
    monkeypatch.setattr(sys, "stdout", FakeStdout())
    monkeypatch.setattr(fwd.websockets, "connect", ConnectStub(RefusedWs()))
    with pytest.raises(SystemExit, match="no NIC"):
        await asyncio.wait_for(fwd.stdio_session("ws://d", "t", None), 5)


async def test_stdio_session_names_an_unreachable_daemon(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Refusing:
        def __call__(self, *args, **kwargs):
            return self

        def __await__(self):
            raise OSError("no route")

    monkeypatch.setattr(fwd.websockets, "connect", Refusing())
    with pytest.raises(SystemExit, match="cannot reach"):
        await asyncio.wait_for(fwd.stdio_session("ws://d", "t", None), 5)


def test_run_workspace_forward_names_a_refused_bind(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MSKSC_TOKEN", "t")
    monkeypatch.setenv("MSKSC_URL", "https://d")
    monkeypatch.delenv("MSKSC_CAFILE", raising=False)

    async def fake_ensure(*args, **kwargs):
        return None

    async def refused_listener(*args):
        raise OSError("address already in use")

    monkeypatch.setattr(fwd, "ensure_running", fake_ensure)
    monkeypatch.setattr(fwd, "local_listener", refused_listener)
    with pytest.raises(SystemExit, match="cannot bind"):
        fwd.run_workspace_forward("ws", 8080, 8122)


def test_stdio_pipe_and_writer_close_are_no_ops(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(sys, "stdout", FakeStdout())
    writer = fwd._StdoutWriter()
    writer.close()
    pipe = fwd._StdinPipe(sys.stdin)
    assert pipe.readable() is True
    pipe.close()

    async def run() -> None:
        await writer.wait_closed()
        await writer.drain()

    asyncio.run(run())


async def test_stdio_session_is_quiet_on_a_clean_close(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    class DoneWs(FakeWs):
        async def recv(self):
            raise closed_with(1000)

    monkeypatch.setattr(sys, "stdin", ClosedFdStdin())
    monkeypatch.setattr(sys, "stdout", FakeStdout())
    monkeypatch.setattr(fwd.websockets, "connect", ConnectStub(DoneWs()))
    assert (
        await asyncio.wait_for(fwd.stdio_session("ws://d", "t", None), 5) == 0
    )
    assert "forward" not in capsys.readouterr().err


async def test_local_handler_is_quiet_on_a_clean_close(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    class CleanWs(FakeWs):
        async def recv(self):
            raise closed_with(1000)

    monkeypatch.setattr(fwd.websockets, "connect", ConnectStub(CleanWs()))
    server = await fwd.local_listener("ws://d", "t", None, 0)
    address = server.sockets[0].getsockname()
    reader, writer = await asyncio.open_connection(*address)
    assert await reader.read() == b""
    assert capsys.readouterr().err == "msks: 127.0.0.1:0 -> ws://d\n"
    server.close()


async def test_local_server_serves_until_the_listener_closes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    served = asyncio.Event()

    class FakeListener:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def serve_forever(self, _done=served):
            _done.set()

    listener = FakeListener()

    async def fake_listener_factory(*args):
        return listener

    monkeypatch.setattr(fwd, "local_listener", fake_listener_factory)
    task = asyncio.create_task(fwd.local_server("ws://d", "t", None, 1))
    assert await asyncio.wait_for(task, 5) == 0
    assert served.is_set()


async def test_stdio_session_names_a_refusal_landing_after_stdin_eof(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The #113 review's verified race: stdin EOF ends the pump and
    # cancels the receiver a beat before the daemon's refusal close
    # frame lands — the drain must still surface it.
    class SlowRefusalWs(FakeWs):
        async def recv(self):
            await asyncio.sleep(0.05)
            raise closed_with(4501, "workspace ws has no NIC")

    monkeypatch.setattr(sys, "stdin", ClosedFdStdin())
    monkeypatch.setattr(sys, "stdout", FakeStdout())
    monkeypatch.setattr(
        fwd.websockets, "connect", ConnectStub(SlowRefusalWs())
    )
    with pytest.raises(SystemExit, match="no NIC"):
        await asyncio.wait_for(fwd.stdio_session("ws://d", "t", None), 5)


async def test_stdio_session_drain_ends_quietly(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A ws that stays quiet after stdin EOF is a clean end: the drain
    # times out and the session exits 0.
    class QuietWs(FakeWs):
        async def recv(self):
            await asyncio.sleep(3600)

    monkeypatch.setattr(sys, "stdin", ClosedFdStdin())
    monkeypatch.setattr(sys, "stdout", FakeStdout())
    monkeypatch.setattr(fwd.websockets, "connect", ConnectStub(QuietWs()))
    assert (
        await asyncio.wait_for(fwd.stdio_session("ws://d", "t", None), 5) == 0
    )


async def test_drain_close_reports_and_passes(monkeypatch) -> None:
    class ClosingWs(FakeWs):
        async def recv(self):
            raise closed_with(4404)

    with pytest.raises(SystemExit, match="no such workspace"):
        await fwd.drain_close(ClosingWs(), timeout_s=0.1)

    class QuietWs(FakeWs):
        async def recv(self):
            await asyncio.sleep(3600)

    assert await asyncio.wait_for(fwd.drain_close(QuietWs(), 0.05), 5) is None
