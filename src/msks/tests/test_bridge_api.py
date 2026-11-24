"""The bridge websocket (#1102): auth, lookup, and the relayed
connection flow — the holder's side driven from the test as the
client's ssh would drive it, over a real bound proxy row."""

import contextlib
import errno
import itertools
import json
import logging
import socket as pysocket
import threading
import time
from pathlib import Path

import anyio
import pytest
from fastapi import WebSocketDisconnect
from fastapi.testclient import TestClient
from msks.app import build_app
from msks.bridge import NO_HOLDER, BridgeHub
from msks.server.api import build_api
from msks.settings import NetSettings, ServerSettings, Settings, VmmSettings
from msks.spec.proxy import CLOSE, OPEN, pack_frame
from starlette.testclient import WebSocketTestSession
from test_api import TOKEN, StubMicrovm, auth

# How long one relayed frame gets on the holder's websocket: the
# peer socket's own timeout. A frame that does not arrive in it
# fails with the daemon's side of the relay described (#1131).
FRAME_WAIT_S = 5.0

# How many fresh ports a forward row's bind gets when the kernel's
# free port turns out taken by the time the daemon binds it (#1131).
BIND_ATTEMPTS = 3


@pytest.fixture
def bridge_api(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
):
    # The relay's refusals log at INFO (proxies.bridge_forward) and
    # pytest's capture keeps WARNING and up by default. The level
    # is set here so a frame a test awaits and never gets has the
    # daemon's refusal in the report beside it (#1131).
    caplog.set_level(logging.INFO, logger="msks.proxies")
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
    # The route's last act on a holder's departure is the hub's
    # publish_down, which reads the model. The session's own exit
    # cancels the route the moment it has sent the disconnect, and
    # the cancel scope it tears down aborts that read's session
    # close mid-flight: the connection stays checked out, and the GC
    # reports it (the SAWarning this suite keeps at zero). The
    # class-level wrapper marks the moment publish_down finished, for
    # held_bridge to wait on before the exit's cancel; patched on the
    # class so a call through the type wakes it too.
    hub = app.state.bridge
    settled = threading.Event()
    publish_down = BridgeHub.publish_down

    async def settled_publish_down(self: BridgeHub, workspace_id: str) -> None:
        await publish_down(self, workspace_id)
        self.settled.set()

    monkeypatch.setattr(BridgeHub, "publish_down", settled_publish_down)
    hub.settled = settled
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


def add_forward_row(client, wid: str) -> dict:
    """A stored forward row on a free port, bound and listening:
    the row as the create answers it. A free port is the kernel's
    answer to a bind released at once, so under xdist another
    worker's listener can take it before this row's bind does — the
    CI hang of #1131: the bind failed, the create still answered
    200, and the test's peer connected to the other listener with
    no OPEN ever coming. A taken port gets the row removed and a
    fresh one, BIND_ATTEMPTS times; any other cause, or the last
    attempt, fails here with the cause named."""
    reason = ""
    for _ in range(BIND_ATTEMPTS):
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
        assert response.status_code == 200, response.text
        row = response.json()
        if row["status"] == "listening":
            return row
        assert row["status"] == "failed", row
        reason = row["reason"] or ""
        removed = client.delete(
            f"/api/v1/workspaces/{wid}/proxies/{row['id']}", headers=offer()
        )
        assert removed.status_code == 200, removed.text
        if not reason.startswith(f"[Errno {errno.EADDRINUSE}]"):
            pytest.fail(
                f"the forward row's bind on port {port} failed: {reason}"
            )
    pytest.fail(
        f"the forward row's bind failed {BIND_ATTEMPTS} times on a "
        f"port something else held: {reason}"
    )


def connect_peer(row: dict):
    """The TCP peer, connected to the row's bound port: the kernel
    completes the handshake before the daemon's accept runs."""
    peer = pysocket.create_connection(
        ("127.0.0.1", row["listen_port"]), timeout=5
    )
    peer.settimeout(FRAME_WAIT_S)
    return peer


