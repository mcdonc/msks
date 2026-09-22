"""Client unit tests: env/URL handling, tty guards, pump semantics.

The interactive loop runs against fakes; the live path is the
smoke suite's console drive.
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
from msks.client import cli, console
from msks.client.console import (
    DEFAULT_URL,
    DETACH,
    env_token,
    env_url,
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

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


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


def test_env_url_overridden_and_slash_trimmed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
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
        == "wss://h:1/api/v1/workspaces/wid/console?token=tok&user=root"
    )
    assert (
        ws_url("http://h:1", "wid", "tok")
        == "ws://h:1/api/v1/workspaces/wid/console?token=tok&user=root"
    )
    # A bare host:port (no scheme) means the TLS shape.
    assert ws_url("h:1", "wid", "tok").startswith("wss://h:1/")


def test_tty_size_reads_ioctl(monkeypatch: pytest.MonkeyPatch) -> None:
    import fcntl as fcntl_mod
    import struct as struct_mod

    from msks.client import console as console_mod

    def fake_ioctl(fd, request, packed):
        return struct_mod.pack("HHHH", 34, 120, 0, 0)

    monkeypatch.setattr(fcntl_mod, "ioctl", fake_ioctl)
    assert console_mod.tty_size(0) == (34, 120)


def test_tty_size_zero_geometry_is_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import fcntl as fcntl_mod
    import struct as struct_mod

    from msks.client import console as console_mod

    def fake_ioctl(fd, request, packed):
        return struct_mod.pack("HHHH", 0, 0, 0, 0)

    monkeypatch.setattr(fcntl_mod, "ioctl", fake_ioctl)
    assert console_mod.tty_size(0) is None


def test_tty_size_without_a_terminal_is_none() -> None:
    import os

    from msks.client.console import tty_size

    r, w = os.pipe()
    try:
        assert tty_size(r) is None
    finally:
        os.close(r)
        os.close(w)


def test_ws_url_carries_user_and_size() -> None:
    url = ws_url("https://d", "ws 1", "tok/en", user="msks", size=(34, 120))
    assert "user=msks" in url
    assert "rows=34" in url
    assert "cols=120" in url


def test_ws_url_carries_term() -> None:
    url = ws_url("https://d", "ws-1", "t", term="tmux-256color")
    assert "term=tmux-256color" in url


def test_ws_url_omits_term_when_absent() -> None:
    assert "term=" not in ws_url("https://d", "ws-1", "t")


def test_ws_url_default_user_root_without_size() -> None:
    url = ws_url("https://d", "ws-1", "t")
    assert "user=root" in url
    assert "rows=" not in url


def test_ws_url_quotes_user() -> None:
    url = ws_url("https://d", "ws-1", "t", user="a b")
    assert "user=a+b" in url


def test_ws_url_quotes_query_unsafe_parts() -> None:
    quoted = ws_url("https://h:1", "w id", "a+b&c=d%e")
    # The id is a PATH segment: a space must encode as %20 (a + would
    # reach the server literally, since paths percent-decode only).
    assert quoted.startswith("wss://h:1/api/v1/workspaces/w%20id/")
    assert quoted.endswith("?token=a%2Bb%26c%3Dd%25e&user=root")
    # The token stays one query parameter, whatever it contains; the
    # user follows it as the next one.
    tail = quoted.split("?token=", 1)[1]
    assert tail.count("&") == 1 and tail.endswith("&user=root")


def test_ssl_context_unverified_warns(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:

    monkeypatch.delenv("MSKSC_CAFILE", raising=False)
    ctx = console.ssl_context()
    assert ctx.verify_mode == ssl.CERT_NONE
    assert "NOT verified" in capsys.readouterr().err


def test_ssl_context_cafile(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:

    cafile = tmp_path / "ca.pem"
    cafile.write_bytes(b"")
    monkeypatch.setenv("MSKSC_CAFILE", str(cafile))
    with pytest.raises(ssl.SSLError):
        # An empty pem fails to load: proof the file was used.
        console.ssl_context()


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
        cli.main([])


def test_main_console_needs_tty(monkeypatch: pytest.MonkeyPatch) -> None:
    class NotATty(io.StringIO):
        def isatty(self) -> bool:
            return False

    monkeypatch.setattr(sys, "stdin", NotATty())
    monkeypatch.setattr(sys, "stdout", NotATty())
    monkeypatch.setattr(sys, "stderr", io.StringIO())
    with pytest.raises(SystemExit, match="interactive tty"):
        cli.main(["console", "wid"])


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
        await asyncio.wait_for(
            pump(feed_stdin(b"l" + DETACH), ws, stdout), timeout=5
        )
    finally:
        await _cancel_orphans()
    assert ws.sent == [b"l"]
    assert stdout.buffer.getvalue() == b"prompt> "


async def test_pump_coalesces_a_paste() -> None:
    payload = bytes(range(65, 91)) * 400  # 10,400 bytes, no 0x1d inside
    ws = FakeWs()
    stdin = asyncio.StreamReader()
    stdin.feed_data(payload)
    stdin.feed_eof()
    try:
        await asyncio.wait_for(pump(stdin, ws, FakeStdout()), timeout=5)
    finally:
        await _cancel_orphans()
    assert b"".join(ws.sent) == payload
    # One frame per read chunk (4,096 B), not one per byte.
    assert len(ws.sent) <= 4


async def test_pump_doubled_escape_sends_literal() -> None:
    ws = FakeWs()
    try:
        await asyncio.wait_for(
            pump(feed_stdin(DETACH + DETACH + b"x"), ws, FakeStdout()),
            timeout=5,
        )
    finally:
        await _cancel_orphans()
    assert ws.sent == [b"\x1dx"]


async def test_pump_doubled_escape_across_window() -> None:
    ws = FakeWs()
    stdin = asyncio.StreamReader()
    stdin.feed_data(DETACH)  # first press: held by the pending read

    async def second_press() -> None:
        await asyncio.sleep(0.01)  # inside ESCAPE_WINDOW (50 ms)
        stdin.feed_data(DETACH)
        stdin.feed_eof()

    asyncio.create_task(second_press())
    try:
        await asyncio.wait_for(pump(stdin, ws, FakeStdout()), timeout=5)
    finally:
        await _cancel_orphans()
    assert ws.sent == [b"\x1d"]


async def test_pump_escape_then_other_byte_detaches() -> None:
    ws = FakeWs()
    try:
        await asyncio.wait_for(
            pump(feed_stdin(DETACH + b"z"), ws, FakeStdout()), timeout=5
        )
    finally:
        await _cancel_orphans()
    assert ws.sent == []


async def test_pump_delivers_prefix_before_detach() -> None:
    # Bytes that coalesce into the escape's chunk (fast typing, a
    # paste ending in Ctrl-]) still belong to the guest — the detach
    # sends them before it closes the session. The consumed
    # escape-follower (here: "z") is not delivered.
    ws = FakeWs()
    try:
        await asyncio.wait_for(
            pump(feed_stdin(b"hi" + DETACH + b"z"), ws, FakeStdout()),
            timeout=5,
        )
    finally:
        await _cancel_orphans()
    assert ws.sent == [b"hi"]


async def test_pump_lone_escape_detaches_after_window() -> None:
    ws = FakeWs()
    stdin = asyncio.StreamReader()
    stdin.feed_data(DETACH)  # no EOF: the window must expire
    try:
        await asyncio.wait_for(pump(stdin, ws, FakeStdout()), timeout=5)
    finally:
        await _cancel_orphans()
    assert ws.sent == []


async def test_pump_writes_messages_between_input_reads() -> None:
    # Wait rounds where ONLY the daemon spoke must still write output:
    # stdin stays pending until its late EOF, and both messages land
    # in rounds of their own.
    ws = FakeWs(incoming=[b"one", b"two"])
    stdin = asyncio.StreamReader()

    async def finish_stdin() -> None:
        await asyncio.sleep(0.05)
        stdin.feed_eof()

    asyncio.create_task(finish_stdin())
    stdout = FakeStdout()
    try:
        await asyncio.wait_for(pump(stdin, ws, stdout), timeout=5)
    finally:
        await _cancel_orphans()
    assert stdout.buffer.getvalue() == b"onetwo"
    assert ws.sent == []


async def test_pump_stdin_eof_detaches() -> None:
    ws = FakeWs()
    try:
        # A byte first (ws stays quiet), then EOF: exercises the
        # loop iteration where only stdin completed.
        await asyncio.wait_for(
            pump(feed_stdin(b"x"), ws, FakeStdout()), timeout=5
        )
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
        self.recorded_ssl = "unset"

    def __call__(self, url, ssl=None, max_size=None):
        self.recorded_ssl = ssl
        outer = self

        class _Ctx:
            async def __aenter__(self):
                return outer._ws

            def __await__(self):
                # websockets.connect() returns an awaitable context
                # manager; the stub must be both too.
                return self.__aenter__().__await__()

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


async def test_run_shell_detaches_on_escape(
    monkeypatch: pytest.MonkeyPatch,
) -> None:

    ws = FakeWs(incoming=[b"hello\n"])
    pipe = PipeStdin()
    monkeypatch.setattr(sys, "stdin", pipe)
    monkeypatch.setattr(console.websockets, "connect", ConnectStub(ws))
    monkeypatch.setenv("MSKSC_CAFILE", "")
    monkeypatch.delenv("MSKSC_CAFILE", raising=False)
    stdout = FakeStdout()
    monkeypatch.setattr(sys, "stdout", stdout)
    loop = asyncio.get_running_loop()
    loop.run_in_executor(None, lambda: (pipe.feed(b"l"), pipe.feed(DETACH)))
    result = await asyncio.wait_for(run_shell_via(console), 5)
    assert result == 0
    assert ws.sent == [b"l"]
    assert stdout.buffer.getvalue() == b"hello\n"


async def run_shell_via(mod):
    return await mod.run_shell("wid", "u", "t", None)


async def test_run_shell_survives_server_close(
    monkeypatch: pytest.MonkeyPatch,
) -> None:

    class ClosingWs:
        def __init__(self) -> None:
            self._first = True

        async def recv(self):
            if self._first:
                self._first = False
                await asyncio.sleep(0)
                close = console.websockets.Close(1000, "bye")
                raise console.websockets.ConnectionClosed(None, close)
            raise AssertionError("unused")

        async def send(self, data):
            raise AssertionError("unused")

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

    pipe = PipeStdin()
    monkeypatch.setattr(sys, "stdin", pipe)
    monkeypatch.setattr(
        console.websockets, "connect", ConnectStub(ClosingWs())
    )
    monkeypatch.setattr(sys, "stdout", FakeStdout())
    assert await console.run_shell("wid", "u", "t", None) == 0


class FdOnly:
    """A stdin stand-in: pytest capture replaces sys.stdin with a
    pseudofile that has no fileno()."""

    def fileno(self) -> int:
        return 0


async def async_noop(*args, **kwargs) -> None:
    """A stand-in for the console's pre-flight REST call."""


