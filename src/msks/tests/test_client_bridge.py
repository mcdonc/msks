"""The client half of the bridge relay (#1102): the framing pump
against a scripted websocket and a real transient agent, the
close-code names, and the ``msks proxy bridge`` flow end to end
against fakes."""

import asyncio
import json
from contextlib import contextmanager

import pytest
import websockets
from cryptography.hazmat.primitives.asymmetric import ed25519
from msks.client import agent, bridge
from msks.spec.proxy import CLOSE, ESTABLISHED, OPEN, pack_frame
from test_client_forward import closed_with


def a_key():
    """A fresh private half, the shape session_identity stages."""
    return ed25519.Ed25519PrivateKey.generate()


class FakeSocket:
    """The websocket stand-in: a queued script of incoming
    messages, a recorded list of outgoing ones."""

    def __init__(self, incoming: list) -> None:
        self.incoming = list(incoming)
        self.outgoing: list = []

    async def recv(self):
        while True:
            await asyncio.sleep(0)
            if not self.incoming:
                # Park forever once the script ends: the caller
                # ends the exchange (established, or a raised
                # close).
                await asyncio.Event().wait()
            item = self.incoming.pop(0)
            if isinstance(item, float):
                await asyncio.sleep(item)
                continue
            return item

    async def send(self, message) -> None:
        self.outgoing.append(message)


def text(kind: str, cid: int) -> str:
    return json.dumps({"t": kind, "cid": cid})


async def test_serve_answers_the_agent_protocol(tmp_path) -> None:
    """One agent connection relayed both ways: the daemon's open,
    a signed-identity listing across, the close, the end."""
    with agent.serve(a_key(), "bridge-test") as staged:
        request = agent.frame(b"\x0b")  # REQUEST_IDENTITIES
        socket = FakeSocket(
            [
                text(OPEN, 0),
                pack_frame(0, request),
                0.1,  # one pump round trip for the agent's answer
                text(CLOSE, 0),
                0.1,  # the pump reads the agent connection's end
                text(ESTABLISHED, 0),
            ]
        )
        await asyncio.wait_for(
            bridge.serve(socket, staged.server_address), timeout=5.0
        )
    frames = [m for m in socket.outgoing if not isinstance(m, str)]
    assert len(frames) == 1
    cid, payload = (int.from_bytes(frames[0][:4], "big"), frames[0][4:])
    # The relay is transparent to the agent protocol's own length
    # framing: the answer rides it whole.
    assert cid == 0 and payload[4] == 12  # IDENTITIES_ANSWER
    closes = [json.loads(m) for m in socket.outgoing if isinstance(m, str)]
    assert {c["t"] for c in closes} == {CLOSE}


async def test_serve_skips_frames_it_does_not_know() -> None:
    """A frame short of its id and a control it ignores both pass
    through without an answer; established still ends the serve."""
    socket = FakeSocket(
        [
            b"\x00\x00",
            pack_frame(9, b"x"),  # no such connection
            text("mystery", 7),  # a kind, but not one that acts
            "not json",
            text(ESTABLISHED, 0),
        ]
    )
    await asyncio.wait_for(bridge.serve(socket, "/nonexistent"), timeout=5.0)
    assert socket.outgoing == []


async def test_serve_ends_on_a_refusal_close() -> None:
    socket = FakeSocket([])

    async def recv_closed():
        raise closed_with(4502, "bridge establish failed: nope")

    socket.recv = recv_closed
    with pytest.raises(websockets.ConnectionClosed):
        await bridge.serve(socket, "/nonexistent")


def test_close_code_message_names_the_refusals() -> None:
    message = bridge.close_code_message(closed_with(4502, "denied"))
    assert message == "msks: bridge establish failed: denied"
    assert bridge.close_code_message(closed_with(1000)) is None


def test_bridge_ws_url_quotes_the_workspace() -> None:
    assert (
        bridge.bridge_ws_url("https://d:1", "ws 1")
        == "wss://d:1/api/v1/workspaces/ws%201/bridge"
    )


async def test_needs_bridge_reads_the_direction() -> None:
    from msks.client.proxies import needs_bridge

    assert needs_bridge([{"direction": "forward"}]) is True
    assert needs_bridge([{"direction": "remote"}]) is False
    assert needs_bridge([]) is False


