"""Console smoke: the root getty and the seeded workspace user (#481)."""

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
from testkeys import mint

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
async def test_local_console_identity_drop() -> None:
    """The console is the root getty (#481); the workspace user's
    shell is reached through su from it. The minted identity's seed
    makes the home on the persistent volume (#171), and su drops to
    uid 1000 with the persistent home as cwd — identity proven by
    command output (id -u is guest-computed, so the marker cannot
    come from the echo)."""
    state_dir = Path(f"/tmp/msks-smoke-{uuid.uuid4().hex[:8]}")
    settings = Settings(vmm=VmmSettings(state_dir=state_dir))
    app = build_app(settings)
    microvm = app.state.microvm
    wid = f"smoke-{uuid.uuid4().hex[:8]}"
    serial_log = state_dir / "vms" / wid / "serial.log"
    # The public half a create sends (#486), supplied by hand so
    # the launch stays direct: it rides the cidata seed and its
    # script makes the home (#171).
    _private_pem, public = mint("ed25519")
    try:
        await microvm.launch(
            VmSpec(
                workspace_id=wid,
                kernel=Path(VMLINUX),
                rootfs=Path(ROOTFS),
                initrd=Path(INITRD) if INITRD else None,
                cmdline=CMDLINE or "console=ttyS0 root=/dev/vda rw",
                root_mib=2048,
                home_mib=256,
                egress=False,
                ssh_pubkey=public,
            )
        )
        await await_guest_up(microvm, wid, hostname=wid)
        # The identity seed made the home before any console connect
        # (#171): owned by the user — and cloud-init created no
        # `debian` account alongside the shipped msks one. The
        # home's contents are image-specific (Debian's skel ships
        # .profile; NixOS ships an empty skel and the seed's skel
        # copy is a best-effort `|| true` — a bare home still
        # starts the shell), so the pin is the home itself, not
        # any dotfile. run_in_console's retries absorb
        # cloud-final still finishing the seed after the serial
        # prompt appears.
        await run_in_console(
            microvm,
            wid,
            "test -d /home/msks "
            '&& test "$(stat -c %U:%G /home/msks)" = msks:msks '
            "&& test ! -e /home/debian "
            "&& ! grep -q '^debian:' /etc/passwd "
            "&& echo S-$((6*7))",
            "S-42",
            hostname=wid,
        )
        # The user's shell through the root console: su - drops to
        # uid 1000 with the persistent home as cwd, and the console
        # itself answers as root.
        await run_in_console(
            microvm,
            wid,
            "su - msks -c 'echo I-$(id -u)'",
            "I-1000",
            hostname=wid,
        )
        await run_in_console(
            microvm,
            wid,
            "su - msks -c 'echo H-$(pwd)'",
            "H-/home/msks",
            hostname=wid,
        )
        await run_in_console(
            microvm,
            wid,
            "echo R-$(id -u)",
            "R-0",
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
