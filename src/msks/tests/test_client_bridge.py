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
    holder = bridge.Holder(socket, "ws-a", "root", "/kh")
    process = FakeProcess()
    spawn_argv = []

    async def fake_spawn(*argv, **kwargs):
        spawn_argv.append(list(argv))
        return process

    holder.spawner = fake_spawn
    await holder.open(0, 8080, "/agent.sock", "/identity.pub")
    assert "-W" in spawn_argv[0]
    assert spawn_argv[0][spawn_argv[0].index("-W") + 1] == "127.0.0.1:8080"
    # The holder's own pin file is the one the channel pins under
    # (#1158): the path the holder was given is the argv's value,
    # whole and alone.
    assert "UserKnownHostsFile=/kh" in spawn_argv[0]
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
    holder = bridge.Holder(socket, "ws-a", "root", "/kh")
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
    holder = bridge.Holder(socket, "ws-a", "root", "/kh")
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


@pytest.mark.parametrize(
    ("row", "directory"),
    [
        ({"id": "ws-id", "seed_epoch": 2}, "ws-id.r2"),
        ({"id": "ws-id", "disk_epoch": 1, "seed_epoch": 2}, "ws-id.e1.r2"),
        ({"id": "ws-id"}, "ws-id"),
    ],
)
def test_run_hold_keys_the_host_cache_like_a_session(
    monkeypatch, row, directory
) -> None:
    """The holder pins the guest's host key in the session's own
    cache directory (#1158): the row's seed and disk epochs rotate
    it, so a renewed workspace's channels read the re-pinned key
    instead of the first boot's."""
    pubkey = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIExample bridge-test"

    async def fake_fetch(*args, **kwargs):
        return {**row, "public_key": pubkey, "user": "dev"}

    monkeypatch.setattr(bridge, "fetch_ssh_key", fake_fetch)

    @contextmanager
    def fake_identity(key, workspace_id):
        yield type(
            "Held", (), {"server_address": "/a", "identity_path": "/i"}
        )()

    monkeypatch.setattr(bridge, "session_identity", fake_identity)

    def fake_known_hosts(cache_ref, base=None, instance=None):
        name = f"{cache_ref}.{instance}" if instance else cache_ref
        return f"/kh/{name}/known_hosts"

    monkeypatch.setattr(bridge, "known_hosts_path", fake_known_hosts)
    made = []

    class Recording(bridge.Holder):
        def __init__(self, socket, workspace_id, user, known_hosts):
            super().__init__(socket, workspace_id, user, known_hosts)
            made.append(self)

    monkeypatch.setattr(bridge, "Holder", Recording)
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
    assert bridge.run_hold("ws-id") == 0
    assert [holder.known_hosts for holder in made] == [
        f"/kh/{directory}/known_hosts"
    ]


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
    holder = bridge.Holder(socket, "ws-a", "root", "/kh")
    spawn = FakeSpawn()
    holder.spawner = spawn
    await holder.open(0, 8080, "/a", "/i")
    await holder.open(1, 8080, "/a", "/i")
    await holder.sweep()
    assert holder.channels == {}


async def test_dispatch_control_closes_a_channel() -> None:
    socket = FakeSocket([])
    holder = bridge.Holder(socket, "ws-a", "root", "/kh")
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
    holder = bridge.Holder(socket, "ws-a", "root", "/kh")

    async def raising(cid, port, agent, ident, host="127.0.0.1"):
        raise OSError("no fork left")

    holder.open = raising
    await bridge.guarded_open(holder, 0, 8080, "/a", "/i")
    assert json.loads(socket.outgoing[-1]) == {"t": CLOSE, "cid": 0}


async def test_pump_out_names_a_failed_channel() -> None:
    """A channel whose ssh exited nonzero says one line on
    stderr (the repair path's diagnosability)."""
    socket = FakeSocket([])
    holder = bridge.Holder(socket, "ws-a", "root", "/kh")
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
    holder = bridge.Holder(socket, "ws-a", "root", "/kh")

    async def raising(cid, port, agent, ident, host="127.0.0.1"):
        raise OSError("no fork left")

    holder.open = raising
    await bridge.guarded_open(holder, 7, 8080, "/a", "/i")
    assert json.loads(socket.outgoing[-1]) == {"t": CLOSE, "cid": 7}


