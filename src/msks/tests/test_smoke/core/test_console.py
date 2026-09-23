"""Console identity smoke: the workspace-user shell drop (#63)."""

import contextlib
import shutil
import uuid
from pathlib import Path

from msks.app import build_app
from msks.client import consoleauth
from msks.identity import mint
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
    """A shell as the image's workspace user (#63): the minted
    identity's seed makes the home on the persistent volume (#171),
    the helper drops from root to uid 1000, and execs a login shell
    whose identity the command output proves (id -u is
    guest-computed, so the marker cannot come from the echo). Root
    sessions keep working alongside it."""
    state_dir = Path(f"/tmp/msks-smoke-{uuid.uuid4().hex[:8]}")
    settings = Settings(vmm=VmmSettings(state_dir=state_dir))
    app = build_app(settings)
    microvm = app.state.microvm
    wid = f"smoke-{uuid.uuid4().hex[:8]}"
    serial_log = state_dir / "vms" / wid / "serial.log"
    # The mint a create performs (#111), replayed by hand so the
    # launch stays direct: the public half rides the cidata seed and
    # its script makes the home (#171); the private half signs the
    # console challenge (#123) in-process, as the client would.
    private_pem, public = mint("ed25519")
    signer, _public = consoleauth.signer_for_key(
        {"public_key": public, "private_key": private_pem}, wid
    )
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
                ssh_pubkey=public,
            )
        )
        await await_guest_up(serial_log)
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
            signer=signer,
        )
        # The real drop: uid 1000, the persistent home, and root
        # alongside.
        await run_in_console(
            microvm,
            wid,
            "echo I-$(id -u)",
            "I-1000",
            user="msks",
            signer=signer,
        )
        await run_in_console(
            microvm,
            wid,
            "echo H-$(pwd)",
            "H-/home/msks",
            user="msks",
            signer=signer,
        )
        await run_in_console(
            microvm,
            wid,
            "echo R-$(id -u)",
            "R-0",
            signer=signer,
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