def test_main_raw_mode_cycle(monkeypatch: pytest.MonkeyPatch) -> None:

    order: list[str] = []

    async def preflight(*args, **kwargs) -> None:
        order.append("preflight")

    monkeypatch.setattr(sys, "stdin", FdOnly())
    monkeypatch.setenv("MSKSC_TOKEN", "t")
    monkeypatch.setattr(console, "require_tty", lambda: None)
    monkeypatch.setattr(console, "ensure_running", preflight)
    stub_login_user(monkeypatch, "root")
    restored: list = []

    async def fake_run(
        wid, url, token, ssl_ctx, user="root", size=None, term=None
    ):
        return 7

    monkeypatch.setattr(console, "run_shell", fake_run)
    monkeypatch.setattr(
        console.termios, "tcgetattr", lambda fd: ["old"], raising=True
    )
    monkeypatch.setattr(
        console.termios,
        "tcsetattr",
        lambda fd, when, attrs: restored.append(attrs),
    )
    monkeypatch.setattr(console.tty, "setraw", lambda fd: order.append("raw"))
    assert cli.main(["console", "wid"]) == 7
    assert restored == [["old"]]
    # The pre-flight boot and its notices must land BEFORE raw mode:
    # setraw clears OPOST, so a mid-session newline would leave the
    # cursor mid-column.
    assert order == ["preflight", "raw"]


