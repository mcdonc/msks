"""The console websocket: auth, lookup, and the byte bridge (#21)."""

import asyncio
import json

import pytest
from fastapi import WebSocketDisconnect
from fastapi.testclient import TestClient
from msks.app import build_app
from msks.microvm import MicrovmError
from msks.server.api import build_api
from msks.server.api.console import bridge_console
from msks.settings import NetSettings, ServerSettings, Settings
from test_api import TOKEN, StubMicrovm, auth


class ConsoleStub(StubMicrovm):
    """A seam whose console() opens a real unix socket pair backend."""

    def __init__(self, tmp_path) -> None:
        super().__init__()
        self._tmp_path = tmp_path
        self.refusals: set[str] = set()
        self.console_calls: list[str] = []

    async def console(self, workspace_id: str):
        self.console_calls.append(workspace_id)
        if workspace_id in self.refusals:
            raise MicrovmError("no live console socket")
        # The socket name shortens the workspace id: the daemon-minted
        # ids (#246) are 10 hex chars, and a deep xdist tmp dir plus
        # that name can push the AF_UNIX path past its 108-byte limit.
        path = self._tmp_path / f"{workspace_id[:8]}.sock"

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
    return response.json()["id"]  # the minted id (#246)


def test_console_rejects_bad_token(console_api) -> None:
    api, app, stub = console_api
    with TestClient(api) as client:
        _make_workspace(client)
        with client.websocket_connect(
            "/api/v1/workspaces/ws-c/console",
            headers=auth("x"),
        ) as s:
            with pytest.raises(WebSocketDisconnect) as caught:
                s.receive_text()
        assert caught.value.code == 4401


def test_console_unknown_workspace_closes(console_api) -> None:
    api, app, stub = console_api
    with TestClient(api) as client:
        with client.websocket_connect(
            "/api/v1/workspaces/nope/console",
            headers=auth(),
        ) as s:
            with pytest.raises(WebSocketDisconnect) as caught:
                s.receive_text()
        assert caught.value.code == 4404


def test_console_seam_error_closes(console_api) -> None:
    api, app, stub = console_api
    with TestClient(api) as client:
        wid = _make_workspace(client)
        stub.refusals.add(wid)
        with client.websocket_connect(
            "/api/v1/workspaces/ws-c/console",
            headers=auth(),
        ) as s:
            with pytest.raises(WebSocketDisconnect) as caught:
                s.receive_text()
        assert caught.value.code == 4501


def test_console_accepts_the_auth_header(console_api) -> None:
    # A valid Authorization header carries the session (#216). The
    # session exchanges one byte before it closes — every console
    # test that closes on a live bridge does the same, so the
    # endpoint's pump reaches its clean end instead of racing the
    # teardown.
    api, app, stub = console_api
    with TestClient(api) as client:
        _make_workspace(client)
        with client.websocket_connect(
            "/api/v1/workspaces/ws-c/console",
            headers=auth(),
        ) as socket:
            socket.send_bytes(b"x")
            got = b""
            while b"X" not in got:
                got += socket.receive_bytes()


def test_console_rejects_a_query_string_token(console_api) -> None:
    # A token in the URL authenticates nothing (#216): the query
    # string is not an auth channel, so the habit cannot work.
    api, app, stub = console_api
    with TestClient(api) as client:
        _make_workspace(client)
        with client.websocket_connect(
            f"/api/v1/workspaces/ws-c/console?token={TOKEN}"
        ) as s:
            with pytest.raises(WebSocketDisconnect) as caught:
                s.receive_text()
        assert caught.value.code == 4401


def test_console_rejects_a_non_bearer_scheme(console_api) -> None:
    # The Authorization header must name the Bearer scheme (#216):
    # anything else authenticates nothing.
    api, app, stub = console_api
    with TestClient(api) as client:
        _make_workspace(client)
        with client.websocket_connect(
            "/api/v1/workspaces/ws-c/console",
            headers={"Authorization": "Basic zzz"},
        ) as s:
            with pytest.raises(WebSocketDisconnect) as caught:
                s.receive_text()
        assert caught.value.code == 4401


def test_console_bridges_bytes_both_ways(console_api) -> None:
    api, app, stub = console_api
    with TestClient(api) as client:
        _make_workspace(client)
        with client.websocket_connect(
            "/api/v1/workspaces/ws-c/console",
            headers=auth(),
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
        await asyncio.sleep(0.5)
        reader.feed_eof()

    race = asyncio.create_task(answer_then_idle())
    await asyncio.wait_for(
        bridge_console(socket, reader, writer, stall_timeout_s=0.2), 10
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


def _make_console_image(tmp_path, hash_name: str = "a" * 64) -> str:
    """A catalog image with console-capable facts; returns its
    hash."""
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
            }
        )
    )
    return hash_name


def _make_console_workspace(
    client, tmp_path, workspace_id: str = "ws-p"
) -> str:
    digest = _make_console_image(tmp_path)
    response = client.post(
        "/api/v1/workspaces",
        json={"id": workspace_id, "image": "debian:13.6"},
        headers=auth(),
    )
    assert response.status_code == 201, response.text
    _ = digest
    return response.json()["id"]  # the minted id (#246)
