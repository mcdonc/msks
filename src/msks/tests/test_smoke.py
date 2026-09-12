"""Opt-in smoke tests against real infrastructure.

- Local: boots a real cloud-hypervisor VM when MSKSD_TEST_VMLINUX and
  MSKSD_TEST_ROOTFS point at guest artifacts (and /dev/kvm is
  accessible); skipped otherwise. The devenv task `msks:build-guest`
  sets all of these from `.guest/` automatically (see conftest.py and
  msks.guestassets); the stock nixpkgs kernel also needs the initrd
  (MSKSD_TEST_INITRD) and the cmdline the manifest carries
  (MSKSD_TEST_CMDLINE) to reach userspace.
- k8s: creates and tears down a runner pod when MSKSD_TEST_KUBECONFIG
  points at a cluster kubeconfig (k3s in dev); skipped otherwise.

These never count toward the coverage gate (the package is fully
covered by the faked-transport unit suites).
"""

import asyncio
import os
import shutil
import uuid
from pathlib import Path

import pytest
from msks.app import build_app
from msks.microvm import VmSpec
from msks.settings import K8sSettings, Settings, VmmSettings

VMLINUX = os.environ.get("MSKSD_TEST_VMLINUX")
INITRD = os.environ.get("MSKSD_TEST_INITRD")
ROOTFS = os.environ.get("MSKSD_TEST_ROOTFS")
CMDLINE = os.environ.get("MSKSD_TEST_CMDLINE")
KUBECONFIG = os.environ.get("MSKSD_TEST_KUBECONFIG")

needs_local = pytest.mark.skipif(
    not VMLINUX or not ROOTFS or not os.access("/dev/kvm", os.W_OK),
    reason="set MSKSD_TEST_VMLINUX/MSKSD_TEST_ROOTFS with /dev/kvm access",
)
needs_k8s = pytest.mark.skipif(not KUBECONFIG, reason="set MSKSD_TEST_KUBECONFIG")

#: The guest init prints this on the serial console once userspace
#: (and the acpid that answers host-side shutdowns) is up.
GUEST_UP_MARKER = "msks guest: kernel"


async def await_guest_up(serial_log: Path, timeout_s: float = 60.0) -> None:
    """Block until the guest announces itself on the serial console."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout_s
    while loop.time() < deadline:
        text = serial_log.read_text(errors="replace") if serial_log.exists() else ""
        if GUEST_UP_MARKER in text:
            return
        await asyncio.sleep(0.2)
    raise AssertionError(f"guest serial never showed {GUEST_UP_MARKER!r}")


@needs_local
async def test_local_vm_boot_and_shutdown() -> None:
    # A shallow base: deep pytest tmp dirs can push the API socket path
    # past the AF_UNIX 108-byte limit under xdist workers.
    state_dir = Path(f"/tmp/msks-smoke-{uuid.uuid4().hex[:8]}")
    settings = Settings(vmm=VmmSettings(state_dir=state_dir))
    app = build_app(settings)
    wid = f"smoke-{uuid.uuid4().hex[:8]}"
    spec = VmSpec(
        workspace_id=wid,
        kernel=Path(VMLINUX),
        rootfs=Path(ROOTFS),
        initrd=Path(INITRD) if INITRD else None,
        cmdline=CMDLINE or "console=hvc0 root=/dev/vda rw",
    )
    await app.state.microvm.launch(spec)
    info = await app.state.microvm.info(wid)
    assert info.status.value == "running"
    # Wait for userspace before shutting down: the graceful shutdown is
    # an ACPI power-button press, and the guest only answers it once its
    # acpid is running — pressing earlier would drop the event and time
    # out against a VM that is running but not yet listening.
    await await_guest_up(state_dir / "vms" / wid / "serial.log")
    await app.state.microvm.shutdown(wid, timeout_s=30)
    final = await app.state.microvm.info(wid)
    assert final.status.value in ("stopped", "absent")
    await app.state.microvm.cleanup(wid)
    shutil.rmtree(state_dir, ignore_errors=True)


@needs_k8s
async def test_k8s_pod_lifecycle(tmp_path: Path) -> None:
    settings = Settings(
        vmm=VmmSettings(driver="k8s"),
        k8s=K8sSettings(
            kubeconfig=KUBECONFIG,
            namespace=os.environ.get("MSKSD_TEST_NAMESPACE", "default"),
        ),
    )
    app = build_app(settings)
    wid = f"smoke-{uuid.uuid4().hex[:8]}"
    spec = VmSpec(
        workspace_id=wid, kernel=tmp_path / "vmlinux", rootfs=tmp_path / "rootfs"
    )
    await app.state.microvm.launch(spec)
    info = await app.state.microvm.info(wid)
    assert info.status.value in ("starting", "running")
    await app.state.microvm.kill(wid)
    gone = await app.state.microvm.info(wid)
    assert gone.status.value in ("stopped", "absent", "unknown")