def lost_frame(ws: str = "ws-a") -> dict:
    """One proxy.failed frame carrying the missing-holder cause —
    the daemon's publish_down shape."""
    return {
        "event": "proxy.failed",
        "data": {
            "id": ws,
            "proxy": 7,
            "direction": "forward",
            "reason": (
                "no client holds the bridge — start the workspace "
                "from a client (the holder it leaves serves after "
                "the command exits), or run 'msks proxy hold "
                "<workspace>'"
            ),
        },
    }


def failed_rows(reason: str | None = bridge.NO_HOLDER_CAUSE) -> list[dict]:
    """One workspace's forward rows as a failing read serves
    them: failed, the cause riding the reason."""
    return [
        {
            "id": 7,
            "direction": "forward",
            "status": "failed",
            "reason": reason,
        }
    ]


def held_rows() -> list[dict]:
    """The same rows as a read serves them once a holder stands
    (#1198): listening, no cause."""
    return [
        {
            "id": 7,
            "direction": "forward",
            "status": "listening",
            "reason": None,
        }
    ]


def status_frame(status: str, ws: str = "ws-a") -> dict:
    """The watcher's status transition, the shape the daemon
    publishes (#1198)."""
    return {"event": "workspace.status", "data": {"id": ws, "status": status}}


class FakeClock:
    """The monotonic stand-in: the tests advance it by hand."""

    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def make_reholder(spawned: list | None = None):
    """A policy over a recording spawn and a hand-advanced
    clock."""
    spawned = [] if spawned is None else spawned
    clock = FakeClock()
    return (
        bridge.ReHolder(
            spawn=lambda ws: bool(spawned.append(ws) or True), clock=clock
        ),
        clock,
    )


def test_the_frame_predicates_name_their_workspaces() -> None:
    """#1127: the bridge-relevant frames — a holder-lost failure,
    a running transition, a serving listener — name their
    workspace; every other shape decides nothing."""
    assert bridge.holder_lost(lost_frame()) == "ws-a"
    # Another cause (a bind refusal) is not the bridge's failure.
    assert bridge.holder_lost(lost_frame()) == "ws-a"
    bind_failed = {
        "event": "proxy.failed",
        "data": {"id": "ws-a", "reason": "[Errno 98] address in use"},
    }
    assert bridge.holder_lost(bind_failed) is None
    assert (
        bridge.holder_lost({"event": "proxy.closed", "data": {"id": "ws-a"}})
        is None
    )
    assert bridge.holder_lost({"event": "proxy.failed", "data": {}}) is None
    running = {
        "event": "workspace.status",
        "data": {"id": "ws-b", "status": "running"},
    }
    assert bridge.became_running(running) == "ws-b"
    assert (
        bridge.became_running(
            {"event": "workspace.status", "data": {"id": "ws-b"}}
        )
        is None
    )
    assert (
        bridge.became_running(
            {
                "event": "workspace.status",
                "data": {"id": "ws-b", "status": "stopping"},
            }
        )
        is None
    )
    assert (
        bridge.became_running(
            {"event": "workspace.created", "data": {"id": "ws-b"}}
        )
        is None
    )
    serving = {"event": "proxy.listening", "data": {"id": "ws-c"}}
    assert bridge.bridge_serving(serving) == "ws-c"
    assert (
        bridge.bridge_serving(
            {"event": "proxy.removed", "data": {"id": "ws-c"}}
        )
        is None
    )


def test_rehold_due_reads_the_rows_honestly() -> None:
    """#1127: a refreshed read owes a holder when a forward row
    reads failed with the missing-holder cause and none reads
    listening — the start hook's own gate, narrowed to the cause
    a holder can serve."""
    assert bridge.rehold_due(failed_rows()) is True
    # A listening row means a holder stands: no standby beside it.
    assert (
        bridge.rehold_due(
            [
                *failed_rows(),
                {"id": 8, "direction": "forward", "status": "listening"},
            ]
        )
        is False
    )
    # A bind refusal reads failed with another cause.
    assert bridge.rehold_due(failed_rows("[Errno 98] address in use")) is False
    # Stopped rows (the workspace went down) owe no holder.
    assert (
        bridge.rehold_due(
            [{"id": 7, "direction": "forward", "status": "stopped"}]
        )
        is False
    )
    # Remote rows never take a holder.
    assert (
        bridge.rehold_due(
            [
                {
                    "id": 7,
                    "direction": "remote",
                    "status": "failed",
                    "reason": bridge.NO_HOLDER_CAUSE,
                }
            ]
        )
        is False
    )
    assert bridge.rehold_due([]) is False


