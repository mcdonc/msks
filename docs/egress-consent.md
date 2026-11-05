# Egress consent

The consent layer decides, per workspace, whether each new
outbound connection is allowed. It uses the per-VM chain the
[guest networking](networking.md) chapter describes.

The TUI includes a consent decider UI, which allows the user to approve or deny
an egress request. Another decider is present in the `msks-term-popup` UI. A
different CLI version of decision is implemented via `msks egress`.

![The egress consent page: three held requests queued over the
in-effect verdicts](screens/consent-holds.svg)

## The consent mode

A workspace picks its consent mode at create time (for example, via `msks
create --egress-mode`). The command `msks egress mode` switches it afterwards
(see [switching modes live](#switching-modes-live)). Every decider offers a
way to switch modes.

The mode decides where each new outbound connection is decided. There are
three available modes:

- **`allow` (the create default).** New requests pass. The
  daemon's resolver records each off-allowlist name the guest
  looks up.
- **`static`.** The workspace's allowlist is the policy. Allowlisted names
  resolve through the daemon's resolver, and their addresses are pinned as
  allowed in the kernel chain for the answer's TTL. For example, `apt update`
  against an allowlist of `.deb.debian.org` might complete with no prompt and
  no hold. A name not on the allowlist answers NXDOMAIN and the denial is
  recorded as an audit row. Address-range entries
  (eg. `10.0.0.0/8`, `203.0.113.7:5432`) accept in the chain directly.
- **`interactive`.** The first packet of every new flow queues
  to the workspace's own queue inside the host kernel.
  msksd holds it until a decider answers.

## Deciders

A decider is any authenticated client that announced itself on the events
websocket, like the egress consent page of the TUI shown in the image above
or `msks egress watch <workspace>`. New holds wait for a decider only while
at least one is connected: the connection is the liveness signal. A hold
created before the last decider left still runs its timeout.

Without a decider, a connect against an off-allowlist destination fails
fast. The prompt shows the DNS name (e.g. `api.anthropic.com:443`). The
resolver is the one the DHCP lease hands the guest. It remembers which
name resolved to which address. A destination given by address prompts with
the address itself, and its verdict keys on the address.

Here's a visualisation of the egress networking configuration:

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

The decision table matches the resolver gate's precedence: a
standing deny first, then the static allowlist (name specs
against the gated name, address specs against the original
destination), then a standing allow, then the mode.

Under `static`, the denial is recorded and the answer is a local
HTTP 403. Under `interactive`, the flow holds through the consent engine, and
the same decider endpoints answer the same rows the kernel queue writes. An
allow completes the handshake.

A deny answers the 403 locally, and nothing forwards. The 403's body says what
happened: consent denied the destination; a decision is already pending (the
flow lost the duplicate race); no decider answered (none registered, or the
request expired undecided); or the request ended without a decision (the gate
failed, the prompt cap refused the hold, or the workspace vanished, stopped, or
switched mode mid-hold).

Verdicts and durations are shared: one allow in any decider covers the
destination's web flows and its other ports alike.

`msks egress revoke` therefore stops a destination's next connection and its
first plain request on a fresh connection. Connections that already carry
consent (established TLS, or a plain connection that already answered that
destination) keep it until they close. The kernel path additionally kills
established flows with their conntrack entries. The interceptor's connections
have no conntrack entry to kill.

## Verdicts and durations

Deciders record `allow`/`deny` with a duration: `once` (this connection only; a
reconnect re-prompts), `5m`, `15m`, `tilrestart` (until the msks VM stops; the
default), `forever` (the workspace's lifetime, replayed at every boot). An
allow pins the destination in the kernel for its duration and covers the
_name_.

A decider revoking a verdict undoes an in-effect verdict at once. The
destination's live connections die with their conntrack entries (on a shared
address that includes the co-resident's), and new connections gate again.

Every row (request, verdict, expiry, revocation) is written to the consent
table with its provenance. Rows are pruned past
`MSKSD_EGRESS_CONSENT_RETENTION_DAYS` and the per-workspace
`MSKSD_EGRESS_CONSENT_ROW_CAP`. With `MSKSD_AUDIT_HMAC_KEY` set, each row also
carries an HMAC-SHA256 tag over its columns.

## Switching modes live

`msks egress mode <ws> <mode> [--allow SPEC]...` (or `m` in the decider TUI or
`msks-term-popup`) moves a workspace between the three postures without
recreating it. The row's mode and allowlist change at once. A running workspace
swaps its whole per-VM table in one nft transaction (the same maneuver the
interceptor's arm/disarm uses). The consent elements (verdict pins,
resolver-learned allows) carry across. Established connections survive the
switch, and an attached `msks ssh` session stays up. A stopped workspace builds
the new posture at its next start.

The switch keeps three invariants:

- **The queue never dangles.** Entering `interactive` binds the
  workspace's NFQUEUE consumer before the chain references the
  queue. Leaving it installs the queue-less table first and
  unbinds the consumer after. An unbound queue drops, so the
  order is the whole rule.
- **Held requests answer.** A switch away from `interactive`
  fail-closes every hold the workspace still owns: the held
  SYNs answer deny at once, rather than waiting out the timeout
  against a queue that no longer exists.
- **Verdicts carry.** The consent rows survive every switch,
  and enforcement follows the current mode: an `allow forever`
  granted under `interactive` keeps acting under `static`. Name-keyed
  verdicts act at the resolver, and address-keyed ones are re-pinned into
  the fresh table's sets. `static` after an
  `interactive` session is therefore the frozen consent set:
  everything approved so far, and nothing else. The pins
  rebuild from the rows across a direct gated→gated switch and at every
  gated entry. A round-trip through `allow` re-pins the `forever` rows.
  A timed address-keyed allow re-prompts or re-learns on the guest's next
  resolution.

A switch to `static` starts answering off-list names with NXDOMAIN mid-session,
and the guest's cached answers run out on their own TTLs. A switch to `static`
with nothing effectively allowed (an empty allowlist and no in-effect allowed
verdict) is refused.
