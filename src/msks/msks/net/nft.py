"""The nftables rulesets behind guest egress (#52, #69).

Tables:

- ``msks-egress`` — the shared base, installed once at daemon start:
  a postrouting NAT chain masquerading everything leaving the
  configured uplink, so guest traffic appears as appliance traffic.
- ``msks-e-<digest>`` — one per egress workspace: the forward hook
  that gates that workspace's traffic and the input hook that pins
  what the guest may reach inside the appliance. The chain shape
  follows the workspace's egress mode (#69):

  - ``allow`` — the #52 posture plus the DNS lockout: established
    traffic and new flows toward the uplink pass; ports 53/853
    toward anywhere but this daemon's resolver drop, so the naming
    layer cannot be routed around.
  - ``static`` — established passes, the allowlist's address specs
    accept, everything else new drops. Off-list *names* never
    resolve (the resolver NXDOMAINs them); off-list addresses never
    forward.
  - ``interactive`` — established passes, the address specs accept,
    then the consent sets (rejected destination ports get a TCP
    RST; allowed destinations pass), then every NEW flow queues to
    this workspace's own NFQUEUE number for a verdict, and the
    default is drop.

Each per-VM table hooks ``forward`` and ``input`` on its own, so
teardown is a whole-table delete — no handle bookkeeping, no
jump-rule residue. Install converges by deleting any previous table
of the same name first (absence tolerated), then adding the fresh
one.

Fail-closed by construction (#69): the queue rule carries no
``bypass`` flag, so a queue with no listener (msksd down, the
consumer not yet bound) and a full queue both *drop* — the opposite
default of klangk's sidecar startup window, documented in
docs/networking.md.
"""

import asyncio

from ..consent.specs import EgressPolicy, IpSpec
from ..microvm.errors import MicrovmError
from .alloc import table_name

BASE_TABLE = "msks-egress"

# nft's error text for a missing table or set element on delete —
# tolerated (idempotent deletes).
ABSENT_TABLE = b"No such file or directory"

# The ports the naming layer owns: plain DNS and DNS-over-TLS. The
# guest's only path to them is this daemon's resolver on the tap;
# everything else to these ports drops before any allow.
DNS_PORTS = "{ 53, 853 }"

# One queue per workspace, numbered from its pool slice: a queue
# number is 16 bits, so the pool must leave room above the base.
QUEUE_MAX = 65535


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


def dns_lockout_rules(tap: str) -> str:
    """The naming-layer lockout: ports 53/853 toward anywhere but
    this daemon's own resolver (which the guest reaches through the
    input chain, not forward) drop, in both TCP and UDP."""
    return (
        f'    iifname "{tap}" tcp dport {DNS_PORTS} drop\n'
        f'    iifname "{tap}" udp dport {DNS_PORTS} drop\n'
    )


def ip_spec_rules(tap: str, specs: tuple[IpSpec, ...]) -> str:
    """One accept per address spec (CIDR or literal), port-scoped
    when the spec is. Static infra given by address never prompts
    and never queues."""
    lines = []
    for spec in specs:
        port = f" tcp dport {spec.port}" if spec.port is not None else ""
        lines.append(
            f'    iifname "{tap}" ip daddr {spec.network}{port} accept\n'
        )
    return "".join(lines)


def consent_sets(policy: EgressPolicy) -> str:
    """The per-VM consent sets (gated modes): all-ports allows and
    port-scoped allows — a static workspace's allowlisted names pin
    their resolved addresses into the same sets an interactive
    verdict does, so both modes share one enforcement shape. The
    rejects set is deny-verdict machinery and ships only with the
    queue. Each element carries its own kernel-side timeout —
    verdict durations are enforced by the kernel, not a userspace
    sweeper."""
    if not policy.gated:
        return ""
    sets = (
        "  set allows_any {\n"
        "    type ipv4_addr; flags timeout;\n"
        "  }\n"
        "  set allows_port {\n"
        "    type ipv4_addr . inet_service; flags timeout;\n"
        "  }\n"
    )
    if policy.interactive:
        sets += (
            "  set rejects {\n"
            "    type ipv4_addr . inet_service; flags timeout;\n"
            "  }\n"
        )
    return sets


def allow_matches(tap: str, policy: EgressPolicy) -> str:
    """The allow-set matches (gated modes): a destination pinned
    by the resolver's allowlist learn or a verdict's enforcement
    passes here — both static and interactive pin into the same
    sets, so both modes must match them."""
    if not policy.gated:
        return ""
    return (
        f'    iifname "{tap}" ip daddr @allows_any accept\n'
        f'    iifname "{tap}" ip daddr . tcp dport @allows_port accept\n'
    )


def queue_gate(tap: str, guest_ip: str, queue_num: int | None) -> str:
    """The deny-match and hold queue (interactive only): rejected
    destination ports answer a SYN with a TCP RST (a dropped SYN
    alone leaves connect() hanging on the kernel's retransmit
    timer — the RST is the fast refusal), and everything else NEW
    queues for a verdict — the queue match carries ``ct state
    new``, so an established flow's later packets never re-enter
    consent (a ``once`` verdict guards the connection it released
    for the connection's whole life, not the cache window). The
    queue carries no ``bypass``: an unbound or full queue drops
    (fail-closed)."""
    if queue_num is None:
        return ""
    return (
        f'    iifname "{tap}" ip daddr . tcp dport @rejects '
        "reject with tcp reset\n"
        f'    iifname "{tap}" ip saddr {guest_ip} ct state new '
        f"queue num {queue_num}\n"
    )


