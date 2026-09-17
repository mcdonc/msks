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
user means editing the file; the installer refuses when the existing
tap's owner differs and names the manual step, `ip link del
mskstap0` followed by a re-run):

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

On NixOS, paste this into `configuration.nix` (or a module) — it is the
installer, shape for shape: the same bridge, the same owner-held tap,
the same forwarding sysctl, the same three firewall rules. Substitute
the user who runs the appliance:

```nix
{ pkgs, ... }:

let
  # The user who runs the appliance; the tap is owned by it, which is
  # what lets the unprivileged cloud-hypervisor open it.
  applianceUser = "chrism";
in
{
  # Host forwarding — the same machine-identity setting the appliance
  # ships internally.
  boot.kernel.sysctl."net.ipv4.ip_forward" = "1";

  # Keep NetworkManager's hands off the appliance's devices (inert
  # where NetworkManager is not enabled).
  networking.networkmanager.unmanaged = [ "msksbr0" "mskstap0" ];

  systemd.services.msks-host-net = {
    description = "msks appliance host network (bridge, tap, NAT)";
    wantedBy = [ "multi-user.target" ];
    after = [ "systemd-modules-load.service" ];
    serviceConfig = {
      Type = "oneshot";
      RemainAfterExit = true;
    };
    path = with pkgs; [ iproute2 iptables ];
    script = ''
      set -e
      if ! ip link show dev msksbr0 >/dev/null 2>&1; then
        ip link add name msksbr0 type bridge
        ip addr add 192.168.77.1/24 dev msksbr0
        ip link set msksbr0 up
      fi
      if ! ip link show dev mskstap0 >/dev/null 2>&1; then
        ip tuntap add mode tap user ${applianceUser} mskstap0
        ip link set mskstap0 master msksbr0
        ip link set mskstap0 up
      fi
      ipt_rule() { # ipt_rule <table> <chain> <rule args...>: add if absent
        table="$1"
        shift
        iptables -t "$table" -C "$@" >/dev/null 2>&1 ||
          iptables -t "$table" -A "$@"
      }
      ipt_rule filter FORWARD -i msksbr0 -m conntrack --ctstate NEW,ESTABLISHED,RELATED -j ACCEPT
      ipt_rule filter FORWARD -o msksbr0 -m conntrack --ctstate ESTABLISHED,RELATED -j ACCEPT
      ipt_rule nat POSTROUTING -s 192.168.77.0/24 ! -o msksbr0 -j MASQUERADE
    '';
  };
}
```

The MASQUERADE rule matches any outbound interface (`! -o msksbr0`),
so it follows the default route across wifi↔eth switches; the whole
unit is idempotent, so a rebuild or a `systemctl restart
msks-host-net` re-arms anything a firewall reload dropped. Hosts that
prefer the blessed NAT module can replace the three `ipt_rule` calls
with `networking.nat.enable = true; networking.nat.internalInterfaces
= [ "msksbr0" ]; networking.nat.externalInterface = "<default-route
iface>";` (that module also sets `ip_forward` itself).

Non-NixOS hosts whose firewall is firewalld, or whose network
NetworkManager manages, keep the installer path: NetworkManager does
not consume networkd's `.netdev` files, and a firewalld complete
reload replaces the whole ruleset — foreign rules added once, by
script or by file, do not survive it. Re-running the installer
re-arms the state after such a reload (the NixOS unit above is
immune on both counts: `networkmanager.unmanaged` claims the devices,
and a rebuild re-runs the idempotent unit).

## Reaching guest services (the forward)

