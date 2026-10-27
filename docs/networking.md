# Guest networking

A workspace talks to the network only when it was created with
`egress: true` — and when it does, every piece of the path lives in
the daemon, where the guest cannot reach it.

```bash
curl -X POST .../api/v1/workspaces \
  -d '{"id": "ws1", "egress": true}'        # or: msks create ws1 --egress
```

This chapter covers the plumbing — the NIC, the tap, DHCP, DNS,
NAT, and the forward that carries ssh and rsync into the
workspace. The consent layer that gates where a new connection
may go is its own chapter: [Egress
consent](egress-consent.md) holds the first packet of each new
connection for an allow/deny decision over the decider channel,
built on the same per-VM chain.

## The path

```text
workspace VM ──virtio-net──► per-VM tap ──► per-VM nftables chain (host kernel)
                                              │  forward: guest source → uplink: accept;
                                              │  established replies back; all else drops
                                              │  input: only DHCP (67) and DNS (53) in
                                              ▼
                                     NAT (masquerade) → host uplink
```

Each egress workspace owns a dedicated /30 carved from the
configured pool (`MSKSD_EGRESS_SUBNET`, default `172.31.0.0/16`):
the guest holds the first host address, the daemon-side tap the
second. The workspace's tap name, nftables table, and NIC MAC derive
deterministically from the workspace id, and the pool slice is
recorded on the workspace row at first attach. Stop/start
cycles, daemon restarts, and even digest collisions between
workspace ids therefore keep the same address.

msksd is the only DHCP server and the only resolver the guest
ever sees. The DHCP offer names the tap as gateway and as DNS
server, and a small forwarder on the tap answers port 53 by
relaying to the host's own upstream (`MSKSD_EGRESS_DNS_UPSTREAM`,
or the first nameserver in the host's `/etc/resolv.conf`). The resolver
speaks UDP; TCP/53 has no listener, so a query needing TCP fallback
(DNSSEC validation, very large RRsets) fails fast. The forwarder
answers queries from its own guest only; anything else arriving on
the tap is dropped unread, so a spoofed-source datagram cannot turn
the daemon into a reflection amplifier. The resolver also grows the
naming layer: query cache, name learning for prompts, and the
lockout that keeps DNS from being routed around, all behind that
same offered
resolver.

## What the chain enforces

The per-VM table hooks two places, both scoped to the workspace's
tap:

- **Forward.** The guest's own source address may leave via the
  uplink, and the replies to connections the guest established may
  come back. Everything else across the tap drops: traffic from a spoofed
  source, traffic headed anywhere but the uplink (which is what
  blocks one workspace from reaching another's tap), and inbound
  the guest never asked for. Nothing outside initiates a
  connection into a workspace.
- **Input.** The guest may reach a fixed set of ports on the host
  through its tap: DHCP (67), the resolver (53), the interceptor's
  listener while armed, and the LLM proxy port when one is
  configured. Everything else
  from the tap drops before the host's own services — the API
  listener among them — so guest root cannot port-scan the
  host.

What the chain deliberately permits depends on the
workspace's consent mode (the [egress consent](egress-consent.md)
chapter). In `allow` mode, the
_destination_ of a guest-initiated connection is unconstrained:
any host reachable through the host uplink is reachable. In
`static` and `interactive` modes, every new flow passes a
destination gate first.

## What runs where

**On the host:** the tap, its address, the per-VM nftables
table, the NAT masquerade on the uplink, the DHCP service, and
the DNS forwarder are all owned by msksd. It runs as a
dedicated service user holding exactly two ambient
capabilities. `CAP_NET_ADMIN` covers taps and their addresses,
the nftables tables, and (because ambient capabilities survive
`exec`) the workspace VMM opening its tap. `CAP_NET_BIND_SERVICE`
covers the DHCP and DNS listeners, UDP 67 and 53. Nothing in the daemon's process tree
runs as uid 0; `/dev/kvm` reaches the VMM through the `kvm` group.
The per-tap service listeners — the interceptor, the LLM proxy,
and the probe service — bind ports above 1024 by default;
a setting that names a privileged port needs the capability.
The host's firewall is never touched; containment stays inside the
host by design.

