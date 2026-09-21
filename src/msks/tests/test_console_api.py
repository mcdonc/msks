"""The console websocket: auth, lookup, and the byte bridge (#21)."""

import asyncio
import json

import pytest
from fastapi import WebSocketDisconnect
from fastapi.testclient import TestClient
from msks.app import build_app
from msks.microvm import MicrovmError
from msks.server.api import _RefusalScan, bridge_console, build_api
from msks.settings import NetSettings, ServerSettings, Settings
from test_api import TOKEN, StubMicrovm, auth

from msks import imagestore


class ConsoleStub(StubMicrovm):
    """A seam whose console() opens a real unix socket pair backend."""

    def __init__(self, tmp_path) -> None:
        super().__init__()
        self._tmp_path = tmp_path
        self.refusals: set[str] = set()
        self.console_calls: list[tuple[str, str | None, int, int]] = []

    async def console(
        self,
        workspace_id: str,
        user: str | None = None,
        rows: int = 0,
        cols: int = 0,
        term: str = "xterm",
    ):
        self.console_calls.append((workspace_id, user, rows, cols, term))
        if workspace_id in self.refusals:
            raise MicrovmError("no live vsock socket")
        path = self._tmp_path / f"{workspace_id}.sock"

        async def echo(reader, writer):
            while True:
                data = await reader.read(4096)
                if not data:
                    break
                writer.write(data.upper())
                await writer.drain()
            writer.close()

        if not hasattr(self, "_servers"):
            self._servers = {}
        server = await asyncio.start_unix_server(echo, str(path))
        self._servers[workspace_id] = server
        return await asyncio.open_unix_connection(str(path))

    async def stop(self) -> None:
        for server in getattr(self, "_servers", {}).values():
            server.close()
            await server.wait_closed()


@pytest.fixture
def console_api(tmp_path):
    settings = Settings(
        net=NetSettings(enabled=False),
        server=ServerSettings(
            db_path=tmp_path / "ws.db",
            bootstrap_token=TOKEN,
            event_poll_s=0.05,
        ),
    )
    app = build_app(settings)
    stub = ConsoleStub(tmp_path)
    app.state.microvm = stub
    api = build_api(app)
    yield api, app, stub


def _make_workspace(client) -> str:
    response = client.post(
        "/api/v1/workspaces",
        json={"id": "ws-c", "kernel": "/k", "rootfs": "/r"},
        headers=auth(),
    )
    assert response.status_code == 201
    return "ws-c"


def test_console_rejects_bad_token(console_api) -> None:
    api, app, stub = console_api
    with TestClient(api) as client:
        _make_workspace(client)
        with client.websocket_connect(
            "/api/v1/workspaces/ws-c/console?token=x"
        ) as s:
            with pytest.raises(WebSocketDisconnect) as caught:
                s.receive_text()
        assert caught.value.code == 4401


def test_console_unknown_workspace_closes(console_api) -> None:
    api, app, stub = console_api
    with TestClient(api) as client:
        with client.websocket_connect(
            f"/api/v1/workspaces/nope/console?token={TOKEN}"
        ) as s:
            with pytest.raises(WebSocketDisconnect) as caught:
                s.receive_text()
        assert caught.value.code == 4404


def test_console_seam_error_closes(console_api) -> None:
    api, app, stub = console_api
    with TestClient(api) as client:
        _make_workspace(client)
        stub.refusals.add("ws-c")
        with client.websocket_connect(
            f"/api/v1/workspaces/ws-c/console?token={TOKEN}"
        ) as s:
            with pytest.raises(WebSocketDisconnect) as caught:
                s.receive_text()
        assert caught.value.code == 4501


def test_console_bridges_bytes_both_ways(console_api) -> None:
    api, app, stub = console_api
    with TestClient(api) as client:
        _make_workspace(client)
        with client.websocket_connect(
            f"/api/v1/workspaces/ws-c/console?token={TOKEN}"
        ) as socket:
            socket.send_text(
                ""
            )  # an empty text frame must not break the bridge
            socket.send_bytes(b"hello ")
            socket.send_bytes(b"world\n")
            got = b""
            while b"WORLD" not in got:
                got += socket.receive_bytes()
            assert got == b"HELLO WORLD\n"
        # After detach the seam-side writer is closed; the echo server
        # for this workspace is torn down by the fixture.