def test_main_without_a_terminal(monkeypatch: pytest.MonkeyPatch) -> None:

    monkeypatch.setattr(sys, "stdin", FdOnly())
    monkeypatch.setenv("MSKSC_TOKEN", "t")
    monkeypatch.setattr(console, "require_tty", lambda: None)
    monkeypatch.setattr(console, "ensure_running", async_noop)
    stub_login_user(monkeypatch, "root")
    monkeypatch.setattr(
        console.termios,
        "tcgetattr",
        lambda fd: (_ for _ in ()).throw(termios.error()),
    )

    async def fake_run(
        wid, url, token, ssl_ctx, user="root", size=None, term=None
    ):
        return 0

    monkeypatch.setattr(console, "run_shell", fake_run)
    assert cli.main(["console", "wid"]) == 0


def test_module_entry_runs(monkeypatch: pytest.MonkeyPatch) -> None:

    monkeypatch.setattr(sys, "argv", ["msks"])
    source = Path(cli.__file__).read_text()
    # Executing the source in a fresh namespace avoids runpy's
    # already-imported RuntimeWarning while still running the module
    # entry block. __package__ lets the exec'd relative import resolve.
    with pytest.raises(SystemExit):
        exec(
            compile(source, cli.__file__, "exec"),
            {"__name__": "__main__", "__package__": "msks.client"},
        )