**Inside the guest:** nothing msks-specific. The image overlay ships
a systemd-networkd DHCP unit (see [images.md](images.md)); the
kernel's `virtio_net` driver configures the NIC, DHCP configures the
address and resolver, and that is the whole of it. No sidecar, no
agent, no firewall.

**A workspace created with `"egress": false`** presents no NIC
at all: no tap, no chains, no services — the posture available on
every backend, and the one that needs zero enforcement machinery.

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

Egress needs the daemon to hold `CAP_NET_ADMIN` and
`CAP_NET_BIND_SERVICE` (the dev host's wrapper and the NixOS
module grant exactly those two) and a kernel that routes. The
host ships `net.ipv4.ip_forward=1` as a boot-time `sysctl.d`
setting, and the daemon verifies it at startup. A daemon that
reads `0` refuses every egress workspace with a cause naming
the sysctl key. The deployment host satisfies the routing kernel the daemon
verifies: the NixOS module in `nix/module.nix` sets
`net.ipv4.ip_forward=1`, names its own uplink through settings, and
grants the daemon the two capabilities — a `nixos-rebuild switch`
is the whole host setup. The dev host's wrapper carries the same
grant for `msks-dev` (its uplink default names this dev host's
interface). Debian and Fedora deployment hosts run their setup
tool — `msks-setup-debian-host` or `msks-setup-fedora-host` —
and [the Installation
chapter](installation.md) walks the tool end to end. For egress
it matters that the tool installs the same capability wrapper,
lands the sysctl and the module set this section names, and
registers the daemon's systemd user service with
`MSKSD_EGRESS_ENABLED=true` and the detected uplink. An
operator who sets `MSKSD_EGRESS_ENABLED=false` arms nothing, and
every egress workspace then refuses to boot with the cause named.
The dev-host egress smoke (`TEST_EGRESS=1`) runs as root,
because ambient capabilities cannot be granted to an arbitrary
shell. The harness creates real taps, loads nftables rules,
and binds ports 67 and 53, so it runs as full root and sets
`ip_forward` itself for the duration of the run. That is the test's constraint,
not the server's: the daemon needs only the two capabilities and an
already-routing kernel.

## Reaching guest services (the forward)

Egress is the guest's outbound path; the forward websocket
(`msks forward`, issue) is the client's inbound one, and it rides
the same per-VM tap.
The daemon dials the workspace's deterministic tap address on
the port the caller names and bridges raw bytes over its
authenticated websocket API. The CLI chapter's `msks forward`
section covers the stdio and `--local` shapes and the
close-code contract.

The workspace image meets the forward with a TCP service plane of
its own: sshd — the image's Debian package, enabled — and
rsync. sshd listens on all interfaces once the guest's NIC has its
address; a boot-time unit holds the port behind DHCP, with a
15s ceiling so a slow lease never blocks it for long. Every
login is a key login: `PasswordAuthentication no`, and root may
log in with a key only (`PermitRootLogin prohibit-password`). Host keys are
generated on the workspace's first boot into its persistent root
overlay, so a stop/start cycle presents the same host key — the
`known_hosts` entry you recorded on first login keeps matching. The
entry lives under the workspace's immutable id, so a workspace
recreated under the same name is a new id with a fresh cache: it
trusts its own first-boot host keys instead of refusing the first
instance's.
A workspace without egress carries the same image unchanged: its
forward is refused at the API with close code 4501 before any dial,
and its console is the virtio-console getty.

### The workspace ssh identity