async def test_bridge_ends_when_guest_eof(tmp_path) -> None:
    class Socket:
        sent: list[bytes] = []
        closed: tuple[int, str] | None = None

        async def receive(self):
            # A client that never sends: the guest EOF must end it.
            await asyncio.sleep(3600)

        async def send_bytes(self, data: bytes) -> None:
            self.sent.append(data)

        async def close(self, code: int = 1000, reason: str = "") -> None:
            self.closed = (code, reason)

    reader = asyncio.StreamReader()
    reader.feed_data(b"tail\n")
    reader.feed_eof()

    class Writer:
        closed = False
        buffer = b""

        def write(self, data):
            self.buffer += data

        async def drain(self):
            return

        def close(self):
            self.closed = True

        async def wait_closed(self):
            return

    await asyncio.wait_for(bridge_console(Socket(), reader, Writer()), 5)


async def test_bridge_closes_cleanly_on_guest_eof() -> None:
    """#217: a guest stream that ends (shell exit, helper shutdown)
    closes the websocket with 1000 — protocol-clean, not transport
    death with no close frame."""
    socket = _StallSocket()
    reader = asyncio.StreamReader()
    reader.feed_data(b"logout\n")
    reader.feed_eof()
    await asyncio.wait_for(bridge_console(socket, reader, _SilentWriter()), 5)
    assert socket.closed is not None
    code, reason = socket.closed
    assert code == 1000, reason
    assert "console closed" in reason


class _RefusalSocket:
    """A websocket fake that never disconnects: the refusal close
    must come from the scan, not a side effect of the client going."""

    def __init__(self) -> None:
        self.closed: tuple[int, str] | None = None

    async def receive(self):
        await asyncio.sleep(3600)

    async def send_bytes(self, data: bytes) -> None:
        return

    async def close(self, code: int = 1000, reason: str = "") -> None:
        self.closed = (code, reason)


async def test_bridge_closes_with_named_code_on_auth_refusal() -> None:
    """#217: the #123 refusal line closes the websocket with 4403
    and the guest's text as the reason — even when the stream stays
    open (the scan wakes the bridge on its own)."""
    socket = _RefusalSocket()
    reader = asyncio.StreamReader()
    reader.feed_data(b"banner\r\nMSKS ERR auth\r\n")
    # Deliberately NO feed_eof: the close must not wait on the
    # guest's exit.
    await asyncio.wait_for(bridge_console(socket, reader, _SilentWriter()), 5)
    assert socket.closed is not None
    code, reason = socket.closed
    assert code == 4403, reason
    assert "MSKS ERR auth" in reason


async def test_refusal_scan_distrusts_buffer_start_after_slide() -> None:
    """Once the bounded tail slides, the buffer's first byte is a
    mid-stream position: a refusal line arriving WITHOUT a leading
    newline (cut exactly at a chunk boundary) does not read as a
    line start."""
    scan = _RefusalScan()
    scan.feed(b"x" * 300)  # the tail slid; the stream start is gone
    assert scan._at_start is False
    scan.feed(b"MSKS ERR auth\r\n")
    assert scan.text is None
    assert not scan.matched.is_set()


async def test_refusal_scan_stands_down_after_auth_ok() -> None:
    """The gate: once the helper says AUTH OK, later `MSKS ERR` text
    in shell output (a log catted, journalctl) is not a refusal."""
    scan = _RefusalScan()
    scan.feed(b"AUTH OK\r\n")
    scan.feed(b"$ cat helper.log\r\nMSKS ERR auth\r\n")
    assert scan.text is None
    assert not scan.matched.is_set()


async def test_bridge_stays_open_after_auth_ok_despite_err_text() -> None:
    """The same gate end-to-end: a logged refusal line AFTER auth is
    shell output; the session keeps its stream (EOF ends it, with
    the normal 1000 — not the 4403 refusal)."""
    socket = _RefusalSocket()
    reader = asyncio.StreamReader()
    reader.feed_data(b"AUTH OK\r\nMSKS ERR auth\r\n")
    reader.feed_eof()
    await asyncio.wait_for(bridge_console(socket, reader, _SilentWriter()), 5)
    assert socket.closed is not None
    assert socket.closed[0] == 1000, socket.closed


async def test_refusal_scan_ignores_bytes_after_the_match() -> None:
    """Post-match chunks are dropped: the scan records the refusal
    once and never re-arms (the bridge closes on the first one)."""
    scan = _RefusalScan()
    scan.feed(b"MSKS ERR auth\r\n")
    assert scan.text == "MSKS ERR auth"
    scan.feed(b"more bytes\r\n")
    assert scan.text == "MSKS ERR auth"


