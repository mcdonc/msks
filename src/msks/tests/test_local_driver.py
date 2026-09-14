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
from msks.microvm import local as local_mod
from msks.microvm.local import disk_entries, map_ch_state, vm_config
from msks.microvm.spec import VmStatus
from msks.settings import Settings, VmmSettings

from msks import persist

WID = "ws-test"


@pytest.fixture
def env(tmp_path: Path):
    """An app whose VMM points at a stub binary under a tmp state dir.

    The state dir lives under a shallow generated path in /tmp: deep
    per-test tmp trees (GitHub runners nest far deeper than dev
    boxes) can push the API socket path past the AF_UNIX 108-byte
    limit, which _check_socket_path rejects before the test's own
    subject gets exercised. The artifact tools (qemu-img, mkfs.ext4)
    are stubs too (#14): a recording no-op pair, so launch's heal
    step creates files without the real binaries.
    """
    stub = tmp_path / "ch-stub"
    stub.write_text("#!/bin/sh\nexec sleep 600\n")
    stub.chmod(0o755)
    qemu_stub = tmp_path / "qemu-img"
    qemu_stub.write_text(
        "#!/bin/sh\n"
        'case "$1" in\n'
        "  info)\n"
        "    for img do :; done\n"
        '    printf \'{"format":"raw","virtual-size":8388608}\\n\'\n'
        "    ;;\n"
        "  create)\n"
        "    shift\n"
        '    while [ "$#" -gt 1 ]; do\n'
        '      case "$1" in -f|-F|-b) shift 2 ;; *) break ;; esac\n'
        "    done\n"
        '    : > "$1"\n'
        "    ;;\n"
        "esac\n"
    )
    qemu_stub.chmod(0o755)
    mkfs_stub = tmp_path / "mkfs.ext4"
    mkfs_stub.write_text("#!/bin/sh\n: \n")
    mkfs_stub.chmod(0o755)
    state_dir = Path(tempfile.mkdtemp(prefix="msks-test-", dir="/tmp"))
    settings = Settings(
        vmm=VmmSettings(
            cloud_hypervisor=str(stub),
            state_dir=state_dir,
            qemu_img=str(qemu_stub),
            mkfs_ext4=str(mkfs_stub),
        )
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
    # The base rootfs must exist: launch reads its virtual size for
    # the overlay (#14).
    base = tmp_path / "rootfs.ext4"
    if not base.is_file():
        with base.open("wb") as handle:
            handle.truncate(8 * 1024 * 1024)
    return VmSpec(workspace_id=WID, kernel=tmp_path / "vmlinux", rootfs=base)


def test_vm_config_matches_v52_schema(tmp_path: Path) -> None:
    config = vm_config(
        VmSpec(
            workspace_id=WID,
            kernel=tmp_path / "k",
            rootfs=tmp_path / "r",
            cpus=4,
            mem_mib=2048,
        ),
        disk_entries(tmp_path, WID),
        tmp_path / "serial.log",
    )
    assert config["cpus"] == {"boot_vcpus": 4, "max_vcpus": 4}
    assert config["memory"] == {"size": 2048 * 1024 * 1024}
    assert config["payload"]["kernel"] == str(tmp_path / "k")
    assert config["payload"]["cmdline"] == "console=hvc0 root=/dev/vda rw"
    assert config["disks"] == disk_entries(tmp_path, WID)
    assert config["serial"] == {
        "mode": "File",
        "file": str(tmp_path / "serial.log"),
    }
    assert config["console"] == {"mode": "Off"}
    assert "initramfs" not in config["payload"]


def test_disk_entries_carry_overlay_and_home(tmp_path: Path) -> None:
    """The VM's disks (#14): writable overlay over the base first,
    home volume second — position makes the root device."""
    overlay, home = disk_entries(tmp_path, WID)
    assert overlay == {
        "path": str(tmp_path / "vms" / WID / "root.qcow2"),
        "readonly": False,
        "image_type": "Qcow2",
        "backing_files": True,
    }
    assert home == {
        "path": str(tmp_path / "volumes" / f"{WID}.ext4"),
        "readonly": False,
        "image_type": "Raw",
    }


def test_vm_config_with_initrd(tmp_path: Path) -> None:
    config = vm_config(
        VmSpec(
            workspace_id=WID,
            kernel=tmp_path / "k",
            rootfs=tmp_path / "r",
            initrd=tmp_path / "i",
        ),
        disk_entries(tmp_path, WID),
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
    app, state_dir, _ = env
    await app.state.microvm.launch(spec(tmp_path))
    methods = [(m, p) for m, p, _b in fake.requests]
    assert methods == [("PUT", "/api/v1/vm.create"), ("PUT", "/api/v1/vm.boot")]
    body = dict(fake.requests[0][2])
    assert body["payload"]["kernel"] == str(tmp_path / "vmlinux")
    assert body["memory"]["size"] == 1024 * 1024 * 1024
    # The VM boots its persistent artifacts (#14), never the base.
    assert body["disks"] == disk_entries(state_dir, WID)


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
    assert ("PUT", "/api/v1/vm.power-button") in [(m, p) for m, p, _b in fake.requests]


async def test_shutdown_represses_button_when_guest_ignores_it(
    env, fake, tmp_path: Path, monkeypatch
) -> None:
    """A press during early boot lands before logind listens and is
    dropped; the driver presses again instead of burning the whole
    deadline on the one lost event (#14)."""
    app, _, _ = env
    await app.state.microvm.launch(spec(tmp_path))
    proc = app.state.microvm.local._procs[WID]
    monkeypatch.setattr(local_mod, "POWER_REPRESS_S", 0.05)
    presses = 0

    def stubborn_until_third_press() -> None:
        nonlocal presses
        presses += 1
        if presses >= 3:
            fake.state = {"state": "Shutdown"}
            proc.terminate()

    fake.on_shutdown.append(stubborn_until_third_press)
    await app.state.microvm.shutdown(WID, timeout_s=5)
    assert presses == 3


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
    # The home volume dies with the workspace (#14), never with a stop.
    assert not persist.home_volume_path(state_dir, WID).exists()


async def test_prepare_creates_artifacts(env, tmp_path: Path) -> None:
    app, state_dir, _ = env
    await app.state.microvm.prepare(spec(tmp_path))
    assert persist.overlay_path(state_dir, WID).is_file()
    assert persist.home_volume_path(state_dir, WID).is_file()


async def test_prepare_refuses_a_predecessors_artifacts(env, tmp_path: Path) -> None:
    """Create never reuses another workspace-of-the-same-id's data.

    The leftover is what a failed create whose cleanup could not
    remove its files leaves behind; adopting it silently would hand
    the new workspace the predecessor's root and /home.
    """
    app, state_dir, _ = env
    home = persist.home_volume_path(state_dir, WID)
    home.parent.mkdir(parents=True, exist_ok=True)
    home.write_bytes(b"predecessor's data")
    with pytest.raises(MicrovmError, match="already exists.*remove it"):
        await app.state.microvm.prepare(spec(tmp_path))
    assert not persist.overlay_path(state_dir, WID).exists()


async def test_launch_heals_a_volume_only_leftover(env, fake, tmp_path: Path) -> None:
    """Launch heals an artifact pair with only the volume present: the
    overlay is recreated (data recovery), the volume is kept as data."""
    app, state_dir, _ = env
    home = persist.home_volume_path(state_dir, WID)
    home.parent.mkdir(parents=True, exist_ok=True)
    home.write_bytes(b"predecessor's data")
    await app.state.microvm.launch(spec(tmp_path))
    assert persist.overlay_path(state_dir, WID).is_file()
    assert home.read_bytes() == b"predecessor's data"
    await app.state.microvm.kill(WID)


async def test_prepare_failure_rolls_back_the_pair(env, tmp_path: Path) -> None:
    """The round-two wedge repro: a tool failure after the volume is
    made must leave NO artifact, or the strict next create refuses
    the id forever (the daemon's own leftover)."""
    app, state_dir, _ = env
    qemu = tmp_path / "qemu-img"
    qemu.write_text(
        "#!/bin/sh\n"
        'case "$1" in\n'
        "  info)\n"
        '    printf \'{"format":"raw","virtual-size":8388608}\\n\'\n'
        "    ;;\n"
        "  create)\n"
        "    exit 1\n"
        "    ;;\n"
        "esac\n"
    )
    qemu.chmod(0o755)
    with pytest.raises(MicrovmError, match="qemu-img create.*failed"):
        await app.state.microvm.prepare(spec(tmp_path))
    assert not persist.overlay_path(state_dir, WID).exists()
    assert not persist.home_volume_path(state_dir, WID).exists()
    # Tool fixed: the retry creates the pair and succeeds — the id is
    # not wedged.
    qemu.write_text(
        "#!/bin/sh\n"
        'case "$1" in\n'
        "  info)\n"
        '    printf \'{"format":"raw","virtual-size":8388608}\\n\'\n'
        "    ;;\n"
        "  create)\n"
        "    shift\n"
        '    while [ "$#" -gt 1 ]; do\n'
        '      case "$1" in -f|-F|-b) shift 2 ;; *) break ;; esac\n'
        "    done\n"
        '    : > "$1"\n'
        "    ;;\n"
        "esac\n"
    )
    await app.state.microvm.prepare(spec(tmp_path))
    assert persist.overlay_path(state_dir, WID).is_file()
    assert persist.home_volume_path(state_dir, WID).is_file()


async def test_launch_heals_missing_artifacts(env, fake, tmp_path: Path) -> None:
    """A workspace row predating #14, or a crash mid-create, gets its
    artifacts back on the next start — data that exists is kept."""
    app, state_dir, _ = env
    overlay = persist.overlay_path(state_dir, WID)
    overlay.parent.mkdir(parents=True, exist_ok=True)
    overlay.write_bytes(b"precious root writes")
    await app.state.microvm.launch(spec(tmp_path))
    assert overlay.read_bytes() == b"precious root writes"
    assert persist.home_volume_path(state_dir, WID).is_file()
    await app.state.microvm.kill(WID)


async def test_launch_missing_base_maps_to_error(env, tmp_path: Path) -> None:
    app, _state_dir, _ = env
    broken = VmSpec(
        workspace_id=WID,
        kernel=tmp_path / "vmlinux",
        rootfs=tmp_path / "never-built.ext4",
    )
    with pytest.raises(MicrovmError, match="base image.*not found"):
        await app.state.microvm.launch(broken)


async def test_reset_drops_overlay_keeps_home(env, fake, tmp_path: Path) -> None:
    app, state_dir, _ = env
    await app.state.microvm.launch(spec(tmp_path))
    await app.state.microvm.kill(WID)
    await app.state.microvm.reset(WID)
    assert not persist.overlay_path(state_dir, WID).exists()
    assert persist.home_volume_path(state_dir, WID).is_file()


async def test_reset_refuses_a_running_vm(env, fake, tmp_path: Path) -> None:
    app, _state_dir, _ = env
    await app.state.microvm.launch(spec(tmp_path))
    with pytest.raises(MicrovmError, match="stop it before reset"):
        await app.state.microvm.reset(WID)
    await app.state.microvm.kill(WID)


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

    monkeypatch.setattr(local_mod.CloudHypervisorApi, "power_button", die)
    await app.state.microvm.driver.shutdown(WID, timeout_s=2)
    await sleeper.wait()  # reap the zombie; kill(pid,0) succeeds until then
    with pytest.raises(ProcessLookupError):
        os.kill(sleeper.pid, 0)


def test_vm_config_with_vsock(tmp_path: Path) -> None:
    config = vm_config(
        VmSpec(workspace_id=WID, kernel=tmp_path / "k", rootfs=tmp_path / "r"),
        disk_entries(tmp_path, WID),
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
    app.state.settings.vmm.vsock_wait_timeout_s = 0.1
    with pytest.raises(MicrovmError, match="no live VMM"):
        await app.state.microvm.console(WID)


async def test_console_refused_handshake(env, tmp_path, monkeypatch) -> None:
    app, state_dir, _ = env
    app.state.settings.vmm.vsock_wait_timeout_s = 0.1
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
        async def prepare(self, spec):
            return None

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

        async def reset(self, workspace_id):
            return None

    with pytest.raises(MicrovmError, match="no console support"):
        await Minimal().console(WID)


async def test_console_silent_server_times_out(env, monkeypatch) -> None:
    """A wedged CH that never answers the handshake fails with the
    named cause instead of hanging."""
    app, state_dir, _ = env
    app.state.settings.vmm.vsock_wait_timeout_s = 0.2
    vm_dir = state_dir / "vms" / WID
    vm_dir.mkdir(parents=True, exist_ok=True)
    vm_dir.joinpath("ch.pid").write_text(str(os.getpid()))

    async def silent(reader, writer):
        await reader.readline()
        await asyncio.sleep(60)

    import msks.microvm.local as local

    monkeypatch.setattr(local, "VSOCK_REPLY_S", 0.1)
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
    app, state_dir, _ = env
    app.state.settings.vmm.vsock_wait_timeout_s = 0.1
    vm_dir = state_dir / "vms" / WID
    vm_dir.mkdir(parents=True, exist_ok=True)
    vm_dir.joinpath("ch.pid").write_text(str(os.getpid()))
    with pytest.raises(MicrovmError, match="vsock socket unreachable"):
        await app.state.microvm.console(WID)


async def test_console_stream_dies_mid_handshake(env, monkeypatch) -> None:
    """A VMM that resets the stream mid-handshake is retried, then
    fails with the named cause (not a raw OSError)."""
    app, state_dir, _ = env
    app.state.settings.vmm.vsock_wait_timeout_s = 0.1
    vm_dir = state_dir / "vms" / WID
    vm_dir.mkdir(parents=True, exist_ok=True)
    vm_dir.joinpath("ch.pid").write_text(str(os.getpid()))

    async def resetter(reader, writer):
        # Accept, then kill the stream before any reply.
        writer.close()

    server = await asyncio.start_unix_server(resetter, str(vm_dir / "vsock.sock"))
    try:
        with pytest.raises(MicrovmError, match="handshake"):
            await app.state.microvm.console(WID)
    finally:
        server.close()
        await server.wait_closed()
