"""The bridge manager (#1102): paths, session state, the ssh
spawn seam's argv and outcomes, and the graceful stop — against a
fake ssh that scripts the master's behavior."""

import asyncio
import contextlib
import shutil
import tempfile
from pathlib import Path

import pytest
from msks.app import build_app
from msks.bridge import (
    ESTABLISH_TIMEOUT_S,
    NO_IDENTITY,
    SESSION_ENDED,
    BridgeError,
    BridgeManager,
    ChannelWriter,
    channel_args,
    exit_args,
    master_args,
)
from msks.microvm import VmSpec
from msks.settings import NetSettings, ServerSettings, Settings, VmmSettings

PUBKEY = (
    "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIA4Yrw1rppdmUVa"
    "4uhT6RmXKlCjeJ6aFyMQsneM+BFaV"
)


class FakeProcess:
    """One scripted ssh invocation."""

    def __init__(self, returncode: int = 0, stderr: bytes = b"") -> None:
        self.returncode = None
        self._returncode = returncode
        self.stderr = _Stderr(stderr)
        self.stdin = None
        self.stdout = None
        self.killed = False
        self.waited = asyncio.Event()

    async def wait(self) -> int:
        await self.waited.wait()
        self.returncode = self._returncode
        return self.returncode

    def kill(self) -> None:
        self.killed = True
        self._returncode = -9
        self.waited.set()


class _Stderr:
    """The stderr pipe's read-once shape."""

    def __init__(self, payload: bytes) -> None:
        self.payload = payload

    async def read(self) -> bytes:
        return self.payload


class FakeSpawn:
    """The ssh seam: records argv, scripts the master (touching
    its control socket on success — the real master's own
    behavior), and hands the tests the process each call made."""

    def __init__(self, manager: BridgeManager, tmp_path: Path) -> None:
        self.argvs: list[list[str]] = []
        self.processes: list[FakeProcess] = []
        self.manager = manager
        self.ctl = manager.paths("ws-a")["ctl"]
        self.master = (False, 0, b"")
        self.control = (0, b"")

    async def __call__(self, *argv, **kwargs):
        self.argvs.append(list(argv))
        hang = False
        returncode = 0
        stderr = b""
        if argv[1] == "-M":
            hang, returncode, stderr = self.master
        elif "-O" in argv:
            if "exit" in argv:
                with contextlib.suppress(OSError):
                    self.ctl.unlink()
            returncode, stderr = self.control
        process = FakeProcess(returncode, stderr)
        if not hang:
            process.waited.set()
        if argv[1] == "-M" and returncode == 0 and not hang:
            with contextlib.suppress(OSError):
                self.ctl.touch()
        self.processes.append(process)
        return process


class FakeNet:
    """The attachment seam: one workspace attached, by choice."""

    def __init__(self, guest_ip: str | None) -> None:
        self.guest_ip = guest_ip

    def attachment(self, workspace_id: str):
        if self.guest_ip is None:
            return None
        return type(
            "Attachment",
            (),
            {"workspace_id": workspace_id, "guest_ip": self.guest_ip},
        )()


@pytest.fixture
async def bridge(tmp_path: Path):
    # A short state root: the bridge's sockets live under it, and
    # the xdist tmp dirs run past the kernel's AF_UNIX path
    # ceiling (the same refusal paths() carries for real runs).
    state = Path(tempfile.mkdtemp(prefix="mskb-"))
    app = build_app(
        Settings(
            server=ServerSettings(db_path=tmp_path / "b.db"),
            net=NetSettings(enabled=False),
            vmm=VmmSettings(state_dir=state),
        )
    )
    app.state.model.migrate()
    await app.state.model.create_workspace(
        VmSpec(
            workspace_id="ws-a",
            kernel=Path("/k"),
            rootfs=Path("/r"),
            egress=True,
        )
    )
    app.state.net = FakeNet("172.31.0.2")
    manager = app.state.bridge
    spawn = FakeSpawn(manager, state)
    manager.spawn = spawn
    try:
        yield manager, app, spawn, state
    finally:
        await app.state.model.close()
        shutil.rmtree(state, ignore_errors=True)


def row(pubkey: str = PUBKEY, user: str | None = None) -> dict:
    """The workspace row shape establish reads."""
    return {"id": "ws-a", "ssh_pubkey": pubkey, "user": user}