def stub_login_user(monkeypatch: pytest.MonkeyPatch, user: str | None) -> None:
    """Pin the workspace-row fetch behind the console's default-user
    resolution (#248): ``user`` is the row's login_user (None for a
    pre-#248 row, which must fall back to the legacy user)."""

    async def fake_row(workspace_id, url, token, ssl_ctx=None, transport=None):
        return {"id": workspace_id, "status": "running", "login_user": user}

    monkeypatch.setattr(console, "workspace_row", fake_row)


def _closed(code: int, reason: str = ""):

    close = console.websockets.Close(code, reason)
    return console.websockets.ConnectionClosed(close, None)


def test_report_close_4400() -> None:
    closed = _closed(4400, "console user 'x' is not served")
    with pytest.raises(SystemExit) as caught:
        console._report_close(closed)
    assert "console refused" in str(caught.value)
    assert "'x'" in str(caught.value)


def test_report_close_4401() -> None:

    with pytest.raises(SystemExit, match="authentication failed"):
        console._report_close(_closed(4401))


def test_report_close_carries_reason() -> None:

    with pytest.raises(SystemExit, match="workspace stopped"):
        console._report_close(_closed(4501, "workspace stopped"))


def test_report_close_4501_keeps_its_hint_only_without_a_reason() -> None:
    """A reason-bearing 4501 names the cause alone; the generic
    is-the-workspace-running hint would read as the cause itself
    beside a specific refusal (#248 review)."""
    with pytest.raises(SystemExit) as bare:
        console._report_close(_closed(4501))
    assert "is the workspace running" in str(bare.value)
    with pytest.raises(SystemExit) as named:
        console._report_close(_closed(4501, "console refused user 'sync'"))
    line = str(named.value)
    assert "is the workspace running" not in line
    assert "refused user 'sync'" in line


def test_report_close_4502_names_the_stall() -> None:

    with pytest.raises(SystemExit, match="console stalled"):
        console._report_close(_closed(4502))


def test_report_close_clean_end_is_quiet() -> None:

    assert console._report_close(_closed(1000)) is None


def test_stdin_pipe_passthrough() -> None:

    pipe = console._StdinPipe(sys.stdin)
    assert pipe.close() is None
    assert pipe.readable() is True


async def test_run_shell_unreachable_daemon_one_liner() -> None:

    class RefusingConnect:
        def __call__(self, address, ssl=None, max_size=None):
            return self

        def __await__(self):
            raise OSError(111, "Connection refused")

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(console.websockets, "connect", RefusingConnect())
        with pytest.raises(SystemExit, match="cannot reach"):
            await console.run_shell("wid", "https://nope:1", "t", None)


async def test_connect_plain_ws_takes_no_ssl() -> None:

    stub = ConnectStub(FakeWs())
    console._connect("ws://plain/", None)
    # The ssl argument is only recorded through the stub's __call__.
    assert stub.recorded_ssl == "unset"
    stub("ws://plain/", ssl=None)
    assert stub.recorded_ssl is None
    stub("wss://secure/", ssl="ctx")
    assert stub.recorded_ssl == "ctx"