@contextlib.contextmanager
def held_bridge(client, app, wid: str):
    """The holder's websocket, open for the block. On exit the
    session ends with the route's teardown finished first: the
    disconnect goes ahead, the hub's publish_down runs to its end
    (the fixture's settled wrapper marks it), and only then does
    the session's own exit cancel the route — the cancel that
    would otherwise land inside publish_down's model read and
    leak its connection. This covers the holder's own departure,
    the one path that runs publish_down; a close the daemon
    starts (close_workspace) skips it and would trip the wait. The
    session's exit then sends a second disconnect behind the ended
    route: the buffered frame is dropped with the session."""
    settled: threading.Event = app.state.bridge.settled
    with client.websocket_connect(
        f"/api/v1/workspaces/{wid}/bridge", headers=offer()
    ) as s:
        try:
            yield s
        finally:
            settled.clear()
            s.close()
            assert settled.wait(FRAME_WAIT_S), (
                "the bridge route's teardown did not finish within "
                f"{FRAME_WAIT_S}s of the disconnect"
            )


def awaits_up(app, wid: str, budget_s: float = 5.0) -> bool:
    """Poll the hub's fact until the route's registration lands."""
    deadline = time.monotonic() + budget_s
    while time.monotonic() < deadline:
        if app.state.bridge.up(wid):
            return True
        time.sleep(0.05)
    return False


def receive_within(session: WebSocketTestSession, seconds: float) -> dict:
    """One receive on the holder's websocket under a deadline,
    raising a close like the plain receives do. The receive runs
    on the session's portal inside ``fail_after``, through the
    session's own receive stream (starlette keeps it private; its
    public receive is a portal call with no timeout, which a
    missing frame parks for the run)."""

    async def bounded() -> dict:
        with anyio.fail_after(seconds):
            message = await session._send_rx.receive()
        session._raise_on_close(message)
        return message

    return session.portal.call(bounded)


def peer_saw_eof(peer) -> bool:
    """Whether the daemon already ended the peer's connection,
    read without consuming what is queued (MSG_PEEK) and without
    blocking (MSG_DONTWAIT). A peer the test itself closed (EBADF)
    reads as not ended too."""
    try:
        return peer.recv(1, pysocket.MSG_PEEK | pysocket.MSG_DONTWAIT) == b""
    except OSError:
        return False


