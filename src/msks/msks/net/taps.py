"""Tap interface lifecycle for one workspace's egress (#52).

cloud-hypervisor's ``net`` device with ``tap=`` opens an existing
persistent tap by name, so the tap — with its address, carrier up —
must exist before the VMM boots. All commands run through the
configured ``ip`` tool; a failure is a named operator error, and a
removal tolerates the tap already being gone (stop/kill/cleanup all
race each other's teardown).
"""

import asyncio

from ..microvm.errors import MicrovmError

# `ip link del` answers this on stderr when the device is already
# gone — the one removal failure that means success.
ABSENT_DEVICE = b"Cannot find device"


async def create_tap(name: str, address: str, settings) -> None:
    """Create the tap, give it the host-side address, raise it.

    A tap left behind by an unclean daemon death is swept first
    (absence tolerated): the crash-recovery boot converges silently
    instead of failing once on "File exists" and succeeding on the
    retry.
    """
    await remove_tap(name, settings)
    await ip_cmd(
        settings,
        ["tuntap", "add", "dev", name, "mode", "tap"],
        f"tap create {name}",
    )
    await ip_cmd(
        settings, ["addr", "add", address, "dev", name], f"tap address {name}"
    )
    await ip_cmd(
        settings, ["link", "set", "dev", name, "up"], f"tap up {name}"
    )


async def remove_tap(name: str, settings) -> None:
    """Delete the tap; an already-absent tap is success."""
    await ip_cmd(
        settings,
        ["link", "del", "dev", name],
        f"tap remove {name}",
        absent_ok=True,
    )


async def ip_cmd(
    settings, args: list[str], what: str, *, absent_ok: bool = False
) -> None:
    """Run one ``ip`` subcommand; a failure becomes a named error."""
    try:
        proc = await asyncio.create_subprocess_exec(
            settings.net.ip_tool,
            *args,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except FileNotFoundError as exc:
        raise MicrovmError(
            f"{what}: tool not found: {settings.net.ip_tool}"
        ) from exc
    output, err = await proc.communicate()
    if proc.returncode == 0:
        return
    if absent_ok and ABSENT_DEVICE in output + err:
        return
    detail = (output + err).decode(errors="replace").strip()[:400]
    raise MicrovmError(f"{what} failed ({proc.returncode}): {detail}")
