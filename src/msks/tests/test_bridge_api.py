"""The bridge websocket (#1102): auth, lookup, and the relayed
connection flow — the holder's side driven from the test as the
client's ssh would drive it, over a real bound proxy row."""

import json
import socket as pysocket
from pathlib import Path

import pytest
from fastapi import WebSocketDisconnect
from fastapi.testclient import TestClient
from msks.app import build_app
from msks.server.api import build_api
from msks.settings import NetSettings, ServerSettings, Settings, VmmSettings
from msks.spec.proxy import CLOSE, OPEN, pack_frame
from test_api import TOKEN, StubMicrovm, auth


@pytest.fixture
def bridge_api(tmp_path: Path):
    settings = Settings(
        vmm=VmmSettings(state_dir=tmp_path / "state"),
        net=NetSettings(enabled=False),
        server=ServerSettings(
            db_path=tmp_path / "ws.db",
            bootstrap_token=TOKEN,
            event_poll_s=10.0,
        ),
    )
    app = build_app(settings)
    app.state.microvm = StubMicrovm()
    api = build_api(app)
    yield api, app


def offer(token: str = TOKEN) -> dict:
    return auth(token)


def make_workspace(client, egress: bool = True) -> str:
    body = {"id": "ws-b", "kernel": "/k", "rootfs": "/r", "egress": egress}
    response = client.post("/api/v1/workspaces", json=body, headers=offer())
    assert response.status_code == 201
    return response.json()["id"]


def free_port() -> int:
    """A port the kernel confirms free (bound then released)."""
    sock = pysocket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


def add_forward_row(client, wid: str) -> int:
    """A stored forward row on a free port; the daemon binds its
    listener at once (the holder registers with the same
    websocket the row's status reads)."""
    port = free_port()
    response = client.post(
        f"/api/v1/workspaces/{wid}/proxies",
        json={
            "direction": "forward",
            "listen_port": port,
            "target_port": 8080,
        },
        headers=offer(),
    )
    assert response.status_code == 200
    return port


def awaits_up(app, wid: str, budget_s: float = 5.0) -> bool:
    """Poll the hub's fact until the route's registration lands."""
    import time

    deadline = time.monotonic() + budget_s
    while time.monotonic() < deadline:
        if app.state.bridge.up(wid):
            return True
        time.sleep(0.05)
    return False


def test_bridge_rejects_missing_token(bridge_api) -> None:
    api, app = bridge_api
    with TestClient(api) as client:
        make_workspace(client)
        with client.websocket_connect("/api/v1/workspaces/ws-b/bridge") as s:
            with pytest.raises(WebSocketDisconnect) as caught:
                s.receive_text()
        assert caught.value.code == 4401


def test_bridge_unknown_workspace_closes(bridge_api) -> None:
    api, app = bridge_api
    with TestClient(api) as client:
        with client.websocket_connect(
            "/api/v1/workspaces/nope/bridge", headers=offer()
        ) as s:
            with pytest.raises(WebSocketDisconnect) as caught:
                s.receive_text()
        assert caught.value.code == 4404


def test_bridge_names_the_missing_nic(bridge_api) -> None:
    api, app = bridge_api
    with TestClient(api) as client:
        make_workspace(client, egress=False)
        with client.websocket_connect(
            "/api/v1/workspaces/ws-b/bridge", headers=offer()
        ) as s:
            with pytest.raises(WebSocketDisconnect) as caught:
                s.receive_text()
        assert caught.value.code == 4501
        assert "no NIC" in caught.value.reason


def test_bridge_registration_is_the_rows_status(bridge_api) -> None:
    api, app = bridge_api
    with TestClient(api) as client:
        wid = make_workspace(client)
        assert app.state.bridge.up(wid) is False
        with client.websocket_connect(
            f"/api/v1/workspaces/{wid}/bridge", headers=offer()
        ):
            # The route registers at its own pace behind the
            # handshake; the fact flips within moments.
            assert awaits_up(app, wid) is True
        assert app.state.bridge.up(wid) is False


def test_a_rows_connection_relays_to_the_holder(bridge_api) -> None:
    """The end-to-end datapath: a TCP connection to the row's
    bound port arrives at the holder as an open, its bytes both
    ways by connection id, and the ends close together."""
    api, app = bridge_api
    with TestClient(api) as client:
        wid = make_workspace(client)
        port = free_port()
        with client.websocket_connect(
            f"/api/v1/workspaces/{wid}/bridge", headers=offer()
        ) as s:
            response = client.post(
                f"/api/v1/workspaces/{wid}/proxies",
                json={
                    "direction": "forward",
                    "listen_port": port,
                    "target_port": 8080,
                },
                headers=offer(),
            )
            assert response.status_code == 200
            peer = pysocket.create_connection(("127.0.0.1", port), timeout=5)
            peer.settimeout(5)
            try:
                opened = json.loads(s.receive_text())
                assert opened == {"t": OPEN, "cid": 0, "port": 8080}
                s.send_bytes(pack_frame(0, b"HELLO"))
                assert peer.recv(64) == b"HELLO"
                peer.sendall(b"WORLD")
                assert s.receive_bytes() == pack_frame(0, b"WORLD")
                peer.close()
                assert json.loads(s.receive_text()) == {
                    "t": CLOSE,
                    "cid": 0,
                }
            finally:
                peer.close()


def test_holder_close_ends_the_relayed_connection(bridge_api) -> None:
    """A close from the holder's side ends the row's accepted
    connection: the TCP peer sees EOF."""
    api, app = bridge_api
    with TestClient(api) as client:
        wid = make_workspace(client)
        port = free_port()
        with client.websocket_connect(
            f"/api/v1/workspaces/{wid}/bridge", headers=offer()
        ) as s:
            response = client.post(
                f"/api/v1/workspaces/{wid}/proxies",
                json={
                    "direction": "forward",
                    "listen_port": port,
                    "target_port": 8080,
                },
                headers=offer(),
            )
            assert response.status_code == 200
            peer = pysocket.create_connection(("127.0.0.1", port), timeout=5)
            peer.settimeout(5)
            try:
                assert json.loads(s.receive_text())["t"] == OPEN
                s.send_text(json.dumps({"t": CLOSE, "cid": 0}))
                assert peer.recv(64) == b""
            finally:
                peer.close()


async def test_dispatch_control_without_a_frame_is_nothing() -> None:
    """A binary-only receive shape dispatches nothing; a close
    text ends its relayed connection."""

    class Manager:
        def __init__(self):
            self.ended = []

        def end_channel(self, holder, cid):
            self.ended.append(cid)

    from msks.server.api.bridge import dispatch_control

    manager = Manager()
    await dispatch_control(manager, None, {"bytes": b"x"})  # no text
    await dispatch_control(manager, None, {"text": "not json"})
    await dispatch_control(
        manager, None, {"text": '{"t":"open","cid":1}'}
    )  # the client never sends opens: nothing
    await dispatch_control(manager, None, {"text": '{"t":"close","cid":4}'})
    assert manager.ended == [4]
