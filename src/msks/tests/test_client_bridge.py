"""The client half of the bridge (#1102): the holder's channel
argv, the frame pump against a scripted websocket and a fake ssh,
and the ``msks proxy hold`` flow — against fakes, end to end."""

import asyncio
import json
from contextlib import contextmanager

import pytest
import websockets
from cryptography.hazmat.primitives.asymmetric import ed25519
from msks.client import bridge
from msks.spec.proxy import CLOSE, OPEN, pack_frame
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
                # ends the exchange (a raised close, a sweep).
                await asyncio.Event().wait()
            item = self.incoming.pop(0)
            if isinstance(item, float):
                await asyncio.sleep(item)
                continue
            return item

    async def send(self, message) -> None:
        self.outgoing.append(message)


def text(kind: str, cid: int, port: int | None = None) -> str:
    body = {"t": kind, "cid": cid}
    if port is not None:
        body["port"] = port
    return json.dumps(body)


class FakeProcess:
    """The ssh channel stand-in: pipes the tests drive by hand."""

    def __init__(self, returncode: int = 0) -> None:
        self.stdin = PipeWriter()
        self.stdout = asyncio.StreamReader()
        self._returncode = returncode
        self.terminated = False

    @property
    def returncode(self) -> int:
        return self._returncode

    async def wait(self) -> int:
        return self._returncode

    def terminate(self) -> None:
        self.terminated = True
        self.stdout.feed_eof()


class PipeWriter:
    """The stdin pipe's recording stand-in."""

    def __init__(self) -> None:
        self.written: list[bytes] = []

    def write(self, data: bytes) -> None:
        self.written.append(data)

    async def drain(self) -> None:
        return None


def test_channel_argv_rides_the_forward_websocket() -> None:
    argv = bridge.channel_argv(
        "ws-a",
        "/a gent.sock",
        "/iden tity.pub",
        "dev",
        8080,
        "/known hosts",
        "db.internal",
    )
    assert argv[0] == "ssh"
    joined = " ".join(argv)
    assert "ProxyCommand=" in joined and "forward" in joined
    assert "-W db.internal:8080" in joined
    assert "-l dev" in joined
    assert 'IdentityAgent="/a gent.sock"' in joined
    assert 'UserKnownHostsFile="/known hosts"' in joined
    assert "IdentitiesOnly=yes" in joined
    assert "StrictHostKeyChecking=accept-new" in joined


def test_parse_control_shapes() -> None:
    assert bridge.parse_control(text(OPEN, 3, 8080)) == (
        "open",
        3,
        8080,
        None,
    )
    assert bridge.parse_control(text(CLOSE, 3)) == ("close", 3, None, None)
    with pytest.raises(ValueError):
        bridge.parse_control("not json")
    with pytest.raises(KeyError):
        bridge.parse_control('{"cid": 1}')


async def test_holder_serves_one_connection() -> None:
    """One open, bytes both ways, the channel's end closing the
    connection — the frames the daemon side sends and expects."""
    socket = FakeSocket([])
    holder = bridge.Holder(socket, "ws-a", "root")
    process = FakeProcess()
    spawn_argv = []

    async def fake_spawn(*argv, **kwargs):
        spawn_argv.append(list(argv))
        return process

    holder.spawner = fake_spawn
    await holder.open(0, 8080, "/agent.sock", "/identity.pub")
    assert "-W" in spawn_argv[0]
    assert spawn_argv[0][spawn_argv[0].index("-W") + 1] == "127.0.0.1:8080"
    # Daemon bytes to the channel's stdin.
    await holder.feed(pack_frame(0, b"ping"))
    assert process.stdin.written == [b"ping"]
    # The channel's stdout to the websocket.
    process.stdout.feed_data(b"pong")
    await asyncio.sleep(0.05)
    assert pack_frame(0, b"pong") in [
        m for m in socket.outgoing if not isinstance(m, str)
    ]
    # The channel ending sends the close.
    process.stdout.feed_eof()
    await asyncio.sleep(0.05)
    assert json.loads(socket.outgoing[-1]) == {"t": CLOSE, "cid": 0}
    # A close from the daemon terminates the channel.
    await holder.open(1, 8080, "/agent.sock", "/identity.pub")
    holder.close(1)
    await holder.sweep()