A workspace created through the client carries the operator's
key as its ssh identity (issues). `identity_file` (or
`MSKSC_IDENTITY_FILE`) names your private key, its derived
public half travels with the create request, and the first
boot plants that line into `authorized_keys` for root and the
workspace's login user. The planting rides the same cidata
seed disk that carries `user_data`, so the guest needs no key
steps of its own. msks
generates no key: a client with no identity configured refuses
the create with a one-line error naming the config keys, the
variables, and `--pubkey`. `pubkey_file` (or
`MSKSC_PUBKEY_FILE`) names the public half directly instead
— a `.pub` file, read in place of any private material,
the create form for a key kept encrypted while the operator's
agent holds it. The daemon stores and seeds the public
line only; its database never holds a private half and its key
fetch carries no private field at all. The
public half persists across daemon
restarts and workspace stop/start: it lives on the workspace's
row, and a stop/start cycle serves the same line again. The seed
composes the identity's script with any `user_data` payload as
MIME siblings, so a workspace can carry both.

The daemon validates the supplied line by shape — a label in the
algorithm-name charset, fields, base64 body, and a blob whose
embedded algorithm name agrees with its label — and annotates it
with its own provenance comment (`msks-client:<id>`). A supplied
key is accepted at any type: the guest's sshd, the platform's
own, is the authority on which keys it will authenticate.

With the identity materialized, the usual client shapes work over
the forward:

```bash
msks forward myws 22 --local 2201 &
ssh -i ~/.ssh/id_ed25519 -p 2201 root@127.0.0.1
rsync -e 'ssh -i ~/.ssh/id_ed25519 -p 2201' \
    -av ./site/ root@127.0.0.1:/root/site/
```

The same login works as the workspace's login user —
`ssh -i ... -p 2201 alice@127.0.0.1`, the name `msks create
--user` recorded — whose home rides the persistent
`/home` volume. A workspace created before records no user
of its own; log in as `root`, or as `msks` — the account the
image itself ships, named directly.

`msks ssh` is the zero-step form of the same login: it boots
the workspace if needed and runs ssh with the forward as its
ProxyCommand. It signs through the operator's own agent when
it holds the workspace's key (`ssh-add`; matched on the key
material, so the file the key came from and its name do not
matter). Otherwise it stages the operator identity from
`identity_file` in a transient in-process ssh-agent, and the
private half never becomes a file. It logs in as the
workspace's login user by default, and as `root` for a
workspace that records none. `-l root` is also the recovery
login; see the CLI chapter's `msks ssh` section. When the command itself booted the workspace, it holds the
connection back until the guest accepts the workspace key. A
probe login retries behind the first boot's identity seeding,
so the first attempt lands as a session instead of
`Permission denied (publickey)`.

`msks rsync` is the copy form of the same seam: it runs the
host rsync over the forward with the identity staged in
memory, and the copy opens its own forward. `msks rsync
devbox -- -aP src/ :src/` targets the login user's home;
`root@:/root/x` targets root-owned paths. The CLI chapter's
`msks rsync` section has the details.

