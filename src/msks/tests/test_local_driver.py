"""Local backend unit tests against the faked CH API socket."""

import asyncio
import os
import stat
from pathlib import Path

import pytest
from fake_ch import FakeCH
from msks.app import build_app
from msks.microvm import MicrovmError, MicrovmTimeoutError, VmSpec
from msks.microvm.local import map_ch_state, vm_config
from msks.settings import Settings, VmmSettings

WID = "ws-test"


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """An app whose VMM points at a stub binary under a tmp state dir."""
    stub = tmp_path / "ch-stub"
    stub.write_text("#!/bin/sh\nexec sleep 600\n")
    stub.chmod(0o755)
    state_dir = tmp_path / "state"
    settings = Settings(
        vmm=VmmSettings(cloud_hypervisor=str(stub), state_dir=state_dir)
    )
    return build_app(settings), state_dir, monkeypatch


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


def test_vm_config_includes_spec(tmp_path: Path) -> None:
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
    assert config["cpus"] == {"boot_count": 4}
    assert config["memory"] == {"size": 2048}
    assert config["kernel"] == {"path": str(tmp_path / "k")}
    assert config["disks"][0]["is_root_device"] is True
    assert "initramfs" not in config


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
    assert config["initramfs"] == {"path": str(tmp_path / "i")}


def test_map_ch_state() -> None:
    from msks.microvm.spec import VmStatus

    assert map_ch_state("Running") == VmStatus.RUNNING
    assert map_ch_state("Paused") == VmStatus.PAUSED
    assert map_ch_state("SomethingNew") == VmStatus.UNKNOWN
    assert map_ch_state(None) == VmStatus.UNKNOWN


async def test_launch_puts_create_then_start(env, fake, tmp_path: Path) -> None:
    app, _, _ = env
    await app.state.microvm.launch(spec(tmp_path))
    methods = [(m, p) for m, p, _b in fake.requests]
    assert methods == [("PUT", "/vm.create"), ("PUT", "/vm.start")]
    body = dict(fake.requests[0][2])
    assert body["kernel"]["path"] == str(tmp_path / "vmlinux")
    assert body["cmdline"]["args"] == "console=hvc0 root=/dev/vda rw"


async def test_launch_error_maps_to_microvm_error(env, tmp_path: Path) -> None:
    app, state_dir, _ = env
    socket_path = state_dir / "vms" / WID / "api.sock"
    socket_path.parent.mkdir(parents=True, exist_ok=True)
    server = FakeCH(socket_path, responses={("PUT", "/vm.create"): (500, "boom")})
    await server.start()
    try:
        with pytest.raises(MicrovmError) as excinfo:
            await app.state.microvm.launch(spec(tmp_path))
        assert excinfo.value.status == 500
    finally:
        await server.stop()
    await app.state.microvm.kill(WID)


async def test_launch_socket_timeout(env, tmp_path: Path) -> None:
    app, state_dir, _ = env
    app.state.settings.vmm.socket_wait_timeout_s = 0.05
    # No fake server bound: the socket never appears.
    with pytest.raises(MicrovmTimeoutError, match="never appeared"):
        await app.state.microvm.launch(spec(tmp_path))
    await app.state.microvm.kill(WID)


async def test_info_absent_when_no_dir(env) -> None:
    app, _, _ = env
    info = await app.state.microvm.info("nope")
    assert info.status.value == "absent"


async def test_info_stopped_when_socket_gone(env, fake) -> None:
    app, _, _ = env
    await app.state.microvm.launch(spec(Path("/tmp")))
    await fake.stop()
    info = await app.state.microvm.info(WID)
    assert info.status.value == "stopped"
    assert info.pid == os.getpid() or info.pid  # pid file written
    await app.state.microvm.kill(WID)


async def test_info_running_via_api(env, fake, tmp_path: Path) -> None:
    app, _, _ = env
    await app.state.microvm.launch(spec(tmp_path))
    info = await app.state.microvm.info(WID)
    assert info.status.value == "running"
    await app.state.microvm.kill(WID)


async def test_info_unknown_on_api_error(env, fake, tmp_path: Path) -> None:
    app, _, _ = env
    await app.state.microvm.launch(spec(tmp_path))
    fake.responses = {("GET", "/vm.info"): (500, "oops")}
    info = await app.state.microvm.info(WID)
    assert info.status.value == "unknown"
    await app.state.microvm.kill(WID)


async def test_shutdown_graceful(env, fake, tmp_path: Path) -> None:
    app, _, _ = env
    await app.state.microvm.launch(spec(tmp_path))
    proc = app.state.microvm.local._procs[WID]
    fake.on_shutdown.append(lambda: proc.terminate())
    await app.state.microvm.shutdown(WID, timeout_s=5)
    assert ("PUT", "/vm.shutdown") in [(m, p) for m, p, _b in fake.requests]


async def test_shutdown_timeout_raises(env, fake, tmp_path: Path) -> None:
    app, _, _ = env
    await app.state.microvm.launch(spec(tmp_path))
    app.state.settings.vmm.shutdown_timeout_s = 0.1
    with pytest.raises(MicrovmTimeoutError, match="did not exit"):
        await app.state.microvm.shutdown(WID)
    await app.state.microvm.kill(WID)


async def test_shutdown_without_socket_is_noop(env, tmp_path: Path) -> None:
    app, state_dir, _ = env
    (state_dir / "vms" / WID).mkdir(parents=True)
    await app.state.microvm.shutdown(WID)


async def test_shutdown_without_process_ref_returns(env, fake, tmp_path: Path) -> None:
    app, _, _ = env
    await app.state.microvm.launch(spec(tmp_path))
    app.state.microvm.local._procs.pop(WID)
    await app.state.microvm.shutdown(WID, timeout_s=1)


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


def test_stub_is_executable(env) -> None:
    _app, _state, _mp = env
    assert (
        stat.S_IMODE(os.stat(_app.state.settings.vmm.cloud_hypervisor).st_mode) & 0o111
    )
