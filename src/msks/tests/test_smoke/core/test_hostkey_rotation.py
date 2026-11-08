"""The host-key rotation across a seed renewal (#803): the guest
side and the row the client reads.

A changed mount set renews the seed at the next start; the
renewal's new instance-id makes cloud-init re-provision the guest,
and the stock ssh module on Debian and Fedora regenerates the ssh
host keys there. The row records the renewal token the bake
pinned, the identity endpoint serves it, and the client rotates
its host-key cache on it instead of refusing the re-keyed guest.

The image this suite boots is the Debian default, whose stock
cloud.cfg runs the ssh module — the re-key is the behavior under
test. NixOS omits the module from its cloud_init_modules, so the
same renewal leaves its host keys alone and the rotation re-pins
the unchanged key; the served token is the contract on every
image, the key posture belongs to the image alone.
"""

import contextlib
import shutil
import uuid
from pathlib import Path

from msks.app import build_app
from msks.microvm import VmSpec
from msks.persist import mounts_fingerprint, renewal_suffix
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
async def test_local_mount_renewal_rotates_the_host_key_pin() -> None:
    """#803's done-when, live: a restart with a changed mount set
    renews the seed, the guest re-provisions and its ssh host key
    regenerates, and the row carries the renewal token the bake
    pinned — the token the identity endpoint serves and the client
    rotates its host-key cache on, so the next session re-pins
    instead of refusing."""
    state_dir = Path(f"/tmp/msks-smoke-{uuid.uuid4().hex[:8]}")
    share = state_dir / "share"
    share.mkdir(parents=True)
    (share / "who.txt").write_text("share\n")
    settings = Settings(
        vmm=VmmSettings(state_dir=state_dir),
        server=ServerSettings(db_path=state_dir / "smoke.db"),
    )
    settings.mounts = MountSettings(host_roots=(str(state_dir),))
    app = build_app(settings)
    app.state.model.migrate()
    microvm = app.state.microvm
    model = app.state.model
    wid = f"smoke-{uuid.uuid4().hex[:8]}"
    serial_log = state_dir / "vms" / wid / "serial.log"

    mounted = (MountSpec(host=str(share), guest="/mnt/share"),)
    changed = (MountSpec(host=str(share), guest="/srv/moved"),)
    try:
        # First boot: the seed bakes plain (no renewal token on the
        # row), and the guest writes its first host key.
        await model.create_workspace(spec_of(wid, mounted))
        await model.set_mounts(wid, [m.as_dict() for m in mounted])
        await microvm.launch(spec_of(wid, mounted))
        await await_guest_up(microvm, wid, hostname=wid)
        await run_in_console(
            microvm, wid, "cloud-init status --wait", "done", hostname=wid
        )
        assert (await model.get_ssh_key(wid))["seed_rev"] is None
        await run_in_console(
            microvm,
            wid,
            "ssh-keygen -lf /etc/ssh/ssh_host_ed25519_key.pub"
            " | sha256sum | cut -c1-16 > /root/fp-before"
            " && echo W-$((6*7))",
            "W-42",
            hostname=wid,
        )
        await microvm.shutdown(wid, timeout_s=SHUTDOWN_TIMEOUT_S)

        # The mount set changes; the next start renews the seed and
        # re-provisions the guest.
        serial_log.unlink(missing_ok=True)
        await model.set_mounts(wid, [m.as_dict() for m in changed])
        await microvm.launch(spec_of(wid, changed))
        await await_guest_up(microvm, wid, hostname=wid)
        await run_in_console(
            microvm, wid, "cloud-init status --wait", "done", hostname=wid
        )
        # The row carries exactly the token the renewal baked.
        ca = await microvm.local.interceptor_ca_pem()
        expected = renewal_suffix(ca, mounts_fingerprint(changed))
        assert (await model.get_ssh_key(wid))["seed_rev"] == expected
        # The guest re-keyed: the stock cloud-init ssh module ran
        # again under the new instance-id.
        await run_in_console(
            microvm,
            wid,
            '[ "$(ssh-keygen -lf /etc/ssh/ssh_host_ed25519_key.pub'
            ' | sha256sum | cut -c1-16)" = "$(cat /root/fp-before)" ]'
            " && echo SAME-$((6*7)) || echo CHANGED-$((6*7))",
            "CHANGED-42",
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
