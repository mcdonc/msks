"""The forward websocket (#109): auth, lookup, refusal reasons, the
byte bridge, and the forward events — against a seam whose dialer
serves a real listener."""

import asyncio
import time

import pytest
from fastapi import WebSocketDisconnect
from fastapi.testclient import TestClient
from msks.app import build_app
from msks.microvm import MicrovmError
from msks.server import api as api_module
from msks.server.api import (
    bearer_token,
    bridge_console,
    build_api,
    forward_allowed,
    forward_port,
    pump_streams,
)
from msks.server.events import EventHub
from msks.settings import NetSettings, ServerSettings, Settings
from test_api import TOKEN, StubMicrovm, auth
from test_console_api import _SilentWriter, _StallSocket


class ForwardNet:
    """The net seam for the forward route: a lazily-started echo
    listener dialed in the app's own loop (one websocket session per
    forward_stream call, recorded). ``close_after_echo`` ends each
    connection after its first answer, so a test can end the session
    from the guest side deterministically."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, int]] = []
        self.tracked: list[tuple] = []
        self.untracked: list[tuple] = []
        self.refusal: str | None = None
        self.close_after_echo = False
        self._server: asyncio.AbstractServer | None = None

    async def _echo(self, reader, writer) -> None:
        while True:
            data = await reader.read(4096)
            if not data:
                break
            writer.write(data.upper())
            await writer.drain()
            if self.close_after_echo:
                break
        writer.close()

    async def forward_stream(self, workspace_id: str, port: int):
        self.calls.append((workspace_id, port))
        if self.refusal is not None:
            raise MicrovmError(self.refusal)
        if self._server is None:
            self._server = await asyncio.start_server(
                self._echo, "127.0.0.1", 0
            )
        address = self._server.sockets[0].getsockname()
        return await asyncio.open_connection(*address)

    def track_forward(self, workspace_id: str, writer) -> None:
        self.tracked.append((workspace_id, writer))

    def untrack_forward(self, workspace_id: str, writer) -> None:
        self.untracked.append((workspace_id, writer))

    async def start(self) -> None:
        return None  # the stub arms nothing (the settings say disabled)

    async def stop(self) -> None:
        # The lifespan calls this (app.state.net is this stub); the
        # listener lives in the same loop there, so the close is
        # ordinary server teardown.
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()


@pytest.fixture
def forward_api(tmp_path):
    settings = Settings(
        net=NetSettings(enabled=False),
        server=ServerSettings(
            db_path=tmp_path / "ws.db",
            bootstrap_token=TOKEN,
            event_poll_s=0.05,
        ),
    )
    app = build_app(settings)
    app.state.microvm = StubMicrovm()
    net = ForwardNet()
    app.state.net = net
    api = build_api(app)
    yield api, app, net
    # The TestClient context exit runs the lifespan shutdown, which
    # stops the net seam (and its listener) in the portal's loop.


def bearer(token: str = TOKEN) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _make_workspace(client, egress: bool = True) -> str:
    body = {"id": "ws-f", "kernel": "/k", "rootfs": "/r", "egress": egress}
    response = client.post("/api/v1/workspaces", json=body, headers=auth())
    assert response.status_code == 201
    return "ws-f"


def test_forward_rejects_missing_token(forward_api) -> None:
    api, app, net = forward_api
    with TestClient(api) as client:
        _make_workspace(client)
        with client.websocket_connect(
            "/api/v1/workspaces/ws-f/forward/22"
        ) as s:
            with pytest.raises(WebSocketDisconnect) as caught:
                s.receive_text()
        assert caught.value.code == 4401


def test_forward_rejects_bad_token(forward_api) -> None:
    api, app, net = forward_api
    with TestClient(api) as client:
        _make_workspace(client)
        with client.websocket_connect(
            "/api/v1/workspaces/ws-f/forward/22", headers=bearer("nope")
        ) as s:
            with pytest.raises(WebSocketDisconnect) as caught:
                s.receive_text()
        assert caught.value.code == 4401


def test_forward_unknown_workspace_closes(forward_api) -> None:
    api, app, net = forward_api
    with TestClient(api) as client:
        with client.websocket_connect(
            "/api/v1/workspaces/nope/forward/22", headers=bearer()
        ) as s:
            with pytest.raises(WebSocketDisconnect) as caught:
                s.receive_text()
        assert caught.value.code == 4404


def test_forward_rejects_non_numeric_port(forward_api) -> None:
    api, app, net = forward_api
    with TestClient(api) as client:
        _make_workspace(client)
        with client.websocket_connect(
            "/api/v1/workspaces/ws-f/forward/notaport", headers=bearer()
        ) as s:
            with pytest.raises(WebSocketDisconnect) as caught:
                s.receive_text()
        assert caught.value.code == 4400
        assert "port must be an integer" in caught.value.reason


def test_forward_rejects_out_of_range_port(forward_api) -> None:
    api, app, net = forward_api
    with TestClient(api) as client:
        _make_workspace(client)
        with client.websocket_connect(
            "/api/v1/workspaces/ws-f/forward/70000", headers=bearer()
        ) as s:
            with pytest.raises(WebSocketDisconnect) as caught:
                s.receive_text()
        assert caught.value.code == 4400
        assert "out of range" in caught.value.reason


def test_forward_names_the_missing_nic(forward_api) -> None:
    api, app, net = forward_api
    with TestClient(api) as client:
        _make_workspace(client, egress=False)
        with client.websocket_connect(
            "/api/v1/workspaces/ws-f/forward/22", headers=bearer()
        ) as s:
            with pytest.raises(WebSocketDisconnect) as caught:
                s.receive_text()
        assert caught.value.code == 4501
        assert "no NIC" in caught.value.reason
    assert net.calls == []


def test_forward_seam_error_closes(forward_api) -> None:
    api, app, net = forward_api
    net.refusal = "workspace ws-f has no live network attachment"
    with TestClient(api) as client:
        _make_workspace(client)
        with client.websocket_connect(
            "/api/v1/workspaces/ws-f/forward/22", headers=bearer()
        ) as s:
            with pytest.raises(WebSocketDisconnect) as caught:
                s.receive_text()
        assert caught.value.code == 4501
        assert "no live network attachment" in caught.value.reason
    assert net.calls == [("ws-f", 22)]


def test_forward_ignores_query_string_tokens(forward_api) -> None:
    # The token travels in the Authorization header only (#109): a
    # query-string token authenticates nothing, so it cannot leak
    # into logs as a habit that works.
    api, app, net = forward_api
    with TestClient(api) as client:
        _make_workspace(client)
        with client.websocket_connect(
            f"/api/v1/workspaces/ws-f/forward/22?token={TOKEN}"
        ) as s:
            with pytest.raises(WebSocketDisconnect) as caught:
                s.receive_text()
        assert caught.value.code == 4401


def test_forward_bridges_bytes_both_ways(forward_api) -> None:
    api, app, net = forward_api
    with TestClient(api) as client:
        _make_workspace(client)
        with client.websocket_connect(
            "/api/v1/workspaces/ws-f/forward/22", headers=bearer()
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
    assert net.calls == [("ws-f", 22)]


def test_two_forwards_run_concurrently(forward_api) -> None:
    api, app, net = forward_api
    with TestClient(api) as client:
        _make_workspace(client)
        with (
            client.websocket_connect(
                "/api/v1/workspaces/ws-f/forward/22", headers=bearer()
            ) as one,
            client.websocket_connect(
                "/api/v1/workspaces/ws-f/forward/8022", headers=bearer()
            ) as two,
        ):
            one.send_bytes(b"first\n")
            two.send_bytes(b"second\n")
            assert one.receive_bytes() == b"FIRST\n"
            assert two.receive_bytes() == b"SECOND\n"
    # Each websocket dialed its own guest connection, port as named
    # (the two dials land in completion order, not entry order).
    assert sorted(net.calls) == [("ws-f", 22), ("ws-f", 8022)]


def test_forward_publishes_open_and_closed_events(
    forward_api, monkeypatch
) -> None:
    # A publish spy beats draining the events websocket: the route
    # awaits the hub, so the recording is synchronous with the
    # session — no drain loop racing watcher noise.
    api, app, net = forward_api
    published: list[tuple[str, dict]] = []
    original = EventHub.publish

    async def spy(hub, event_type, data):
        published.append((event_type, data))
        await original(hub, event_type, data)

    monkeypatch.setattr(EventHub, "publish", spy)
    closed = ("forward.closed", {"id": "ws-f", "port": 22})
    # The guest service hangs up after its answer: the session ends
    # from the stream side, so the route's close path (and its
    # forward.closed) runs before any client teardown — no race.
    net.close_after_echo = True
    with TestClient(api) as client:
        _make_workspace(client)
        with client.websocket_connect(
            "/api/v1/workspaces/ws-f/forward/22", headers=bearer()
        ) as socket:
            socket.send_bytes(b"ping\n")
            assert socket.receive_bytes() == b"PING\n"
        assert ("forward.opened", {"id": "ws-f", "port": 22}) in published
        deadline = time.monotonic() + 5
        while closed not in published and time.monotonic() < deadline:
            time.sleep(0.01)
        assert closed in published


def test_forward_port_parses_and_refuses() -> None:
    assert forward_port("22") == (22, None)
    assert "must be an integer" in forward_port("ssh")[1]
    assert "out of range" in forward_port("0")[1]
    assert "out of range" in forward_port("65536")[1]


def test_bearer_token_reads_the_authorization_header() -> None:
    class Headers:
        def __init__(self, value: str) -> None:
            self.value = value

        def get(self, name: str, default: str = "") -> str:
            return self.value if name == "authorization" else default

    class Socket:
        def __init__(self, value: str) -> None:
            self.headers = Headers(value)

    assert bearer_token(Socket("Bearer tok")) == "tok"
    assert (
        bearer_token(Socket("bearer tok")) == "tok"
    )  # scheme is case-insensitive
    assert bearer_token(Socket("Basic dXNlcg==")) is None
    assert bearer_token(Socket("")) is None


def test_forward_allowed_passes_every_port_today(forward_api) -> None:
    # The policy seam (#108): open in v1 — a token that reached the
    # dial already owns the root console.
    api, app, net = forward_api
    row = {"id": "ws-f", "egress": True}
    assert forward_allowed(app, row, 22) is None
    assert forward_allowed(app, row, 8022) is None


async def test_pump_streams_ends_on_stream_eof() -> None:
    class Socket:
        async def receive(self):
            await asyncio.sleep(3600)  # a client that never sends

        async def send_bytes(self, data: bytes) -> None:
            return

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

    async def on_input():
        raise AssertionError("a plain forward observes no traffic")

    await asyncio.wait_for(
        pump_streams(Socket(), reader, Writer(), on_input=on_input), 5
    )


def test_forward_refusal_from_the_policy_seam_closes_4403(
    forward_api, monkeypatch
) -> None:
    # The seam is open in v1 (#108); the route's refusal shape is
    # pinned here so port-scoped tokens land on a working branch.
    api, app, net = forward_api

    def scoped(_app, _row, _port):
        return "token does not reach port 22"

    monkeypatch.setattr(api_module, "forward_allowed", scoped)
    with TestClient(api) as client:
        _make_workspace(client)
        with client.websocket_connect(
            "/api/v1/workspaces/ws-f/forward/22", headers=bearer()
        ) as s:
            with pytest.raises(WebSocketDisconnect) as caught:
                s.receive_text()
        assert caught.value.code == 4403
        assert "does not reach" in caught.value.reason


async def test_bridge_console_stall_cancels_the_pump_tasks() -> None:
    """#103's contract, restated for the refactor: when the watchdog
    closes first, the pump's inner tasks end with it — nothing writes
    into a closed stream and no task outlives the bridge."""
    reader = asyncio.StreamReader()  # a silent guest: no echo, no EOF
    writer = _SilentWriter()
    socket = _StallSocket()
    await asyncio.wait_for(
        bridge_console(socket, reader, writer, stall_timeout_s=0.1), 5
    )
    # Let any surviving task a chance to misbehave, then require a
    # clean slate: no strays, no writes into the closed stream.
    for _ in range(10):
        await asyncio.sleep(0.01)
    assert writer.buffer == b"echo hi\n"
    strays = [
        task
        for task in asyncio.all_tasks()
        if task is not asyncio.current_task()
    ]
    assert strays == []


def test_forward_tracks_and_releases_its_stream(forward_api) -> None:
    api, app, net = forward_api
    with TestClient(api) as client:
        _make_workspace(client)
        with client.websocket_connect(
            "/api/v1/workspaces/ws-f/forward/22", headers=bearer()
        ) as socket:
            socket.send_bytes(b"ping\n")
            assert socket.receive_bytes() == b"PING\n"
        # The live stream was tracked (a stop/kill can end it) and
        # released when the session ended.
        assert len(net.tracked) == 1
        assert net.untracked == net.tracked