async def test_bridge_refusal_scan_spans_chunk_splits() -> None:
    """The refusal line can arrive split across relayed reads; the
    scan's tail buffer must reassemble it."""
    socket = _RefusalSocket()
    reader = asyncio.StreamReader()
    reader.feed_data(b"junk\r\nMSKS E")
    reader.feed_data(b"RR auth\r\n")
    await asyncio.wait_for(bridge_console(socket, reader, _SilentWriter()), 5)
    assert socket.closed is not None
    code, reason = socket.closed
    assert code == 4403, reason
    assert "MSKS ERR auth" in reason


class _StallSocket:
    """A websocket fake: one input message, then silence forever."""

    def __init__(self) -> None:
        self.closed: tuple[int, str] | None = None
        self.first = True

    async def receive(self):
        if self.first:
            self.first = False
            return {"type": "websocket.receive", "bytes": b"echo hi\n"}
        await asyncio.sleep(3600)

    async def send_bytes(self, data: bytes) -> None:
        return

    async def close(self, code: int = 1000, reason: str = "") -> None:
        self.closed = (code, reason)


class _SilentWriter:
    """A vsock writer fake that accepts writes without a drain hang."""

    def __init__(self) -> None:
        self.buffer = b""

    def write(self, data):
        self.buffer += data

    async def drain(self):
        return

    def close(self):
        return

    async def wait_closed(self):
        return


async def test_bridge_closes_stalled_console_with_named_code() -> None:
    """#103: input the guest never echoed past the stall window closes
    the websocket with 4502 (a named failure) instead of hanging."""
    reader = asyncio.StreamReader()  # silent guest: no echo, no EOF
    writer = _SilentWriter()
    socket = _StallSocket()
    await asyncio.wait_for(
        bridge_console(socket, reader, writer, stall_timeout_s=0.1), 5
    )
    assert socket.closed is not None
    code, reason = socket.closed
    assert code == 4502, reason
    assert "stalled" in reason


async def test_bridge_answered_input_then_idle_stays_open() -> None:
    """The clock the echo resets must actually be reset: input that
    the guest answers, then an idle period LONGER than the window,
    keeps the session open (the review's regression case — the
    deadline is cleared, not re-armed by the answer)."""
    socket = _StallSocket()
    reader = asyncio.StreamReader()
    writer = _SilentWriter()

    async def answer_then_idle():
        # The echo lands early in a generous window: a loaded runner
        # delaying the feed must not flip the outcome.
        await asyncio.sleep(0.05)
        reader.feed_data(b"echo hi\nhi\n")
        # Idle past the whole window, twice over: no input, no EOF —
        # only a correctly disarmed clock keeps the session open.
        await asyncio.sleep(1.2)
        reader.feed_eof()

    race = asyncio.create_task(answer_then_idle())
    await asyncio.wait_for(
        bridge_console(socket, reader, writer, stall_timeout_s=0.5), 10
    )
    await race
    # The idle window passed without the stall close; the helper's
    # EOF afterwards ends the session cleanly (1000, #217) — what
    # must NOT happen is the 4502 stall close.
    assert socket.closed is None or socket.closed[0] == 1000, socket.closed


class _ChatteringSocket(_StallSocket):
    """A client that keeps sending: one input message every call."""

    def __init__(self, interval_s: float) -> None:
        super().__init__()
        self.interval_s = interval_s

    async def receive(self):
        await asyncio.sleep(self.interval_s)
        return {"type": "websocket.receive", "bytes": b"cmd\n"}


async def test_bridge_continuous_input_cannot_starve_the_watchdog() -> None:
    """The deadline is anchored at the FIRST unanswered input (#103,
    second review): a client sending every 50 ms into a silent stream
    must still be closed one window after that first input, not one
    window after its last send."""
    reader = asyncio.StreamReader()  # silent guest
    writer = _SilentWriter()
    socket = _ChatteringSocket(interval_s=0.05)
    loop = asyncio.get_running_loop()
    started = loop.time()
    await asyncio.wait_for(
        bridge_console(socket, reader, writer, stall_timeout_s=0.2), 5
    )
    elapsed = loop.time() - started
    assert socket.closed is not None, "continuous input starved the watchdog"
    assert socket.closed[0] == 4502
    # One window (plus scheduling slack) after the first input — far
    # short of what last-input anchoring would allow.
    assert elapsed < 0.8, (
        f"close took {elapsed:.2f}s; deadline not first-anchored"
    )


