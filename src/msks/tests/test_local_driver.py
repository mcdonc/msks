"""Local backend unit tests against the faked CH API socket.

The fake encodes the verified v52 contract: routes under /api/v1,
``vm.boot`` (no ``vm.start``), the v52 create-body schema, and the
daemon-style shutdown sequence (PUT vm.shutdown -> guest state leaves
Running -> VMM SIGTERM).
"""

import asyncio
import os
import shutil
import tempfile
from pathlib import Path

import pytest
from fake_ch import FakeCH
from msks.app import build_app
from msks.microvm import MicrovmError, MicrovmTimeoutError, VmSpec
from msks.microvm.local import map_ch_state, vm_config
from msks.microvm.spec import VmStatus
from msks.settings import Settings, VmmSettings

WID = "ws-test"


@pytest.fixture
def env(tmp_path: Path):
    """An app whose VMM points at a stub binary under a tmp state dir.

    The state dir lives under a shallow generated path in /tmp: deep
    per-test tmp trees (GitHub runners nest far deeper than dev
    boxes) can push the API socket path past the AF_UNIX 108-byte
    limit, which _check_socket_path rejects before the test's own
    subject gets exercised.
    """
    stub = tmp_path / "ch-stub"
    stub.write_text("#!/bin/sh\nexec sleep 600\n")
    stub.chmod(0o755)
    state_dir = Path(tempfile.mkdtemp(prefix="msks-test-", dir="/tmp"))
    settings = Settings(
        vmm=VmmSettings(cloud_hypervisor=str(stub), state_dir=state_dir)
    )
    yield build_app(settings), state_dir, None
    shutil.rmtree(state_dir, ignore_errors=True)


@pytest.fixture
async def fake(env):
    """A fake CH API server bound at the VM's expected socket path."""
    app, state_dir, _ = env
    socket_path = state_dir / "vms" / WID / "api.sock"
    socket_path.parent.mkdir(parents=True, exist_ok=True)
    server = FakeCH(socket_path)
    await server.start()
    try:
        yield server
    finally:
        await server.stop()


def spec(tmp_path: Path) -> VmSpec:
    return VmSpec(
        workspace_id=WID, kernel=tmp_path / "vmlinux", rootfs=tmp_path / "rootfs.ext4"
    )


def test_vm_config_matches_v52_schema(tmp_path: Path) -> None:
    config = vm_config(
        VmSpec(
            workspace_id=WID,
            kernel=tmp_path / "k",
            rootfs=tmp_path / "r",
            cpus=4,
            mem_mib=2048,
        ),
        tmp_path / "serial.log",
    )
    assert config["cpus"] == {"boot_vcpus": 4, "max_vcpus": 4}
    assert config["memory"] == {"size": 2048 * 1024 * 1024}
    assert config["payload"]["kernel"] == str(tmp_path / "k")
    assert config["payload"]["cmdline"] == "console=hvc0 root=/dev/vda rw"
    assert config["disks"] == [
        {"path": str(tmp_path / "r"), "readonly": True, "image_type": "Raw"}
    ]
    assert config["serial"] == {
        "mode": "File",
        "file": str(tmp_path / "serial.log"),
    }
    assert "initramfs" not in config["payload"]


def test_vm_config_with_initrd(tmp_path: Path) -> None:
    config = vm_config(
        VmSpec(
            workspace_id=WID,
            kernel=tmp_path / "k",
            rootfs=tmp_path / "r",
            initrd=tmp_path / "i",
        ),
        tmp_path / "serial.log",
    )
    assert config["payload"]["initramfs"] == str(tmp_path / "i")


def test_map_ch_state_covers_v52_states() -> None:
    assert map_ch_state("Running") == VmStatus.RUNNING
    assert map_ch_state("Paused") == VmStatus.PAUSED
    assert map_ch_state("Created") == VmStatus.STARTING
    assert map_ch_state("Shutdown") == VmStatus.STOPPED
    assert map_ch_state("SomethingNew") == VmStatus.UNKNOWN
    assert map_ch_state(None) == VmStatus.UNKNOWN


async def test_launch_puts_create_then_boot(env, fake, tmp_path: Path) -> None:
    app, _, _ = env
    await app.state.microvm.launch(spec(tmp_path))
    methods = [(m, p) for m, p, _b in fake.requests]
    assert methods == [("PUT", "/api/v1/vm.create"), ("PUT", "/api/v1/vm.boot")]
    body = dict(fake.requests[0][2])
    assert body["payload"]["kernel"] == str(tmp_path / "vmlinux")
    assert body["memory"]["size"] == 1024 * 1024 * 1024