async def test_paths_live_under_the_state_root(bridge) -> None:
    manager, _app, _spawn, tmp_path = bridge
    paths = manager.paths("ws-a")
    assert paths["ctl"] == tmp_path / "bridges" / "ws-a" / "ctl.sock"
    assert paths["relay"].name == "relay.sock"
    assert paths["identity"].name == "identity.pub"
    assert paths["known_hosts"].name == "known_hosts"


async def test_paths_refuse_a_socket_the_kernel_cannot_address(
    bridge,
) -> None:
    manager, _app, _spawn, _tmp = bridge
    with pytest.raises(BridgeError) as caught:
        manager.paths("ws-" + "x" * 120)
    assert "too long" in str(caught.value)


async def test_up_reads_the_control_socket(bridge) -> None:
    manager, _app, spawn, tmp_path = bridge
    ctl = manager.paths("ws-a")["ctl"]
    ctl.parent.mkdir(parents=True, exist_ok=True)
    assert manager.up("ws-a") is False
    assert manager.cause("ws-a") == NO_IDENTITY
    ctl.touch()
    assert manager.up("ws-a") is True
    ctl.unlink()
    assert manager.up("ws-a") is False
    assert manager.cause("ws-a") == NO_IDENTITY  # never established
    await manager.establish(row(), "172.31.0.2", tmp_path / "r.sock")
    assert manager.up("ws-a") is True
    spawn.ctl.unlink()
    assert manager.up("ws-a") is False
    assert manager.cause("ws-a") == SESSION_ENDED


async def test_relay_claims_are_exclusive(bridge) -> None:
    manager, _app, _spawn, _tmp = bridge
    assert manager.claim_relay("ws-a") is True
    assert manager.claim_relay("ws-a") is False
    manager.release_relay("ws-a")
    assert manager.claim_relay("ws-a") is True


async def test_establish_spawns_the_master_and_writes_the_key(
    bridge,
) -> None:
    manager, _app, spawn, tmp_path = bridge
    relay = manager.paths("ws-a")["dir"] / "r.sock"
    cause = await manager.establish(row(), "172.31.0.2", relay)
    assert cause is None
    argv = spawn.argvs[0]
    assert argv[:2] == ["ssh", "-M"]
    assert f"IdentityAgent={relay}" in argv
    assert "IdentitiesOnly=yes" in argv
    assert "StrictHostKeyChecking=accept-new" in argv
    assert "ControlPersist=yes" in argv
    assert argv[argv.index("-l") + 1] == "root"
    written = manager.paths("ws-a")["identity"]
    assert written.read_text() == PUBKEY + "\n"
    assert manager.paths("ws-a")["known_hosts"].exists()


async def test_establish_names_the_login_user(bridge) -> None:
    manager, _app, spawn, tmp_path = bridge
    await manager.establish(row(user="dev"), "172.31.0.2", tmp_path / "r.sock")
    argv = spawn.argvs[0]
    user_at = argv.index("-l")
    assert argv[user_at + 1] == "dev"


async def test_establish_failure_carries_ssh_stderr(bridge) -> None:
    manager, _app, spawn, tmp_path = bridge
    spawn.master = (
        False,
        255,
        b"first line\nPermission denied (publickey).\n",
    )
    relay = manager.paths("ws-a")["dir"] / "r.sock"
    cause = await manager.establish(row(), "172.31.0.2", relay)
    assert "Permission denied" in cause
    assert manager.cause("ws-a") == cause
    assert manager.up("ws-a") is False


async def test_establish_timeout_kills_the_stalled_master(
    bridge, monkeypatch
) -> None:
    from msks import bridge as bridge_mod

    monkeypatch.setattr(bridge_mod, "ESTABLISH_TIMEOUT_S", 0.1)
    manager, _app, spawn, tmp_path = bridge
    spawn.master = (True, 0, b"")
    relay = manager.paths("ws-a")["dir"] / "r.sock"
    cause = await manager.establish(row(), "172.31.0.2", relay)
    assert "timed out" in cause
    assert spawn.processes[0].killed is True


async def test_establish_skips_when_a_session_stands(bridge) -> None:
    manager, _app, spawn, _tmp = bridge
    manager.paths("ws-a")["dir"].mkdir(parents=True, exist_ok=True)
    manager.paths("ws-a")["ctl"].touch()
    result = await manager.establish(row(), "172.31.0.2", Path("/r"))
    assert result is None
    assert spawn.argvs == []