def test_run_workspace_shell_preflights_boot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The console command boots a not-running workspace before going raw."""

    seen = {}

    async def fake_ensure(workspace_id, url, token, ssl_ctx=None):
        seen.update(
            workspace_id=workspace_id, url=url, token=token, ssl=ssl_ctx
        )

    monkeypatch.setattr(sys, "stdin", FdOnly())
    monkeypatch.setenv("MSKSC_TOKEN", "t")
    monkeypatch.setenv("MSKSC_URL", "u")
    monkeypatch.setattr(console, "require_tty", lambda: None)
    monkeypatch.setattr(console, "ensure_running", fake_ensure)
    stub_login_user(monkeypatch, "root")
    monkeypatch.setattr(console, "ssl_context", lambda: "ctx")
    monkeypatch.setattr(
        console.termios,
        "tcgetattr",
        lambda fd: (_ for _ in ()).throw(termios.error()),
    )

    async def fake_run(
        wid, url, token, ssl_ctx, user="root", size=None, term=None
    ):
        return 0

    monkeypatch.setattr(console, "run_shell", fake_run)
    assert console.run_workspace_shell("wid") == 0
    assert seen == {
        "workspace_id": "wid",
        "url": "u",
        "token": "t",
        "ssl": "ctx",
    }


# --- the default login user (#248) ---


def test_console_login_user_reads_the_row(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The console default is the workspace's recorded login user —
    the same name ``msks ssh`` logs in as, read from the same row
    the daemon serves the identity fetch from."""
    stub_login_user(monkeypatch, "alice")
    assert console.console_login_user("wid", "https://d", "tok", None) == (
        "alice"
    )


def test_console_login_user_falls_back_to_the_legacy_user(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A row without a login user (a pre-#248 workspace) keeps the
    image's own account — and a daemon predating the field serves
    no key at all, answered the same way."""

    async def row_without_field(workspace_id, url, token, ssl_ctx=None):
        return {"id": workspace_id, "status": "running"}

    monkeypatch.setattr(console, "workspace_row", row_without_field)
    assert (
        console.console_login_user("wid", "https://d", "tok", None) == "msks"
    )


def test_run_workspace_shell_defaults_to_the_recorded_user(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No --user: the session opens as the workspace's login user —
    root stays the explicit recovery shell."""
    monkeypatch.setattr(sys, "stdin", FdOnly())
    monkeypatch.setenv("MSKSC_TOKEN", "t")
    monkeypatch.setattr(console, "require_tty", lambda: None)
    monkeypatch.setattr(console, "ensure_running", async_noop)
    stub_login_user(monkeypatch, "alice")
    monkeypatch.setattr(console, "ssl_context", lambda: "ctx")
    monkeypatch.setattr(
        console.termios,
        "tcgetattr",
        lambda fd: (_ for _ in ()).throw(termios.error()),
    )
    seen: dict = {}

    async def fake_run(
        wid, url, token, ssl_ctx, user=None, size=None, term=None
    ):
        seen["user"] = user
        return 0

    monkeypatch.setattr(console, "run_shell", fake_run)
    assert console.run_workspace_shell("wid") == 0
    assert seen["user"] == "alice"


def test_run_workspace_shell_keeps_an_explicit_user(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """--user root skips the resolution entirely: the recovery
    shell never waits on a row fetch."""
    monkeypatch.setattr(sys, "stdin", FdOnly())
    monkeypatch.setenv("MSKSC_TOKEN", "t")
    monkeypatch.setattr(console, "require_tty", lambda: None)
    monkeypatch.setattr(console, "ensure_running", async_noop)

    def no_fetch(*args, **kwargs):
        raise AssertionError("an explicit user resolves nothing")

    monkeypatch.setattr(console, "console_login_user", no_fetch)
    monkeypatch.setattr(console, "ssl_context", lambda: "ctx")
    monkeypatch.setattr(
        console.termios,
        "tcgetattr",
        lambda fd: (_ for _ in ()).throw(termios.error()),
    )
    seen: dict = {}

    async def fake_run(
        wid, url, token, ssl_ctx, user=None, size=None, term=None
    ):
        seen["user"] = user
        return 0

    monkeypatch.setattr(console, "run_shell", fake_run)
    assert console.run_workspace_shell("wid", "root") == 0
    assert seen["user"] == "root"