async def test_bridge_rearm_wakes_the_sleeping_watchdog() -> None:
    """The re-arm wake path (#103): a second input after an answered
    first re-arms the clock and must wake the watchdog mid-window
    (the changed-event return from the deadline sleep). The sleeps
    pin the interleaving: the watchdog parks on the first window
    before the echo disarms it, and the second input's arm lands
    while it sleeps."""
    reader = asyncio.StreamReader()
    writer = _SilentWriter()

    class TwoInputSocket(_StallSocket):
        async def receive(self):
            # A beat before each message: the watchdog reaches its
            # deadline sleep before the next arm.
            await asyncio.sleep(0.05)
            if self.first:
                self.first = False
                return {"type": "websocket.receive", "bytes": b"one\n"}
            await asyncio.sleep(0.05)
            return {"type": "websocket.receive", "bytes": b"two\n"}

    socket = TwoInputSocket()

    async def echo_between_inputs():
        # The answer to input one, disarming the clock while the
        # watchdog sleeps toward the first deadline.
        await asyncio.sleep(0.1)
        reader.feed_data(b"one\n")
        await asyncio.sleep(0.2)
        reader.feed_eof()

    race = asyncio.create_task(echo_between_inputs())
    await asyncio.wait_for(
        bridge_console(socket, reader, writer, stall_timeout_s=0.5), 10
    )
    await race
    # The subject is the re-arm wake; the race's trailing EOF ends
    # the session cleanly (1000, #217). What must not happen is the
    # 4502 stall close.
    assert socket.closed is None or socket.closed[0] == 1000, socket.closed


async def test_bridge_zero_stall_timeout_disables_the_watchdog() -> None:
    """The documented off switch: with the window at zero, input that
    draws no guest bytes ever still never closes the session."""
    socket = _StallSocket()
    reader = asyncio.StreamReader()
    writer = _SilentWriter()

    async def end_after_a_while():
        await asyncio.sleep(0.2)
        reader.feed_eof()

    race = asyncio.create_task(end_after_a_while())
    await asyncio.wait_for(
        bridge_console(socket, reader, writer, stall_timeout_s=0), 5
    )
    await race
    # The off switch held (no 4502); the race's EOF ends cleanly.
    assert socket.closed is None or socket.closed[0] == 1000, (
        "a zero window must disable the stall close"
    )


def _make_prelude_image(tmp_path, hash_name: str = "a" * 64) -> str:
    """A catalog image with console markers; returns its hash."""
    from msks import imagestore

    cache = imagestore.images_dir(tmp_path) / hash_name
    cache.mkdir(parents=True)
    for member in ("kernel", "initrd", "rootfs.ext4"):
        (cache / member).write_bytes(b"artifact")
    (cache / "image.json").write_text(
        json.dumps(
            {
                "schema": 2,
                "name": "debian",
                "version": "13.6",
                "cmdline": "console=ttyS0",
                "vsock_shell_port": 1023,
                "console_protocol": "prelude-v1",
                "console_users": ["root", "msks"],
            }
        )
    )
    return hash_name


def _make_prelude_workspace(
    client, tmp_path, workspace_id: str = "ws-p"
) -> None:
    digest = _make_prelude_image(tmp_path)
    response = client.post(
        "/api/v1/workspaces",
        json={"id": workspace_id, "image": "debian:13.6"},
        headers=auth(),
    )
    assert response.status_code == 201, response.text
    _ = digest


def test_console_default_user_root_legacy(console_api, tmp_path) -> None:
    api, app, stub = console_api
    with TestClient(api) as client:
        _make_workspace(client)
        with client.websocket_connect(
            f"/api/v1/workspaces/ws-c/console?token={TOKEN}"
        ) as socket:
            socket.send_bytes(b"hello")
            got = b""
            while b"HELLO" not in got:
                got += socket.receive_bytes()
    assert stub.console_calls == [("ws-c", None, 0, 0, "xterm")]


def test_console_unknown_user_closes_4400(console_api) -> None:
    api, app, stub = console_api
    with TestClient(api) as client:
        _make_workspace(client)
        with client.websocket_connect(
            f"/api/v1/workspaces/ws-c/console?token={TOKEN}&user=nobody"
        ) as s:
            with pytest.raises(WebSocketDisconnect) as caught:
                s.receive_text()
        assert caught.value.code == 4400
        assert "not served" in (caught.value.reason or "")


def test_console_bad_rows_closes_4400(console_api) -> None:
    api, app, stub = console_api
    with TestClient(api) as client:
        _make_workspace(client)
        for query in ("rows=abc", "rows=0", "cols=99999", "user=Bad.Name"):
            with client.websocket_connect(
                f"/api/v1/workspaces/ws-c/console?token={TOKEN}&{query}"
            ) as s:
                with pytest.raises(WebSocketDisconnect) as caught:
                    s.receive_text()
                assert caught.value.code == 4400, query
    assert stub.console_calls == []


