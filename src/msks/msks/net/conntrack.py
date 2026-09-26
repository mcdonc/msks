"""Conntrack eviction for consent revocation (#69).

Removing a flow rule stops *new* connections; an established one
keeps flowing on its conntrack entry. Revocation must kill the live
flows too, so the engine deletes the workspace's tracked
connections to the revoked destination through the ``conntrack``
tool — the piece a same-netns sidecar never needed (its rules and
its conntrack died together with the namespace), and a per-VM-tap
design does.

Best-effort by design: the tool is a setting (the msksd package
ships it; a dev shell may not), and a missing entry is success —
the rule clear already happened, so the only flows that survive a
failed delete are ones conntrack no longer tracks anyway.
"""

import asyncio
import contextlib

from ..microvm.errors import MicrovmError

# conntrack's stderr for a delete that matched nothing.
ABSENT = b"0 flow entries"


async def delete_flows(
    tool: str, source_ip: str, dest_ip: str, deadline_s: float = 10.0
) -> None:
    """Delete the tracked connections between the guest and one
    destination (best-effort; a missing tool or entry is logged at
    most once by the caller's settings). ``deadline_s`` bounds the
    tool's runtime before the kill — a parameter so tests can pin
    the kill against a short deadline instead of paying the
    production one."""
    try:
        proc = await asyncio.create_subprocess_exec(
            tool,
            "-D",
            "-s",
            source_ip,
            "-d",
            dest_ip,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except FileNotFoundError:
        raise MicrovmError(
            f"conntrack delete for {source_ip} -> {dest_ip}: "
            f"tool not found: {tool}"
        ) from None
    try:
        await asyncio.wait_for(proc.communicate(), timeout=deadline_s)
    except TimeoutError:
        proc.kill()
        with contextlib.suppress(ProcessLookupError):
            await proc.wait()
