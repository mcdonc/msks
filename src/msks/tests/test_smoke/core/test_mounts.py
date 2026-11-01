"""Host-share mount smokes (#620, part #623; #667): the guest
side.

The seed carries the systemd mount units for the workspace's
current mount set; each boot bakes the set this start attached,
and a changed set renews the seed so cloud-init re-provisions the
units (the prune half removes the dropped mounts'). The mounts
are host-path shares (#685: the one grammar), identity mode on
the Linux smoke host.
"""

import contextlib
import shutil
import uuid
from pathlib import Path

from msks.app import build_app
from msks.microvm import VmSpec
from msks.settings import (
    MountSettings,
    ServerSettings,
    Settings,
    VmmSettings,
)
from msks.spec.mounts import MountSpec

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


def smoke_shares(state_dir: Path) -> tuple[Path, Path]:
    """Two host trees under the smoke state dir, each carrying a
    file whose content names it — the in-guest probe reads the
    files through the mounts."""
    trees = []
    for name in ("alpha", "beta"):
        tree = state_dir / f"share-{name}"
        tree.mkdir(parents=True)
        (tree / "who.txt").write_text(f"share-{name}\n")
        trees.append(tree)
    return trees[0], trees[1]


def spec_of(wid: str, mounts: tuple) -> VmSpec:
    """The smoke workspace's spec at a given mount set."""
    return VmSpec(
        workspace_id=wid,
        kernel=Path(VMLINUX),
        rootfs=Path(ROOTFS),
        initrd=Path(INITRD) if INITRD else None,
        cmdline=CMDLINE or "console=ttyS0 root=/dev/vda rw",
        root_mib=2048,
        home_mib=256,
        egress=False,
        mounts=mounts,
    )


@needs_local
async def test_local_mounts_land_and_follow_the_set() -> None:
    """The done-when of #623, live: two mounts land at their
    requested paths on the first boot; a restart with a changed set
    lands the new mount and prunes the dropped one's — the renewal
    re-provisions the units, the surviving mounts keep their
    data."""
    state_dir = Path(f"/tmp/msks-smoke-{uuid.uuid4().hex[:8]}")
    alpha_tree, beta_tree = smoke_shares(state_dir)
    settings = Settings(
        vmm=VmmSettings(state_dir=state_dir),
        server=ServerSettings(db_path=state_dir / "smoke.db"),
    )
    settings.mounts = MountSettings(host_roots=(str(state_dir),))
    # A healed launch bumps the disk epoch (#697), so the app's
    # throwaway db (not the default daemon path) is migrated here —
    # the driver-direct app never enters the lifespan that migrates.
    app = build_app(settings)
    app.state.model.migrate()
    microvm = app.state.microvm
    wid = f"smoke-{uuid.uuid4().hex[:8]}"
    serial_log = state_dir / "vms" / wid / "serial.log"

    alpha = MountSpec(host=str(alpha_tree), guest="/mnt/alpha")
    beta = MountSpec(host=str(beta_tree), guest="/srv/beta")
    try:
        # Two mounts, first boot: both land, both read their
        # share's file through the guest path.
        await microvm.launch(spec_of(wid, (alpha, beta)))
        await await_guest_up(microvm, wid, hostname=wid)
        await run_in_console(
            microvm, wid, "cloud-init status --wait", "done", hostname=wid
        )
        await run_in_console(
            microvm, wid, "cat /mnt/alpha/who.txt", "share-alpha", hostname=wid
        )
        await run_in_console(
            microvm, wid, "cat /srv/beta/who.txt", "share-beta", hostname=wid
        )
        # The mounts are virtiofs, at the requested paths.
        await run_in_console(
            microvm,
            wid,
            # /proc/mounts fields run device, mountpoint, type —
            # the virtiofs mount names its tag as the device.
            "grep '/mnt/alpha virtiofs' /proc/mounts && echo A-$((6*7))",
            "A-42",
            hostname=wid,
        )
        await microvm.shutdown(wid, timeout_s=SHUTDOWN_TIMEOUT_S)

        # The set changes on restart: alpha is dropped, beta keeps
        # its mount — the pruned mount's path answers nothing, the
        # survivor still reads through.
        serial_log.unlink(missing_ok=True)
        await microvm.launch(spec_of(wid, (beta,)))
        await await_guest_up(microvm, wid, hostname=wid)
        await run_in_console(
            microvm, wid, "cloud-init status --wait", "done", hostname=wid
        )
        await run_in_console(
            microvm, wid, "cat /srv/beta/who.txt", "share-beta", hostname=wid
        )
        await run_in_console(
            microvm,
            wid,
            "if mountpoint -q /mnt/alpha 2>/dev/null; then echo STILL;"
            "else echo GONE-$((6*7)); fi",
            "GONE-42",
            hostname=wid,
        )
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