def test_reholder_spawns_once_per_signal_burst() -> None:
    """#1127: the burst one failure publishes per row lands as
    one — the first signal weighs and spawns, the rest meet the
    window and decide nothing."""
    holder, _clock = make_reholder()
    assert holder.signal("ws-a") == bridge.WEIGH
    assert holder.signal("ws-a") is None
    assert holder.signal("ws-a") is None
    assert holder.rehold("ws-a", failed_rows()) is True
    assert holder.spent == {"ws-a": 1}


def test_reholder_retries_at_the_backoff_pace() -> None:
    """#1127: a signal past the window weighs again — a
    crash-looping holder retries at the window's pace, not the
    frame rate."""
    holder, clock = make_reholder()
    assert holder.signal("ws-a") == bridge.WEIGH
    assert holder.rehold("ws-a", failed_rows()) is True
    clock.now += bridge.ReHolder.RETRY_AFTER_S
    clock.now += bridge.ReHolder.AWAIT_S + 1
    assert holder.signal("ws-a") == bridge.WEIGH
    clock.now += bridge.ReHolder.RETRY_AFTER_S - 1
    assert holder.signal("ws-a") is None
    clock.now += bridge.ReHolder.AWAIT_S + 2
    assert holder.signal("ws-a") == bridge.WEIGH


def test_reholder_gives_up_once_per_session() -> None:
    """#1127: the budget is bounded per workspace per session —
    the failure after the last spawn meets GAVE_UP once, and the
    ones after it meet silence (the row's cause stands)."""
    holder, clock = make_reholder()
    for _ in range(bridge.ReHolder.MAX_TRIES):
        assert holder.signal("ws-a") == bridge.WEIGH
        assert holder.rehold("ws-a", failed_rows()) is True
        clock.now += bridge.ReHolder.RETRY_AFTER_S
        clock.now += bridge.ReHolder.AWAIT_S + 1
    assert holder.signal("ws-a") == bridge.GAVE_UP
    assert holder.signal("ws-a") is None
    # Another workspace keeps its own budget.
    assert holder.signal("ws-b") == bridge.WEIGH


def test_reholder_does_not_spawn_past_the_budget() -> None:
    """#1127: a rows read that lands after the budget spent
    spawns nothing — the ruling and the read cannot race the
    budget loose."""
    holder, clock = make_reholder()
    for _ in range(bridge.ReHolder.MAX_TRIES):
        assert holder.rehold("ws-a", failed_rows()) is True
        clock.now += bridge.ReHolder.AWAIT_S + 1
    assert holder.rehold("ws-a", failed_rows()) is False


def test_reholder_says_the_recovery_once_inside_the_window() -> None:
    """#1127: a listener that lands inside the outcome window is
    the spawn's recovery, said once; a later one — an operator's
    manual hold — is not the watch's work, and the deferral a
    standing window carries resolves through it (the pending
    failure the recovery answers is spent, not re-ruled)."""
    holder, clock = make_reholder()
    assert holder.rehold("ws-a", failed_rows()) is True
    assert holder.serving("ws-a") is True
    assert holder.serving("ws-a") is False
    # A late listener past the window reads as someone else's.
    assert holder.rehold("ws-b", failed_rows()) is True
    clock.now += bridge.ReHolder.AWAIT_S + 1
    assert holder.serving("ws-b") is False


