"""Console identity smoke: the workspace-user shell drop (#63)."""

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
    """A shell as the image's workspace user (#63): the helper drops
    from root to uid 1000, creates the home on the persistent volume,
    and execs a login shell whose identity the command output proves
    (id -u is guest-computed, so the marker cannot come from the
    echo). Root sessions keep working alongside it."""
    state_dir = Path(f"/tmp/msks-smoke-{uuid.uuid4().hex[:8]}")
    settings = Settings(vmm=VmmSettings(state_dir=state_dir))
    app = build_app(settings)
    microvm = app.state.microvm
    wid = f"smoke-{uuid.uuid4().hex[:8]}"
    serial_log = state_dir / "vms" / wid / "serial.log"
    try:
        await microvm.launch(
            VmSpec(
                workspace_id=wid,
                kernel=Path(VMLINUX),
                rootfs=Path(ROOTFS),
                initrd=Path(INITRD) if INITRD else None,
                cmdline=CMDLINE or "console=hvc0 root=/dev/vda rw",
                root_mib=2048,
                home_mib=256,
                egress=False,
            )
        )
        await await_guest_up(serial_log)
        # Seed the workspace user's dotfiles from skel as root: the
        # helper creates a bare home, and bash without rc files
        # prints no recognizable prompt.
        await run_in_console(
            microvm, wid, "cp -r /etc/skel/. /home/msks/ && echo S-$((6*7))", "S-42"
        )
        await run_in_console(
            microvm, wid, "chown -R msks:msks /home/msks && echo O-$((6*7))", "O-42"
        )
        # The real drop: uid 1000, the persistent home, and root
        # alongside.
        await run_in_console(microvm, wid, "echo I-$(id -u)", "I-1000", user="msks")
        await run_in_console(microvm, wid, "echo H-$(pwd)", "H-/home/msks", user="msks")
        await run_in_console(microvm, wid, "echo R-$(id -u)", "R-0")
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
