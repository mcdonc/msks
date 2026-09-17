"""user_data provisioning smokes: script payloads and cloud-config."""

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
async def test_local_user_data_provisioning() -> None:
    """The #41 seed end to end on real KVM, through cloud-init: a
    user_data workspace gets a cidata seed built at prepare; the
    guest's cloud-init runs a script payload on the first boot, a
    stop/start cycle does not re-run it, and a factory reset (the
    overlay's death takes /var/lib/cloud with it) re-provisions from
    the same seed. A cloud-config document lands too (write_files),
    and the seed reaches the guest as a labeled disk.
    """
    state_dir = Path(f"/tmp/msks-smoke-{uuid.uuid4().hex[:8]}")
    settings = Settings(vmm=VmmSettings(state_dir=state_dir))
    app = build_app(settings)
    microvm = app.state.microvm
    wid = f"smoke-{uuid.uuid4().hex[:8]}"
    serial_log = state_dir / "vms" / wid / "serial.log"
    script = (
        "#!/bin/sh\n"
        "count=$(cat /root/firstboot-count 2>/dev/null || echo 0)\n"
        "echo $((count + 1)) > /root/firstboot-count\n"
    )
    spec = VmSpec(
        workspace_id=wid,
        kernel=Path(VMLINUX),
        rootfs=Path(ROOTFS),
        initrd=Path(INITRD) if INITRD else None,
        cmdline=CMDLINE or "console=hvc0 root=/dev/vda rw",
        root_mib=2048,
        home_mib=256,
        egress=False,
        user_data=script,
    )

    async def boot_and_probe(expected_count: int) -> None:
        await microvm.launch(spec)
        await await_guest_up(serial_log)
        # Payloads run in cloud-final, which can lag the login getty;
        # wait for cloud-init to be done before asserting on files it
        # was supposed to write.
        await run_in_console(microvm, wid, "cloud-init status --wait", "done")
        await run_in_console(
            microvm, wid, "cat /root/firstboot-count", str(expected_count)
        )
        # The seed reaches the guest as a labeled, read-only disk.
        await run_in_console(microvm, wid, "blkid -o value -s LABEL /dev/vdc", "cidata")
        await microvm.shutdown(wid, timeout_s=SHUTDOWN_TIMEOUT_S)

    try:
        await microvm.prepare(spec)
        seed = persist.seed_path(state_dir, wid)
        assert seed.is_file()
        await boot_and_probe(1)
        # stop/start: cloud-init's state survives on the overlay, the
        # script does not run again.
        serial_log.unlink(missing_ok=True)
        await boot_and_probe(1)
        # Factory reset: the overlay (cloud-init state included) dies;
        # the seed stays and provisions the pristine root again.
        serial_log.unlink(missing_ok=True)
        await microvm.reset(wid)
        assert not persist.overlay_path(state_dir, wid).exists()
        assert seed.is_file()
        await boot_and_probe(1)
    except BaseException:
        collect_failure_evidence(state_dir, wid, serial_log)
        with contextlib.suppress(Exception):
            await microvm.kill(wid)
        raise
    finally:
        with contextlib.suppress(Exception):
            await microvm.cleanup(wid)
        shutil.rmtree(state_dir, ignore_errors=True)


@needs_local
async def test_local_user_data_cloud_config() -> None:
    """A cloud-config document (the payload form scripts cannot
    cover) lands through cloud-init: write_files puts the file where
    the document says, on the first boot and only there."""
    state_dir = Path(f"/tmp/msks-smoke-{uuid.uuid4().hex[:8]}")
    settings = Settings(vmm=VmmSettings(state_dir=state_dir))
    app = build_app(settings)
    microvm = app.state.microvm
    wid = f"smoke-{uuid.uuid4().hex[:8]}"
    serial_log = state_dir / "vms" / wid / "serial.log"
    marker = f"CLOUDCONFIG-{uuid.uuid4().hex[:6]}"
    cloud_config = (
        "#cloud-config\n"
        "write_files:\n"
        "  - path: /root/provisioned.txt\n"
        f"    content: {marker}\n"
    )
    spec = VmSpec(
        workspace_id=wid,
        kernel=Path(VMLINUX),
        rootfs=Path(ROOTFS),
        initrd=Path(INITRD) if INITRD else None,
        cmdline=CMDLINE or "console=hvc0 root=/dev/vda rw",
        root_mib=2048,
        home_mib=256,
        egress=False,
        user_data=cloud_config,
    )
    try:
        await microvm.launch(spec)
        await await_guest_up(serial_log)
        await run_in_console(microvm, wid, "cloud-init status --wait", "done")
        await run_in_console(microvm, wid, "cat /root/provisioned.txt", marker)
        await microvm.shutdown(wid, timeout_s=SHUTDOWN_TIMEOUT_S)
    except BaseException:
        collect_failure_evidence(state_dir, wid, serial_log)
        with contextlib.suppress(Exception):
            await microvm.kill(wid)
        raise
    finally:
        with contextlib.suppress(Exception):
            await microvm.cleanup(wid)
        shutil.rmtree(state_dir, ignore_errors=True)
