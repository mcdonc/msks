# Guest networking

In msks, each workspace VM gets its own "tap": the VM's virtio-net NIC plugs
into one end, the host kernel (nftables chains, the DHCP/DNS services, NAT)
sits on the other.

A tap is a Linux TAP device — a virtual network interface created in software
(/dev/tap, part of the kernel's TUN/TAP subsystem). Unlike a physical NIC, a
program running on the host can open the tap and read/write raw Ethernet
frames to it, so anything the VM sends out its NIC appears as packets arriving
on the host-side tap, and vice versa.

Creating taps and assigning addresses requires a Linux capability
`CAP_NET_ADMIN`, which is why msksd runs using `systemd` and a capability
wrapper.

A workspace talks to the network only when it was created with
`egress: true`, and when it does, every piece of the path lives in
the daemon, where the guest cannot reach it.

For example:

```bash
curl -X POST .../api/v1/workspaces -d '{"id": "ws1", "egress": true}'
```

Or:

```bash
msks create ws1 --egress
```

This chapter covers the plumbing: the NIC, the tap, DHCP, DNS, NAT, and the
forward that carries ssh and rsync into the workspace.

The consent layer that gates where a new connection may go is its own chapter:
[Egress consent](egress-consent.md) holds the first packet of each new
connection for an allow/deny decision over the decider channel, built on the
same per-VM chain.

## The path

```text
workspace VM ──virtio-net──► per-VM tap ──► per-VM nftables chain (host kernel)
                                              │  forward: guest source → uplink: accept;
                                              │  established replies back; all else drops
                                              │  input: only DHCP (67) and DNS (53) in
                                              ▼
                                     NAT (masquerade) → host uplink
```

Each egress-enabled workspace owns a dedicated /30 carved from the
configured pool (`MSKSD_EGRESS_SUBNET`, default `172.31.0.0/16`):
the guest holds the first host address, the daemon-side tap the
second.

The workspace's tap name, nftables table, and NIC MAC derive
deterministically from the workspace id, and the pool slice is
recorded on the workspace row at first attach. Stop/start
cycles, daemon restarts, and even digest collisions between
workspace ids therefore keep the same address.

msksd is the DHCP server. The DHCP offer gives the tap as gateway and as DNS
server, and a small forwarder on the tap answers port 53 by relaying to the
host's own upstream (`MSKSD_EGRESS_DNS_UPSTREAM`, or the first nameserver in
the host's `/etc/resolv.conf`).

The resolver speaks UDP. TCP/53 has no listener, so a query needing TCP
fallback (DNSSEC validation, very large RRsets) fails fast. The forwarder
answers queries from its own guest only. Anything else arriving on the tap is
dropped unread, so a spoofed-source datagram cannot turn the daemon into a
reflection amplifier. The resolver also grows the naming layer: query cache,
name learning for prompts, and the lockout that keeps DNS from being routed
around, all behind that same offered resolver.

## What the chain enforces

The per-VM table hooks two places, both scoped to the workspace's
tap:

- **Forward.** The guest's own source address may leave via the
  uplink, and the replies to connections the guest established may
  come back. Everything else across the tap drops: traffic from a
  spoofed source, traffic headed anywhere but the uplink (which is
  what blocks one workspace from reaching another's tap), and
  inbound the guest never asked for. Nothing outside initiates a
  connection into a workspace.
- **Input.** The guest may reach a fixed set of ports on the host
  through its tap: DHCP (67), the resolver (53), the interceptor's
  listener while armed, and the LLM proxy port when one is
  configured. Everything else
  from the tap drops before the host's own services (the API
  listener among them), so guest root cannot port-scan the
  host.

What the chain deliberately permits depends on the
workspace's consent mode (the [egress consent](egress-consent.md)
chapter). In `allow` mode, the
_destination_ of a guest-initiated connection is unconstrained:
any host reachable through the host uplink is reachable. In
`static` and `interactive` modes, every new flow passes a
destination gate first.

## What runs where