async def test_launch_error_maps_and_reaps_process(env, tmp_path: Path) -> None:
    app, state_dir, _ = env
    socket_path = state_dir / "vms" / WID / "api.sock"
    socket_path.parent.mkdir(parents=True, exist_ok=True)
    server = FakeCH(
        socket_path, responses={("PUT", "/api/v1/vm.create"): (500, "boom")}
    )
    await server.start()
    try:
        with pytest.raises(MicrovmError) as excinfo:
            await app.state.microvm.launch(spec(tmp_path))
        assert excinfo.value.status == 500
    finally:
        await server.stop()
    proc = app.state.microvm.local._procs.get(WID)
    assert proc is None or proc.returncode is not None


async def test_dir_rejects_unsafe_ids(env) -> None:
    app, _, _ = env
    driver = app.state.microvm.local
    for bad in ("..", ".", "", "a/b", "a\\b", " spaced"):
        with pytest.raises(MicrovmError, match="unsafe workspace id"):
            driver._dir(bad)


async def test_launch_rejects_overlong_socket_path(tmp_path: Path) -> None:
    deep = tmp_path / ("x" * 90) / ("y" * 30)
    settings = Settings(vmm=VmmSettings(cloud_hypervisor="false", state_dir=deep))
    app = build_app(settings)
    with pytest.raises(MicrovmError, match="AF_UNIX"):
        await app.state.microvm.launch(spec(tmp_path))


async def test_launch_rejects_double_launch(env, fake, tmp_path: Path) -> None:
    app, _, _ = env
    await app.state.microvm.launch(spec(tmp_path))
    with pytest.raises(MicrovmError, match="already exists"):
        await app.state.microvm.launch(spec(tmp_path))
    await app.state.microvm.kill(WID)


async def test_launch_missing_binary_maps_to_error(env, tmp_path: Path) -> None:
    app, _, _ = env
    app.state.settings.vmm.cloud_hypervisor = "/nonexistent/ch"
    with pytest.raises(MicrovmError, match="not found"):
        await app.state.microvm.launch(spec(tmp_path))


async def test_launch_binary_exits_early_maps_to_error(env, tmp_path: Path) -> None:
    app, _, _ = env
    app.state.settings.vmm.cloud_hypervisor = "false"
    with pytest.raises(MicrovmError, match="exited with"):
        await app.state.microvm.launch(spec(tmp_path))


async def test_launch_socket_timeout(env, tmp_path: Path) -> None:
    app, _, _ = env
    app.state.settings.vmm.socket_wait_timeout_s = 0.05
    # No fake server bound: the socket never appears.
    with pytest.raises(MicrovmTimeoutError, match="never appeared"):
        await app.state.microvm.launch(spec(tmp_path))
    proc = app.state.microvm.local._procs.get(WID)
    assert proc is None or proc.returncode is not None


async def test_info_absent_when_no_dir(env) -> None:
    app, _, _ = env
    info = await app.state.microvm.info("nope")
    assert info.status is VmStatus.ABSENT


async def test_info_stopped_when_socket_gone(env, fake, tmp_path: Path) -> None:
    app, _, _ = env
    await app.state.microvm.launch(spec(tmp_path))
    await fake.stop()
    info = await app.state.microvm.info(WID)
    assert info.status is VmStatus.STOPPED
    assert info.pid  # pid file written
    await app.state.microvm.kill(WID)


async def test_info_running_via_api(env, fake, tmp_path: Path) -> None:
    app, _, _ = env
    await app.state.microvm.launch(spec(tmp_path))
    info = await app.state.microvm.info(WID)
    assert info.status is VmStatus.RUNNING
    await app.state.microvm.kill(WID)


async def test_info_created_maps_to_starting(env, fake, tmp_path: Path) -> None:
    app, _, _ = env
    await app.state.microvm.launch(spec(tmp_path))
    fake.state = {"state": "Created"}
    info = await app.state.microvm.info(WID)
    assert info.status is VmStatus.STARTING
    await app.state.microvm.kill(WID)


async def test_info_unknown_on_api_error_with_live_pid(
    env, fake, tmp_path: Path
) -> None:
    app, _, _ = env
    await app.state.microvm.launch(spec(tmp_path))
    fake.responses = {("GET", "/api/v1/vm.info"): (500, "oops")}
    info = await app.state.microvm.info(WID)
    assert info.status is VmStatus.UNKNOWN
    await app.state.microvm.kill(WID)


