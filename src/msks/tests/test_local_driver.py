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
    assert config["disks"] == [{"path": str(tmp_path / "r")}]
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


async def test_kill_unknown_workspace_raises(env) -> None:
    app, _, _ = env
    with pytest.raises(MicrovmError, match="no such VM"):
        await app.state.microvm.kill("ghost")


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
