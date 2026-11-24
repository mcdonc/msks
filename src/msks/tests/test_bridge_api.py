"""The bridge relay websocket (#1102): auth, lookup, the named
refusals, the establish outcome, and the agent-protocol framing —
against a scripted ssh seam, with the relay socket driven from
the test as the daemon's own ssh would drive it."""

import asyncio
import contextlib
import json
import shutil
import socket as pysocket
import tempfile
from pathlib import Path

import pytest
from fastapi import WebSocketDisconnect
from fastapi.testclient import TestClient
from msks.app import build_app
from msks.server.api import build_api
from msks.settings import NetSettings, ServerSettings, Settings, VmmSettings
from msks.spec.proxy import CLOSE, ESTABLISHED, OPEN, pack_frame
from test_api import TOKEN, StubMicrovm, auth
from test_bridge import PUBKEY, FakeSpawn

PUBKEY_LINE = (
    "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIMmJmF9wFOeNl1Z"
    "S7srFS88DLtk1kXD17noFG2jOUndu"
)


class AttachedNet:
    """The net seam: the attachment the bridge dials through."""

    def __init__(self) -> None:
        self.guest_ip: str | None = "172.31.0.2"

    def attachment(self, workspace_id: str):
        if self.guest_ip is None:
            return None
        return type(
            "Attachment",
            (),
            {"workspace_id": workspace_id, "guest_ip": self.guest_ip},
        )()

    async def start(self) -> None:
        return None  # the stub arms nothing (net is disabled)

    async def stop(self) -> None:
        return None


@pytest.fixture
def bridge_api(tmp_path: Path):
    # A short state root (the bridge sockets' AF_UNIX ceiling; the
    # xdist tmp dirs run past it).
    state = Path(tempfile.mkdtemp(prefix="mskb-"))
    settings = Settings(
        vmm=VmmSettings(state_dir=state),
        net=NetSettings(enabled=False),
        server=ServerSettings(
            db_path=tmp_path / "ws.db",
            bootstrap_token=TOKEN,
            event_poll_s=10.0,
        ),
    )
    app = build_app(settings)
    app.state.microvm = StubMicrovm()
    net = AttachedNet()
    app.state.net = net
    spawn = FakeSpawn(app.state.bridge, state)
    spawn.master = (False, 0, b"")
    app.state.bridge.spawn = spawn
    api = build_api(app)
    try:
        yield api, app, net, spawn, state
    finally:
        shutil.rmtree(state, ignore_errors=True)


def offer(token: str = TOKEN) -> dict:
    return auth(token)


def make_workspace(
    client, egress: bool = True, pubkey: str = PUBKEY_LINE
) -> str:
    body = {
        "id": "ws-b",
        "kernel": "/k",
        "rootfs": "/r",
        "egress": egress,
        "ssh_pubkey": pubkey,
    }
    response = client.post("/api/v1/workspaces", json=body, headers=offer())
    assert response.status_code == 201
    return response.json()["id"]


def test_bridge_rejects_missing_token(bridge_api) -> None:
    api, app, _net, _spawn, _tmp = bridge_api
    with TestClient(api) as client:
        with client.websocket_connect("/api/v1/workspaces/ws-b/bridge") as s:
            with pytest.raises(WebSocketDisconnect) as caught:
                s.receive_text()
        assert caught.value.code == 4401


def test_bridge_unknown_workspace_closes(bridge_api) -> None:
    api, app, _net, _spawn, _tmp = bridge_api
    with TestClient(api) as client:
        with client.websocket_connect(
            "/api/v1/workspaces/nope/bridge", headers=offer()
        ) as s:
            with pytest.raises(WebSocketDisconnect) as caught:
                s.receive_text()
        assert caught.value.code == 4404


def test_bridge_names_the_missing_nic(bridge_api) -> None:
    api, app, _net, _spawn, _tmp = bridge_api
    with TestClient(api) as client:
        make_workspace(client, egress=False)
        with client.websocket_connect(
            "/api/v1/workspaces/ws-b/bridge", headers=offer()
        ) as s:
            with pytest.raises(WebSocketDisconnect) as caught:
                s.receive_text()
        assert caught.value.code == 4501
        assert "no NIC" in caught.value.reason


def test_bridge_names_the_not_running_workspace(bridge_api) -> None:
    api, app, net, _spawn, _tmp = bridge_api
    net.guest_ip = None
    with TestClient(api) as client:
        make_workspace(client)
        with client.websocket_connect(
            "/api/v1/workspaces/ws-b/bridge", headers=offer()
        ) as s:
            with pytest.raises(WebSocketDisconnect) as caught:
                s.receive_text()
        assert caught.value.code == 4501
        assert "not running" in caught.value.reason