def established_accept(tap: str, policy: EgressPolicy) -> str:
    """The outbound established accept (gated modes): a flow that
    passed the gates once (its SYN carried a verdict, or it hit a
    pin) keeps flowing for the connection's life — consent is once
    per flow, and the queue match's ``ct state new`` sends only new
    flows to it."""
    if not policy.gated:
        return ""
    return f'    iifname "{tap}" ct state established,related accept\n'


def vm_ruleset(
    workspace_id: str,
    tap: str,
    guest_ip: str,
    tap_ip: str,
    uplink: str,
    policy: EgressPolicy | None = None,
    queue_num: int | None = None,
) -> str:
    """One workspace's enforcement tables.

    Two hooked chains, scoped to this workspace's tap:

    - ``egress`` (forward): the mode-shaped gate above; whatever the
      mode, traffic not sourced as this guest drops, replies to
      connections the guest established return, and everything else
      toward the tap (inbound that nothing inside asked for) drops —
      which is also what blocks guest-to-guest hops across taps.
    - ``ingress`` (input): the guest may reach exactly two ports on
      the appliance through this tap — DHCP (67) and the resolver
      (53) — and the replies to connections the appliance itself
      opened into the guest (the forward endpoint's dial, #109)
      return on their conntrack state. Everything else from the tap
      drops before the appliance's own wildcard-bound services (the
      API among them): a guest-initiated connection arrives state
      NEW and does not match the established rule.
    """
    mode = policy or EgressPolicy(workspace_id, "allow", ())
    gated = mode.gated
    final = "drop" if gated else "accept"
    return (
        f"table inet {table_name(workspace_id)} {{\n"
        f"{consent_sets(mode)}"
        "  chain egress {\n"
        "    type filter hook forward priority filter; policy accept;\n"
        f'    oifname "{tap}" ct state established,related accept\n'
        f"{dns_lockout_rules(tap)}"
        f"{established_accept(tap, mode)}"
        f"{ip_spec_rules(tap, mode.ip_specs)}"
        f"{allow_matches(tap, mode)}"
        f"{queue_gate(tap, guest_ip, queue_num)}"
        f'    iifname "{tap}" ip saddr {guest_ip} '
        f'oifname "{uplink}" {final}\n'
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


def timeout_text(ttl_s: float) -> str:
    """An nft element timeout in whole seconds."""
    return f"{max(1, int(round(ttl_s)))}s"


async def allow_element(
    settings,
    workspace_id: str,
    ip: str,
    port: int | None,
    ttl_s: float,
) -> None:
    """Pin one destination IP (optionally port-scoped) as allowed
    for ``ttl_s``; the kernel expires it."""
    table = table_name(workspace_id)
    target = "allows_any" if port is None else "allows_port"
    scope = ip if port is None else f"{ip} . {port}"
    await nft_run(
        settings,
        [
            "add",
            "element",
            "inet",
            table,
            target,
            f"{{ {scope} timeout {timeout_text(ttl_s)} }}",
        ],
        what=f"nft allow {ip}:{port or '*'} for {workspace_id}",
    )


async def reject_element(
    settings, workspace_id: str, ip: str, port: int, ttl_s: float
) -> None:
    """Answer SYNs to one destination port with a TCP RST for
    ``ttl_s`` — the fast refusal a dropped SYN cannot give."""
    table = table_name(workspace_id)
    await nft_run(
        settings,
        [
            "add",
            "element",
            "inet",
            table,
            "rejects",
            f"{{ {ip} . {port} timeout {timeout_text(ttl_s)} }}",
        ],
        what=f"nft reject {ip}:{port} for {workspace_id}",
    )


async def clear_elements(
    settings, workspace_id: str, ip: str, port: int | None
) -> None:
    """Drop every consent element for one destination (revocation);
    an already-expired element is success. A port-scoped verdict
    clears its port sets; an all-ports verdict clears the
    all-ports allow (any stray port-scoped elements self-expire —
    the table dies with the workspace stop regardless)."""
    table = table_name(workspace_id)
    targets = [("allows_any", ip)]
    if port is not None:
        targets.append(("allows_port", f"{ip} . {port}"))
        targets.append(("rejects", f"{ip} . {port}"))
    for target, scope in targets:
        await nft_run(
            settings,
            [
                "delete",
                "element",
                "inet",
                table,
                target,
                f"{{ {scope} }}",
            ],
            what=f"nft clear {target} {scope} for {workspace_id}",
            absent_ok=True,
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
    settings,
    workspace_id: str,
    tap: str,
    guest_ip: str,
    tap_ip: str,
    policy: EgressPolicy | None = None,
    queue_num: int | None = None,
) -> None:
    """Install one workspace's tables, converging on any previous
    table of the same name first."""
    await delete_vm_table(settings, workspace_id)
    ruleset = vm_ruleset(
        workspace_id,
        tap,
        guest_ip,
        tap_ip,
        settings.net.uplink,
        policy=policy,
        queue_num=queue_num,
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
