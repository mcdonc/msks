"""Local backend unit tests against the faked CH API socket.

The fake encodes the verified v52 contract: routes under /api/v1,
``vm.boot`` (no ``vm.start``), the v52 create-body schema, and the
daemon-style shutdown sequence (PUT vm.shutdown -> guest state leaves
Running -> VMM SIGTERM).
"""

import asyncio
import itertools
import os
import shutil
import socket
import tempfile
from dataclasses import replace
from pathlib import Path

import pytest
from fake_ch import FakeCH
from msks.app import build_app
from msks.microvm import MicrovmError, MicrovmTimeoutError, VmSpec
from msks.microvm import local as local_mod
from msks.microvm.driver import MicrovmDriver
from msks.microvm.local import disk_entries, map_ch_state, vm_config
from msks.microvm.spec import VmStatus
from msks.net import alloc
from msks.net import manager as manager_mod
from msks.net.manager import NetManager
from msks.settings import NetSettings, Settings, VmmSettings
from netstubs import stub_ip, stub_nft
from test_net_manager import FakeService

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
    # A single python process, not `sh -c 'exec sleep'`: #151's
    # identity-checked liveness reads /proc/<pid>/cmdline, and an
    # exec'd sleep erases the stub (and --api-socket) from it. The
    # shebang keeps the stub's own path in the cmdline, exactly like
    # the real binary spawn. With MSKS_STUB_READY set the stub also
    # touches that path once userland runs -- spawn_stub_vmm waits
    # for it, because a shebang exec chain satisfies the /proc
    # identity check mid-chain (env phase) and then briefly reads
    # EMPTY at the next exec transition (#154 r2).
    stub.write_text(
        "#!/usr/bin/env python3\n"
        "import os, pathlib, time\n"
        'ready = os.environ.get("MSKS_STUB_READY")\n'
        "if ready:\n"
        "    pathlib.Path(ready).touch()\n"
        "time.sleep(600)\n"
    )
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
    geniso_stub = tmp_path / "mkisofs"
    geniso_stub.write_text(
        "#!/bin/sh\n"
        "prev=\n"
        "out=\n"
        "for arg do\n"
        '  case "$prev" in -output) out=$arg ;; esac\n'
        "  prev=$arg\n"
        "done\n"
        'printf iso > "$out"\n'
    )
    geniso_stub.chmod(0o755)
    state_dir = Path(tempfile.mkdtemp(prefix="msks-test-", dir="/tmp"))
    settings = Settings(
        vmm=VmmSettings(
            cloud_hypervisor=str(stub),
            state_dir=state_dir,
            qemu_img=str(qemu_stub),
            mkfs_ext4=str(mkfs_stub),
            mkisofs=str(geniso_stub),
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


def spec(tmp_path: Path, egress: bool = False) -> VmSpec:
    # The base rootfs must exist: launch reads its virtual size for
    # the overlay (#14).
    base = tmp_path / "rootfs.ext4"
    if not base.is_file():
        with base.open("wb") as handle:
            handle.truncate(8 * 1024 * 1024)
    return VmSpec(
        workspace_id=WID,
        kernel=tmp_path / "vmlinux",
        rootfs=base,
        egress=egress,
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
    assert methods == [
        ("PUT", "/api/v1/vm.create"),
        ("PUT", "/api/v1/vm.boot"),
    ]
    body = dict(fake.requests[0][2])
    assert body["payload"]["kernel"] == str(tmp_path / "vmlinux")
    assert body["memory"]["size"] == 8192 * 1024 * 1024
    # The VM boots its persistent artifacts (#14), never the base.
    assert body["disks"] == disk_entries(state_dir, WID)
    await app.state.microvm.kill(WID)  # reap the stub VMM


async def test_failed_boot_lands_stopped_even_with_a_recycled_pid(
    env, tmp_path: Path
) -> None:
    """The #158 wrinkle, pinned dead on the post-#154 daemon.

    The incident: ``vm.boot`` failed (vsock ``AddrInUse``), and
    the workspace reported ``unknown`` -- a status matching
    neither the disk nor the VM -- because the dead-socket probe's
    liveness check was a bare pid test a recycled pid satisfied.
    Now the failed launch reaps its spawn and the probe consults
    the identity check: a dead socket plus a pidfile naming an
    innocent process reports ``stopped``, the status the next
    ``msks start`` retries from.
    """
    app, state_dir, _ = env
    driver = app.state.microvm.local
    vm_dir = state_dir / "vms" / WID
    vm_dir.mkdir(parents=True, exist_ok=True)
    server = FakeCH(
        state_dir / "vms" / WID / "api.sock",
        responses={
            ("PUT", "/api/v1/vm.boot"): (
                500,
                '["Error from API","The VM could not boot",'
                '"Error from device manager",'
                '"Cannot create virtio-vsock backend",'
                '"Error binding to the host-side Unix socket",'
                '"Address already in use (os error 98)"]',
            )
        },
    )
    await server.start()
    try:
        with pytest.raises(MicrovmError, match="The VM could not boot"):
            await app.state.microvm.launch(spec(tmp_path))
        # The failed launch reaped its spawn: registered nowhere,
        # no zombie left under the pidfile.
        assert WID not in driver._procs
        pid = int((vm_dir / "ch.pid").read_text())
        with pytest.raises(ProcessLookupError):
            os.kill(pid, 0)
    finally:
        await server.stop()
    # The pre-#154 ``unknown`` trigger, verbatim: a dead socket
    # file at the name (Python 3.14's asyncio unlinks the server's
    # socket on close, so the residue is re-created the way a
    # hard-killed VMM leaves it) plus a pidfile naming a live,
    # innocent process.
    innocent = await asyncio.create_subprocess_exec("sleep", "600")
    (vm_dir / "ch.pid").write_text(str(innocent.pid))
    try:
        bind_then_abandon(vm_dir / "api.sock")
        info = await app.state.microvm.info(WID)
        assert info.status is VmStatus.STOPPED
        assert info.pid == innocent.pid
    finally:
        innocent.kill()
        await innocent.wait()
    # Recovery is a plain start: fresh API server, fresh spawn,
    # and the boot lands running.
    (vm_dir / "api.sock").unlink()
    fresh = FakeCH(state_dir / "vms" / WID / "api.sock")
    await fresh.start()
    try:
        await app.state.microvm.launch(spec(tmp_path))
        assert (await app.state.microvm.info(WID)).status is VmStatus.RUNNING
    finally:
        await fresh.stop()


async def test_launch_error_maps_and_reaps_process(
    env, tmp_path: Path
) -> None:
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
    settings = Settings(
        vmm=VmmSettings(cloud_hypervisor="false", state_dir=deep)
    )
    app = build_app(settings)
    with pytest.raises(MicrovmError, match="AF_UNIX"):
        await app.state.microvm.launch(spec(tmp_path))


async def test_launch_rejects_double_launch(env, fake, tmp_path: Path) -> None:
    app, _, _ = env
    await app.state.microvm.launch(spec(tmp_path))
    with pytest.raises(MicrovmError, match="already exists"):
        await app.state.microvm.launch(spec(tmp_path))
    await app.state.microvm.kill(WID)


async def test_launch_missing_binary_maps_to_error(
    env, tmp_path: Path
) -> None:
    app, _, _ = env
    app.state.settings.vmm.cloud_hypervisor = "/nonexistent/ch"
    with pytest.raises(MicrovmError, match="not found"):
        await app.state.microvm.launch(spec(tmp_path))


async def test_launch_binary_exits_early_maps_to_error(
    env, tmp_path: Path
) -> None:
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


async def test_info_stopped_when_socket_gone(
    env, fake, tmp_path: Path
) -> None:
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


async def test_info_created_maps_to_starting(
    env, fake, tmp_path: Path
) -> None:
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
    assert ("PUT", "/api/v1/vm.power-button") in [
        (m, p) for m, p, _b in fake.requests
    ]


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


async def test_shutdown_without_process_ref_terminates_pidfile_pid(
    env, fake
) -> None:
    app, state_dir, _ = env
    fake.state = {"state": "Shutdown"}
    sleeper = await spawn_stub_vmm(app)
    (state_dir / "vms" / WID / "ch.pid").write_text(str(sleeper.pid))
    await app.state.microvm.shutdown(WID, timeout_s=5)
    await sleeper.wait()


async def test_shutdown_timeout_when_vmm_ignores_sigterm(
    env, tmp_path: Path
) -> None:
    # A stub that traps SIGTERM: the guest powers off, the VMM refuses to die.
    app, state_dir, _ = env
    stubborn = tmp_path / "ch-stubborn"
    ready = tmp_path / "stubborn-ready"
    stubborn.write_text(
        "#!/usr/bin/env python3\n"
        "import signal, time\n"
        "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
        f"open({str(ready)!r}, 'w').close()\n"
        "time.sleep(600)\n"
    )
    stubborn.chmod(0o755)
    app.state.settings.vmm.cloud_hypervisor = str(stubborn)
    socket_path = state_dir / "vms" / WID / "api.sock"
    socket_path.parent.mkdir(parents=True, exist_ok=True)
    server = FakeCH(socket_path)
    server.state = {"state": "Shutdown"}
    await server.start()
    try:
        await app.state.microvm.launch(spec(tmp_path))
        proc = app.state.microvm.local._procs[WID]
        # Wait out interpreter startup: the handler must be installed
        # before shutdown's SIGTERM, or the stub dies to it (the race
        # flaked ~50% of -n auto runs; #154 review round 2).
        for _ in range(100):
            if ready.exists():
                break
            await asyncio.sleep(0.05)
        else:
            pytest.fail("stubborn stub never became ready")
        with pytest.raises(
            MicrovmTimeoutError, match="did not exit after SIGTERM"
        ):
            await app.state.microvm.shutdown(WID, timeout_s=0.5)
    finally:
        await server.stop()
    await app.state.microvm.kill(WID)
    await proc.wait()  # reap: no orphaned-Process GC warning


async def test_shutdown_without_pidfile_returns_after_guest_down(
    env, fake
) -> None:
    app, state_dir, _ = env
    fake.state = {"state": "Shutdown"}
    await app.state.microvm.shutdown(WID, timeout_s=5)
    assert not (state_dir / "vms" / WID / "ch.pid").exists()


async def test_kill_via_pidfile(env, tmp_path: Path) -> None:
    app, state_dir, _ = env
    vm_dir = state_dir / "vms" / WID
    vm_dir.mkdir(parents=True)
    sleeper = await spawn_stub_vmm(app)
    (vm_dir / "ch.pid").write_text(str(sleeper.pid))
    await app.state.microvm.kill(WID)
    await sleeper.wait()
    with pytest.raises(ProcessLookupError):
        os.kill(sleeper.pid, 0)


async def test_kill_unknown_workspace_is_success(env) -> None:
    # Killing an absent VM is success (the absent-VM contract): a
    # never-started workspace must stay deletable.
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


async def test_cleanup_removes_dir_and_reaps_running_vmm(
    env, fake, tmp_path: Path
) -> None:
    app, state_dir, _ = env
    await app.state.microvm.launch(spec(tmp_path))
    proc = app.state.microvm.local._procs[WID]
    await app.state.microvm.cleanup(WID)
    assert not (state_dir / "vms" / WID).exists()
    # The home volume dies with the workspace (#14), never with a stop.
    assert not persist.home_volume_path(state_dir, WID).exists()
    with pytest.raises(ProcessLookupError):
        os.kill(proc.pid, 0)  # killed and reaped, not orphaned
    await app.state.microvm.cleanup("ghost")  # absent VM: plain success


async def test_prepare_creates_artifacts(env, tmp_path: Path) -> None:
    app, state_dir, _ = env
    await app.state.microvm.prepare(spec(tmp_path))
    assert persist.overlay_path(state_dir, WID).is_file()
    assert persist.home_volume_path(state_dir, WID).is_file()


async def test_prepare_refuses_a_predecessors_artifacts(
    env, tmp_path: Path
) -> None:
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


async def test_launch_heals_a_volume_only_leftover(
    env, fake, tmp_path: Path
) -> None:
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


async def test_prepare_failure_rolls_back_the_pair(
    env, tmp_path: Path
) -> None:
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


async def test_launch_heals_missing_artifacts(
    env, fake, tmp_path: Path
) -> None:
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
        egress=False,
    )
    with pytest.raises(MicrovmError, match="base image.*not found"):
        await app.state.microvm.launch(broken)


async def test_reset_drops_overlay_keeps_home(
    env, fake, tmp_path: Path
) -> None:
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


async def test_driver_resolution_and_validation(env) -> None:
    app, _, _ = env
    assert app.state.microvm.driver is app.state.microvm.local
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
    sleeper = await spawn_stub_vmm(app)
    (vm_dir / "ch.pid").write_text(str(sleeper.pid))
    await app.state.microvm.driver._terminate(
        WID, asyncio.get_running_loop().time() + 5
    )
    await sleeper.wait()  # reap the zombie; kill(pid,0) succeeds until then
    with pytest.raises(ProcessLookupError):
        os.kill(sleeper.pid, 0)


async def test_shutdown_escalates_when_api_dies_midcall(
    env, monkeypatch
) -> None:
    # The VMM looked alive but its API died between the liveness check
    # and the call: shutdown must fall through to SIGTERM (the
    # _terminate path), not surface a 500.
    app, state_dir, _ = env
    vm_dir = state_dir / "vms" / WID
    vm_dir.mkdir(parents=True)
    (vm_dir / "api.sock").write_bytes(b"")
    sleeper = await spawn_stub_vmm(app)
    (vm_dir / "ch.pid").write_text(str(sleeper.pid))

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
    # A live-seeming VMM: a stub whose cmdline passes the identity
    # check (#151).
    stub_vm = await spawn_stub_vmm(app)
    sock.parent.joinpath("ch.pid").write_text(str(stub_vm.pid))
    server = await _fake_vsock_server(sock, b"OK 1073741824\n")
    try:
        reader, writer = await app.state.microvm.console(WID)
        writer.write(b"ping\n")
        await writer.drain()
        assert await reader.readline() == b"PING\n"
        writer.close()
        await writer.wait_closed()
    finally:
        stub_vm.kill()
        await stub_vm.wait()
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
    stub_vm = await spawn_stub_vmm(app)
    (state_dir / "vms" / WID / "ch.pid").write_text(str(stub_vm.pid))
    sock = state_dir / "vms" / WID / "vsock.sock"
    server = await _fake_vsock_server(sock, b"NOK bad-port\n")
    try:
        with pytest.raises(MicrovmError, match="handshake refused"):
            await app.state.microvm.console(WID)
        assert stub_vm.returncode is None, "stub died mid-test"
    finally:
        stub_vm.kill()
        await stub_vm.wait()
        server.close()
        await server.wait_closed()


async def test_default_console_unsupported() -> None:
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
    app.state.settings.vmm.vsock_wait_timeout_s = 1.0
    vm_dir = state_dir / "vms" / WID
    vm_dir.mkdir(parents=True, exist_ok=True)
    stub_vm = await spawn_stub_vmm(app)
    vm_dir.joinpath("ch.pid").write_text(str(stub_vm.pid))

    handlers: list[asyncio.Task] = []

    async def silent(reader, writer):
        try:
            await reader.readline()
            await asyncio.sleep(60)
        finally:
            # Cancelled at teardown below: the writer closes then,
            # not at loop teardown — wait_closed() returns at once.
            writer.close()
            await writer.wait_closed()

    def accept(reader, writer):
        handlers.append(asyncio.create_task(silent(reader, writer)))

    monkeypatch.setattr(local_mod, "VSOCK_REPLY_S", 0.3)
    server = await asyncio.start_unix_server(
        accept, str(vm_dir / "vsock.sock")
    )
    try:
        with pytest.raises(
            MicrovmError, match="handshake reply never arrived"
        ):
            await app.state.microvm.console(WID)
        assert stub_vm.returncode is None, "stub died mid-test"
    finally:
        stub_vm.kill()
        await stub_vm.wait()
        for task in handlers:
            task.cancel()
        await asyncio.gather(*handlers, return_exceptions=True)
        server.close()
        await server.wait_closed()


async def test_console_socket_never_appears(env, monkeypatch) -> None:
    """A live VMM whose vsock socket never appears fails at the
    deadline with the named cause."""
    app, state_dir, _ = env
    app.state.settings.vmm.vsock_wait_timeout_s = 0.1
    vm_dir = state_dir / "vms" / WID
    vm_dir.mkdir(parents=True, exist_ok=True)
    stub_vm = await spawn_stub_vmm(app)
    vm_dir.joinpath("ch.pid").write_text(str(stub_vm.pid))
    try:
        with pytest.raises(MicrovmError, match="vsock socket unreachable"):
            await app.state.microvm.console(WID)
    finally:
        stub_vm.kill()
        await stub_vm.wait()


async def test_console_stream_dies_mid_handshake(env, monkeypatch) -> None:
    """A VMM that resets the stream mid-handshake is retried, then
    fails with the named cause (not a raw OSError)."""
    app, state_dir, _ = env
    app.state.settings.vmm.vsock_wait_timeout_s = 0.1
    vm_dir = state_dir / "vms" / WID
    vm_dir.mkdir(parents=True, exist_ok=True)
    stub_vm = await spawn_stub_vmm(app)
    vm_dir.joinpath("ch.pid").write_text(str(stub_vm.pid))

    async def resetter(reader, writer):
        # Accept, then kill the stream before any reply.
        writer.close()

    server = await asyncio.start_unix_server(
        resetter, str(vm_dir / "vsock.sock")
    )
    try:
        with pytest.raises(MicrovmError, match="handshake"):
            await app.state.microvm.console(WID)
        assert stub_vm.returncode is None, "stub died mid-test"
    finally:
        stub_vm.kill()
        await stub_vm.wait()
        server.close()
        await server.wait_closed()


# --- egress networking (#52) ----------------------------------------------


def test_vm_config_carries_the_net_device(tmp_path: Path) -> None:
    config = vm_config(
        VmSpec(workspace_id=WID, kernel=tmp_path / "k", rootfs=tmp_path / "r"),
        disk_entries(tmp_path, WID),
        tmp_path / "serial.log",
        net={"tap": "msks-abc123", "mac": "02:11:22:33:44:55"},
    )
    assert config["net"] == [
        {"tap": "msks-abc123", "mac": "02:11:22:33:44:55"}
    ]
    # Without egress the VM presents no net device at all.
    plain = vm_config(
        VmSpec(workspace_id=WID, kernel=tmp_path / "k", rootfs=tmp_path / "r"),
        disk_entries(tmp_path, WID),
        tmp_path / "serial.log",
    )
    assert "net" not in plain


def test_vm_net_maps_an_attachment() -> None:
    class Attachment:
        tap = alloc.tap_name(WID)
        mac = alloc.guest_mac(WID)

    assert local_mod.vm_net(Attachment()) == {
        "tap": alloc.tap_name(WID),
        "mac": alloc.guest_mac(WID),
    }
    assert local_mod.vm_net(None) is None


@pytest.fixture
async def egress_env(env, tmp_path: Path, monkeypatch):
    """The env app with egress armed: stub tools, fake services."""
    app, _state_dir, _ = env
    ip_log = tmp_path / "egress-ip.log"
    app.state.settings.net = NetSettings(
        enabled=True,
        ip_tool=str(stub_ip(tmp_path, ip_log)),
        nft_tool=str(stub_nft(tmp_path, tmp_path / "egress-nft.log")),
        dns_upstream="10.9.9.9",
    )
    monkeypatch.setattr(
        manager_mod, "verify_forwarding", lambda path=None: None
    )
    manager = NetManager(
        app, dhcp_factory=FakeService, dns_factory=FakeService
    )
    app.state.net = manager
    # claim_slice records slices on workspace rows (#70 review): the
    # egress boots need a real model behind them.
    app.state.settings.server.db_path = tmp_path / "egress.db"
    app.state.model.migrate()
    await app.state.model.create_workspace(
        VmSpec(
            workspace_id=WID, kernel=Path("/k"), rootfs=Path("/r"), egress=True
        )
    )
    await manager.start()
    return app, ip_log


async def test_launch_attaches_egress_and_configures_the_nic(
    egress_env, fake, tmp_path: Path
) -> None:
    app, ip_log = egress_env
    await app.state.microvm.launch(spec(tmp_path, egress=True))
    body = dict(fake.requests[0][2])
    assert body["net"][0]["tap"].startswith("msks-")
    assert body["net"][0]["mac"].startswith("02:")
    assert app.state.net._attachments[WID] is not None
    # The stub VMM powers off on the button press, like a real guest.
    fake.on_shutdown.append(lambda: fake.state.update(state="Shutdown"))
    await app.state.microvm.shutdown(WID)
    assert WID not in app.state.net._attachments
    assert any(
        line.startswith("link del dev")
        for line in ip_log.read_text().splitlines()
    )


async def test_launch_failure_unwinds_egress(
    egress_env, fake, tmp_path: Path
) -> None:
    app, ip_log = egress_env
    fake.responses[("PUT", "/api/v1/vm.create")] = (500, "boom")
    with pytest.raises(MicrovmError):
        await app.state.microvm.launch(spec(tmp_path, egress=True))
    assert WID not in app.state.net._attachments
    assert any(
        line.startswith("link del dev")
        for line in ip_log.read_text().splitlines()
    )


async def test_launch_refuses_egress_without_the_plumbing(
    env, fake, tmp_path: Path
) -> None:
    app, _state_dir, _ = env  # machinery off: the disabled refusal
    app.state.settings.net = NetSettings(enabled=False)
    await app.state.net.start()
    with pytest.raises(MicrovmError, match="MSKSD_EGRESS_ENABLED"):
        await app.state.microvm.launch(spec(tmp_path, egress=True))


def test_disk_entries_attach_the_seed_read_only(tmp_path: Path) -> None:
    """A user_data workspace (#41) attaches its cidata seed as a
    third, read-only raw disk; a plain workspace keeps two disks."""
    overlay, home, seed = disk_entries(
        tmp_path, WID, user_data="#!/bin/sh\ntrue\n"
    )
    assert overlay["path"] == str(tmp_path / "vms" / WID / "root.qcow2")
    assert home["path"] == str(tmp_path / "volumes" / f"{WID}.ext4")
    assert seed == {
        "path": str(tmp_path / "vms" / WID / "seed.img"),
        "readonly": True,
        "image_type": "Raw",
    }
    assert len(disk_entries(tmp_path, WID)) == 2


def test_disk_entries_attach_the_seed_for_identity(tmp_path: Path) -> None:
    """A minted identity attaches the seed with no user_data at all
    (#111) — the seeding script is the whole payload; both present
    together still attach exactly one seed.
    """
    seed_only = disk_entries(
        tmp_path, WID, ssh_pubkey="ecdsa-sha2-nistp256 AAAA"
    )
    assert len(seed_only) == 3
    assert seed_only[2]["readonly"] is True
    both = disk_entries(
        tmp_path, WID, user_data="#!/bin/sh\ntrue\n", ssh_pubkey="ecdsa AAAA"
    )
    assert len(both) == 3


async def test_launch_attaches_the_user_data_seed(
    env, fake, tmp_path: Path
) -> None:
    """A user_data workspace boots with three disks (#41): the seed
    is healed by launch like the other artifacts and reaches the VMM
    read-only."""
    app, state_dir, _ = env
    payload = "#!/bin/sh\necho seeded > /root/stamp\n"
    boot_spec = replace(spec(tmp_path), user_data=payload)
    await app.state.microvm.launch(boot_spec)
    seed = persist.seed_path(state_dir, WID)
    assert seed.is_file()
    body = dict(fake.requests[0][2])
    assert body["disks"] == disk_entries(state_dir, WID, user_data=payload)
    assert body["disks"][2]["readonly"] is True
    await app.state.microvm.kill(WID)


# --- console identity prelude (#63) -----------------------------------


@pytest.mark.asyncio
async def test_handshake_sends_prelude(tmp_path: Path) -> None:
    path = tmp_path / "guest.sock"
    seen = {}

    async def session(reader, writer):
        seen["connect"] = await reader.readline()
        writer.write(b"OK 5\n")
        await writer.drain()
        seen["prelude"] = await reader.readuntil(b"GO\n")
        writer.write(b"MSKS OK msks\n")
        await writer.drain()
        writer.close()

    server = await asyncio.start_unix_server(session, str(path))
    try:
        reader, writer = await local_mod._vsock_handshake(
            path, 1023, user="msks", rows=34, cols=120
        )
    finally:
        server.close()
        await server.wait_closed()
    assert seen["connect"] == b"CONNECT 1023\n"
    assert seen["prelude"] == (
        b"HELLO 1\nUSER msks\nTERM xterm\nWINSZ 34 120\nGO\n"
    )
    writer.close()


@pytest.mark.asyncio
async def test_prelude_carries_term(tmp_path: Path) -> None:
    path = tmp_path / "guest.sock"
    seen = {}

    async def session(reader, writer):
        await reader.readline()
        writer.write(b"OK 5\n")
        await writer.drain()
        seen["prelude"] = await reader.readuntil(b"GO\n")
        writer.write(b"MSKS OK msks\n")
        await writer.drain()
        writer.close()

    server = await asyncio.start_unix_server(session, str(path))
    try:
        reader, writer = await local_mod._vsock_handshake(
            path, 1023, user="msks", term="tmux-256color"
        )
    finally:
        server.close()
        await server.wait_closed()
    assert b"TERM tmux-256color\n" in seen["prelude"]
    writer.close()


@pytest.mark.asyncio
async def test_prelude_refusal_raises(tmp_path: Path) -> None:
    path = tmp_path / "guest.sock"

    async def session(reader, writer):
        await reader.readline()
        writer.write(b"OK 5\n")
        await writer.drain()
        assert await reader.readuntil(b"GO\n")
        writer.write(b"MSKS ERR auth\n")
        await writer.drain()
        writer.close()

    server = await asyncio.start_unix_server(session, str(path))
    try:
        with pytest.raises(MicrovmError) as caught:
            await local_mod._vsock_handshake(path, 1023, user="msks")
    finally:
        server.close()
        await server.wait_closed()
    assert "refused user 'msks': auth" in str(caught.value)


@pytest.mark.asyncio
async def test_prelude_user_refusal_is_retryable(tmp_path: Path) -> None:
    """The helper's absent-account refusal (#248) reads as a
    boot-state, not a verdict: the handshake raises the retryable
    marker the console loop waits out, because the daemon's own
    gate already admitted the name and the first-boot seed that
    creates the account lands with the same boot."""
    path = tmp_path / "guest.sock"

    async def session(reader, writer):
        await reader.readline()
        writer.write(b"OK 5\n")
        await writer.drain()
        assert await reader.readuntil(b"GO\n")
        writer.write(b"MSKS ERR user\n")
        await writer.drain()
        writer.close()

    server = await asyncio.start_unix_server(session, str(path))
    try:
        with pytest.raises(local_mod._UserRetry) as caught:
            await local_mod._vsock_handshake(
                path, 1023, user="a" * 32
            )  # the charset's longest name
    finally:
        server.close()
        await server.wait_closed()
    assert "serves no such account" in str(caught.value)
    # The message rides a websocket close reason (a 123-byte wire
    # budget; the daemon truncates at 120) — at the longest name
    # it must still fit whole, or the refusal names nothing.
    assert len(str(caught.value).encode()) <= 120


@pytest.mark.asyncio
async def test_prelude_garbage_fails_closed(tmp_path: Path) -> None:
    path = tmp_path / "guest.sock"

    async def session(reader, writer):
        await reader.readline()
        writer.write(b"OK 5\n")
        await writer.drain()
        assert await reader.readuntil(b"GO\n")
        writer.write(b"welcome to debian\n")
        await writer.drain()
        writer.close()

    server = await asyncio.start_unix_server(session, str(path))
    try:
        with pytest.raises(MicrovmError, match="unrecognized"):
            await local_mod._vsock_handshake(path, 1023, user="root")
    finally:
        server.close()
        await server.wait_closed()


@pytest.mark.asyncio
async def test_prelude_silence_fails_closed(tmp_path: Path) -> None:
    path = tmp_path / "guest.sock"

    async def session(reader, writer):
        await reader.readline()
        writer.write(b"OK 5\n")
        await writer.drain()
        assert await reader.readuntil(b"GO\n")
        # No reply at all: the guest went away mid-negotiation.
        writer.close()

    server = await asyncio.start_unix_server(session, str(path))
    try:
        with pytest.raises(MicrovmError, match="prelude"):
            await local_mod._vsock_handshake(path, 1023, user="root")
    finally:
        server.close()
        await server.wait_closed()


@pytest.mark.asyncio
async def test_prelude_timeout_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "guest.sock"

    async def session(reader, writer):
        await reader.readline()
        writer.write(b"OK 5\n")
        await writer.drain()
        assert await reader.readuntil(b"GO\n")
        # Reads the prelude, then goes silent forever.
        await asyncio.sleep(10)

    monkeypatch.setattr(local_mod, "PRELUDE_REPLY_S", 0.2)
    server = await asyncio.start_unix_server(session, str(path))
    try:
        with pytest.raises(MicrovmError, match="prelude"):
            await local_mod._vsock_handshake(path, 1023, user="root")
    finally:
        server.close()
        await server.wait_closed()


@pytest.mark.asyncio
async def test_handshake_without_user_sends_no_prelude(tmp_path: Path) -> None:
    path = tmp_path / "guest.sock"
    seen = {}

    async def session(reader, writer):
        seen["connect"] = await reader.readline()
        writer.write(b"OK 5\n")
        await writer.drain()
        # A bounded peek: the legacy path sends no prelude, so nothing
        # arrives before the client hangs up.
        try:
            data = await asyncio.wait_for(reader.read(64), 1.0)
        except TimeoutError:
            data = b"<timeout>"
        seen["rest"] = data
        writer.close()

    server = await asyncio.start_unix_server(session, str(path))
    try:
        reader, writer = await local_mod._vsock_handshake(path, 1023)
    finally:
        server.close()
        await server.wait_closed()
    assert seen["rest"] in (b"", None) or not seen["rest"].startswith(b"HELLO")
    writer.close()


async def spawn_stub_vmm(
    app, workspace_id: str = WID
) -> asyncio.subprocess.Process:
    """A process that #151's identity check accepts as OUR VMM.

    The liveness checks read /proc/<pid>/cmdline and require the
    configured VMM binary and this workspace's --api-socket
    argument there; spawning the fixture stub itself with the same
    argv the driver uses gives a killable process whose identity
    matches exactly. Bare `sleep` reads as a foreign process and
    takes the dead-VMM path (which never signals it).

    The stub announces userland via MSKS_STUB_READY, and only that
    announcement releases this helper: a `#!/usr/bin/env python3`
    shebang runs an exec CHAIN whose env phase carries the same
    stub and --api-socket fields (identity confirms mid-chain), and
    the next exec transition briefly reads an EMPTY cmdline --
    sampled in that window, liveness read as "no live VMM" and
    flaked the console tests under load (#154 r2).
    """
    binary = app.state.settings.vmm.cloud_hypervisor
    vm_dir = app.state.microvm.local._dir(workspace_id)
    vm_dir.mkdir(parents=True, exist_ok=True)
    ready = vm_dir / f"stub-ready-{os.getpid()}-{next(_stub_seq)}"
    proc = await asyncio.create_subprocess_exec(
        str(binary),
        "--api-socket",
        str(vm_dir / "api.sock"),
        env={**os.environ, "MSKS_STUB_READY": str(ready)},
    )
    for _ in range(200):
        if ready.exists():
            return proc
        assert proc.returncode is None, "stub exited before ready"
        await asyncio.sleep(0.05)
    proc.kill()
    await proc.wait()
    pytest.fail("stub VMM never became ready")


_stub_seq = itertools.count()


async def test_kill_never_signals_a_foreign_pid(env, tmp_path: Path) -> None:
    """The identity check's security property, pinned (#154 r2): a
    recycled pidfile pid owned by some innocent process is never
    signaled -- kill() reports success and leaves it alive."""
    app, state_dir, _ = env
    vm_dir = state_dir / "vms" / WID
    vm_dir.mkdir(parents=True)
    foreign = await asyncio.create_subprocess_exec("sleep", "600")
    (vm_dir / "ch.pid").write_text(str(foreign.pid))
    try:
        await app.state.microvm.kill(WID)  # must NOT touch it
        assert foreign.returncode is None
        os.kill(foreign.pid, 0)  # alive and well
    finally:
        foreign.kill()
        await foreign.wait()


async def test_pid_alive_names_a_foreign_owner(env, monkeypatch) -> None:
    """kill(0) EPERM on a FOREIGN pid reads as dead -- mixed-uid
    hosts recycle pids too; silence, not an error (#154 r2)."""
    app, _, _ = env
    driver = app.state.microvm.local
    foreign = await asyncio.create_subprocess_exec("sleep", "600")

    def eperm(pid: int, sig: int) -> None:
        raise PermissionError(1, "not yours")

    monkeypatch.setattr(local_mod.os, "kill", eperm)
    try:
        assert not driver._pid_alive(foreign.pid, WID)
    finally:
        monkeypatch.undo()  # before proc cleanup: it signals too
        foreign.kill()
        await foreign.wait()


async def test_pid_alive_names_our_eperm_vmm(env, monkeypatch) -> None:
    """kill(0) EPERM on OUR VMM is named, not silent: the operator
    learns the pid is owned by another user (#154 r2)."""
    app, _, _ = env
    driver = app.state.microvm.local
    stub_vm = await spawn_stub_vmm(app)

    def eperm(pid: int, sig: int) -> None:
        raise PermissionError(1, "not yours")

    monkeypatch.setattr(local_mod.os, "kill", eperm)
    try:
        with pytest.raises(MicrovmError, match="owned by another user"):
            driver._pid_alive(stub_vm.pid, WID)
    finally:
        monkeypatch.undo()
        stub_vm.kill()
        await stub_vm.wait()


def bind_then_abandon(path: Path) -> None:
    """A genuine stale socket: bound, then closed with the file left.

    The exact residue a hard-killed VMM leaves — a socket file at
    the name with nothing listening (#151).
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    srv.bind(str(path))
    srv.close()  # the file stays; no listener does


async def test_launch_sweeps_stale_sockets(env, fake, tmp_path: Path) -> None:
    """Socket residue from a hard kill must not doom the next start (#151).

    A VMM killed without cleanup leaves api.sock and vsock.sock
    behind; the next spawn died binding them (AddrInUse) and the
    operator saw a misleading unreachable-API 503. The stale vsock
    residue here is the incident's exact shape (bound, abandoned);
    the fake's LIVE api.sock pre-bound beside it proves the sweep
    takes only what nothing listens on — its removal is asserted
    because nothing (the fake does not emulate vsock) re-creates it.
    """
    app, _, _ = env
    vm_dir = app.state.microvm.local._dir(WID)
    assert (vm_dir / "api.sock").exists()  # the fake's live listener
    stale = vm_dir / "vsock.sock"
    bind_then_abandon(stale)
    inode_before = (vm_dir / "api.sock").stat().st_ino
    await app.state.microvm.launch(spec(tmp_path))
    assert not stale.exists()  # swept; nothing recreated it
    # The pidfile is rewritten by the live launch; the fake's live
    # socket keeps its very inode -- the sweep never touched it.
    assert (vm_dir / "ch.pid").read_text().strip().isdigit()
    assert (vm_dir / "api.sock").stat().st_ino == inode_before
    info = await app.state.microvm.info(WID)
    assert info.status is VmStatus.RUNNING


async def test_launch_names_a_sweep_obstacle_it_cannot_remove(
    env, tmp_path: Path
) -> None:
    """A directory squatting on the socket name is refused (stale)
    but cannot be unlinked; the operator error says so, pointing at
    the exact path (#154 review)."""
    app, state_dir, _ = env
    vm_dir = state_dir / "vms" / WID
    vm_dir.mkdir(parents=True, exist_ok=True)
    (vm_dir / "api.sock").mkdir()
    (vm_dir / "ch.pid").mkdir()
    with pytest.raises(
        MicrovmError, match="cannot remove stale socket.*remove it by hand"
    ):
        await app.state.microvm.launch(spec(tmp_path))
    # The pidfile obstacle names itself the same way.
    (vm_dir / "api.sock").rmdir()
    with pytest.raises(MicrovmError, match="cannot remove stale pidfile"):
        await app.state.microvm.launch(spec(tmp_path))


def test_socket_stale_sweeps_a_dangling_symlink(tmp_path: Path) -> None:
    """lexists, not exists: a dangling symlink at the socket name is
    residue too -- it refuses the bind as surely as a file (#154
    r3)."""
    from msks.microvm.local import socket_stale

    target = tmp_path / "gone.sock"
    link = tmp_path / "api.sock"
    link.symlink_to(target)  # target never exists: the link dangles
    assert socket_stale(link)


def test_cmdline_is_vmm_matrix(tmp_path: Path) -> None:
    """The identity check's adversarial matrix (#154 r2/r3): exact-
    field binary match plus this workspace's --api-socket pair."""
    from msks.microvm.local import cmdline_is_vmm

    sock = tmp_path / "vms" / "ws" / "api.sock"
    ours = b"cloud-hypervisor\0--api-socket\0" + os.fsencode(str(sock)) + b"\0"
    assert cmdline_is_vmm(ours, "cloud-hypervisor", sock)
    # Shebang chain: interpreter argv[0], binary rides as argv[1].
    chain = (
        b"/usr/bin/env\0cloud-hypervisor\0--api-socket\0"
        + os.fsencode(str(sock))
        + b"\0"
    )
    assert cmdline_is_vmm(chain, "cloud-hypervisor", sock)
    # The F-a lookalikes: substring and bare-name-without-socket.
    assert not cmdline_is_vmm(
        b"grep\0cloud-hypervisor\0/var/log/syslog\0",
        "cloud-hypervisor",
        sock,
    )
    assert not cmdline_is_vmm(
        b"cloud-hypervisor-v2\0--api-socket\0"
        + os.fsencode(str(sock))
        + b"\0",
        "cloud-hypervisor",
        sock,
    )
    # Another workspace's socket is not ours.
    other = tmp_path / "vms" / "other" / "api.sock"
    assert not cmdline_is_vmm(
        b"cloud-hypervisor\0--api-socket\0" + os.fsencode(str(other)) + b"\0",
        "cloud-hypervisor",
        sock,
    )
    # Empty cmdline (an exec window) proves nothing.
    assert not cmdline_is_vmm(b"", "cloud-hypervisor", sock)
    # A --api-socket flag at argv end with no value cannot pair.
    assert not cmdline_is_vmm(
        b"cloud-hypervisor\0--api-socket", "cloud-hypervisor", sock
    )


def test_socket_stale_contract(tmp_path: Path) -> None:
    """Absent no, abandoned yes, live no — the sweep's whole policy."""
    from msks.microvm.local import socket_stale

    assert not socket_stale(tmp_path / "absent.sock")
    abandoned = tmp_path / "abandoned.sock"
    bind_then_abandon(abandoned)
    assert socket_stale(abandoned)
    live = tmp_path / "live.sock"
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    srv.bind(str(live))
    srv.listen(1)
    fillers: list[socket.socket] = []
    try:
        assert not socket_stale(live)
        # Saturate the accept backlog (#154 review): a live listener
        # whose queue is full must still read as LIVE -- connect
        # then answers EAGAIN or times out, neither of the refusal
        # pair that may be swept.
        for _ in range(64):
            filler = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            filler.settimeout(0.2)
            try:
                filler.connect(str(live))
                fillers.append(filler)
            except OSError:
                break  # the queue is full; the probe is next
        else:
            pytest.fail("could not saturate the backlog")
        assert not socket_stale(live)  # EAGAIN must read as LIVE
    finally:
        for filler in fillers:
            filler.close()
        srv.close()


async def test_launch_reports_the_vmm_log_tail_on_early_exit(
    env, tmp_path: Path
) -> None:
    """A dying VMM names its own cause in the error (#151).

    The stub binary replays the real fatal line from the incident
    (CreateApiServerSocket AddrInUse) into ch.log and exits: the
    operator error must carry it, not a bare exit code.
    """
    app, _, _ = env
    stub = tmp_path / "vmm-stub"
    stub.write_text(
        "#!/bin/sh\n"
        'echo "cloud-hypervisor: Fatal error: '
        'CreateApiServerSocket(Os { code: 98, AddrInUse })" >&2\n'
        "exit 1\n"
    )
    stub.chmod(0o755)
    app.state.settings.vmm.cloud_hypervisor = str(stub)
    with pytest.raises(MicrovmError, match="AddrInUse"):
        await app.state.microvm.launch(spec(tmp_path))


def test_log_tail_shapes() -> None:
    from msks.microvm.local import LocalCloudHypervisor

    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "ch.log"
        p.write_text("one line\n")
        assert LocalCloudHypervisor._log_tail(p) == "; last log line: one line"
        p.write_text("")
        assert LocalCloudHypervisor._log_tail(p) == " (log empty)"
        p.unlink()
        assert LocalCloudHypervisor._log_tail(p) == " (log unreadable)"


@pytest.mark.asyncio
async def test_console_user_refusal_expires_at_the_deadline(
    env, monkeypatch
) -> None:
    """A guest that keeps refusing the login user past the vsock
    deadline fails with the seed named (#248) — not the generic
    is-the-VM-running line a bring-up failure carries."""
    app, state_dir, _ = env
    app.state.settings.vmm.vsock_wait_timeout_s = 0.1
    vm_dir = state_dir / "vms" / WID
    vm_dir.mkdir(parents=True, exist_ok=True)
    stub_vm = await spawn_stub_vmm(app)
    vm_dir.joinpath("ch.pid").write_text(str(stub_vm.pid))
    path = vm_dir / "vsock.sock"

    async def session(reader, writer):
        await reader.readline()
        writer.write(b"OK 5\n")
        await writer.drain()
        assert await reader.readuntil(b"GO\n")
        writer.write(b"MSKS ERR user\n")
        await writer.drain()
        writer.close()

    server = await asyncio.start_unix_server(session, str(path))
    try:
        with pytest.raises(MicrovmError, match="serves no such account"):
            await app.state.microvm.console(WID, user="alice")
    finally:
        server.close()
        await server.wait_closed()
        stub_vm.kill()
        await stub_vm.wait()
