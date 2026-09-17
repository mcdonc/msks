"""Local VM lifecycle smokes: boot, graceful shutdown, persistence, reset."""

import contextlib
import shutil
import uuid
from pathlib import Path

from msks.app import build_app
from msks.microvm import VmSpec
from msks.settings import (
    Settings,
    VmmSettings,
)

from msks import persist
from test_smoke import (
    CMDLINE,
    INITRD,
    ROOTFS,
    SHUTDOWN_TIMEOUT_S,
    VMLINUX,
    await_guest_up,
    collect_failure_evidence,
    needs_local,
    run_in_console,
)


@needs_local
async def test_local_vm_boot_and_shutdown() -> None:
    # A shallow base: deep pytest tmp dirs can push the API socket path
    # past the AF_UNIX 108-byte limit under xdist workers. Shutdown is
    # the power-button press — the guest's logind runs the clean
    # poweroff, which matters with persistent disks (#14: a hard stop
    # would drop page-cache writes).
    state_dir = Path(f"/tmp/msks-smoke-{uuid.uuid4().hex[:8]}")
    settings = Settings(vmm=VmmSettings(state_dir=state_dir))
    app = build_app(settings)
    microvm = app.state.microvm
    wid = f"smoke-{uuid.uuid4().hex[:8]}"
    serial_log = state_dir / "vms" / wid / "serial.log"
    spec = VmSpec(
        workspace_id=wid,
        kernel=Path(VMLINUX),
        rootfs=Path(ROOTFS),
        initrd=Path(INITRD) if INITRD else None,
        cmdline=CMDLINE or "console=hvc0 root=/dev/vda rw",
        egress=False,
    )
    try:
        await microvm.launch(spec)
        info = await microvm.info(wid)
        assert info.status.value == "running"
        # Wait for userspace before shutting down: the graceful shutdown
        # is an ACPI power-button press, and the guest only answers it
        # once its logind is running — pressing earlier would drop the
        # event and time out against a VM that is running but not yet
        # listening.
        await await_guest_up(serial_log)
        await microvm.shutdown(wid, timeout_s=SHUTDOWN_TIMEOUT_S)
        final = await microvm.info(wid)
        assert final.status.value in ("stopped", "absent")
    except BaseException:
        # Never leak a live VMM (and its /dev/kvm handle) on failure;
        # print and keep the guest's evidence for the artifact upload.
        collect_failure_evidence(state_dir, wid, serial_log)
        with contextlib.suppress(Exception):
            await microvm.kill(wid)
        raise
    finally:
        with contextlib.suppress(Exception):
            await microvm.cleanup(wid)
        shutil.rmtree(state_dir, ignore_errors=True)


@needs_local
async def test_local_persistence_across_restart_and_reset() -> None:
    """The two persistent artifacts (#14), end to end on real KVM:

    - a root write (what an ``apt install`` does) survives a full
      stop/start cycle — the overlay, not the base, carries it;
    - a /home write survives the same cycle — the home volume;
    - factory reset drops the root write and keeps the /home write.
    """
    state_dir = Path(f"/tmp/msks-smoke-{uuid.uuid4().hex[:8]}")
    settings = Settings(vmm=VmmSettings(state_dir=state_dir))
    app = build_app(settings)
    microvm = app.state.microvm
    wid = f"smoke-{uuid.uuid4().hex[:8]}"
    serial_log = state_dir / "vms" / wid / "serial.log"
    spec = VmSpec(
        workspace_id=wid,
        kernel=Path(VMLINUX),
        rootfs=Path(ROOTFS),
        initrd=Path(INITRD) if INITRD else None,
        cmdline=CMDLINE or "console=hvc0 root=/dev/vda rw",
        root_mib=2048,
        home_mib=256,
        egress=False,
    )
    root_marker = f"ROOT-{uuid.uuid4().hex[:6]}"
    home_marker = f"HOME-{uuid.uuid4().hex[:6]}"

    async def boot_and_probe(probe_commands: list[tuple[str, str]]) -> None:
        await microvm.launch(spec)
        await await_guest_up(serial_log)
        for command, marker in probe_commands:
            await run_in_console(microvm, wid, command, marker)
        await microvm.shutdown(wid, timeout_s=SHUTDOWN_TIMEOUT_S)

    try:
        await microvm.prepare(spec)
        assert persist.overlay_path(state_dir, wid).is_file()
        assert persist.home_volume_path(state_dir, wid).is_file()
        await boot_and_probe(
            [
                # The write sentinels are guest-computed ($((6*7)) → 42):
                # the pty echoes the sent bytes, so a marker that
                # appears in the command text would match the echo —
                # the restart boot's `cat` (below) carries the actual
                # content claim, with a per-run marker the sent bytes
                # never contain.
                (
                    f"echo {root_marker} > /root/probe && echo WROTE-$((6*7))",
                    "WROTE-42",
                ),
                (
                    f"echo {home_marker} > /home/probe && echo WROTE-$((6*7))",
                    "WROTE-42",
                ),
            ]
        )
        serial_log.unlink(missing_ok=True)
        await boot_and_probe(
            [
                ("cat /root/probe", root_marker),
                ("cat /home/probe", home_marker),
            ]
        )
        # Factory reset: pristine root, same /home.
        serial_log.unlink(missing_ok=True)
        await microvm.reset(wid)
        assert not persist.overlay_path(state_dir, wid).exists()
        assert persist.home_volume_path(state_dir, wid).is_file()
        await boot_and_probe(
            [
                ("cat /home/probe", home_marker),
                ("test ! -e /root/probe && echo GONE-$((6*7))", "GONE-42"),
            ]
        )
    except BaseException:
        collect_failure_evidence(state_dir, wid, serial_log)
        with contextlib.suppress(Exception):
            await microvm.kill(wid)
        raise
    finally:
        with contextlib.suppress(Exception):
            await microvm.cleanup(wid)
        shutil.rmtree(state_dir, ignore_errors=True)