**On the host:** the tap, its address, the per-VM nftables table, the NAT
masquerade on the uplink, the DHCP service, and the DNS forwarder are all owned
by msksd. It runs as a dedicated service user holding exactly two ambient
capabilities. `CAP_NET_ADMIN` covers taps and their addresses, the nftables
tables, and (because ambient capabilities survive `exec`) the workspace VMM
opening its tap. `CAP_NET_BIND_SERVICE` covers the DHCP and DNS listeners, UDP
67 and 53. Nothing in the daemon's process tree runs as uid 0; `/dev/kvm`
reaches the VMM through the `kvm` group. The per-tap service listeners (the
interceptor, the LLM proxy, and the probe service) bind ports above 1024 by
default. A setting that gives a privileged port needs the capability. The
host's firewall is never touched. Containment stays inside the host by design.

**Inside the guest:** nothing msks-specific. The image overlay ships
a systemd-networkd DHCP unit (see [images.md](images.md)); the
kernel's `virtio_net` driver configures the NIC, DHCP configures the
address and resolver.

**A workspace created with `"egress": false`** presents no NIC
at all. The posture is available
on every backend, and it needs zero enforcement machinery.

## Configuration

| Variable                           | Default         | Meaning                                                                                 |
| ---------------------------------- | --------------- | --------------------------------------------------------------------------------------- |
| `MSKSD_EGRESS_ENABLED`             | `false`         | Arm the egress plumbing at daemon start (the dev daemon and the NixOS module enable it) |
| `MSKSD_EGRESS_SUBNET`              | `172.31.0.0/16` | The pool per-workspace /30s are carved from                                             |
| `MSKSD_EGRESS_UPLINK`              | `eth0`          | The host uplink NAT hides guests behind                                                 |
| `MSKSD_EGRESS_DNS_UPSTREAM`        | resolv.conf     | Where the forwarder relays queries                                                      |
| `MSKSD_EGRESS_LEASE_S`             | `3600`          | DHCP lease lifetime                                                                     |
| `MSKSD_EGRESS_DNS_TIMEOUT_S`       | `3.0`           | How long the forwarder waits on the upstream                                            |
| `MSKSD_IP_TOOL` / `MSKSD_NFT_TOOL` | `ip` / `nft`    | The plumbing tools' paths                                                               |

A ruleset apply hands `nft` a file under the daemon's state
directory and passes it by path, so the tool configured above
runs with the daemon's own read access to that directory. A
wrapper that execs the real `nft` as another user -- or in another
mount namespace -- must carry that access along.

Egress needs the daemon to hold `CAP_NET_ADMIN` and
`CAP_NET_BIND_SERVICE` (the dev host's wrapper and the NixOS
module grant exactly those two) and a kernel that routes. The
host ships `net.ipv4.ip_forward=1` as a boot-time `sysctl.d`
setting, and the daemon verifies it at startup. A daemon that
reads `0` refuses every egress workspace with a cause that
gives the sysctl key.

Debian and Fedora deployment hosts run their setup tool,
`msks-setup-debian-host` or `msks-setup-fedora-host`, and [the Installation
chapter](installation.md) describes the tool end to end. For egress it matters
that the tool installs a capability wrapper, lands the sysctl and the module
set this section lists, and registers the daemon's systemd user service with
`MSKSD_EGRESS_ENABLED=true` and the detected uplink. An operator who sets
`MSKSD_EGRESS_ENABLED=false` arms nothing, and every egress workspace then
refuses to boot with the cause given.

## Reaching guest services (msks forward)

Egress is the guest's outbound path. The forward websocket (`msks forward`,
issue) is the client's inbound one, and it uses the same per-VM tap. The
daemon dials the workspace's deterministic tap address on the port the
caller gives and bridges raw bytes over its authenticated websocket API. The
CLI chapter's `msks forward` section covers the stdio and `--local` shapes
and the close-code contract.