def test_an_in_window_signal_defers_until_the_window_closes() -> None:
    """#1127: a signal the backoff window swallows waits as a
    mark, not a drop — the failure frames are edge-triggered,
    and nothing re-delivers them. The drain re-rules it exactly
    as a fresh signal would, so a holder that dies as fast as it
    connects retries at the window's pace."""
    holder, clock = make_reholder()
    assert holder.signal("ws-a") == bridge.WEIGH
    assert holder.signal("ws-a") is None  # the burst lands as one
    assert holder.take_due() == []  # the window still stands
    clock.now += bridge.ReHolder.RETRY_AFTER_S - 1
    assert holder.take_due() == []
    clock.now += 2
    assert holder.take_due() == [("ws-a", bridge.WEIGH)]
    assert holder.take_due() == []  # taken once
    assert holder.rehold("ws-a", failed_rows()) is True


def test_a_signal_over_an_outstanding_spawn_defers_to_its_outcome() -> None:
    """#1127: a failure landing while the last spawn's holder is
    still connecting neither spends a give-up line early nor
    weighs a second child beside it: the deferral holds until
    the outcome window lapses, and a serving resolves it at
    once — the pending failure is the one the recovery
    answered."""
    holder, clock = make_reholder()
    assert holder.rehold("ws-a", failed_rows()) is True
    clock.now += bridge.ReHolder.RETRY_AFTER_S  # the backoff lapses
    assert holder.signal("ws-a") is None  # the outcome window stands
    assert "ws-a" in holder.pending
    holder.serving("ws-a")  # the listener resolves it
    assert holder.pending == set()
    assert holder.take_due() == []
    # Without the listener, the lapse re-rules the deferral.
    assert holder.rehold("ws-b", failed_rows()) is True
    assert holder.signal("ws-b") is None  # the outcome stands
    clock.now += bridge.ReHolder.AWAIT_S + 1
    clock.now += bridge.ReHolder.RETRY_AFTER_S
    assert holder.take_due() == [("ws-b", bridge.WEIGH)]


def test_the_give_up_line_waits_for_the_last_spawns_outcome() -> None:
    """#1127: the spent budget's give-up line arrives through the
    drain once the outcome window lapses — a failure landing
    while the third spawn's holder still connects defers, and
    the late listener that follows says the recovery, not a
    give-up line over it."""
    holder, clock = make_reholder()
    for _ in range(bridge.ReHolder.MAX_TRIES):
        assert holder.rehold("ws-a", failed_rows()) is True
        clock.now += bridge.ReHolder.AWAIT_S + 1
    assert holder.signal("ws-a") == bridge.GAVE_UP
    # The listener of a spawn whose window a fresh signal closed
    # is not that spawn's recovery.
    assert holder.serving("ws-a") is False


def test_rehold_refuses_while_a_spawns_outcome_stands() -> None:
    """#1127: a rows read that lands while the last spawn's
    outcome window stands spawns nothing — a second child beside
    a holder that may be connecting is the standby the
    no-standby rule forbids, and a slow read racing a second
    signal lands here on either surface."""
    holder, clock = make_reholder()
    assert holder.rehold("ws-a", failed_rows()) is True
    assert holder.rehold("ws-a", failed_rows()) is False
    assert holder.spent == {"ws-a": 1}
    clock.now += bridge.ReHolder.AWAIT_S + 1
    assert holder.rehold("ws-a", failed_rows()) is True


def test_the_start_paths_own_spawn_opens_the_outcome_window() -> None:
    """#1127: a holder the start path spawns outside the watch is
    still a spawn — its listener resolves a deferral the way the
    watch's own would, and a failure under it defers."""
    holder, clock = make_reholder()
    holder.note_spawn("ws-a")
    assert holder.signal("ws-a") is None
    assert holder.serving("ws-a") is True
    holder.note_spawn("ws-b")
    clock.now += bridge.ReHolder.AWAIT_S + 1
    assert holder.serving("ws-b") is False


def test_reholder_spends_only_real_attempts() -> None:
    """#1127: rows that read listening, stopped, or failed with
    another cause spawn nothing and spend nothing — the stop race
    and the bind refusal never exhaust a session's budget."""
    holder, _clock = make_reholder()
    assert (
        holder.rehold(
            "ws-a",
            [{"id": 7, "direction": "forward", "status": "listening"}],
        )
        is False
    )
    assert holder.rehold("ws-a", failed_rows("[Errno 98] used")) is False
    assert holder.spent == {}