async def test_info_stopped_on_stale_socket_with_dead_pid(env) -> None:
    # A SIGKILLed VMM leaves the socket file behind; the pid is dead.
    app, state_dir, _ = env
    vm_dir = state_dir / "vms" / WID
    vm_dir.mkdir(parents=True)
    (vm_dir / "api.sock").write_bytes(b"")
    (vm_dir / "ch.pid").write_text("4000000")
    info = await app.state.microvm.info(WID)
    assert info.status is VmStatus.STOPPED


async def test_info_non_dict_document_maps_to_unknown(
    env, fake, tmp_path: Path
) -> None:
    app, _, _ = env
    await app.state.microvm.launch(spec(tmp_path))
    fake.responses = {("GET", "/api/v1/vm.info"): (200, "null")}
    info = await app.state.microvm.info(WID)
    assert info.status is VmStatus.UNKNOWN
    await app.state.microvm.kill(WID)


async def test_shutdown_graceful(env, fake, tmp_path: Path) -> None:
    app, _, _ = env
    await app.state.microvm.launch(spec(tmp_path))
    proc = app.state.microvm.local._procs[WID]

    def guest_powers_off() -> None:
        fake.state = {"state": "Shutdown"}
        proc.terminate()

    fake.on_shutdown.append(guest_powers_off)
    await app.state.microvm.shutdown(WID, timeout_s=5)
    assert ("PUT", "/api/v1/vm.shutdown") in [(m, p) for m, p, _b in fake.requests]


async def test_shutdown_timeout_when_guest_never_powers_off(
    env, fake, tmp_path: Path
) -> None:
    app, _, _ = env
    await app.state.microvm.launch(spec(tmp_path))
    with pytest.raises(MicrovmTimeoutError, match="did not power off"):
        await app.state.microvm.shutdown(WID, timeout_s=0.2)
    await app.state.microvm.kill(WID)


async def test_shutdown_without_socket_is_noop(env, tmp_path: Path) -> None:
    app, state_dir, _ = env
    (state_dir / "vms" / WID).mkdir(parents=True)
    await app.state.microvm.shutdown(WID)


async def test_shutdown_without_process_ref_terminates_pidfile_pid(env, fake) -> None:
    app, state_dir, _ = env
    fake.state = {"state": "Shutdown"}
    sleeper = await asyncio.create_subprocess_exec("sleep", "600")
    (state_dir / "vms" / WID / "ch.pid").write_text(str(sleeper.pid))
    await app.state.microvm.shutdown(WID, timeout_s=5)
    await sleeper.wait()


async def test_shutdown_timeout_when_vmm_ignores_sigterm(env, tmp_path: Path) -> None:
    # A stub that traps SIGTERM: the guest powers off, the VMM refuses to die.
    app, state_dir, _ = env
    stubborn = tmp_path / "ch-stubborn"
    stubborn.write_text("#!/bin/sh\ntrap '' TERM\nexec sleep 600\n")
    stubborn.chmod(0o755)
    app.state.settings.vmm.cloud_hypervisor = str(stubborn)
    socket_path = state_dir / "vms" / WID / "api.sock"
    socket_path.parent.mkdir(parents=True, exist_ok=True)
    server = FakeCH(socket_path)
    server.state = {"state": "Shutdown"}
    await server.start()
    try:
        await app.state.microvm.launch(spec(tmp_path))
        with pytest.raises(MicrovmTimeoutError, match="did not exit after SIGTERM"):
            await app.state.microvm.shutdown(WID, timeout_s=0.5)
    finally:
        await server.stop()
    await app.state.microvm.kill(WID)


async def test_shutdown_without_pidfile_returns_after_guest_down(env, fake) -> None:
    app, state_dir, _ = env
    fake.state = {"state": "Shutdown"}
    await app.state.microvm.shutdown(WID, timeout_s=5)
    assert not (state_dir / "vms" / WID / "ch.pid").exists()


async def test_kill_via_pidfile(env, tmp_path: Path) -> None:
    app, state_dir, _ = env
    vm_dir = state_dir / "vms" / WID
    vm_dir.mkdir(parents=True)
    sleeper = await asyncio.create_subprocess_exec("sleep", "600")
    (vm_dir / "ch.pid").write_text(str(sleeper.pid))
    await app.state.microvm.kill(WID)
    await sleeper.wait()
    with pytest.raises(ProcessLookupError):
        os.kill(sleeper.pid, 0)


