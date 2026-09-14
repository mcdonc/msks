"""The nftables rulesets behind guest egress (#52).

Two tables:

- ``msks-egress`` — the shared base, installed once at daemon start:
  a postrouting NAT chain masquerading everything leaving the
  configured uplink, so guest traffic appears as appliance traffic.
- ``msks-e-<digest>`` — one per egress workspace: a forward hook
  whose chain accepts that workspace's guest source address toward
  the uplink and drops everything else arriving on its tap. The
  per-flow consent gates (#69) will tighten this chain, not replace
  it.

Each per-VM table hooks ``forward`` on its own, so teardown is a
whole-table delete — no handle bookkeeping, no jump-rule residue.
Install converges by deleting any previous table of the same name
first (absence tolerated), then adding the fresh one.
"""

import asyncio

from ..microvm.errors import MicrovmError
from .alloc import table_name

BASE_TABLE = "msks-egress"

# nft's error text for a missing table on delete — tolerated.
ABSENT_TABLE = b"No such file or directory"


def base_ruleset(uplink: str) -> str:
    """The shared NAT table: masquerade out the appliance uplink."""
    return (
        f"table inet {BASE_TABLE} {{\n"
        "  chain nat_out {\n"
        "    type nat hook postrouting priority srcnat; policy accept;\n"
        f'    oifname "{uplink}" masquerade\n'
        "  }\n"
        "}\n"
    )


def vm_ruleset(workspace_id: str, tap: str, guest_ip: str, uplink: str) -> str:
    """One workspace's forward table: its tap, its source address."""
    return (
        f"table inet {table_name(workspace_id)} {{\n"
        "  chain egress {\n"
        "    type filter hook forward priority filter; policy accept;\n"
        f'    iifname "{tap}" ip saddr {guest_ip} oifname "{uplink}" accept\n'
        f'    iifname "{tap}" drop\n'
        "  }\n"
        "}\n"
    )


async def apply_base(settings) -> None:
    """Install the shared NAT table (idempotent by daemon lifetime)."""
    await nft_run(
        settings,
        ["-f", "-"],
        input_text=base_ruleset(settings.net.uplink).encode(),
        what="nft base ruleset apply",
    )


async def install_vm(settings, workspace_id: str, tap: str, guest_ip: str) -> None:
    """Install one workspace's forward table, converging on any
    previous table of the same name first."""
    await delete_vm_table(settings, workspace_id)
    ruleset = vm_ruleset(workspace_id, tap, guest_ip, settings.net.uplink)
    await nft_run(
        settings,
        ["-f", "-"],
        input_text=ruleset.encode(),
        what=f"nft ruleset apply for {workspace_id}",
    )


async def delete_vm_table(settings, workspace_id: str) -> None:
    """Drop one workspace's table; an absent table is success."""
    table = table_name(workspace_id)
    await nft_run(
        settings,
        ["delete", "table", "inet", table],
        what=f"nft table delete {table}",
        absent_ok=True,
    )


async def nft_run(
    settings,
    args: list[str],
    what: str,
    *,
    input_text: bytes | None = None,
    absent_ok: bool = False,
) -> None:
    """Run one ``nft`` invocation; a failure becomes a named error.

    ``input_text`` rides stdin (``-f -``); None leaves it unused.
    """
    try:
        proc = await asyncio.create_subprocess_exec(
            settings.net.nft_tool,
            *args,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except FileNotFoundError as exc:
        raise MicrovmError(f"{what}: tool not found: {settings.net.nft_tool}") from exc
    output, err = await proc.communicate(input_text)
    if proc.returncode == 0:
        return
    if absent_ok and ABSENT_TABLE in output + err:
        return
    detail = (output + err).decode(errors="replace").strip()[:400]
    raise MicrovmError(f"{what} failed ({proc.returncode}): {detail}")