class RelayedConnection:
    """One relayed connection as the test drives it: the holder's
    websocket read within FRAME_WAIT_S, and the daemon's side of
    the relay read for a frame that did not arrive (#1131)."""

    def __init__(
        self,
        session,
        app,
        wid: str,
        row: dict,
        peer,
        budget_s: float = FRAME_WAIT_S,
    ) -> None:
        self.session = session
        self.app = app
        self.wid = wid
        self.row = row
        self.peer = peer
        self.budget_s = budget_s

    def text(self, what: str) -> str:
        return self.frame(what)["text"]

    def bytes(self, what: str) -> bytes:
        return self.frame(what)["bytes"]

    def frame(self, what: str) -> dict:
        try:
            return receive_within(self.session, self.budget_s)
        except TimeoutError:
            raise AssertionError(
                f"the {what} did not arrive within {self.budget_s}s: "
                f"{self.state()}"
            ) from None

    def state(self) -> str:
        """The daemon's side of the relay, for the failure: the
        row's live status and cause, the holders serving, the
        channels open, and the side they add up to."""
        status, reason = self.app.state.proxies.status(self.row)
        holders = self.app.state.bridge.holders.get(self.wid, [])
        opened = sum(len(holder.channels) for holder in holders)
        facts = (
            f"row {self.row['id']} on port {self.row['listen_port']} "
            f"reads {status}"
        )
        if reason:
            facts += f" ({reason})"
        facts += (
            f"; {len(holders)} holder(s) serve the workspace and "
            f"{opened} channel(s) stand open: "
        )
        if status == "stopped":
            return facts + "the row's listener is gone"
        if status == "failed" and reason == NO_HOLDER:
            return facts + "no holder serves the workspace: the route ended"
        if status == "failed":
            return facts + (
                "the row's listener never bound, so the peer reached "
                "whatever holds the port"
            )
        if self.peer is not None and peer_saw_eof(self.peer):
            return facts + "the daemon ended the peer's connection"
        if opened == 0:
            return facts + (
                "the listener has not accepted the peer's connection, "
                "or its channel already ended"
            )
        return facts + (
            "the frame went out on the holder's websocket and the "
            "test session did not deliver it"
        )


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
        with held_bridge(client, app, wid):
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
        with held_bridge(client, app, wid) as s:
            row = add_forward_row(client, wid)
            peer = connect_peer(row)
            relay = RelayedConnection(s, app, wid, row, peer)
            try:
                opened = json.loads(relay.text("OPEN frame"))
                assert opened == {"t": OPEN, "cid": 0, "port": 8080}
                s.send_bytes(pack_frame(0, b"HELLO"))
                assert peer.recv(64) == b"HELLO"
                peer.sendall(b"WORLD")
                assert relay.bytes("WORLD frame") == pack_frame(0, b"WORLD")
                peer.close()
                assert json.loads(relay.text("CLOSE frame")) == {
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
        with held_bridge(client, app, wid) as s:
            row = add_forward_row(client, wid)
            peer = connect_peer(row)
            relay = RelayedConnection(s, app, wid, row, peer)
            try:
                assert json.loads(relay.text("OPEN frame"))["t"] == OPEN
                s.send_text(json.dumps({"t": CLOSE, "cid": 0}))
                assert peer.recv(64) == b""
            finally:
                peer.close()


def test_a_missing_frame_names_the_side(bridge_api) -> None:
    """A frame that does not arrive fails within the budget, with
    the daemon's side of the relay described (#1131): no peer
    connected, so the OPEN wait names the listening row and the
    listener that accepted nothing."""
    api, app = bridge_api
    budget_s = 0.2
    with TestClient(api) as client:
        wid = make_workspace(client)
        with held_bridge(client, app, wid) as s:
            row = add_forward_row(client, wid)
            relay = RelayedConnection(s, app, wid, row, None, budget_s)
            started = time.monotonic()
            with pytest.raises(AssertionError) as caught:
                relay.text("OPEN frame")
            assert time.monotonic() - started < budget_s * 10
            message = str(caught.value)
            assert message.startswith(
                f"the OPEN frame did not arrive within {budget_s}s: "
            )
            assert (
                f"row {row['id']} on port {row['listen_port']} reads listening"
            ) in message
            assert "1 holder(s) serve the workspace" in message
            assert "0 channel(s) stand open" in message
            assert "has not accepted" in message


def test_a_taken_port_gets_the_row_a_fresh_one(
    bridge_api, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A free port something else holds by the time the daemon
    binds it (#1131): the create answers failed with the cause,
    the helper removes that row, and the next port binds."""
    api, app = bridge_api
    squatter = pysocket.socket()
    squatter.bind(("127.0.0.1", 0))
    squatter.listen(1)
    taken = squatter.getsockname()[1]
    # The squatted port first, then a real free port on every later
    # ask: a second collision cannot exhaust the helper's retries.
    ports = itertools.chain([taken], iter(free_port, None))
    monkeypatch.setitem(globals(), "free_port", lambda: next(ports))
    try:
        with TestClient(api) as client:
            wid = make_workspace(client)
            row = add_forward_row(client, wid)
            assert row["status"] == "listening"
            assert row["listen_port"] != taken
            response = client.get(
                f"/api/v1/workspaces/{wid}/proxies", headers=offer()
            )
            assert [r["id"] for r in response.json()] == [row["id"]]
    finally:
        squatter.close()


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
