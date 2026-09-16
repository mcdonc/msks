# Guest networking

A workspace talks to the network only when it was created with
`egress: true` — and when it does, every piece of the path lives in
the daemon, where the guest cannot reach it.

```bash
curl -X POST .../api/v1/workspaces \
  -d '{"id": "ws1", "egress": true}'        # or: msks create ws1 --egress
```

This chapter covers the plumbing: the NIC, the tap, DHCP, DNS, and
NAT. Per-flow consent — holding the first packet of each new
connection for an allow/deny decision over the decider channel —
arrives with #69 and tightens the same per-VM chain.

## The path

```text
workspace VM ──virtio-net──► per-VM tap ──► per-VM nftables chain (appliance kernel)
                                              │  forward: guest source → uplink: accept;
                                              │  established replies back; all else drops
                                              │  input: only DHCP (67) and DNS (53) in
                                              ▼
                                     NAT (masquerade) → appliance uplink
```

Each egress workspace owns a dedicated /30 carved from the
configured pool (`MSKSD_EGRESS_SUBNET`, default `172.31.0.0/16`):
the guest holds the first host address, the daemon-side tap the
second. The workspace's tap name, nftables table, and NIC MAC derive
deterministically from the workspace id, and the pool slice is
recorded on the workspace row at first attach — so stop/start cycles,
daemon restarts, and even digest collisions between workspace ids
keep the same address.

msksd is the only DHCP server and the only resolver the guest ever
sees: the DHCP offer names the tap as gateway and as DNS server, and
a small forwarder on the tap answers port 53 by relaying to the
appliance's own upstream (`MSKSD_EGRESS_DNS_UPSTREAM`, or the first
nameserver in the appliance's `/etc/resolv.conf`). The resolver
speaks UDP; TCP/53 has no listener, so a query needing TCP fallback
(DNSSEC validation, very large RRsets) fails fast. The forwarder
answers queries from its own guest only; anything else arriving on
the tap is dropped unread, so a spoofed-source datagram cannot turn
the appliance into a reflection amplifier. #69 grows the naming
layer — query cache, name learning for prompts, and the lockout that
keeps DNS from being routed around — behind that same offered
resolver.

## What the chain enforces

The per-VM table hooks two places, both scoped to the workspace's
tap:

