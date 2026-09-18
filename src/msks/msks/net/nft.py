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


def vm_ruleset(
    workspace_id: str, tap: str, guest_ip: str, tap_ip: str, uplink: str
) -> str:
    """One workspace's enforcement tables.

    Two hooked chains, scoped to this workspace's tap:

    - ``egress`` (forward): the guest's source address may leave via
      the uplink; the replies to connections the guest established
      may come back; everything else toward the tap (inbound that
      nothing inside asked for) drops, and everything else from the
      tap (traffic not sourced as this guest, or headed anywhere but
      the uplink — which is what blocks guest-to-guest hops across
      taps) drops.
    - ``ingress`` (input): the guest may reach exactly two ports on
      the appliance through this tap — DHCP (67) and the resolver
      (53) — and the replies to connections the appliance itself
      opened into the guest (the forward endpoint's dial, #109)
      return on their conntrack state. Everything else from the tap
      drops before the appliance's own wildcard-bound services (the
      API among them): a guest-initiated connection arrives state
      NEW and does not match the established rule — and the rare
      loose-conntrack mid-stream pickup (nf_conntrack_tcp_loose=1)
      still dies on the iifname/saddr pins when the local stack RSTs
      it.

    The destination of guest-initiated egress is unconstrained in
    this issue's scope — any host reachable through the uplink is
    reachable; the per-flow consent gates of #69 tighten that.
    """
    return (
        f"table inet {table_name(workspace_id)} {{\n"
        "  chain egress {\n"
        "    type filter hook forward priority filter; policy accept;\n"
        f'    iifname "{tap}" ip saddr {guest_ip} oifname "{uplink}" accept\n'
        f'    oifname "{tap}" ct state established,related accept\n'
        f'    oifname "{tap}" drop\n'
        f'    iifname "{tap}" drop\n'
        "  }\n"
        "  chain ingress {\n"
        "    type filter hook input priority filter; policy accept;\n"
        # DHCP speaks broadcast (discover to 255.255.255.255), so
        # port 67 from the tap is accepted without a daddr match; the
        # resolver rule stays unicast to the tap address and pinned to
        # the guest's source (the forwarder checks per datagram, the
        # kernel rule is free defense-in-depth). The resolver speaks
        # UDP only — TCP/53 has no listener, so the chain does not
        # accept it.
        f'    iifname "{tap}" udp dport 67 accept\n'
        f'    iifname "{tap}" ip saddr {guest_ip} ip daddr {tap_ip} '
        f"udp dport 53 accept\n"
        # The forward's dial is appliance-originated: its replies —
        # and only those, per conntrack — come home here (#109).
        f'    iifname "{tap}" ip saddr {guest_ip} '
        f"ct state established,related accept\n"
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


async def install_vm(
    settings, workspace_id: str, tap: str, guest_ip: str, tap_ip: str
) -> None:
    """Install one workspace's tables, converging on any previous
    table of the same name first."""
    await delete_vm_table(settings, workspace_id)
    ruleset = vm_ruleset(
        workspace_id, tap, guest_ip, tap_ip, settings.net.uplink
    )
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
        raise MicrovmError(
            f"{what}: tool not found: {settings.net.nft_tool}"
        ) from exc
    output, err = await proc.communicate(input_text)
    if proc.returncode == 0:
        return
    if absent_ok and ABSENT_TABLE in output + err:
        return
    detail = (output + err).decode(errors="replace").strip()[:400]
    raise MicrovmError(f"{what} failed ({proc.returncode}): {detail}")
