# Guest networking

A workspace talks to the network only when it was created with
`egress: true` — and when it does, every piece of the path lives in
the daemon, where the guest cannot reach it.

```bash
curl -X POST .../api/v1/workspaces \
  -d '{"id": "ws1", "egress": true}'        # or: msks create ws1 --egress
```

This chapter covers the plumbing — the NIC, the tap, DHCP, DNS,
and NAT — and the consent layer that gates it: the
[Egress consent](#egress-consent-69) section describes holding the
first packet of each new connection for an allow/deny decision
over the decider channel, built on the same per-VM chain.

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
recorded on the workspace row at first attach — so stop/start cycles,
daemon restarts, and even digest collisions between workspace ids
keep the same address.

msksd is the only DHCP server and the only resolver the guest ever
sees: the DHCP offer names the tap as gateway and as DNS server, and
a small forwarder on the tap answers port 53 by relaying to the
host's own upstream (`MSKSD_EGRESS_DNS_UPSTREAM`, or the first
nameserver in the host's `/etc/resolv.conf`). The resolver
speaks UDP; TCP/53 has no listener, so a query needing TCP fallback
(DNSSEC validation, very large RRsets) fails fast. The forwarder
answers queries from its own guest only; anything else arriving on
the tap is dropped unread, so a spoofed-source datagram cannot turn
the daemon into a reflection amplifier. #69 grows the naming
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
- **Input.** The guest may reach a fixed set of ports on the host
  through its tap: DHCP (67), the resolver (53), the interceptor's
  listener while armed (#199), and the LLM proxy port when one is
  configured (#259). Everything else
  from the tap drops before the host's own services — the API
  listener among them — so guest root cannot port-scan the
  host.

What the chain deliberately permits depends on the workspace's
consent mode (next section): in `allow` mode the _destination_ of a
guest-initiated connection is unconstrained — any host reachable
through the host uplink is reachable; in `static` and
`interactive` modes every new flow passes a destination gate first.

## Egress consent (#69)

A workspace picks its consent posture at create time
(`msks create --egress-mode`), and `msks egress mode` switches it
afterwards (#280) — see [switching modes
live](#switching-modes-live-280) below. The posture decides where
each new outbound connection is decided:

- **`allow` (the create default).** New flows pass. The daemon's
  resolver records each off-allowlist name the guest looks up, as an
  audit row, and that is the whole of it — the #52 behavior plus the
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
- **`interactive`.** The first packet of every new flow queues to
  the workspace's own NFQUEUE number inside the host kernel
  and msksd holds it until a decider answers:

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

  A decider is any authenticated client that announced itself on
  the events websocket (`msks egress watch <workspace>`): new holds
  wait for a decider only while at least one is connected (the
  connection itself is the decider's liveness — a hold created
  before the last decider left still runs its timeout). Without a
  decider an off-list connect fails fast — no prompt, no hang. The prompt names
  the DNS name (`api.anthropic.com:443`), because the resolver is
  the one the DHCP lease hands the guest and it remembers which
  name resolved to which address; a destination given by address (a
  Postgres host) prompts with the address itself and keys its
  verdict on the address.

**Verdicts and durations.** `msks egress decide` records
`allow`/`deny` with a duration — `once` (this connection only; a
reconnect re-prompts), `5m`, `15m`, `tilrestart` (until the VM
stops; the default), `forever` (the workspace's lifetime — replayed
at every boot). An allow pins the destination in the kernel for its
duration and covers the _name_, so a CDN-rotated address of an
allowed host resolves and passes without re-prompting. A deny
answers the SYN and its retransmits with a TCP RST — the guest's
`connect()` fails immediately instead of hanging on the kernel's
retransmit timer. `msks egress revoke` undoes an in-effect verdict
at once: the pinned rules clear, the destination's live connections
die with their conntrack entries, and new connections gate again.
Every row — request, verdict, expiry, revocation — lands in the
consent table with its provenance, pruned past
`MSKSD_EGRESS_CONSENT_RETENTION_DAYS` and the per-workspace
`MSKSD_EGRESS_CONSENT_ROW_CAP`; with `MSKSD_AUDIT_HMAC_KEY` set
each row also carries an HMAC-SHA256 tag over its columns.

**The naming layer owns DNS.** Ports 53 and 853 toward anywhere but
the daemon's own resolver drop in the per-VM chain, in every mode,
so the guest cannot route around the resolver that names its
prompts. Plain-IP egress to a DoH endpoint on 443 is enforced like
any other flow — in `static`/`interactive` it meets the same
destination gate.

**Fail-closed, by construction.** The queue rule carries no bypass:
a queue with no listener (msksd down, the consumer not yet bound)
and a full queue both _drop_ — the opposite default of a
sidecar-in-netns design, which inherits the namespace's plumbing
only while its process lives. Each workspace owns its queue number
(derived from its address-pool slice), so one workspace flooding
SYNs delays only its own verdicts. And the whole mechanism — taps,
chains, queues, the resolver, the verdict table — lives in the
host's kernel and msksd's userspace: guest root sees none of
it (no syscall, no `/proc`, no signal target).

**Local-only semantics.** Per-flow holds are the local backend's
enforcement; the consent API itself (requests, verdicts, durations,
revocation, audit) is enforcement-agnostic, so a future backend can
drive a different mechanism from the same model.

### Switching modes live (#280)

`msks egress mode <ws> <mode> [--allow SPEC]...` (or `m` in the
decider TUI, from any of its screens) moves a workspace between the three
postures without recreating it. The row's mode and allowlist
change at once; a running workspace then swaps its whole per-VM
table in one nft transaction — the same maneuver the interceptor's
arm/disarm uses (#199) — carrying the consent elements (verdict
pins, resolver-learned allows) across, so established connections
survive the switch and an attached `msks ssh` session stays up. A
stopped workspace builds the new posture at its next start.

The switch keeps three invariants:

- **The queue never dangles.** Entering `interactive` binds the
  workspace's NFQUEUE consumer before the chain references the
  queue; leaving it installs the queue-less table first and unbinds
  the consumer after — an unbound queue drops, so the order is the
  whole rule.
- **Held requests answer.** A switch away from `interactive`
  fail-closes every hold the workspace still owns — the held SYNs
  answer deny at once rather than waiting out the timeout against
  a queue that no longer exists.
- **Verdicts carry.** The consent rows survive every switch, and
  enforcement follows the current mode: an `allow forever` granted
  under `interactive` keeps acting under `static` (name-keyed
  verdicts at the resolver, address-keyed ones re-pinned into the
  fresh table's sets), so `static` after an `interactive` session
  is the frozen consent set — everything approved so far, and
  nothing else. The pins rebuild from the rows across a direct
  gated→gated switch and at every gated entry; a round-trip
  through `allow` re-pins the `forever` rows, while a timed
  address-keyed allow re-prompts or re-learns on the guest's next
  resolution.

The resolver flips with the chain in the same step, so a switch to
`static` starts answering off-list names with NXDOMAIN mid-session
(the guest's cached answers run out on their own TTLs). A switch
to `static` with nothing effectively allowed — an empty allowlist
and no in-effect allowed verdict — is refused with a message
naming the escape (`--allow` entries, or `--offline` to run the
switch): that posture answers every name NXDOMAIN, an offline
workspace. The daemon logs each switch and broadcasts a
refreshed `egress.rules` frame, so an attached TUI repaints its
header without reconnecting.

## What runs where

**On the host:** the tap, its address, the per-VM
nftables table, the NAT masquerade on the uplink, the DHCP service,
and the DNS forwarder — all owned by msksd, which runs as a
dedicated service user holding exactly two ambient capabilities:
`CAP_NET_ADMIN` (taps and their addresses, the nftables tables,
and — because ambient capabilities survive `exec` — the workspace
VMM opening its tap) and `CAP_NET_BIND_SERVICE` (the DHCP and DNS
listeners, UDP 67 and 53). Nothing in the daemon's process tree
runs as uid 0; `/dev/kvm` reaches the VMM through the `kvm` group.
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
`CAP_NET_BIND_SERVICE` — the dev host's wrapper and the NixOS
module grant exactly those two — and a kernel that routes: the
host ships
`net.ipv4.ip_forward=1` as a boot-time `sysctl.d` setting, the
daemon verifies it at startup, and a daemon that reads `0` refuses
every egress workspace with a cause naming the sysctl key. The deployment host satisfies the routing kernel the daemon
verifies: the NixOS module in `nix/module.nix` sets
`net.ipv4.ip_forward=1`, names its own uplink through settings, and
grants the daemon the two capabilities — a `nixos-rebuild switch`
is the whole host setup. The dev host's wrapper carries the same
grant for `msks-dev` (its uplink default names this dev host's
interface). An
operator who sets `MSKSD_EGRESS_ENABLED=false` arms nothing, and
every egress workspace then refuses to boot with the cause named.
The dev-host egress smoke (`MSKSD_TEST_EGRESS=1`) runs as root:
ambient capabilities cannot be granted to an arbitrary shell, so
the harness — which creates real taps, loads nftables rules, and
binds ports 67 and 53 — runs as full root and sets `ip_forward`
itself for the duration of the run. That is the test's constraint,
not the server's: the daemon needs only the two capabilities and an
already-routing kernel.

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
`known_hosts` entry you recorded on first login keeps matching. The
entry lives under the workspace's immutable id (#246), so a workspace
recreated under the same name is a new id with a fresh cache: it
trusts its own first-boot host keys instead of refusing the first
instance's.
A workspace without egress carries the same image unchanged: its
forward is refused at the API with close code 4501 before any dial,
and its console is the vsock one.

### The minted workspace identity

A workspace created without a client-supplied key — a direct API
create, or `msks create --daemon-mint` — carries an ssh
identity msksd minted at create (issue #111): a keypair stored with the workspace's state, whose
public half the first boot plants into `authorized_keys` for root
and the workspace's login user — through the same cidata seed
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
`MSKSD_SSH_KEY_TYPE`): Ed25519 by default, `ecdsa` (P-256) and
`rsa` (3072-bit) selectable. The identity is minted at create. With
the identity materialized, the usual client shapes work over the
forward:

```bash
msks key myws --out ~/.cache/msks/myws.key
msks forward myws 22 --local 2201 &
ssh -i ~/.cache/msks/myws.key -p 2201 root@127.0.0.1
rsync -e 'ssh -i ~/.cache/msks/myws.key -p 2201' \
    -av ./site/ root@127.0.0.1:/root/site/
```

The same login works as the workspace's login user —
`ssh -i ... -p 2201 alice@127.0.0.1` (the name `msks create
--user` recorded, #248; the image's `msks` account for a
workspace created before #248) — whose home rides the persistent
`/home` volume.

`msks ssh` (#112) is the zero-step form of the same login: it boots
the workspace if needed, serves the minted identity from a
transient in-process ssh-agent (the private half never becomes a
file), and runs ssh with the forward as its ProxyCommand — as the
workspace's login user by default, with `-l root` as the recovery
login (see the CLI chapter's `msks ssh` section). When the command
itself booted the workspace, it holds the connection back until
the guest accepts the workspace key (#168): a probe login retries
behind the first boot's identity seeding, so the first attempt
lands as a session instead of `Permission denied (publickey)`.

The same login works as the workspace's login user —
`ssh -i ... -p 2201 alice@127.0.0.1` — whose home rides the
persistent `/home` volume.

`msks rsync` (#190) is the copy form of the same seam: it runs
the host rsync over the forward with the identity staged in
memory and the copy opening its own forward
(`msks rsync devbox -- -aP src/ :src/`, the login user's home;
`root@:/root/x` for root-owned paths) — see the CLI chapter's
`msks rsync` section.

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
over the authenticated API). Those paths are the client cache
root's defaults and stay where the block writes them: OpenSSH
reads its own config, so `MSKSC_CACHE_DIR` (the variable the
devenv shell presets for its command forms, #251) moves only the
`msks ssh`/`msks rsync` state — a relocated cache takes the alias
block's paths with it only when you edit the block to match.
`msks ssh` is the command form that
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
the operator's own agent into the workspace — as does
`msks ssh -- -A` (#174), which rewrites the request onto the same
socket without the alias block.

### The client-minted default (no escrow)

`msks create` mints the workspace's ssh keypair on the client by
default (issue #121): the client generates it locally, sends the
public half with the create request, and keeps the private half —
the daemon stores the public line and seeds it into the guest's
`authorized_keys` exactly as it seeds its own minted half, and its
database never holds a private half for the workspace (the API's
key fetch answers `private_key: null`). The daemon validates the
supplied line by shape — a label in the algorithm-name charset,
fields, base64 body, and a blob whose embedded algorithm name
agrees with its label, at any key type (#132) — and annotates it
with its
own provenance comment (`msks-client:<id>`, beside the minted
mode's `msksd:<id>`). `--daemon-mint` opts back into the
daemon-minted mode above.

The private half is written mode 0600 under the client data root
after the create succeeds — `~/.local/share/msks/<id>/identity`,
honoring `XDG_DATA_HOME` or `MSKSC_DATA_DIR` — and `msks ssh` reads it from there when
the API serves the public half alone (checking the stored half
against the served public line, so a stale copy fails as one named
line, not ssh's opaque `Permission denied`). Losing the file loses ssh
to that workspace and the console with it — unless the operator's
ssh-agent holds the same key (`SSH_AUTH_SOCK`), which the console
consults next; the alias workflow can point `IdentityFile` at a
copy kept anywhere the operator likes. The data root, not the cache, holds the key on purpose:
cache sweeps leave it alone. Deleting the workspace leaves the
stored half behind, like its `known_hosts` — remove the
per-workspace directory under the data root when you want the
material gone. The key type is the client's choice at create
(`--key-type`: `ed25519` by default, `ecdsa`, `rsa`), independent
of the daemon's `MSKSD_SSH_KEY_TYPE` setting.

This is the ssh half of the client-held-secrets posture: the
daemon host keeps every capability the console and forward grant,
but no longer holds a private key that opens the workspace's ssh. The console challenge-response half is #123.

### The console challenge

Every workspace seeded with an identity carries a second trust
store: `/etc/msks/console.allowed_signers`, planted beside
`authorized_keys` by the same first-boot script (issue #123). Its
presence turns the vsock console into a challenge-response channel:

1. After the daemon's prelude, the guest helper emits
   `AUTH CHALLENGE <nonce>` — 32 fresh bytes per connection, so a
   captured exchange cannot be replayed.
2. The client answers with an SSHSIG signature over the nonce in the
   `msks-console` namespace.
3. The guest verifies with its own ssh-keygen against the trust
   store, principal-bound to the workspace id, and execs the shell
   only on success. Anything else — silence, a wrong key, a
   signature for another purpose — draws one refusal line and a
   closed session.

The daemon relays both lines and can answer for neither: it sees a
public half and a signature, never the private half a client-held
key keeps. ssh and console share the keypair, so the creating client
signs transparently (`msks console` resolves the key the way
`msks ssh` does: the daemon-mint escrow, the client data root, or
the operator's ssh-agent — a hardware key works, and msks never
reads a half the agent holds).

A guest without the trust store — an image or workspace seeded
before this change — serves no challenge and keeps the earlier
behavior; the extension is opt-in per workspace by what its seed
planted.

A refusal that arrives with no challenge served names a broken
trust store: the seed created the file but no identity line landed
in it. The workspace key still opens sshd, so the recovery is to
ssh in and restore the line — the same public key that
`authorized_keys` carries, with the workspace id as the principal
— or to re-create the workspace.

The signature covers the nonce alone, so one signature authorizes
exactly one session: the daemon always brokers every console
connection, and a hostile daemon could relay its own session's
challenge to a concurrently-connecting client — within the model
that declared the daemon trusted to connect clients at all. The
host-root attacker the challenge answers is the passive one: it
holds the workspace's public half, the database, and every relayed
byte, but cannot produce the signature a client-held half writes.
A host-root attacker watching a live session still sees the tty.

### An operator-supplied key

`msks create --pubkey FILE` (issue #132) builds the workspace
around a public key the operator already owns — the key that
`~/.ssh/id_ed25519.pub` names, a hardware token's key, any
well-formed OpenSSH line. The supplied line travels to the daemon
at its own key type: keys the machine mints stay limited to the
FIPS-approvable types, while a supplied key is accepted at any type
— the guest's sshd, the platform's own, is the authority on which
keys it will authenticate. The daemon re-annotates the line with
its provenance comment and seeds it like any other identity; its
database holds the public half only.

The private half never leaves the operator's custody: nothing is
written client-side, and login uses the operator's own key — `ssh
-i` through a forward, or the `Host msks-*` alias with `IdentityFile`
pointing at the operator's key file — the private half already
exists wherever the operator keeps it, so the client fetches and
stores nothing. `msks ssh` on such a workspace exits with a line
naming that recovery instead of pointing at a file it never wrote.

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

- `msks ssh my-workspace -- -A`, the one-command login with
  forwarding, forwards your agent too (#174): the command rewrites
  the request onto the socket `SSH_AUTH_SOCK` names, the same agent
  the alias form above forwards — no ssh-config block needed. The
  plain alias (`ssh -A msks-devbox`) and the forward port directly
  (`ssh -A -i ~/.cache/msks/msks-devbox.key -p 2201 msks@127.0.0.1`)
  work as always.
- With the alias's ControlMaster, the agent arrives only on the
  connection that creates the master. If you connected earlier
  without `-A`, close the master first — `ssh -O exit msks-devbox`
  — then reconnect with `-A`, or wait out the ControlPersist
  window.

What the network allows depends on the workspace's consent mode:
`allow` reaches any destination beyond the host without a grant —
remotes, package mirrors, any host reachable through the uplink —
while `static` and `interactive` gate each destination first (the
[Egress consent](#egress-consent-69) section covers the semantics).
Guest-initiated connections aimed at the host itself stay
dropped in every mode (only DHCP and the resolver answer it). The
end-to-end proofs are the `test_local_egress_git_out` smoke
(`MSKSD_TEST_EGRESS=1` locally, and part of CI's KVM workflow) —
it installs git in the guest over a plain `allow` egress path and
pushes a commit, over a test-widened input pin, since the
host itself stays unreachable from the guest by design, using
only a key that arrived through the forward as a forwarded agent —
and the `test_local_egress_consent_*` smokes, which drive a hold,
a verdict, and the fail-closed denials through the real kernel
path.

### Cryptographic agility (a future FIPS posture)

The image pins login policy — who may authenticate, and how — and
leaves algorithm selection to the platform. No cipher, MAC,
key-exchange, or host-key algorithm lists appear in the guest's sshd
configuration or the daemon's own settings, so an OpenSSH build whose
crypto library enforces a FIPS module applies its restrictions by
itself, without msks-side config surgery. The guest's libraries are
Debian's own (OpenSSL 3), the line that carries a certified provider
when one exists. The algorithm choices in play are FIPS-approvable
from the start: identities default to Ed25519 (#138 — FIPS 186-5
approves EdDSA, and ssh clients restricted to the common
`ssh-ed25519,ssh-rsa` set accept it out of the box), with ECDSA
P-256 and RSA as explicit `--key-type` / `MSKSD_SSH_KEY_TYPE`
choices,
and first boot generates the full `ssh-keygen -A` host-key set,
whose RSA and ECDSA members are the keys a FIPS-mode sshd serves —
all persisting across stop/start on the overlay.
Issue #115 records the constraint that keeps it that way: every
crypto choice stays a setting or a platform default, never a pinned
list.

## Backend support

Egress is a local-backend feature; the consent API itself is
enforcement-agnostic (see “Local-only semantics” under [Egress
consent](#egress-consent-69)), so a future backend can drive a
different mechanism from the same model. The no-NIC posture works
everywhere: a workspace created with `"egress": false` presents no
NIC whatever runs it.

## Lifecycle

`start` arms the tap and services before the VMM boots (the VMM
opens the tap by name); `stop`, `kill`, and `delete` tear the tap,
its chain, and its services down again. A failed boot unwinds its
own plumbing — no half-open path outlives a failed workspace start.