- **Forward.** The guest's own source address may leave via the
  uplink, and the replies to connections the guest established may
  come back. Everything else across the tap drops: traffic from a
  spoofed source, traffic headed anywhere but the uplink (which is
  what blocks one workspace from reaching another's tap), and
  inbound the guest never asked for — nothing outside initiates a
  connection into a workspace.
- **Input.** The guest may reach exactly two ports in the appliance
  through its tap: DHCP (67) and the resolver (53). Everything else
  from the tap drops before the appliance's own services — the API
  listener among them — so guest root cannot port-scan the
  appliance.

What the chain deliberately permits today: the _destination_ of a
guest-initiated connection is unconstrained — any host reachable
through the appliance uplink is reachable, until the per-flow
consent gates of #69 decide each new connection.

## What runs where

**Inside the appliance:** the tap, its address, the per-VM
nftables table, the NAT masquerade on the uplink, the DHCP service,
and the DNS forwarder — all owned by msksd, which runs as a
dedicated service user holding exactly two ambient capabilities:
`CAP_NET_ADMIN` (taps and their addresses, the nftables tables,
and — because ambient capabilities survive `exec` — the workspace
VMM opening its tap) and `CAP_NET_BIND_SERVICE` (the DHCP and DNS
listeners, UDP 67 and 53). Nothing in the daemon's process tree
runs as uid 0; `/dev/kvm` reaches the VMM through the `kvm` group.
The host's firewall is never touched; containment stays inside the
appliance by design.

**Inside the guest:** nothing msks-specific. The image overlay ships
a systemd-networkd DHCP unit (see [images.md](images.md)); the
kernel's `virtio_net` driver configures the NIC, DHCP configures the
address and resolver, and that is the whole of it. No sidecar, no
agent, no firewall.

**A workspace created with `"egress": false`** presents no NIC
at all: no tap, no chains, no services — the posture available on
every backend, and the one that needs zero enforcement machinery.

## Configuration

| Variable                           | Default         | Meaning                                                         |
| ---------------------------------- | --------------- | --------------------------------------------------------------- |
| `MSKSD_EGRESS_ENABLED`             | `false`         | Arm the egress plumbing at daemon start (the appliance sets it) |
| `MSKSD_EGRESS_SUBNET`              | `172.31.0.0/16` | The pool per-workspace /30s are carved from                     |
| `MSKSD_EGRESS_UPLINK`              | `eth0`          | The appliance uplink NAT hides guests behind                    |
| `MSKSD_EGRESS_DNS_UPSTREAM`        | resolv.conf     | Where the forwarder relays queries                              |
| `MSKSD_EGRESS_LEASE_S`             | `3600`          | DHCP lease lifetime                                             |
| `MSKSD_EGRESS_DNS_TIMEOUT_S`       | `3.0`           | How long the forwarder waits on the upstream                    |
| `MSKSD_IP_TOOL` / `MSKSD_NFT_TOOL` | `ip` / `nft`    | The plumbing tools' paths                                       |

Egress needs the daemon to hold `CAP_NET_ADMIN` and
`CAP_NET_BIND_SERVICE` — the appliance grants exactly those two to
its service user — and a kernel that routes: the appliance ships
`net.ipv4.ip_forward=1` as a boot-time `sysctl.d` setting, the
daemon verifies it at startup, and a daemon that reads `0` refuses
every egress workspace with a cause naming the sysctl key. The
appliance's own uplink needs the host side wired —
`sudo bash scripts/appliance-host-setup.sh` performs that setup
once, as root: a `sysctl.d` forwarding drop-in plus a systemd unit
that re-arms the bridge, tap, and NAT rules at every host reboot,
so starting the appliance needs no sudo (the per-start
`appliance-setup.sh` verifies the install and names it when
something is missing; re-run the installer if egress ever stops
working — a firewall reload can drop its rules). The
appliance pins its NIC to the kernel name `eth0`
(its kernel cmdline carries `net.ifnames=0`), which is the default
`MSKSD_EGRESS_UPLINK`, and sets `MSKSD_EGRESS_ENABLED=true`, so
workspaces are networked there once the installer has run. An
operator who sets `MSKSD_EGRESS_ENABLED=false` arms nothing, and
every egress workspace then refuses to boot with the cause named.
When msksd cannot arm the plumbing (a dev-shell daemon, say), it
stays up for everything else and every egress workspace **refuses
to boot** with a named cause, rather than running with a half-open
path — create those with `"egress": false` instead.

The dev-host egress smoke (`MSKSD_TEST_EGRESS=1`) runs as root:
ambient capabilities cannot be granted to an arbitrary shell, so
the harness — which creates real taps, loads nftables rules, and
binds ports 67 and 53 — runs as full root and sets `ip_forward`
itself for the duration of the run. That is the test's constraint,
not the server's: the daemon needs only the two capabilities and an
already-routing kernel.

### The host-side network: portable installer or static config

`sudo bash scripts/appliance-host-setup.sh` is the portable path:
one run as root arms the bridge, tap, forwarding, and NAT rules, and
installs the persistence — a `sysctl.d` drop-in plus
`/etc/msks/host-net.sh` behind `msks-host-net.service`, which
re-arms the state at every host boot. It works on any systemd host
regardless of which network manager or firewall owns the rest of
the stack. The appliance's per-start check (`appliance-setup.sh`)
verifies the resulting state — bridge, tap, `ip_forward` — not the
mechanism that produced it, so the static forms below satisfy it
too.

On a host where systemd-networkd manages the network and
`nftables.service` owns the firewall, the same state is entirely
declarative: files the OS itself applies, no boot script. The
bridge and its address:

```ini
# /etc/systemd/network/90-msksbr0.netdev
[NetDev]
Name=msksbr0
Kind=bridge

# /etc/systemd/network/90-msksbr0.network
[Match]
Name=msksbr0
[Network]
Address=192.168.77.1/24
```

The tap — `Owner=` names the user who runs the appliance, which is
what lets the unprivileged cloud-hypervisor open it (changing that
user means editing the file; the installer equivalent is re-running
it as the new user):

```ini
# /etc/systemd/network/90-mskstap0.netdev
[NetDev]
Name=mskstap0
Kind=tap

[Tap]
Owner=chrism
```

Forwarding keeps the `sysctl.d` form — machine identity, the same
contract as inside the appliance — rather than a per-link toggle:

```ini
# /etc/sysctl.d/90-msks-appliance.conf
net.ipv4.ip_forward = 1
```

And the firewall/NAT table, loaded by the distro's
`nftables.service` (add the table to the file that service reads,
typically `/etc/nftables.conf`):

```nft
table ip msks-host {
  chain forward_msks {
    type filter hook forward priority filter; policy accept;
    iifname "msksbr0" ct state new,established,related accept
    oifname "msksbr0" ct state established,related accept
  }
  chain nat_msks {
    type nat hook postrouting priority srcnat; policy accept;
    ip saddr 192.168.77.0/24 oifname != "msksbr0" masquerade
  }
}
```

On NixOS the same shapes are native configuration: the netdev and
network content through the `systemd.network` module options, the
masquerade through `networking.nat` (`internalInterfaces =
[ "msksbr0" ]`, `externalInterface` = the default route's
interface).

Hosts whose firewall is firewalld, or whose network NetworkManager
manages, keep the installer path: NetworkManager does not consume
networkd's `.netdev` files, and a firewalld complete reload replaces
the whole ruleset — foreign rules added once, by script or by file,
do not survive it. Re-running the installer re-arms the state after
such a reload.

## Backend support

Egress is a local-backend feature today. On k8s the runner pod
refuses the netns privilege the enforcement needs, so an egress
workspace fails its boot with the cause named — k8s workspaces boot
with `"egress": false` until then; the NetworkPolicy parity work is
tracked in #69. The no-NIC posture works everywhere.

## Lifecycle

`start` arms the tap and services before the VMM boots (the VMM
opens the tap by name); `stop`, `kill`, and `delete` tear the tap,
its chain, and its services down again. A failed boot unwinds its
own plumbing — no half-open path outlives a failed workspace start.
