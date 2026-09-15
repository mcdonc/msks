"""The console websocket: auth, lookup, and the byte bridge (#21)."""

import asyncio
import json

import pytest
from fastapi import WebSocketDisconnect
from fastapi.testclient import TestClient
from msks.app import build_app
from msks.microvm import MicrovmError
from msks.server.api import bridge_console, build_api
from msks.settings import ServerSettings, Settings
from test_api import TOKEN, StubMicrovm, auth


class ConsoleStub(StubMicrovm):
    """A seam whose console() opens a real unix socket pair backend."""

    def __init__(self, tmp_path) -> None:
        super().__init__()
        self._tmp_path = tmp_path
        self.refusals: set[str] = set()
        self.console_calls: list[tuple[str, str | None, int, int]] = []

    async def console(
        self, workspace_id: str, user: str | None = None, rows: int = 0, cols: int = 0
    ):
        self.console_calls.append((workspace_id, user, rows, cols))
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
        server=ServerSettings(
            db_path=tmp_path / "ws.db", bootstrap_token=TOKEN, event_poll_s=0.05
        )
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
        with client.websocket_connect("/api/v1/workspaces/ws-c/console?token=x") as s:
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
            socket.send_text("")  # an empty text frame must not break the bridge
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

        async def receive(self):
            # A client that never sends: the guest EOF must end it.
            await asyncio.sleep(3600)

        async def send_bytes(self, data: bytes) -> None:
            self.sent.append(data)

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


def _make_prelude_workspace(client, tmp_path, workspace_id: str = "ws-p") -> None:
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
    assert stub.console_calls == [("ws-c", None, 0, 0)]


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


def test_console_prelude_image_passes_user_and_size(console_api, tmp_path) -> None:
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
    assert stub.console_calls == [("ws-p", "msks", 34, 120)]


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
    assert stub.console_calls == [("ws-p", "root", 24, 80)]