def test_bridge_establish_failure_closes_with_the_cause(bridge_api) -> None:
    api, app, _net, spawn, _tmp = bridge_api
    spawn.master = (False, 255, b"Permission denied (publickey).\n")
    with TestClient(api) as client:
        make_workspace(client)
        with client.websocket_connect(
            "/api/v1/workspaces/ws-b/bridge", headers=offer()
        ) as s:
            with pytest.raises(WebSocketDisconnect) as caught:
                s.receive_text()
        assert caught.value.code == 4502
        assert "Permission denied" in caught.value.reason


def test_bridge_refuses_a_second_relay(bridge_api) -> None:
    api, app, _net, _spawn, _tmp = bridge_api
    with TestClient(api) as client:
        wid = make_workspace(client)
        app.state.bridge.claim_relay(wid)
        with client.websocket_connect(
            f"/api/v1/workspaces/{wid}/bridge", headers=offer()
        ) as s:
            with pytest.raises(WebSocketDisconnect) as caught:
                s.receive_text()
        assert caught.value.code == 4501
        assert "already serving" in caught.value.reason
    # The refused relay never took the slot: the caller's claim
    # stands, and giving it back frees the next one.
    app.state.bridge.release_relay(wid)
    assert app.state.bridge.claim_relay(wid)


def test_bridge_serves_the_agent_relay(bridge_api) -> None:
    """The relay end to end: establish, one agent connection, its
    bytes framed both ways by connection id, and the sweep at the
    websocket's end."""
    api, app, _net, spawn, tmp_path = bridge_api
    with TestClient(api) as client:
        wid = make_workspace(client, pubkey=PUBKEY)
        relay_path = app.state.bridge.paths(wid)["relay"]
        with client.websocket_connect(
            f"/api/v1/workspaces/{wid}/bridge", headers=offer()
        ) as s:
            assert s.receive_text() == json.dumps({"t": ESTABLISHED, "cid": 0})
            assert spawn.argvs and spawn.argvs[0][:2] == ["ssh", "-M"]
            written = app.state.bridge.paths(wid)["identity"]
            # The daemon re-annotates the stored line's comment
            # (#486); the key material is what the master offers.
            assert written.read_text().split()[:2] == PUBKEY.split()[:2]
            # The daemon's ssh connects to the relay socket; the
            # test plays it, from this thread, with a plain socket.
            agent = pysocket.socket(pysocket.AF_UNIX, pysocket.SOCK_STREAM)
            agent.settimeout(2.0)
            agent.connect(str(relay_path))
            try:
                assert json.loads(s.receive_text()) == {"t": OPEN, "cid": 0}
                request = b"\x00\x00\x00\x15\x0b\x00\x00\x00\x01stub-key"
                agent.sendall(request)
                assert s.receive_bytes() == pack_frame(0, request)
                answer = b"\x00\x00\x00\x0b\x0cstub-answer"
                s.send_bytes(pack_frame(0, answer))
                assert agent.recv(64) == answer
                agent.close()
                assert json.loads(s.receive_text()) == {
                    "t": CLOSE,
                    "cid": 0,
                }
            finally:
                with contextlib.suppress(OSError):
                    agent.close()
        assert not relay_path.exists()


def test_bridge_already_established_skips_the_spawn(bridge_api) -> None:
    api, app, _net, spawn, _tmp = bridge_api
    with TestClient(api) as client:
        wid = make_workspace(client)
        paths = app.state.bridge.paths(wid)
        paths["dir"].mkdir(parents=True, exist_ok=True)
        paths["ctl"].touch()
        with client.websocket_connect(
            f"/api/v1/workspaces/{wid}/bridge", headers=offer()
        ) as s:
            assert json.loads(s.receive_text())["t"] == ESTABLISHED
    assert spawn.argvs == []


def test_bridge_relay_skips_frames_it_does_not_know(bridge_api) -> None:
    """Every malformed relay frame passes through without an
    answer: not-JSON text, a JSON non-object, a non-integer cid, a
    binary frame short of its id, an unknown id, and a close for a
    connection that never stood."""
    api, app, _net, _spawn, _state = bridge_api
    with TestClient(api) as client:
        wid = make_workspace(client)
        with client.websocket_connect(
            f"/api/v1/workspaces/{wid}/bridge", headers=offer()
        ) as s:
            assert json.loads(s.receive_text())["t"] == ESTABLISHED
            s.send_text("not json")
            s.send_text("[1,2]")
            s.send_text('{"t":"open","cid":"x"}')
            s.send_bytes(b"\x00\x01")
            s.send_bytes(b"\x00\x00\x00\x09rest")
            s.send_text('{"t":"close","cid":9}')
    assert not app.state.bridge.paths(wid)["relay"].exists()