Egress is the guest's outbound path; the forward websocket
(`msks forward`, issue #109) is the client's inbound one, and it rides
the same per-VM tap.
The daemon dials the workspace's deterministic tap address on the
port the caller names and bridges raw bytes over its authenticated
websocket API — see the CLI chapter's `msks forward` section for the
stdio and `--local` shapes and the close-code contract.

The workspace image meets the forward with a TCP service plane of
its own (#110): sshd — the image's Debian package, enabled — and
rsync. sshd listens on all interfaces once the guest's NIC has its
address (a boot-time unit holds the port behind DHCP, with a 15s
ceiling so a slow lease never blocks it for long), and every login
is a key login: `PasswordAuthentication no`, and root may log in
with a key only (`PermitRootLogin prohibit-password`). Host keys are
generated on the workspace's first boot into its persistent root
overlay, so a stop/start cycle presents the same host key — the
`known_hosts` entry you recorded on first login keeps matching.
A workspace without egress carries the same image unchanged: its
forward is refused at the API with close code 4501 before any dial,
and its console is the vsock one.

### The minted workspace identity

A workspace created without a client-supplied key — a direct API
create, or `msks create --daemon-mint` — carries an ssh
identity msksd minted at create (issue #111): a keypair stored with the workspace's state, whose
public half the first boot plants into `authorized_keys` for both
root and the `msks` workspace user — through the same cidata seed
disk that carries `user_data`, so the guest needs no key steps of
its own. The halves persist across daemon restarts and workspace
stop/start: they live on the workspace's row, and a stop/start
cycle serves the same identity again. The seed composes the
identity's script with any `user_data` payload as MIME siblings, so
a workspace can carry both.

The private half is fetched over the authenticated API — a token
holder already owns the workspace's root console, so it grants
nothing new:

```bash
msks key myws                    # the public authorized_keys line
msks key myws --private          # the private half, on stdout
msks key myws --out ./myws.key   # the private half, mode 0600
```

The key type is the daemon's setting (`ssh_key_type` /
`MSKSD_SSH_KEY_TYPE`): ECDSA P-256 by default, `ed25519` and `rsa`
(3072-bit) selectable. The identity is minted at create on the local
backend — a workspace created there and later started by a daemon
reconfigured for the k8s runner keeps its halves, and the runner
plants nothing (the same posture as its `user_data`). With the identity materialized, the usual
client shapes work over the forward:

```bash
msks key myws --out ~/.cache/msks/myws.key
msks forward myws 22 --local 2201 &
ssh -i ~/.cache/msks/myws.key -p 2201 root@127.0.0.1
rsync -e 'ssh -i ~/.cache/msks/myws.key -p 2201' \
    -av ./site/ root@127.0.0.1:/root/site/
```

The same login works as the workspace user —
`ssh -i ... -p 2201 msks@127.0.0.1` — whose home rides the
persistent `/home` volume.

`msks ssh` (#112) is the zero-step form of the same login: it boots
the workspace if needed, serves the minted identity from a
transient in-process ssh-agent (the private half never becomes a
file), and runs ssh with the forward as its ProxyCommand — as the
`msks` workspace user by default, with `-l root` as the recovery
login (see the CLI chapter's `msks ssh` section).

The user's own ssh config carries the same workflow for plain `ssh`
invocations — one wildcard block serves every workspace:

```text
Host msks-*
    User msks
    ProxyCommand sh -c 'exec msks forward "${1#msks-}" 22' _ %h
    UserKnownHostsFile ~/.cache/msks/%h/known_hosts
    StrictHostKeyChecking accept-new
    IdentityFile ~/.cache/msks/%h.key
    IdentitiesOnly yes
    ControlMaster auto
    ControlPath ~/.cache/msks/%h.ctl
    ControlPersist 10m
```

`ssh msks-devbox`, `rsync -aP src/ msks-devbox:/src/`, `git clone
msks-devbox:srv/proj.git`, and VS Code Remote-SSH work against the
alias; ControlMaster shares one forward connection across
concurrent invocations. The alias block names its identity with
`IdentityFile` — create it once with `msks key devbox --out
~/.cache/msks/msks-devbox.key` (mode 0600, the private half fetched
over the authenticated API). `msks ssh` is the command form that
carries the identity per-session from memory instead — plain `ssh`
invocations against the alias need the file. For a client-minted
workspace (#121, the create default) that file is the client-held
private half itself (`~/.local/share/msks/<id>/identity`, written
at create); when #123
lands, the alias points at the operator's own key and the minted
identity retires to a first-boot enrollment credential. The
ProxyCommand runs `msks` in the user's environment,
so `MSKSC_URL`, `MSKSC_TOKEN`, and `MSKSC_CAFILE` must be set there;
`ssh -l root msks-devbox` is the recovery login, and `-A` forwards
the operator's own agent into the workspace.

### The client-minted default (no escrow)

`msks create` mints the workspace's ssh keypair on the client by
default (issue #121): the client generates it locally, sends the
public half with the create request, and keeps the private half —
the daemon stores the public line and seeds it into the guest's
`authorized_keys` exactly as it seeds its own minted half, and its
database never holds a private half for the workspace (the API's
key fetch answers `private_key: null`). The daemon validates the
supplied line the way it validates its own output — the accepted
algorithms are the ones it mints itself — and annotates it with its
own provenance comment (`msks-client:<id>`, beside the minted
mode's `msksd:<id>`). `--daemon-mint` opts back into the
daemon-minted mode above; the k8s backend serves no identity in
either mode, so creates against it pass `--daemon-mint`.

The private half is written mode 0600 under the client data root
after the create succeeds — `~/.local/share/msks/<id>/identity`,
honoring `XDG_DATA_HOME` — and `msks ssh` reads it from there when
the API serves the public half alone (checking the stored half
against the served public line, so a stale copy fails as one named
line, not ssh's opaque `Permission denied`). Losing the file loses
ssh to that workspace (the console still opens); the alias workflow
can point `IdentityFile` at a copy kept anywhere the operator
likes. The data root, not the cache, holds the key on purpose:
cache sweeps leave it alone. Deleting the workspace leaves the
stored half behind, like its `known_hosts` — remove the
per-workspace directory under the data root when you want the
material gone. The key type is the client's choice at create
(`--key-type`: `ecdsa` by default, `ed25519`, `rsa`), independent
of the daemon's `MSKSD_SSH_KEY_TYPE` setting.

This is the ssh half of the client-held-secrets posture: an
appliance owner keeps every capability the console and forward
grant, but no longer holds a private key that opens the workspace's
ssh. The console challenge-response half is #123.

### Pushing code out with your own credentials

Written for the developer working inside a workspace: your repo's
remote — GitHub, a private GitLab host, anything reachable on the
network — accepts a push only with your credentials, and the
workspace has none of them and should hold none. The workflow is
one login with agent forwarding, after which a push from inside
the workspace works exactly as it does on your own machine.

On your client machine, load the key into your agent (`ssh-add`),
then log in through the forward with `-A`:

```bash
ssh -A msks-devbox
```

Inside the session your agent is present: `ssh-add -l` lists your
keys, and every ssh the session starts — git's included — offers
them to the remote, so the ordinary push needs no extra setup:

```bash
git -C ~/work/proj push origin main
```

The credential stays on your machine. The agent rides the
connection as a socket, so nothing is written to the workspace's
disks, the image, or the seed; when the session closes, the
workspace keeps no copy of the key. Host-key checking for the
remote behaves as anywhere else (the workspace has its own
`~/.ssh/known_hosts`).

Two msks-specific details:

- `msks ssh`, the one-command login, forwards its own transient
  agent — the one holding the minted workspace identity. That
  agent carries no credentials for your remotes. To push with
  your own keys, use a plain `ssh -A`: the alias form above, or
  the forward port directly
  (`ssh -A -i ~/.cache/msks/msks-devbox.key -p 2201 msks@127.0.0.1`).
- With the alias's ControlMaster, the agent arrives only on the
  connection that creates the master. If you connected earlier
  without `-A`, close the master first — `ssh -O exit msks-devbox`
  — then reconnect with `-A`, or wait out the ControlPersist
  window.

What the network allows: a workspace with egress reaches any
off-appliance destination without a grant — remotes, package
mirrors, any host reachable through the uplink. Guest-initiated
connections aimed at the appliance itself stay dropped (only DHCP
and the resolver answer it); per-destination consent gates (#69)
narrow guest-initiated egress later. The end-to-end proof is the
`test_local_egress_git_out` smoke (`MSKSD_TEST_EGRESS=1` locally,
and part of CI's KVM workflow): it installs git in the guest over
the egress path and pushes a commit — over a test-widened input
pin, since the appliance itself stays unreachable from the guest
by design — using only a key that arrived through the forward as a
forwarded agent.

### Cryptographic agility (a future FIPS posture)

The image pins login policy — who may authenticate, and how — and
leaves algorithm selection to the platform. No cipher, MAC,
key-exchange, or host-key algorithm lists appear in the guest's sshd
configuration or the daemon's own settings, so an OpenSSH build whose
crypto library enforces a FIPS module applies its restrictions by
itself, without msks-side config surgery. The guest's libraries are
Debian's own (OpenSSL 3), the line that carries a certified provider
when one exists. The algorithm choices in play are FIPS-approvable
from the start: identities are ECDSA P-256 (#111's mint, and the
example above takes whatever key the mint hands it), and first boot
generates the full `ssh-keygen -A` host-key set, whose RSA and ECDSA
members are the keys a FIPS-mode sshd serves — all persisting across
stop/start on the overlay.
Issue #115 records the constraint that keeps it that way: every
crypto choice stays a setting or a platform default, never a pinned
list.

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
