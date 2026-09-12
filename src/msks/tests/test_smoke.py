"""Opt-in smoke tests against real infrastructure.

- Local: boots a real cloud-hypervisor VM when MSKSD_TEST_VMLINUX and
  MSKSD_TEST_ROOTFS point at guest artifacts (and /dev/kvm is
  accessible); skipped otherwise. The devenv task `msks:build-guest`
  sets all of these from `.guest/` automatically (see conftest.py and
  msks.guestassets); the stock nixpkgs kernel also needs the initrd
  (MSKSD_TEST_INITRD) and the cmdline the manifest carries
  (MSKSD_TEST_CMDLINE) to reach userspace.
- k8s: boots the runner image's guest in a pod when MSKSD_TEST_KUBECONFIG
  points at a cluster kubeconfig (k3s in dev); skipped otherwise. The
  runner image owns the guest artifacts, so the pod needs a KVM-capable
  node and the imported image, nothing from this host.

These never count toward the coverage gate (the package is fully
covered by the faked-transport unit suites).
"""

import asyncio
import contextlib
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


def serial_tail(serial_log: Path, limit: int = 2000) -> str:
    """The end of the guest's serial log, for failure messages."""
    if not serial_log.exists():
        return "(no serial log)"
    return serial_log.read_text(encoding="utf-8", errors="replace")[-limit:]


async def await_guest_up(serial_log: Path, timeout_s: float = 60.0) -> None:
    """Block until the guest announces itself on the serial console."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout_s
    while loop.time() < deadline:
        if GUEST_UP_MARKER in serial_tail(serial_log):
            return
        await asyncio.sleep(0.2)
    raise AssertionError(
        f"guest serial never showed {GUEST_UP_MARKER!r} within {timeout_s}s; "
        f"serial log tail:\n{serial_tail(serial_log)}"
    )


async def await_pod_running(
    microvm, workspace_id: str, timeout_s: float = 120.0
) -> None:
    """Block until the runner pod reports the VM as running.

    A crash-looping or never-scheduled pod (bad image, no /dev/kvm on
    the node) never reaches ``running``; fail with the observed status
    instead of letting a ``starting`` snapshot pass.
    """
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout_s
    while loop.time() < deadline:
        status = (await microvm.info(workspace_id)).status.value
        if status == "running":
            return
        if status in ("stopped", "absent"):
            raise AssertionError(
                f"runner pod for {workspace_id!r} went {status!r} before "
                "running — check the image import and the node's /dev/kvm"
            )
        await asyncio.sleep(1.0)
    raise AssertionError(
        f"runner pod for {workspace_id!r} stayed {status!r} for {timeout_s}s "
        "— check the image import and the node's /dev/kvm"
    )


@needs_local
async def test_local_vm_boot_and_shutdown() -> None:
    # A shallow base: deep pytest tmp dirs can push the API socket path
    # past the AF_UNIX 108-byte limit under xdist workers.
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
    )
    try:
        await microvm.launch(spec)
        info = await microvm.info(wid)
        assert info.status.value == "running"
        # Wait for userspace before shutting down: the graceful shutdown
        # is an ACPI power-button press, and the guest only answers it
        # once its acpid is running — pressing earlier would drop the
        # event and time out against a VM that is running but not yet
        # listening.
        await await_guest_up(serial_log)
        await microvm.shutdown(wid, timeout_s=30)
        final = await microvm.info(wid)
        assert final.status.value in ("stopped", "absent")
    except BaseException:
        # Never leak a live VMM (and its /dev/kvm handle) on failure.
        with contextlib.suppress(Exception):
            await microvm.kill(wid)
        raise
    finally:
        with contextlib.suppress(Exception):
            await microvm.cleanup(wid)
        shutil.rmtree(state_dir, ignore_errors=True)


@needs_k8s
async def test_k8s_pod_lifecycle() -> None:
    settings = Settings(
        vmm=VmmSettings(driver="k8s"),
        k8s=K8sSettings(
            kubeconfig=KUBECONFIG,
            namespace=os.environ.get("MSKSD_TEST_NAMESPACE", "default"),
            # The image devenv task `msks:build-runner-image` builds and
            # `k3s ctr images import` loads; conftest.py exposes it via
            # MSKSD_TEST_RUNNER_IMAGE once the archive exists. The image
            # owns the guest it boots, so the spec's host-side artifact
            # paths do not reach the pod.
            runner_image=os.environ.get(
                "MSKSD_TEST_RUNNER_IMAGE", K8sSettings.runner_image
            ),
        ),
    )
    app = build_app(settings)
    microvm = app.state.microvm
    wid = f"smoke-{uuid.uuid4().hex[:8]}"
    # The runner image owns the guest it boots; these spec fields are
    # host-side bookkeeping and do not reach the pod (see spec_env).
    spec = VmSpec(
        workspace_id=wid,
        kernel=Path("/opt/msks/vmlinux"),
        rootfs=Path("/opt/msks/rootfs.ext4"),
    )
    try:
        await microvm.launch(spec)
        await await_pod_running(microvm, wid)
    finally:
        # k8s cleanup deletes the pod and tolerates it already being
        # gone, on both the success and failure paths.
        with contextlib.suppress(Exception):
            await microvm.cleanup(wid)
