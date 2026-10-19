# Guest networking

A workspace talks to the network only when it was created with
`egress: true` — and when it does, every piece of the path lives in
the daemon, where the guest cannot reach it.

```bash
curl -X POST .../api/v1/workspaces \
  -d '{"id": "ws1", "egress": true}'        # or: msks create ws1 --egress
```

This chapter covers the plumbing (the NIC, the tap, DHCP,
DNS, and NAT) and the consent layer that gates it. The [Egress
consent](#egress-consent) section describes holding the first
packet of each new connection for an allow/deny decision over
the decider channel, built on the same per-VM chain.

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
the daemon into a reflection amplifier. grows the naming
layer — query cache, name learning for prompts, and the lockout that
keeps DNS from being routed around — behind that same offered
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
workspace's consent mode (next section). In `allow` mode, the
_destination_ of a guest-initiated connection is unconstrained:
any host reachable through the host uplink is reachable. In
`static` and `interactive` modes, every new flow passes a
destination gate first.

## Egress consent

A workspace picks its consent posture at create time
(`msks create --egress-mode`), and `msks egress mode` switches it
afterwards — see [switching modes
live](#switching-modes-live) below. The posture decides where
each new outbound connection is decided:

- **`allow` (the create default).** New flows pass. The daemon's
  resolver records each off-allowlist name the guest looks up, as an
  audit row, and that is the whole of it — the behavior plus the
  naming-layer audit.
- **`static`.** The workspace's allowlist (repeatable
  `--allow SPEC` at create) is the policy. Allowlisted names resolve
  through the daemon's resolver and their addresses are pinned as
  allowed in the kernel chain for the answer's TTL, so
  `apt update` against `--allow .deb.debian.org` completes with no
  prompt and no hold. An off-list name answers NXDOMAIN — the guest
  learns nothing, and the lookup cannot serve as a resolution
  oracle or an exfil channel — and the denial is recorded as an
  audit row. Address-range entries (`10.0.0.0/8`, `203.0.113.7:5432`)
  accept in the chain directly.
- **`interactive`.** The first packet of every new flow
  queues to the workspace's own NFQUEUE number inside the host
  kernel, and msksd holds it until a decider answers:

  ```text
  workspace VM ──virtio-net──► tap ──► per-VM nftables chain
                                    │  ESTABLISHED → accept (conntrack)
                                    │  allowlist / prior verdicts → accept
                                    │  NEW → NFQUEUE ──► msksd consent engine
                                    │                        │ verdict
                                    │                        ▼
                                    │             accept + pin / drop + RST
                                    ▼                        / expire
                                  NAT → host uplink
  ```

  A decider is any authenticated client that announced itself
  on the events websocket (`msks egress watch <workspace>`).
  New holds wait for a decider only while at least one is
  connected, and the connection itself is the decider's
  liveness. A hold created before the last decider left still
  runs its timeout. Without a
  decider an off-list connect fails fast — no prompt, no hang. The prompt names the DNS name (`api.anthropic.com:443`),
  because the resolver is the one the DHCP lease hands the guest
  and it remembers which name resolved to which address. A
  destination given by address (a Postgres host) prompts with the
  address itself and keys its verdict on the address.

  **Web flows while the interceptor is armed.** A covered
  workspace's TCP flows toward ports 80 and 443 redirect into
  the [interceptor](secrets.md#the-interceptor-the-swap-on-the-wire)
  before the kernel chain, so those flows gate at the
  interceptor itself. They gate on the name the wire carries,
  which is the one thing the network layer cannot read: the SNI
  of the TLS handshake on HTTPS, the Host header on plain
  HTTP. The name counts only when the daemon's naming memory binds it
  to the connection's address, which happens when the guest
  resolved it through the daemon's resolver. An address the
  memory never learned (a hosts-file entry) keys the gate by the
  address itself, exactly as the kernel queue keys a raw-IP
  connect. A name claiming an address the memory holds under a
  different name keys by that name. A name's allowlist entry or
  verdict therefore covers no address it never resolved to. The decision table matches the resolver gate's precedence: a
  standing deny verdict first, then the static allowlist (name
  specs against the gated name, address specs against the
  original destination), then a standing allow, then the mode.
  `static` records the denial and answers a local HTTP 403.
  `interactive` holds through the consent engine, so the same
  decider endpoints answer the same rows the kernel queue
  writes; an allow completes the handshake, and a deny answers
  the 403 locally (nothing forwards). The 403's body names what happened. A verdict's refusal says
  consent denied the destination. A flow that lost the duplicate
  race says a decision for the destination is already pending
  and points the operator at the consent prompt. A prompt no
  decider answered (none registered, or the request expired
  undecided) says so. A request that ended without a decision
  (the gate failed, the prompt cap refused the hold, or the
  workspace vanished, stopped, or switched mode mid-hold) says
  that instead. Verdicts and durations are shared: one allow
  covers the destination's web flows and its other ports alike,
  and the session memory that skips re-prompting is one table for
  both gates. The gating boundary is the connection, both protocols alike.
  A TLS handshake gates once; kept-alive requests, several Hosts
  included, ride the connection's own verdict until it closes. A
  plain connection answers each destination once: a different
  Host gates fresh, and repeats of the same destination share
  the connection's own answer. `msks egress revoke` therefore stops a destination's next
  connection and its first plain request on a fresh connection.
  Connections that already carry consent (established TLS, or a
  plain connection that already answered that destination) keep
  it until they close. The kernel path additionally kills
  established flows with their conntrack entries; the
  interceptor's connections have no conntrack entry to kill.

**Verdicts and durations.** `msks egress decide` records
`allow`/`deny` with a duration — `once` (this connection only; a
reconnect re-prompts), `5m`, `15m`, `tilrestart` (until the VM
stops; the default), `forever` (the workspace's lifetime — replayed
at every boot). An allow pins the destination in the kernel for its
duration and covers the _name_, so a CDN-rotated address of an
allowed host resolves and passes without re-prompting. A deny
answers the SYN and its retransmits with a TCP RST — the guest's
`connect()` fails immediately instead of hanging on the kernel's
retransmit timer. On an address two names resolved to (shared hosting, a CDN
front), a verdict stays with its own name. The address carries
no allow pin while both names' resolutions are live; each new
connection gates at the queue, named by whichever resolution
came last. A deny refuses only its own connection (a per-flow
RST, keyed by that connection's source port). The pins the
address already carried are withdrawn the moment the second
name resolves to it, except the pins a verdict given by
address owns: those are address-scoped by construction and
re-pin after the withdrawal. The queue attributes a connection to the most recent
resolution of its address. A guest whose cached resolution
outlives a later one for the same address is indistinguishable
at this layer; separating those needs the connection's own
hostname (TLS SNI), which the network layer does not read. A static workspace keeps its allowlist pins on a shared
address: its chain has no queue to hand a withdrawn pin's
connections to, and the allowlist's resolved-address pins are the
whole of its enforcement. `msks egress revoke` undoes an in-effect verdict at once. The
pinned rules clear, the destination's live connections die with
their conntrack entries (on a shared address that includes the
co-resident's live connections to it), and new connections
gate again. Every row — request, verdict,
expiry, revocation — lands in the consent table with its
provenance, pruned past `MSKSD_EGRESS_CONSENT_RETENTION_DAYS` and
the per-workspace `MSKSD_EGRESS_CONSENT_ROW_CAP`; with
`MSKSD_AUDIT_HMAC_KEY` set each row also carries an HMAC-SHA256
tag over its columns.

**The naming layer owns DNS.** Ports 53 and 853 toward anywhere but
the daemon's own resolver drop in the per-VM chain, in every mode,
so the guest cannot route around the resolver that names its
prompts. Plain-IP egress to a DoH endpoint on 443 is enforced like
any other flow — in `static`/`interactive` it meets the same
destination gate.

**Fail-closed, by construction.** The queue rule carries no
bypass: a queue with no listener (msksd down, the consumer not
yet bound) and a full queue both _drop_. That is the opposite
default of a sidecar-in-netns design, which inherits the
namespace's plumbing only while its process lives. Each workspace owns its queue number
(derived from its address-pool slice), so one workspace flooding
SYNs delays only its own verdicts. And the whole mechanism — taps,
chains, queues, the resolver, the verdict table — lives in the
host's kernel and msksd's userspace: guest root sees none of
it (no syscall, no `/proc`, no signal target).

**Local-only semantics.** Per-flow holds are the local backend's
enforcement; the consent API itself (requests, verdicts, durations,
revocation, audit) is enforcement-agnostic, so a future backend can
drive a different mechanism from the same model.

### Switching modes live

`msks egress mode <ws> <mode> [--allow SPEC]...` (or `m` in the
decider TUI, from any of its screens) moves a workspace between the three
postures without recreating it. The row's mode and allowlist change at once. A running
workspace then swaps its whole per-VM table in one nft
transaction (the same maneuver the interceptor's arm/disarm
uses), carrying the consent elements (verdict pins,
resolver-learned allows) across. Established connections
survive the switch, and an attached `msks ssh` session stays
up. A
stopped workspace builds the new posture at its next start.

The switch keeps three invariants:

- **The queue never dangles.** Entering `interactive` binds
  the workspace's NFQUEUE consumer before the chain references
  the queue. Leaving it installs the queue-less table first
  and unbinds the consumer after. An unbound queue drops, so
  the order is the whole rule.
- **Held requests answer.** A switch away from `interactive`
  fail-closes every hold the workspace still owns — the held SYNs
  answer deny at once rather than waiting out the timeout against
  a queue that no longer exists.
- **Verdicts carry.** The consent rows survive every switch,
  and enforcement follows the current mode: an `allow forever`
  granted under `interactive` keeps acting under `static`
  (name-keyed verdicts at the resolver, address-keyed ones
  re-pinned into the fresh table's sets). `static` after an
  `interactive` session is therefore the frozen consent set:
  everything approved so far, and nothing else. The pins rebuild from the rows across a direct
  gated→gated switch and at every gated entry; a round-trip
  through `allow` re-pins the `forever` rows, while a timed
  address-keyed allow re-prompts or re-learns on the guest's next
  resolution.

The resolver flips with the chain in the same step, so a switch to
`static` starts answering off-list names with NXDOMAIN mid-session
(the guest's cached answers run out on their own TTLs). A switch to `static` with nothing effectively allowed (an
empty allowlist and no in-effect allowed verdict) is refused
with a message naming the escape: `--allow` entries, or
`--offline` to run the switch. That posture answers every name
NXDOMAIN, an offline workspace. The daemon logs each switch and broadcasts a
refreshed `egress.rules` frame, so an attached TUI repaints its
header without reconnecting.

## The probe endpoint

The daemon serves one deliberately ordinary HTTPS service every
workspace can reach: **`https://secretprobe.msks/`** — an emulated
external endpoint protected by HTTP basic auth, username `msks`,
password `msks`. It exists so an operator can verify from inside a workspace
that **secret interception works end to end**. The `ok` page
it answers with can only exist when the whole chain ran:
redirect, TLS splice, leaf mint, and the sentinel-to-secret
swap.

The daemon seeds the machinery itself at first-time startup
(the daemon-wide placeholder named `probe`, whose secret is the
base64 of the whole credential `bXNrczptc2tz`), and the
interception CA is installed at first boot. Nothing is left to
do by hand. The
verification:

```bash
# on the host: read the seeded sentinel (token-gated API)
curl -s -H "Authorization: Bearer $TOKEN" https://msksd/api/v1/probe

# inside the workspace: the sentinel rides as the raw Basic blob
curl -H "Authorization: Basic <sentinel>" https://secretprobe.msks/
ok
```

A request that reaches the service has already passed the nft
redirect (guest TCP 80/443 to the per-tap interceptor
listener), the splice tier (the SNI matched an allowlisted
entry), and the daemon CA's leaf mint. The swap then rewrote
the raw blob into a well-formed credential. The interceptor's upstream dial was repointed to the
service's own per-tap port (`probe_port`, default 8644) and
verified against the service CA the daemon mints. The guest's
443 is the redirect's port; the service listener binds the
setting's unprivileged number, which a host wildcard cannot
squat. The service
validated the fixed pair. A broken link answers
distinctly: a TLS failure names the splice or the CA, a `401`
names the swap (or a wrong credential), a timeout names the
redirect.

The endpoint performs no other action: it reads no workspace
state, accepts no commands, and returns no other data. The credentials are a fixed probe value shared by every
deployment, and the endpoint gates nothing beyond its own `ok`
page. That is also why the seeded row's sentinel is served
back over the token-gated API (`GET /api/v1/probe`): a token
holder already owns the daemon.

Two reachability notes. The probe answers only while the workspace is **armed**. A
workspace scoped against daemon-wide placeholders (no covering
row of its own) never arms, so its direct dial to
`secretprobe.msks:443` finds nothing admitted and times out.
That posture is the documented way to opt a workspace out of
interception entirely. The guest's trust in its own interception CA is installed at
first boot on both images. Debian guests link it into the
system trust store. Both guests export `SSL_CERT_FILE` (a
bundle built from the platform's own roots with the CA
appended; the variable replaces the default lookup, so it must
carry both) and `NODE_EXTRA_CA_CERTS` for node-based clients,
plus `GIT_SSL_CAINFO` naming that same bundle for git. NixOS's
`/etc/profile` reads no
`profile.d`, so its sanctioned `/etc/profile.local` hook sources
the same exports — which also brings the LLM proxy's environment
alive there. On the NixOS image the trust then completes the NixOS way. A
background oneshot (`msks-interceptor-ca`, ordered after
cloud-init, one rebuild per fresh certificate, with a marker
no-oping later boots) runs `nixos-rebuild switch`, whose
evaluation reads the staged certificate into `security.pki`.
The system bundle itself therefore carries the CA, and every
client that reads the system bundle trusts it. node keeps its
own exported variable, and the exports remain the first-boot
bridge that keeps the workspace usable while that rebuild
runs.

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
interface). A Debian deployment host runs
`msks-setup-debian-host` — [the Installation
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
first (the [Egress consent](#egress-consent) section covers
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

### Cryptographic agility (a future FIPS posture)

The image pins login policy — who may authenticate, and how — and
leaves algorithm selection to the platform. No cipher, MAC,
key-exchange, or host-key algorithm lists appear in the guest's sshd
configuration or the daemon's own settings, so an OpenSSH build whose
crypto library enforces a FIPS module applies its restrictions by
itself, without msks-side config surgery. The guest's libraries are
Debian's own (OpenSSL 3), the line that carries a certified provider
when one exists. The algorithm choices in play are FIPS-approvable from the
start. The operator's own key is the identity, so the key type
is the operator's choice: any type the guest's sshd
authenticates, at whatever curve or size it carries. First
boot generates the full `ssh-keygen -A` host-key set, whose
RSA and ECDSA members are the keys a FIPS-mode sshd serves.
All of it persists across stop/start on the overlay.
Issue records the constraint that keeps it that way: every
crypto choice stays a setting or a platform default, never a pinned
list.

## Backend support

Egress is a local-backend feature; the consent API itself is
enforcement-agnostic (see “Local-only semantics” under [Egress
consent](#egress-consent)), so a future backend can drive a
different mechanism from the same model. The no-NIC posture works
everywhere: a workspace created with `"egress": false` presents no
NIC whatever runs it.

## Lifecycle

`start` arms the tap and services before the VMM boots (the VMM
opens the tap by name); `stop`, `kill`, and `delete` tear the tap,
its chain, and its services down again. A failed boot unwinds its
own plumbing — no half-open path outlives a failed workspace start.