async def test_kill_unknown_workspace_is_success(env) -> None:
    # Killing an absent VM is success (the absent-VM contract): a
    # never-started workspace must stay deletable, like the k8s
    # backend guarantees since the round-2 review.
    app, _, _ = env
    await app.state.microvm.kill("ghost")


async def test_shutdown_stale_socket_is_stopped(env, tmp_path: Path) -> None:
    # A dead VMM leaves its api socket file behind: connect() then
    # fails ECONNREFUSED (not ENOENT), which shutdown must treat as
    # already-stopped instead of wedging the caller (found by the
    # appliance e2e: delete-after-stop 500'd exactly this way).
    app, state_dir, _ = env
    vm_dir = state_dir / "vms" / WID
    vm_dir.mkdir(parents=True)
    (vm_dir / "api.sock").write_bytes(b"")  # stale: no VMM behind it
    dead = await asyncio.create_subprocess_exec("sleep", "0.1")
    (vm_dir / "ch.pid").write_text(str(dead.pid))
    await dead.wait()
    await app.state.microvm.shutdown(WID, timeout_s=1)  # must not raise


async def test_cleanup_removes_dir(env, fake, tmp_path: Path) -> None:
    app, state_dir, _ = env
    await app.state.microvm.launch(spec(tmp_path))
    await app.state.microvm.cleanup(WID)
    assert not (state_dir / "vms" / WID).exists()


async def test_driver_switch_and_validation(env) -> None:
    app, _, _ = env
    assert app.state.microvm.driver is app.state.microvm.local
    app.state.settings.vmm.driver = "k8s"
    assert app.state.microvm.driver is app.state.microvm.k8s
    app.state.settings.vmm.driver = "bogus"
    with pytest.raises(MicrovmError, match="unknown vmm driver"):
        app.state.microvm.driver


async def test_terminate_without_proc_or_pid(env, tmp_path: Path) -> None:
    # _terminate with no tracked process and no pid file: a plain
    # return, no error (pin the arc explicitly — xdist/sysmon arc
    # capture is order-sensitive and CI missed this one once).
    app, state_dir, _ = env
    vm_dir = state_dir / "vms" / WID
    vm_dir.mkdir(parents=True)  # dir exists, no ch.pid
    await app.state.microvm.driver._terminate(
        WID, asyncio.get_running_loop().time() + 1
    )


async def test_terminate_sigterms_pidfile_pid(env, tmp_path: Path) -> None:
    # _terminate with no tracked process but a live pidfile pid: the
    # pid gets SIGTERM (the orphaned-VMM path).
    app, state_dir, _ = env
    vm_dir = state_dir / "vms" / WID
    vm_dir.mkdir(parents=True)
    sleeper = await asyncio.create_subprocess_exec("sleep", "600")
    (vm_dir / "ch.pid").write_text(str(sleeper.pid))
    await app.state.microvm.driver._terminate(
        WID, asyncio.get_running_loop().time() + 5
    )
    await sleeper.wait()  # reap the zombie; kill(pid,0) succeeds until then
    with pytest.raises(ProcessLookupError):
        os.kill(sleeper.pid, 0)


async def test_shutdown_escalates_when_api_dies_midcall(env, monkeypatch) -> None:
    # The VMM looked alive but its API died between the liveness check
    # and the call: shutdown must fall through to SIGTERM (the
    # _terminate path), not surface a 500.
    app, state_dir, _ = env
    vm_dir = state_dir / "vms" / WID
    vm_dir.mkdir(parents=True)
    (vm_dir / "api.sock").write_bytes(b"")
    sleeper = await asyncio.create_subprocess_exec("sleep", "600")
    (vm_dir / "ch.pid").write_text(str(sleeper.pid))

    from msks.microvm import local as local_mod
    from msks.microvm.errors import MicrovmError

    async def die(self):
        raise MicrovmError("connection refused mid-call")

    monkeypatch.setattr(local_mod.CloudHypervisorApi, "shutdown", die)
    await app.state.microvm.driver.shutdown(WID, timeout_s=2)
    await sleeper.wait()  # reap the zombie; kill(pid,0) succeeds until then
    with pytest.raises(ProcessLookupError):
        os.kill(sleeper.pid, 0)


def test_vm_config_with_vsock(tmp_path: Path) -> None:
    config = vm_config(
        VmSpec(workspace_id=WID, kernel=tmp_path / "k", rootfs=tmp_path / "r"),
        tmp_path / "serial.log",
        vsock_socket=tmp_path / "vms" / WID / "vsock.sock",
    )
    assert config["vsock"] == {
        "cid": 3,
        "socket": str(tmp_path / "vms" / WID / "vsock.sock"),
    }


