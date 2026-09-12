"""Opt-in smoke tests against real infrastructure.

- Local: boots a real cloud-hypervisor VM when MSKSD_TEST_VMLINUX and
  MSKSD_TEST_ROOTFS point at guest artifacts (and /dev/kvm is
  accessible); skipped otherwise.
- k8s: creates and tears down a runner pod when MSKSD_TEST_KUBECONFIG
  points at a cluster kubeconfig (k3s in dev); skipped otherwise.

These never count toward the coverage gate (the package is fully
covered by the faked-transport unit suites).
"""

import os
import uuid
from pathlib import Path

import pytest
from msks.app import build_app
from msks.microvm import VmSpec
from msks.settings import K8sSettings, Settings, VmmSettings

VMLINUX = os.environ.get("MSKSD_TEST_VMLINUX")
ROOTFS = os.environ.get("MSKSD_TEST_ROOTFS")
KUBECONFIG = os.environ.get("MSKSD_TEST_KUBECONFIG")

needs_local = pytest.mark.skipif(
    not VMLINUX or not ROOTFS or not os.access("/dev/kvm", os.W_OK),
    reason="set MSKSD_TEST_VMLINUX/MSKSD_TEST_ROOTFS with /dev/kvm access",
)
needs_k8s = pytest.mark.skipif(not KUBECONFIG, reason="set MSKSD_TEST_KUBECONFIG")


@needs_local
async def test_local_vm_boot_and_shutdown(tmp_path: Path) -> None:
    settings = Settings(vmm=VmmSettings(state_dir=tmp_path / "state"))
    app = build_app(settings)
    wid = f"smoke-{uuid.uuid4().hex[:8]}"
    spec = VmSpec(workspace_id=wid, kernel=Path(VMLINUX), rootfs=Path(ROOTFS))
    await app.state.microvm.launch(spec)
    info = await app.state.microvm.info(wid)
    assert info.status.value == "running"
    await app.state.microvm.shutdown(wid, timeout_s=30)
    final = await app.state.microvm.info(wid)
    assert final.status.value in ("stopped", "absent")
    await app.state.microvm.cleanup(wid)


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