def test_bridge_relay_sweeps_when_the_websocket_ends_first(
    bridge_api,
) -> None:
    """A live agent connection outlives the websocket's end by one
    beat: the handler's send fails into its sweep, the connection
    closes, and the relay socket goes away."""
    api, app, _net, _spawn, _state = bridge_api
    with TestClient(api) as client:
        wid = make_workspace(client)
        relay_path = app.state.bridge.paths(wid)["relay"]
        with client.websocket_connect(
            f"/api/v1/workspaces/{wid}/bridge", headers=offer()
        ) as s:
            assert json.loads(s.receive_text())["t"] == ESTABLISHED
            agent = pysocket.socket(pysocket.AF_UNIX, pysocket.SOCK_STREAM)
            agent.settimeout(2.0)
            agent.connect(str(relay_path))
            try:
                assert json.loads(s.receive_text()) == {"t": OPEN, "cid": 0}
                # The websocket ends with the agent connection live:
                # the context exit closes it here.
            finally:
                with contextlib.suppress(OSError):
                    agent.close()
        assert not relay_path.exists()


async def test_relay_text_without_a_frame_is_nothing() -> None:
    """The None-text branch (a binary-only receive shape) is a
    no-op, not an error."""
    from msks.server.api.bridge import Relay, relay_text

    relay = Relay(None, "/nonexistent")
    await relay_text(relay, None)
    await relay_text(relay, '{"t":"close","cid":1}')


def test_bridge_names_a_state_path_past_the_ceiling(
    bridge_api, monkeypatch
) -> None:
    """A state root whose bridge socket path cannot exist answers
    the dial with its own named refusal (#1102's paths check)."""
    from msks.bridge import BridgeError

    api, app, _net, _spawn, _state = bridge_api

    def refusing(workspace_id):
        raise BridgeError(
            "bridge socket path /x is too long; use a shorter one"
        )

    monkeypatch.setattr(app.state.bridge, "paths", refusing)
    with TestClient(api) as client:
        wid = make_workspace(client)
        with client.websocket_connect(
            f"/api/v1/workspaces/{wid}/bridge", headers=offer()
        ) as s:
            with pytest.raises(WebSocketDisconnect) as caught:
                s.receive_text()
        assert caught.value.code == 4501
        assert "too long" in caught.value.reason


async def test_relay_sweep_without_a_server_still_closes_connections() -> None:
    """A relay whose socket already went still ends its agent
    connections: the sweep's second half owns them alone."""
    from msks.server.api.bridge import Relay

    relay = Relay(None, "/nonexistent")
    closed = []

    class Conn:
        def close(self):
            closed.append(True)

    relay.connections[3] = (None, Conn())
    await relay.sweep()
    assert closed == [True] and relay.connections == {}


async def test_relay_handler_sweeps_a_dead_websocket() -> None:
    """A handler whose websocket dies mid-connection ends through
    its own sweep: the exception lands, the writer closes."""
    from msks.server.api.bridge import Relay

    class DeadSocket:
        async def send_text(self, message):
            raise RuntimeError("websocket went")

        async def send_bytes(self, message):
            raise RuntimeError("websocket went")

    reader = asyncio.StreamReader()
    relay = Relay(DeadSocket(), "/nonexistent")
    writer = asyncio.StreamWriter(
        _Nowhere(), asyncio.Protocol(), reader, asyncio.get_running_loop()
    )
    reader.feed_data(b"x")
    reader.feed_eof()
    await relay.handler(reader, writer)
    assert writer.is_closing()


class _Nowhere:
    """A transport stand-in the StreamWriter accepts, closing like
    a real one."""

    closing = False

    def close(self):
        self.closing = True

    def is_closing(self):
        return self.closing

    def write(self, data):
        return None

    def flush(self):
        return None

    def get_extra_info(self, name):
        return None


async def test_relay_handler_hands_back_a_cancellation() -> None:
    """A handler cancelled mid-read re-raises the cancellation (it
    is control flow, not an error to swallow) and still closes its
    agent connection on the way out."""
    from msks.server.api.bridge import Relay

    class OpenSocket:
        async def send_text(self, message):
            return None

        async def send_bytes(self, message):
            return None

    relay = Relay(OpenSocket(), "/nonexistent")
    reader = asyncio.StreamReader()
    writer = asyncio.StreamWriter(
        _Nowhere(), asyncio.Protocol(), reader, asyncio.get_running_loop()
    )
    task = asyncio.create_task(relay.handler(reader, writer))
    await asyncio.sleep(0.05)  # parked in its read
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert writer.is_closing()