def test_reholder_takes_the_starts_window() -> None:
    """#1127: the start path's own spawn takes the window a
    signal would — the running transition the boot publishes
    must not stack a second child beside it."""
    holder, clock = make_reholder()
    holder.note_start("ws-a")
    assert holder.signal("ws-a") is None
    clock.now += bridge.ReHolder.RETRY_AFTER_S
    assert holder.signal("ws-a") == bridge.WEIGH


def test_boot_announced_names_the_booting_workspace() -> None:
    """#1198: the daemon's two words on a start — ``starting``
    before the launch, ``running`` at its end — name the
    workspace booting; a stop, a pause, a frame without the
    word, and every other event decide nothing."""
    assert bridge.boot_announced(status_frame("starting")) == "ws-a"
    assert bridge.boot_announced(status_frame("running")) == "ws-a"
    assert bridge.boot_announced(status_frame("stopped")) is None
    assert bridge.boot_announced(status_frame("paused")) is None
    assert (
        bridge.boot_announced(
            {"event": "workspace.status", "data": {"id": "ws-a"}}
        )
        is None
    )
    assert (
        bridge.boot_announced(
            {"event": "proxy.listening", "data": {"id": "ws-a"}}
        )
        is None
    )


def test_a_foreign_boots_running_frame_waits_for_its_holder() -> None:
    """#1198: a boot another client makes announces itself, and
    the watch holds the boot's signals until the window closes —
    the booting client's start path spawns its holder after the
    running frame, and a read at once found the rows still
    failed and stacked a standby. At the close the rows read
    listening, owe nothing, and spend nothing."""
    holder, clock = make_reholder()
    holder.note_boot("ws-a")  # the starting frame
    assert holder.signal("ws-a") is None  # the running frame
    assert holder.take_due() == []
    clock.now += bridge.ReHolder.RETRY_AFTER_S - 1
    assert holder.take_due() == []
    clock.now += 1
    assert holder.take_due() == [("ws-a", bridge.WEIGH)]
    assert holder.rehold("ws-a", held_rows()) is False
    assert holder.spent == {}
    assert holder.awaited == {}


def test_the_boots_last_word_extends_the_window_past_the_boot() -> None:
    """#1198: ``running`` re-opens the window ``starting`` opened,
    so the holder's time counts from the boot's end, not its
    start — a boot longer than the window still gives its
    holder the whole window to connect."""
    holder, clock = make_reholder()
    holder.note_boot("ws-a")
    clock.now += bridge.ReHolder.RETRY_AFTER_S - 5
    holder.note_boot("ws-a")
    assert holder.signal("ws-a") is None
    clock.now += bridge.ReHolder.RETRY_AFTER_S - 1
    assert holder.take_due() == []
    clock.now += 1
    assert holder.take_due() == [("ws-a", bridge.WEIGH)]


def test_a_foreign_start_that_left_no_holder_is_reheld_a_window_late() -> None:
    """#1198: the raw-API start keeps its re-hold — one window
    after the running frame, the read finds the rows failed
    with the missing-holder cause and takes the spawn the
    start itself would have left."""
    holder, clock = make_reholder()
    holder.note_boot("ws-a")
    assert holder.signal("ws-a") is None
    clock.now += bridge.ReHolder.RETRY_AFTER_S
    assert holder.take_due() == [("ws-a", bridge.WEIGH)]
    assert holder.rehold("ws-a", failed_rows()) is True
    assert holder.spent == {"ws-a": 1}


def test_a_holder_lost_without_a_boot_is_weighed_at_once() -> None:
    """#1198: only a boot opens the window — a holder that dies
    under a running workspace publishes its failure with no
    transition before it, and the watch reads the rows at once,
    the pace #1127 gave it."""
    holder, _clock = make_reholder()
    assert holder.signal("ws-a") == bridge.WEIGH
    assert holder.rehold("ws-a", failed_rows()) is True


def test_reholder_names_a_failed_spawn() -> None:
    """#1127: a spawn that cannot start spends its attempt and
    answers False — the failure is the surface's to name, and no
    outcome window stands for a child that never left."""
    holder, _clock = make_reholder()
    holder.spawn = lambda ws: False
    assert holder.rehold("ws-a", failed_rows()) is False
    assert holder.spent == {"ws-a": 1}
    assert holder.awaited == {}