async def test_dial_refusals_name_their_causes(bridge) -> None:
    manager, app, _spawn, _tmp = bridge
    with pytest.raises(BridgeError) as caught:
        await manager.dial("ws-a", 22)
    assert "no bridge session" in str(caught.value)
    app.state.net = FakeNet(None)
    with pytest.raises(BridgeError) as caught:
        await manager.dial("ws-a", 22)
    assert "no live network attachment" in str(caught.value)


async def test_dial_opens_a_channel(bridge) -> None:
    manager, _app, spawn, _tmp = bridge
    manager.paths("ws-a")["dir"].mkdir(parents=True, exist_ok=True)
    manager.paths("ws-a")["ctl"].touch()
    ctl = str(manager.paths("ws-a")["ctl"])
    stdout = asyncio.StreamReader()
    stdin = _SinkWriter()
    process = FakeProcess()
    process.waited.set()
    process.stdout = stdout
    process.stdin = stdin

    async def spawn_channel(*argv, **kwargs):
        spawn.argvs.append(list(argv))
        return process

    manager.spawn = spawn_channel
    reader, writer = await manager.dial("ws-a", 8020)
    argv = spawn.argvs[0]
    assert argv[:2] == ["ssh", "-S"]
    assert argv[2] == ctl
    assert argv[argv.index("-W") + 1] == "127.0.0.1:8020"
    assert reader is stdout
    writer.write(b"payload")
    await writer.drain()
    assert stdin.written == [b"payload"]
    writer.close()
    await writer.wait_closed()
    assert process.returncode == 0


class _SinkWriter:
    """The stdin pipe's recording stand-in."""

    def __init__(self) -> None:
        self.written: list[bytes] = []
        self.closed = False

    def write(self, data: bytes) -> None:
        self.written.append(data)

    async def drain(self) -> None:
        return None

    def close(self) -> None:
        self.closed = True

    async def wait_closed(self) -> None:
        return None


async def test_stop_exits_the_master_and_clears_the_relay(bridge) -> None:
    manager, _app, spawn, _tmp = bridge
    paths = manager.paths("ws-a")
    paths["dir"].mkdir(parents=True, exist_ok=True)
    paths["ctl"].touch()
    paths["relay"].touch()
    await manager.stop("ws-a")
    assert "-O" in spawn.argvs[0] and "exit" in spawn.argvs[0]
    assert not paths["relay"].exists()


async def test_stop_without_a_session_spawns_nothing(bridge) -> None:
    manager, _app, spawn, _tmp = bridge
    await manager.stop("ws-a")
    assert spawn.argvs == []


def test_argv_builders() -> None:
    """The three argv shapes, pinned as the ssh contract."""
    master = master_args(
        "root", "172.31.0.2", Path("/c"), Path("/r"), Path("/i"), Path("/k")
    )
    assert master[:2] == ["ssh", "-M"]
    assert "-fN" in master
    assert "IdentityAgent=/r" in master
    assert "IdentitiesOnly=yes" in master
    assert master[master.index("-i") + 1] == "/i"
    channel = channel_args("root", "172.31.0.2", Path("/c"), "127.0.0.1", 8020)
    assert channel[:2] == ["ssh", "-S"]
    assert channel[channel.index("-W") + 1] == "127.0.0.1:8020"
    assert "BatchMode=yes" in channel
    exit_argv = exit_args(Path("/c"))
    assert exit_argv == ["ssh", "-S", "/c", "-O", "exit", "localhost"]


def test_establish_timeout_default_is_bounded() -> None:
    assert ESTABLISH_TIMEOUT_S <= 60


async def test_channel_writer_endures_a_process_without_stdin() -> None:
    """A ``-W`` process whose stdin already left (the channel died
    at birth) still closes and reaps without raising."""
    process = FakeProcess()
    process.waited.set()
    process.stdin = None
    writer = ChannelWriter(process)
    writer.close()
    await writer.wait_closed()
    assert process.returncode == 0


async def test_dial_names_a_gone_row(bridge) -> None:
    manager, _app, _spawn, _tmp = bridge
    manager.paths("ws-a")["dir"].mkdir(parents=True, exist_ok=True)
    manager.paths("ws-a")["ctl"].touch()
    with pytest.raises(BridgeError) as caught:
        await manager.dial("ws-gone", 22)
    assert "is gone" in str(caught.value)