The workspace image meets the forward with a TCP service plane of its own:
sshd (the image's Debian package, enabled) and rsync. sshd listens on all
interfaces once the guest's NIC has its address. A boot-time unit holds the
port behind DHCP, with a 15s ceiling so a slow lease never blocks it for
long. Every login is a key login: `PasswordAuthentication no`, and root may
log in with a key only (`PermitRootLogin prohibit-password`). Host keys are
generated on the workspace's first boot into its persistent root overlay, so
a stop/start cycle presents the same host key: the `known_hosts` entry you
recorded on first login keeps matching. The entry lives under the
workspace's immutable id, so a workspace recreated under the same name is a
new id with a fresh cache: it trusts its own first-boot host keys instead of
refusing the first instance's. A workspace without egress carries the same
image unchanged: its forward is refused at the API with close code 4501
before any dial, and its console is the virtio-console getty.

### The workspace ssh identity

A workspace created through the client carries the operator's
key as its ssh identity (issues). `identity_file` (or
`MSKSC_IDENTITY_FILE`) is queried for your private key. Its derived
public half travels with the create request, and the first
boot plants that line into `authorized_keys` for root and the
workspace's login user.

The daemon stores and seeds the public ssh key only. Its database never holds a
private half and its key fetch carries no private field at all. The public half
persists across daemon restarts and workspace stop/start: it lives on the
workspace's row, and a stop/start cycle serves the same line again. The seed
composes the identity's script with any `user_data` payload as MIME siblings,
so a workspace can carry both.

The daemon validates the supplied line by shape: a label in the
algorithm-name charset, fields, base64 body, and a blob whose
embedded algorithm name agrees with its label. It annotates the
line with its own provenance comment (`msks-client:<id>`). A
supplied key is accepted at any type: the guest's sshd, the
platform's own, is the authority on which keys it will
authenticate.

With the identity materialized, the usual client shapes work over
the forward:

```bash
msks forward myws 22 --local 2201 &
ssh -i ~/.ssh/id_ed25519 -p 2201 root@127.0.0.1
rsync -e 'ssh -i ~/.ssh/id_ed25519 -p 2201' \
    -av ./site/ root@127.0.0.1:/root/site/
```

The same login works as the workspace's login user (`ssh -i ... -p 2201
alice@127.0.0.1`, the name `msks create --user` recorded), whose home lives
on the persistent `/home` volume. A workspace created before records no user
of its own. Log in as `root`, or as `msks`, the account the image itself
ships, given directly.

`msks ssh` is the zero-step form of the same login: it boots
the workspace if needed and runs ssh with the forward as its
ssh `ProxyCommand`.

It signs through the operator's own agent when it holds the workspace's key
(`ssh-add`; matched on the key material, so the file the key came from and its
name do not matter). Otherwise it stages the operator identity from
`identity_file` in a transient in-process ssh-agent, and the private half never
becomes a file.

It logs in as the workspace's login user by default, and as `root` for a
workspace that records none. `-l root` is also the recovery login; see the CLI
chapter's `msks ssh` section. When the command itself booted the workspace, it
holds the connection back until the guest accepts the workspace key. A probe
login retries behind the first boot's identity seeding, so the first attempt
lands as a session instead of `Permission denied (publickey)`.

`msks rsync` is the copy form of the same: it runs the
host rsync over the forward with the identity staged in
memory, and the copy opens its own forward. `msks rsync
devbox -- -aP src/ :src/` targets the login user's home;
`root@:/root/x` targets root-owned paths. The CLI chapter's
`msks rsync` section has the details.

The user's own ssh config may carry the same workflow for plain `ssh`
invocations; one wildcard block serves every workspace:

```text
Host msks-*
    ProxyCommand sh -c 'exec msks forward "${1#msks-}" 22' _ %h
    UserKnownHostsFile ~/.cache/msks/%h/known_hosts
    StrictHostKeyChecking accept-new
    IdentityFile ~/.ssh/id_ed25519
    IdentitiesOnly yes
    ControlMaster auto
    ControlPath ~/.cache/msks/%h.ctl
    ControlPersist 10m
```

`ssh msks-devbox`, `rsync -aP src/ msks-devbox:/src/`, `git clone
msks-devbox:srv/proj.git`, and VS Code Remote-SSH work against the alias;
ControlMaster shares one forward connection across concurrent invocations.
The block sets no `User`: plain `ssh` logs in as its own default, the local
username. That is the workspace's recorded login user for the operator who
created it (the name `msks create --user` planted; `ssh -l alice` reaches a
workspace another operator's name recorded). A workspace that records no
user dials `root`, the account every seed provisions a key for. The alias
block gives the operator's own key with `IdentityFile`, the same file
`identity_file` points at. Those paths are the client cache root's defaults
and stay where the block writes them. OpenSSH reads its own config, so
`MSKSC_CACHE_DIR` (the variable the devenv shell presets for its command
forms) moves only the `msks ssh`/`msks rsync` state. A relocated cache takes
the alias block's paths with it only when you edit the block to match. `msks
ssh` is the command form that carries the identity per-session from memory
instead. Plain `ssh` invocations against the alias need the file. The
ProxyCommand runs `msks` in the user's environment, so `MSKSC_URL`,
`MSKSC_TOKEN`, and `MSKSC_CAFILE` must be set there. `ssh -l root
msks-devbox` is the recovery login. `-A` forwards the operator's own agent
into the workspace, and so does `msks ssh -- -A`, which rewrites the request
onto the same socket without the alias block.

### An operator-supplied key for one workspace

`msks create --pubkey FILE` (issue) builds the workspace around a public key
chosen for that one create (the key that `~/.ssh/id_ed25519.pub` gives, a
hardware token's key, any well-formed OpenSSH line) instead of the
configured key. The supplied line travels to the daemon at its own key type
and is accepted at any type: the guest's sshd, the platform's own, is the
authority on which keys it will authenticate. The daemon re-annotates the
line with its provenance comment and seeds it like any other identity. Its
database holds the public half only.

### Pushing code out with your own credentials

Written for the developer working inside a workspace: your repo's remote
(GitHub, a private GitLab host, anything reachable on the network) accepts a
push only with your credentials, and the workspace has none of them and
should hold none. The workflow is one login with agent forwarding, after
which a push from inside the workspace works exactly as it does on your own
machine.

On your client machine, load the key into your agent (`ssh-add`),
then log in through the forward with `-A`:

```bash
ssh -A msks-devbox
```

Inside the session your agent is present: `ssh-add -l` lists your
keys, and every ssh the session starts (git's included) offers
them to the remote, so the ordinary push needs no extra setup:

```bash
git -C ~/work/proj push origin main
```

The credential stays on your machine. The agent travels the
connection as a socket, so nothing is written to the workspace's
disks, the image, or the seed; when the session closes, the
workspace keeps no copy of the key. Host-key checking for the
remote behaves as anywhere else (the workspace has its own
`~/.ssh/known_hosts`).

Two msks-specific details:

- `msks ssh my-workspace -- -A`, the one-command login with
  forwarding, forwards your agent too. The command rewrites
  the request onto the socket `SSH_AUTH_SOCK` gives, the same
  agent the alias form above forwards, so no ssh-config block
  is needed. The
  plain alias (`ssh -A msks-devbox`) and the forward port directly
  (`ssh -A -i ~/.cache/msks/msks-devbox.key -p 2201 msks@127.0.0.1`)
  work as always.
- With the alias's ControlMaster, the agent arrives only on the
  connection that creates the master. If you connected earlier
  without `-A`, close the master first (`ssh -O exit
msks-devbox`), then reconnect with `-A`, or wait out the
  ControlPersist window.

What the network allows depends on the workspace's consent
mode. `allow` reaches any destination beyond the host without
a grant: remotes, package mirrors, any host reachable through
the uplink. `static` and `interactive` gate each destination
first (the [egress consent](egress-consent.md) chapter covers
the semantics).

Guest-initiated connections aimed at the host itself stay
dropped in every mode (only DHCP and the resolver answer it).

## Backend support

Egress is a local-backend feature; the consent API itself is
enforcement-agnostic (see “Local-only semantics” in [Egress
consent](egress-consent.md)). A future backend can therefore drive a
different mechanism from the same model. The no-NIC posture works
everywhere: a workspace created with `"egress": false` presents no NIC
whatever runs it.

## Lifecycle

`start` arms the tap and services before the VMM boots (the VMM
opens the tap by name); `stop`, `kill`, and `delete` tear the tap,
its chain, and its services down again. A failed boot unwinds its
own plumbing: no half-open path outlives a failed workspace start.