async def test_holder_feed_skips_what_it_cannot_place() -> None:
    socket = FakeSocket([])
    holder = bridge.Holder(socket, "ws-a", "root")
    await holder.feed(b"\x00\x01")  # shorter than an id
    await holder.feed(pack_frame(9, b"x"))  # no such channel
    holder.close(9)  # never stood: quiet


class ClosingSocket(FakeSocket):
    """A socket whose recv answers the script, then ends with the
    close it was given."""

    def __init__(self, incoming: list, code: int = 1000) -> None:
        super().__init__(incoming)
        self.code = code

    async def recv(self):
        if not self.incoming:
            raise closed_with(self.code)
        item = self.incoming.pop(0)
        if isinstance(item, float):
            await asyncio.sleep(item)
            return await self.recv()
        return item


async def test_serve_dispatches_until_the_websocket_ends() -> None:
    socket = ClosingSocket(
        [
            text(OPEN, 0, 8080),
            pack_frame(0, b"payload"),
            text(CLOSE, 0),
            "mystery",  # a kind that acts on nothing
            text(CLOSE, 9),  # a close for nothing standing
        ]
    )
    holder = bridge.Holder(socket, "ws-a", "root")
    spawn = FakeSpawn()
    holder.spawner = spawn
    with pytest.raises(websockets.ConnectionClosed):
        await bridge.serve(socket, holder, "/a", "/i")
    assert spawn.argvs and "-W" in spawn.argvs[0]
    assert holder.channels == {}


class FakeSpawn:
    """The spawner stand-in: records, serves quiet processes."""

    def __init__(self) -> None:
        self.argvs: list[list[str]] = []

    async def __call__(self, *argv, **kwargs):
        self.argvs.append(list(argv))
        return FakeProcess()


def test_close_code_message_names_the_refusals() -> None:
    assert (
        bridge.close_code_message(closed_with(4404, "gone"))
        == "msks: no such workspace: gone"
    )
    assert bridge.close_code_message(closed_with(1000)) is None


def test_bridge_ws_url_quotes_the_workspace() -> None:
    assert (
        bridge.bridge_ws_url("https://d:1", "ws 1")
        == "wss://d:1/api/v1/workspaces/ws%201/bridge"
    )


def test_run_hold_serves_until_the_websocket_ends(monkeypatch, capsys) -> None:
    """The command flow: fetch the identity row, resolve the
    holder, serve until the daemon's websocket ends clean."""
    pubkey = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIExample bridge-test"

    async def fake_fetch(*args, **kwargs):
        return {"public_key": pubkey, "user": "dev"}

    monkeypatch.setattr(bridge, "fetch_ssh_key", fake_fetch)

    @contextmanager
    def fake_identity(key, workspace_id):
        yield type(
            "Held", (), {"server_address": "/a", "identity_path": "/i"}
        )()

    monkeypatch.setattr(bridge, "session_identity", fake_identity)
    monkeypatch.setattr(bridge, "known_hosts_path", lambda ws, **kw: "/kh")

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
    monkeypatch.setenv("MSKSC_URL", "https://d")
    monkeypatch.setenv("MSKSC_TOKEN", "t")
    assert bridge.run_hold("ws-a") == 0


def test_run_hold_names_a_refusal_close(monkeypatch) -> None:
    held = type("Held", (), {"server_address": "/a", "identity_path": "/i"})()

    async def fake_fetch(*args, **kwargs):
        return {"public_key": "k", "user": "root"}

    monkeypatch.setattr(bridge, "fetch_ssh_key", fake_fetch)

    @contextmanager
    def fake_identity(key, workspace_id):
        yield held

    monkeypatch.setattr(bridge, "session_identity", fake_identity)
    refusing = FakeSocket([])

    async def recv_refused():
        raise closed_with(4404, "gone")

    refusing.recv = recv_refused

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
    monkeypatch.setenv("MSKSC_URL", "https://d")
    monkeypatch.setenv("MSKSC_TOKEN", "t")
    with pytest.raises(SystemExit, match="no such workspace"):
        bridge.run_hold("ws-a")