async def _fake_vsock_server(path: Path, reply: bytes):
    """A unix server speaking the CONNECT handshake, echoing bytes."""

    async def handle(reader, writer):
        line = await reader.readline()
        assert line.startswith(b"CONNECT ")
        writer.write(reply)
        await writer.drain()
        while True:
            data = await reader.read(4096)
            if not data:
                break
            writer.write(data.upper())
            await writer.drain()
        writer.close()

    return await asyncio.start_unix_server(handle, str(path))


async def test_console_handshake_and_stream(env, tmp_path: Path) -> None:
    app, state_dir, _ = env
    sock = state_dir / "vms" / WID / "vsock.sock"
    sock.parent.mkdir(parents=True, exist_ok=True)
    # A live-seeming VMM: our own pid passes the liveness check.
    (state_dir / "vms" / WID / "ch.pid").write_text(str(os.getpid()))
    server = await _fake_vsock_server(sock, b"OK 1073741824\n")
    try:
        reader, writer = await app.state.microvm.console(WID)
        writer.write(b"ping\n")
        await writer.drain()
        assert await reader.readline() == b"PING\n"
        writer.close()
        await writer.wait_closed()
    finally:
        server.close()
        await server.wait_closed()


async def test_console_without_vm_fails_fast(env, monkeypatch) -> None:
    app, _, _ = env
    import msks.microvm.local as local

    monkeypatch.setattr(local, "VSOCK_WAIT_S", 0.1)
    with pytest.raises(MicrovmError, match="no live VMM"):
        await app.state.microvm.console(WID)


async def test_console_refused_handshake(env, tmp_path, monkeypatch) -> None:
    app, state_dir, _ = env
    import msks.microvm.local as local

    monkeypatch.setattr(local, "VSOCK_WAIT_S", 0.1)
    # A live-seeming VM: the pid check must not fail the retry early.
    (state_dir / "vms" / WID).mkdir(parents=True, exist_ok=True)
    (state_dir / "vms" / WID / "ch.pid").write_text(str(os.getpid()))
    sock = state_dir / "vms" / WID / "vsock.sock"
    server = await _fake_vsock_server(sock, b"NOK bad-port\n")
    try:
        with pytest.raises(MicrovmError, match="handshake refused"):
            await app.state.microvm.console(WID)
    finally:
        server.close()
        await server.wait_closed()


async def test_default_console_unsupported() -> None:
    from msks.microvm.driver import MicrovmDriver

    class Minimal(MicrovmDriver):
        async def launch(self, spec):
            return None

        async def info(self, workspace_id):
            raise NotImplementedError

        async def shutdown(self, workspace_id, timeout_s=None):
            return None

        async def kill(self, workspace_id):
            return None

        async def cleanup(self, workspace_id):
            return None

    with pytest.raises(MicrovmError, match="no console support"):
        await Minimal().console(WID)


async def test_console_silent_server_times_out(env, monkeypatch) -> None:
    """A wedged CH that never answers the handshake fails with the
    named cause instead of hanging."""
    import msks.microvm.local as local

    monkeypatch.setattr(local, "VSOCK_WAIT_S", 0.2)
    monkeypatch.setattr(local, "VSOCK_REPLY_S", 0.1)
    app, state_dir, _ = env
    vm_dir = state_dir / "vms" / WID
    vm_dir.mkdir(parents=True, exist_ok=True)
    vm_dir.joinpath("ch.pid").write_text(str(os.getpid()))

    async def silent(reader, writer):
        await reader.readline()
        await asyncio.sleep(60)

    server = await asyncio.start_unix_server(silent, str(vm_dir / "vsock.sock"))
    try:
        with pytest.raises(MicrovmError, match="handshake reply never arrived"):
            await app.state.microvm.console(WID)
    finally:
        server.close()
        await server.wait_closed()


async def test_console_socket_never_appears(env, monkeypatch) -> None:
    """A live VMM whose vsock socket never appears fails at the
    deadline with the named cause."""
    import msks.microvm.local as local

    monkeypatch.setattr(local, "VSOCK_WAIT_S", 0.1)
    app, state_dir, _ = env
    vm_dir = state_dir / "vms" / WID
    vm_dir.mkdir(parents=True, exist_ok=True)
    vm_dir.joinpath("ch.pid").write_text(str(os.getpid()))
    with pytest.raises(MicrovmError, match="no live vsock socket"):
        await app.state.microvm.console(WID)
