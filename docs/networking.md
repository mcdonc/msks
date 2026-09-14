# Guest networking

A workspace talks to the network only when it was created with
`egress: true` — and when it does, every piece of the path lives in
the appliance, where the guest cannot reach it.

```bash
curl -X POST .../api/v1/workspaces \
  -d '{"id": "ws1", "egress": true}'        # or: msks create ws1 --egress
```

This chapter covers the plumbing: the NIC, the tap, DHCP, DNS, and
NAT. Per-flow consent — holding the first packet of each new
connection for an allow/deny decision over the decider channel —
arrives with #69 and tightens the same per-VM chain.

## The path

```
workspace VM ──virtio-net──► per-VM tap ──► per-VM nftables chain (appliance kernel)
                                              │  guest source → uplink: accept
                                              │  everything else on the tap: drop
                                              ▼
                                     NAT (masquerade) → appliance uplink
```

Each egress workspace owns a dedicated /30 carved from the
configured pool (`MSKSD_EGRESS_SUBNET`, default `172.31.0.0/16`):
the guest holds the first host address, the appliance-side tap the
second. The workspace's address, tap name, nftables table, and NIC
MAC all derive deterministically from the workspace id, so a
stop/start cycle reassembles the same network without state.

msksd is the only DHCP server and the only resolver the guest ever
sees: the DHCP offer names the tap as gateway and as DNS server, and
a small forwarder on the tap answers port 53 by relaying to the
appliance's own upstream (`MSKSD_EGRESS_DNS_UPSTREAM`, or the first
nameserver in the appliance's `/etc/resolv.conf`). #69 grows the
naming layer — query cache, name learning for prompts, and the
lockout that keeps DNS from being routed around — behind that same
offered resolver.

## What runs where

**Inside the appliance:** the tap, its address, the per-VM
nftables table, the NAT masquerade on the uplink, the DHCP service,
and the DNS forwarder — all owned by msksd, which runs as root
inside the appliance VM. The host's firewall is never touched;
containment stays inside the appliance by design.

**Inside the guest:** nothing msks-specific. The image overlay ships
a systemd-networkd DHCP unit (see [images.md](images.md)); the
kernel's `virtio_net` driver configures the NIC, DHCP configures the
address and resolver, and that is the whole of it. No sidecar, no
agent, no firewall.

**A workspace without egress** presents no NIC at all: no tap, no
chains, no services — the default posture on every backend, and the
one that needs zero enforcement machinery.

## Configuration

| Variable                     | Default            | Meaning                                        |
| ---------------------------- | ------------------ | ---------------------------------------------- |
| `MSKSD_EGRESS_ENABLED`       | `false`            | Arm the egress plumbing at daemon start (the appliance sets it) |
| `MSKSD_EGRESS_SUBNET`        | `172.31.0.0/16`    | The pool per-workspace /30s are carved from    |
| `MSKSD_EGRESS_UPLINK`        | `eth0`             | The appliance uplink NAT hides guests behind   |
| `MSKSD_EGRESS_DNS_UPSTREAM`  | resolv.conf        | Where the forwarder relays queries             |
| `MSKSD_EGRESS_LEASE_S`       | `3600`             | DHCP lease lifetime                            |
| `MSKSD_EGRESS_DNS_TIMEOUT_S` | `3.0`              | How long the forwarder waits on the upstream   |
| `MSKSD_IP_TOOL` / `MSKSD_NFT_TOOL` | `ip` / `nft` | The plumbing tools' paths                |

Egress needs the daemon to hold `CAP_NET_ADMIN`. The appliance sets
`MSKSD_EGRESS_ENABLED=true` and runs as root, so workspaces are
networked out of the box there. When msksd cannot arm the plumbing
(a dev-shell daemon, say), it stays up for everything else and every
egress workspace **refuses to boot** with a named cause, rather than
running with a half-open path — create those with `"egress": false`
instead.

## Backend support

Egress is a local-backend feature today. On k8s the runner pod
refuses the netns privilege the enforcement needs, so an egress
workspace fails its boot with the cause named — k8s workspaces boot
with `"egress": false` until then; the NetworkPolicy parity work is
#69. The no-NIC posture works everywhere.

## Lifecycle

`start` arms the tap and services before the VMM boots (the VMM
opens the tap by name); `stop`, `kill`, and `delete` tear the tap,
its chain, and its services down again. A failed boot unwinds its
own plumbing — no half-open path outlives a failed workspace start.