The user's own ssh config carries the same workflow for plain `ssh`
invocations — one wildcard block serves every workspace:

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
msks-devbox:srv/proj.git`, and VS Code Remote-SSH work against the
alias; ControlMaster shares one forward connection across
concurrent invocations. The block sets no `User`: plain `ssh` logs in as its own
default, the local username. That is the workspace's recorded
login user for the operator who created it (the name `msks
create --user` planted; `ssh -l alice` reaches a workspace
another operator's name recorded). A workspace that records no
user dials `root`, the account every seed provisions a key
for. The alias block names the operator's own
key with `IdentityFile` — the same file `identity_file` names.
Those paths are the client cache root's defaults and stay
where the block writes them: OpenSSH reads its own config, so
`MSKSC_CACHE_DIR` (the variable the devenv shell presets for
its command forms) moves only the `msks ssh`/`msks rsync`
state. A relocated cache takes the alias block's paths with it
only when you edit the block to match. `msks ssh` is the command form
that carries the identity per-session from memory instead — plain
`ssh` invocations against the alias need the file. The ProxyCommand runs `msks` in the user's environment, so
`MSKSC_URL`, `MSKSC_TOKEN`, and `MSKSC_CAFILE` must be set
there. `ssh -l root msks-devbox` is the recovery login. `-A`
forwards the operator's own agent into the workspace, and so
does `msks ssh -- -A`, which rewrites the request onto the
same socket without the alias block.

This is the client-held-secrets posture: the daemon host keeps
every capability the console and forward grant, but holds no
private key that opens the workspace's ssh.

### An operator-supplied key for one workspace

`msks create --pubkey FILE` (issue) builds the workspace
around a public key chosen for that one create — the key that
`~/.ssh/id_ed25519.pub` names, a hardware token's key, any
well-formed OpenSSH line — instead of the configured key. The
supplied line travels to the daemon at its own key type and is
accepted at any type — the guest's sshd, the platform's own, is
the authority on which keys it will authenticate. The daemon
re-annotates the line with its provenance comment and seeds it
like any other identity; its database holds the public half only.

The private half never leaves the operator's custody:
nothing is written client-side, and login uses the operator's
own key (`ssh -i` through a forward, or the `Host msks-*`
alias with `IdentityFile` pointing at the operator's key
file). The private half already exists wherever the operator
keeps it, so the client fetches and stores nothing. `msks ssh` on such a workspace signs
through the operator's agent when it holds the planted key
, reads the key `identity_file` names when it pairs with
the planted public half, and otherwise exits with a line naming
those recoveries.

### Identities minted before

A workspace whose row predates keeps the public half it was
created with, and the private half stays wherever the mint left
it — on the client that minted it, or in the operator's own
files. The daemon holds none (the stored halves and the serving of
them are long dropped), so an older workspace logs in the same
way a current one does. The login works through the operator's
agent when it holds the key; through the file `identity_file`
names when it pairs with the planted public half; or through
`ssh -i` with the half directly. The
console keeps working either way: the token-gated root
autologin, not a key login.

### Pushing code out with your own credentials

Written for the developer working inside a workspace: your
repo's remote (GitHub, a private GitLab host, anything
reachable on the network) accepts a push only with your
credentials, and the workspace has none of them and should
hold none. The workflow is
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

- `msks ssh my-workspace -- -A`, the one-command login with
  forwarding, forwards your agent too. The command rewrites
  the request onto the socket `SSH_AUTH_SOCK` names, the same
  agent the alias form above forwards, so no ssh-config block
  is needed. The
  plain alias (`ssh -A msks-devbox`) and the forward port directly
  (`ssh -A -i ~/.cache/msks/msks-devbox.key -p 2201 msks@127.0.0.1`)
  work as always.
- With the alias's ControlMaster, the agent arrives only on the
  connection that creates the master. If you connected earlier
  without `-A`, close the master first — `ssh -O exit msks-devbox`
  — then reconnect with `-A`, or wait out the ControlPersist
  window.

What the network allows depends on the workspace's consent
mode. `allow` reaches any destination beyond the host without
a grant: remotes, package mirrors, any host reachable through
the uplink. `static` and `interactive` gate each destination
first (the [egress consent](egress-consent.md) chapter covers
the semantics).
Guest-initiated connections aimed at the host itself stay
dropped in every mode (only DHCP and the resolver answer it). The end-to-end proofs are the `test_local_egress_git_out`
smoke (`TEST_EGRESS=1` locally, and part of CI's KVM
workflow) and the `test_local_egress_consent_*` smokes. The git smoke installs git in the guest over a plain `allow`
egress path and pushes a commit over a test-widened input pin.
It uses only a key that arrived through the forward as a
forwarded agent, since the host itself stays unreachable from
the guest by design. The consent smokes drive a hold, a verdict,
and the fail-closed denials through the real kernel path.

## Backend support

Egress is a local-backend feature; the consent API itself is
enforcement-agnostic (see “Local-only semantics” in [Egress
consent](egress-consent.md)), so a future backend can drive a
different mechanism from the same model. The no-NIC posture works
everywhere: a workspace created with `"egress": false` presents no
NIC whatever runs it.

## Lifecycle

`start` arms the tap and services before the VMM boots (the VMM
opens the tap by name); `stop`, `kill`, and `delete` tear the tap,
its chain, and its services down again. A failed boot unwinds its
own plumbing — no half-open path outlives a failed workspace start.