def test_console_prelude_image_passes_user_and_size(
    console_api, tmp_path
) -> None:
    api, app, stub = console_api
    app.state.settings.vmm.state_dir = tmp_path
    with TestClient(api) as client:
        _make_prelude_workspace(client, tmp_path)
        with client.websocket_connect(
            f"/api/v1/workspaces/ws-p/console?token={TOKEN}&user=msks&rows=34&cols=120"
        ) as socket:
            socket.send_bytes(b"hello")
            got = b""
            while b"HELLO" not in got:
                got += socket.receive_bytes()
    assert stub.console_calls == [("ws-p", "msks", 34, 120, "xterm")]


def test_console_bad_term_closes_4400(console_api) -> None:
    api, app, stub = console_api
    with TestClient(api) as client:
        _make_workspace(client)
        for query in ("term=bad%20term", "term=" + "x" * 33):
            with client.websocket_connect(
                f"/api/v1/workspaces/ws-c/console?token={TOKEN}&{query}"
            ) as s:
                with pytest.raises(WebSocketDisconnect) as caught:
                    s.receive_text()
                assert caught.value.code == 4400, query
    assert stub.console_calls == []


def test_console_prelude_image_carries_term(console_api, tmp_path) -> None:
    api, app, stub = console_api
    app.state.settings.vmm.state_dir = tmp_path
    with TestClient(api) as client:
        _make_prelude_workspace(client, tmp_path)
        with client.websocket_connect(
            f"/api/v1/workspaces/ws-p/console?token={TOKEN}"
            f"&user=msks&term=tmux-256color"
        ) as socket:
            socket.send_bytes(b"hello")
            got = b""
            while b"HELLO" not in got:
                got += socket.receive_bytes()
    assert stub.console_calls == [("ws-p", "msks", 24, 80, "tmux-256color")]


def test_console_unreadable_image_record_closes_4501(
    console_api, tmp_path
) -> None:
    api, app, stub = console_api
    app.state.settings.vmm.state_dir = tmp_path
    with TestClient(api) as client:
        _make_prelude_workspace(client, tmp_path)
    # Corrupt the bound image's record out-of-band: the console must
    # refuse loudly, not silently downgrade to a legacy raw stream.
    digest = next(
        p.name for p in imagestore.images_dir(tmp_path).iterdir() if p.is_dir()
    )
    (imagestore.images_dir(tmp_path) / digest / "image.json").write_text(
        "{corrupt"
    )
    with TestClient(api) as client:
        with client.websocket_connect(
            f"/api/v1/workspaces/ws-p/console?token={TOKEN}"
        ) as s:
            with pytest.raises(WebSocketDisconnect) as caught:
                s.receive_text()
        assert caught.value.code == 4501
        assert "unreadable" in (caught.value.reason or "")
    assert stub.console_calls == []


def test_console_missing_image_record_closes_4501(
    console_api, tmp_path
) -> None:
    api, app, stub = console_api
    app.state.settings.vmm.state_dir = tmp_path
    with TestClient(api) as client:
        _make_prelude_workspace(client, tmp_path)
    # The bound image's record vanished (out-of-band deletion): the
    # console refuses loudly instead of silently downgrading.
    digest = next(
        p.name for p in imagestore.images_dir(tmp_path).iterdir() if p.is_dir()
    )
    (imagestore.images_dir(tmp_path) / digest / "image.json").unlink()
    with TestClient(api) as client:
        with client.websocket_connect(
            f"/api/v1/workspaces/ws-p/console?token={TOKEN}"
        ) as s:
            with pytest.raises(WebSocketDisconnect) as caught:
                s.receive_text()
        assert caught.value.code == 4501
        assert "unreadable" in (caught.value.reason or "")
    assert stub.console_calls == []


def test_console_prelude_image_default_size(console_api, tmp_path) -> None:
    api, app, stub = console_api
    app.state.settings.vmm.state_dir = tmp_path
    with TestClient(api) as client:
        _make_prelude_workspace(client, tmp_path)
        with client.websocket_connect(
            f"/api/v1/workspaces/ws-p/console?token={TOKEN}&user=root"
        ) as socket:
            socket.send_bytes(b"x")
            got = b""
            while b"X" not in got:
                got += socket.receive_bytes()
    assert stub.console_calls == [("ws-p", "root", 24, 80, "xterm")]