async def test_sweep_ends_live_channels() -> None:
    socket = FakeSocket([])
    holder = bridge.Holder(socket, "ws-a", "root")
    spawn = FakeSpawn()
    holder.spawner = spawn
    await holder.open(0, 8080, "/a", "/i")
    await holder.open(1, 8080, "/a", "/i")
    await holder.sweep()
    assert holder.channels == {}


async def test_dispatch_control_closes_a_channel() -> None:
    socket = FakeSocket([])
    holder = bridge.Holder(socket, "ws-a", "root")
    spawn = FakeSpawn()
    holder.spawner = spawn
    await bridge.dispatch_control(holder, text(OPEN, 0, 8080), "/a", "/i")
    assert 0 in holder.channels
    await bridge.dispatch_control(holder, text(CLOSE, 0), "/a", "/i")
    assert holder.channels == {}
    # A parseable frame that acts on nothing passes through.
    await bridge.dispatch_control(
        holder, '{"t":"mystery","cid":5}', "/a", "/i"
    )
    assert holder.channels == {}


def test_close_code_message_separates_the_three_ends() -> None:
    """Clean ends stay quiet; named refusals name themselves;
    everything else (a dropped connection) is an unexpected end,
    not a silent success."""
    assert bridge.close_code_message(closed_with(1000)) is None
    assert bridge.close_code_message(closed_with(1001)) is None
    assert bridge.close_code_message(closed_with(4404, "gone")) == (
        "msks: no such workspace: gone"
    )
    dropped = websockets.ConnectionClosed(None, None)
    assert "ended unexpectedly" in bridge.close_code_message(dropped)
    assert "ended unexpectedly" in bridge.close_code_message(closed_with(1014))


async def test_guarded_open_isolates_a_failed_spawn() -> None:
    """A spawn that cannot start ends its own connection with a
    close and one stderr line; the holder keeps serving."""

    class Raising:
        async def open(self, cid, port, agent, ident):
            raise OSError("no fork left")

        class socket:
            sent = []

            @staticmethod
            async def send(message):
                Raising.socket.sent.append(message)

    import contextlib

    with contextlib.suppress(AttributeError):
        pass
    socket = FakeSocket([])
    holder = bridge.Holder(socket, "ws-a", "root")

    async def raising(cid, port, agent, ident, host="127.0.0.1"):
        raise OSError("no fork left")

    holder.open = raising
    await bridge.guarded_open(holder, 0, 8080, "/a", "/i")
    assert json.loads(socket.outgoing[-1]) == {"t": CLOSE, "cid": 0}


async def test_pump_out_names_a_failed_channel() -> None:
    """A channel whose ssh exited nonzero says one line on
    stderr (the repair path's diagnosability)."""
    socket = FakeSocket([])
    holder = bridge.Holder(socket, "ws-a", "root")
    process = FakeProcess(returncode=255)
    process.stdout.feed_data(b"data")
    process.stdout.feed_eof()
    await holder.pump_out(0, process)
    kinds = [json.loads(m) for m in socket.outgoing if isinstance(m, str)]
    assert kinds[-1] == {"t": CLOSE, "cid": 0}
    assert pack_frame(0, b"data") in socket.outgoing


async def test_guarded_open_answers_its_own_close_on_failure() -> None:
    """The spawn-isolated open: an OSError from the channel spawn
    prints one line and answers the connection's close."""
    socket = FakeSocket([])
    holder = bridge.Holder(socket, "ws-a", "root")

    async def raising(cid, port, agent, ident, host="127.0.0.1"):
        raise OSError("no fork left")

    holder.open = raising
    await bridge.guarded_open(holder, 7, 8080, "/a", "/i")
    assert json.loads(socket.outgoing[-1]) == {"t": CLOSE, "cid": 7}