def test_run_bridge_establishes_and_prints(monkeypatch, capsys) -> None:
    """The command flow: fetch the identity row, resolve the
    holder, serve the relay until the daemon says established."""
    pubkey = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIExample bridge-test"

    async def fake_fetch(*args, **kwargs):
        return {"public_key": pubkey, "user": "root"}

    monkeypatch.setattr(bridge, "fetch_ssh_key", fake_fetch)

    @contextmanager
    def fake_identity(key, workspace_id):
        yield type("Holder", (), {"server_address": "/nonexistent"})()

    monkeypatch.setattr(bridge, "session_identity", fake_identity)

    class FakeConnection:
        def __init__(self) -> None:
            self.socket = FakeSocket([text(ESTABLISHED, 0)])

        async def __aenter__(self):
            return self.socket

        async def __aexit__(self, *exc):
            return None

    def fake_dial(*args, **kwargs):
        async def connect():
            return FakeConnection()

        return connect()

    monkeypatch.setattr(bridge.wsdial, "dial", fake_dial)
    monkeypatch.setenv("MSKSC_URL", "https://d")
    monkeypatch.setenv("MSKSC_TOKEN", "t")
    assert bridge.run_bridge("ws-a") == 0
    assert "bridge established" in capsys.readouterr().out


def test_establish_names_a_refusal_close(monkeypatch) -> None:
    holder = type("Holder", (), {"server_address": "/x"})()
    refusing = FakeSocket([])

    async def recv_closed():
        raise closed_with(4502, "Permission denied")

    refusing.recv = recv_closed

    class RefusingConnection:
        async def __aenter__(self):
            return refusing

        async def __aexit__(self, *exc):
            return None

    def fake_dial(*args, **kwargs):
        async def connect():
            return RefusingConnection()

        return connect()

    monkeypatch.setattr(bridge.wsdial, "dial", fake_dial)
    with pytest.raises(SystemExit, match="Permission denied"):
        asyncio.run(bridge.establish("ws-a", "https://d", "t", None, holder))


async def test_relay_client_open_refusal_tells_the_daemon() -> None:
    """An agent socket that refuses its connection answers the
    daemon's open with a close, not silence."""
    socket = FakeSocket([])
    client = bridge.RelayClient(socket, "/nonexistent/agent")
    await client.open(3)
    assert json.loads(socket.outgoing[0]) == {"t": CLOSE, "cid": 3}
    await client.sweep()


def test_parse_control_refuses_every_broken_shape() -> None:
    assert bridge.parse_control("[1,2]") is None  # not an object
    assert bridge.parse_control('{"t":"open","cid":"x"}') is None
    assert bridge.parse_control('{"t":"open"}') is None
    assert bridge.parse_control("not json") is None
    assert bridge.parse_control('{"t":"open","cid":4}') == ("open", 4)


def test_relay_client_close_of_an_unknown_cid_is_quiet() -> None:
    client = bridge.RelayClient(FakeSocket([]), "/nonexistent")
    client.close(9)  # nothing opened: no error, nothing leaked


async def test_sweep_ends_a_live_connection_task() -> None:
    socket = FakeSocket([])
    client = bridge.RelayClient(socket, "/nonexistent")
    held = asyncio.Event()
    client.tasks.add(asyncio.create_task(held.wait()))
    await client.sweep()
    assert all(task.done() for task in client.tasks)


async def test_sweep_closes_a_still_open_connection() -> None:
    """The sweep owns both halves: the still-open agent connection
    closes, and the live pump task ends cancelled."""

    class Conn:
        closed = False

        def close(self):
            self.closed = True

    client = bridge.RelayClient(FakeSocket([]), "/nonexistent")
    conn = Conn()
    client.connections[0] = (None, conn)
    task = asyncio.create_task(asyncio.sleep(60))
    client.tasks.add(task)
    await client.sweep()
    assert conn.closed is True
    assert client.connections == {}
    assert task.done()


def test_establish_takes_a_clean_close_quietly(monkeypatch) -> None:
    """A websocket that closes clean (the daemon's own end, code
    1000) is not a refusal: establish returns without raising."""
    holder = type("Holder", (), {"server_address": "/x"})()
    clean = FakeSocket([])

    async def recv_clean():
        raise closed_with(1000)

    clean.recv = recv_clean

    class CleanConnection:
        async def __aenter__(self):
            return clean

        async def __aexit__(self, *exc):
            return None

    def fake_dial(*args, **kwargs):
        async def connect():
            return CleanConnection()

        return connect()

    monkeypatch.setattr(bridge.wsdial, "dial", fake_dial)
    asyncio.run(bridge.establish("ws-a", "https://d", "t", None, holder))
