"""The workspace user's sudo (#169): setuid bits survive the build.

The image build extracts the Debian root unprivileged, which drops
every setuid/setgid bit; the build records the source image's
special modes and the fakeroot pack stage bakes them back (see
nix/guest-assets.nix). This smoke pins the result in a real boot:
the binary's mode (04755, root-owned — the pairing sudo demands
before anything else) and the behavior as the msks user, whose
sudoers grant (#169) makes a successful elevation the observable
outcome. With the bits lost, sudo refuses to run at all — the
marker never arrives — so the assertion catches the exact
regression class the build once shipped.
"""

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
async def test_local_workspace_user_sudo() -> None:
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
    try:
        await microvm.launch(spec)
        info = await microvm.info(wid)
        assert info.status.value == "running"
        await await_guest_up(serial_log)
        # The pairing sudo checks first: uid-0 ownership of a 04755
        # binary. Guest-computed sentinel, per run_in_console's
        # echo-collision rule.
        await run_in_console(
            microvm,
            wid,
            'test "$(stat -c %a /usr/bin/sudo)" = 4755 '
            '&& test "$(stat -c %u /usr/bin/sudo)" = 0 '
            "&& echo MODE-$((6*7))",
            "MODE-42",
        )
        # The behavior the issue repro names: sudo elevates the
        # workspace user (the sudoers grant is part of the fix).
        await run_in_console(
            microvm,
            wid,
            "sudo -n /bin/true && echo SUDO-$((6*7))",
            "SUDO-42",
            user="msks",
        )
        await microvm.shutdown(wid, timeout_s=SHUTDOWN_TIMEOUT_S)
        final = await microvm.info(wid)
        assert final.status.value in ("stopped", "absent")
    except BaseException:
        collect_failure_evidence(state_dir, wid, serial_log)
        with contextlib.suppress(Exception):
            await microvm.kill(wid)
        raise
    finally:
        with contextlib.suppress(Exception):
            await microvm.cleanup(wid)
        shutil.rmtree(state_dir, ignore_errors=True)
