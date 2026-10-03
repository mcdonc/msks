# Placeholder secrets

A workspace never holds a real secret. It holds a **placeholder** —
a sentinel token the operator mints once and pastes into the
workspace — and the daemon swaps the sentinel for the real secret
on the wire, in flight, only toward the destinations the mint
named. The real secret is minted by the daemon itself (#423): it
lives in msksd's **secret store** — one age-encrypted agefile —
and in the daemon's memory, and it reaches the operator exactly
once, in the mint reply, to be pasted into the external service.

A mint covers **workspaces** (#339). With no target it is the
**daemon-wide placeholder**: one row, one sentinel, valid on
every workspace's tap — an operator running several workspaces
against one daemon mints the credential once and distributes the
same sentinel everywhere. A `--workspace` target (one name or a
comma list, repeatable) scopes the row to exactly those
workspaces: one row, one sentinel, one coverage set. The
sentinel's prefix names its reach from the string alone —
`mskssec1_…` scopes to its row's workspaces, `mskssec2_…`
reaches every accepting workspace on the daemon.

This chapter is about the store and the mint/revoke/renew commands.
The in-flight swap itself is the **egress interceptor** — the next
section.

## The interceptor (the swap on the wire)

A workspace covered by at least one live placeholder — minted,
unrevoked, and unexpired; its own scoped placeholders, or any
daemon-wide placeholder while the workspace accepts one — is
**armed**: the daemon redirects that workspace's TCP flows toward
ports 80 and 443 into an in-process HTTPS/HTTP proxy, and the swap
happens there. The proxy is
[mitmproxy](https://mitmproxy.org), embedded in msksd — one process
serving every armed workspace, one listener per workspace's tap,
so a guest that spoofs another workspace's source address still
lands on its own tap's placeholders.

What each request gets:

- **The swap.** The sentinel is exchanged for the real secret in
  every request header and every query-string pair — duplicate
  keys included — when the destination matches the placeholder's
  allowlist (exact host or label-anchored suffix, as minted).
  Request **bodies** are not scanned: the sentinel rides headers
  and URLs, and an operator pasting it into a body payload sends
  it nowhere the placeholder covers. The origin sees the real
  secret; the workspace never holds it. On HTTPS the connection's
  TLS handshake (its SNI) names the destination and the request's
  Host header is pinned to that same name — a guest cannot claim
  one name in the handshake and route by another in the header
  (domain fronting). On plain HTTP the request is dialed by the
  Host header's name, for the same binding.
- **The splice.** An HTTPS destination no placeholder of this
  workspace covers is relayed undecrypted: the origin's real
  certificate reaches the workspace, pinned clients keep working,
  and the sentinel rides raw. Detection of off-allowlist sightings
  is limited to decrypted flows — this blind spot is recorded on
  the [#194 decision doc](spikes/194-egress-interceptor.md).
- **The sighting.** A sentinel seen toward a destination its own
  allowlist misses — while another placeholder decrypts the flow —
  passes through unrewritten and publishes a `secret.sighting`
  event on the events channel. The same is true for a sentinel a
  workspace holds no covering row for: a daemon-wide sentinel
  used from a workspace scoped against it, or another workspace's
  scoped sentinel, reads as an off-allowlist sighting — the
  cross-workspace leak is announced, never serviced. A revoked or
  expired sentinel passes through the same way; its placeholder
  is gone, so there is nothing to swap and nothing to report.
- **QUIC stays down while armed.** The workspace's UDP flows toward
  port 443 are dropped, so browsers fall back to the TCP flow the
  redirect owns — nothing routes around the interceptor.

While a workspace is armed, the redirect takes its web egress
(TCP 80 and 443) **before** the egress-consent gates see it: the
interceptor's own allowlist is what gates web traffic during that
time, and a static or interactive workspace with a live
placeholder reaches any web destination through the splice tier.
The consent modes keep gating every other port, and the verdict
pins and resolver-learned allows they hold in the kernel carry
across the interceptor's arm/disarm table swaps — re-pinned with
their remaining lifetimes in the same transaction that swaps the
table.

Each swap publishes a `secret.swap` event; mint, revoke, and expiry
publish their own (`secret.mint`, `secret.revoke`, `secret.expiry`).
All five ride the events websocket beside the workspace lifecycle
events, each naming the placeholder's row id, its workspace and
name, and a timestamp — the row id is the durable handle, because
the row itself retires on revoke and expiry. A swap or sighting
also names the destination the wire saw; a mint names the
allowlist it was minted with; revoke and expiry carry the
identity alone. The `msks tui` secrets page's audit view (`e`
from the page) shows every workspace's events newest first,
daemon-wide: the newest hundred recorded mints, revokes, and
expiries replay onto it when the view opens, and swaps and
sightings stream in live, with the off-allowlist sighting
highlighted as the exfil signal and a header line stating the
detection boundary above.
Kind and workspace filters narrow the view — a daemon-wide row's
events cover every workspace, a scoped row's its members. Each
lifecycle event carries its audit row's id on both the live
stream and the replay, so one fact lands once on the screen
however it arrives; a replayed row keeps no placeholder row-id
suffix (the audit record holds the identity, not the
placeholder's id). An empty view says so — the screen never
renders the header line alone.

Fail-closed on the swap path: a secret the store cannot serve, or a
row the database cannot read, answers the request locally with a
502 instead of forwarding it — a request that still carries the
sentinel never leaves the host.

The interceptor presents each connection a leaf certificate signed
by the workspace's own CA, and each workspace's CA is its own: one
workspace's leaves never validate under another's. A workspace
whose guest already trusts its CA sees HTTPS toward allowlisted
destinations validate normally from the first placeholder. That
trust arrives with [#200]'s first-boot seeding; until it lands,
nothing installs the CA into a guest — a workspace minted a
placeholder against does not validate HTTPS toward allowlisted
destinations at all, across reboots, until #200 ships and the
workspace is recreated (or the operator installs the CA by hand —
`.devenv/state/msksd/vms/<id>/interceptor-ca.crt` into
`/usr/local/share/ca-certificates/` + `update-ca-certificates`;
the interactive recipe on the issue does exactly that). The splice
leg needs none of this: it presents the origin's own certificate. Arming and disarming swap the workspace's firewall
table in one nft transaction — the redirect, the widened input
rule for the listener, and the QUIC drop appear and disappear
together, with no window in between where the table is absent.

The listeners bind one shared port on each armed tap address
(`interceptor_port`, default 8643 — see the
[key reference](config.md)). Upstream connections are verified
against the CA bundle mitmproxy ships (certifi's Mozilla list) —
not the platform's own store; pointing verification at the
platform bundle is a setting the certification effort brings,
never a code change. The daemon pins no cipher list of its own
anywhere: the contexts it builds take mitmproxy's curated default
list, and any override arrives as a setting with the certification
effort, never as code. One functional limit rides that posture:
the client-facing leg negotiates HTTP/1.1 only (no ALPN callback
is installed), so guests with HTTP/2 fall back — the swap, the
splice, and the detection behave the same over HTTP/1.1.

Two lifecycle facts worth knowing: a workspace whose interceptor
cannot arm (its listener cannot bind) fails its boot with the
named cause — an armed workspace is the whole point of a
placeholder, and half-armed is worse than refused. And a
connection the guest opened before the first placeholder armed
keeps flowing unintercepted until it ends (the kernel's connection
tracking outlives the rule swap); an established _redirected_
flow, symmetrically, breaks when the last placeholder goes — the
listener is gone while its NAT entry lingers. Both sit in the
same accepted blind-spot class as the splice tier — as does
guest-to-guest web traffic while armed: the redirect takes every
tap 80/443 flow, including one workspace dialing another's
address, and the interceptor dials the destination from the host,
where the per-VM forward gates do not apply.

## The seeded probe placeholder (#424)

A fresh daemon seeds one placeholder itself at first-time
startup: the daemon-wide row named **`probe`**, allowlisting
`secretprobe.msks`, carrying the fixed credential blob
`bXNrczptc2tz` as its secret. It exists so an operator can
verify interception from inside a workspace with zero minting
(the [probe endpoint](networking.md#the-probe-endpoint-424)); its
sentinel never needs to be treated as a secret, because the
credential it swaps to is public and gates nothing beyond the
probe's own `ok` page.

Read the seeded sentinel back over the token-gated API —
`GET /api/v1/probe` answers the row id, the sentinel, and the
endpoint's recipe — and revoke it like any other placeholder when
you do not want it. The seed runs exactly when the placeholder
table is empty: a daemon that already holds rows seeds nothing,
and a revoked probe row stays gone while the operator's rows hold
the table. The seed's mint rides the audit trail like any other.

## The mint flow

```console
$ msks secret mint --name github_api --dest api.github.com
minted */github_api for api.github.com
value (shown once): msksval1_9Jm3...kQ
sentinel (shown once): mskssec2_9Jm3...kQ
```

- The mint **generates the value** (#423): the daemon creates a
  strong random value (the platform CSPRNG — `msksval1_` plus 32
  URL-safe bytes), stores it in the agefile, and answers with it
  exactly once, beside the sentinel. Paste the value into the
  external service then; every later view omits it, and a lost
  value is re-minted, not recalled. The operator never supplies
  a value, and the client — on another machine — receives the
  one-time reply over the existing token-authenticated TLS API;
  the agefile and the age identity stay on the daemon's host, and
  the exposure point is the client's terminal, the same exposure
  the sentinel has always had.
- The mint above carries no workspace target: it is the
  **daemon-wide** mint — one row, one `mskssec2_` sentinel, valid
  on every workspace's tap toward the minted destinations. A
  workspace created later is covered from its first boot;
  coverage acts as policy, not as a snapshot of the workspace set
  at mint time.
- `--workspace` scopes the mint: `--workspace myws` (exactly
  today's single-workspace placeholder), or a comma list
  (`--workspace ci,deploy`) for several — one row, one
  `mskssec1_` sentinel, one coverage set either way. A mint
  produces a single sentinel whether it targets every workspace
  or a set of one or several; the same label can live on the
  daemon-wide row and on scoped rows beside it, one row per label
  per coverage set.
- `--dest` repeats: an exact host (`api.github.com`) binds the swap
  to that host; a suffix (`.github.com`) binds it to every host
  under that domain. A placeholder carries one coverage set, one
  allowlist, one secret — per-workspace destination scoping stays
  available by minting a scoped placeholder instead.
- `--ttl SECONDS` gives the placeholder a lifetime; without it the
  placeholder lives until revoked. `msks secret renew` extends a
  lifetime in place — the sentinel never changes and nothing is
  re-delivered.
- The value and the sentinel are printed once, at mint. Every
  later view (list, audit, logs) omits them; a lost value or
  sentinel is re-minted, not recalled.

The `msks tui` secrets page mints too (#393): `c` opens the form
— name, repeatable destinations, coverage (the daemon-wide row,
or a multi-select of the workspaces the tree's own list offers),
and the lifetime (`unbounded` by default, an hour to thirty days
beside it). There is no value field — the daemon mints the value
(#423). The submit checks the store (`msks secret check`'s
endpoint) before it mints, and a refusal — a store that cannot
answer writes, a name collision on the chosen coverage set —
names itself on the form with the fields kept for a retry. A
successful mint answers with the one-time panel: the value, the
sentinel, its reach decoded from its prefix, OSC 52 clipboard
copies for each (over ssh included, where the terminal honors
it), and the rule that the display ends with the panel — a lost
value or sentinel is re-minted, never recalled. Closing the panel
clears its text.

`msks secret revoke --name github_api` retires the daemon-wide row
of that label everywhere at once; `msks secret revoke
--workspace myws --name github_api` retires the scoped row whose
coverage is exactly that set — the targeting mirrors the mint's,
and `msks secret renew` selects its row the same way. `msks
secret ls` lists every row with its coverage (`*` for the
daemon-wide row, the workspace list scoped). `msks secret check`
verifies the configured store answers writes before the first
mint — a typo'd setting fails there, loudly.

A workspace can exempt itself from daemon-wide placeholders
(#339): `msks create --secret-coverage scoped` (or
`msks secret coverage myws scoped` later) means only placeholders
minted directly at that workspace arm it. A daemon-wide sentinel
used from a scoped workspace publishes an off-allowlist sighting
and swaps nothing; every new workspace accepts daemon-wide
coverage unless its own setting says scoped, and flipping the
setting back to `all` arms it with any live daemon-wide row.

## Where the real secret lives

The store is [SecretSpec](https://secretspec.dev) driven through its
CLI (`secretspec`, named by `secret_store_cli`): one
age-encrypted **agefile** — `<store root>/secrets.age`, default
`<state_dir>/secrets/secrets.age` — holding every value msks
itself minted. Values exist in plaintext only in the daemon's
memory, the one-time mint reply, and the `secretspec` child's
pipe; at rest they are ciphertext.

The age identity is the daemon's own (#423): minted the first
time a store operation needs it — a plaintext age-keygen X25519
file, 0600, named by `secret_store_age_identity` (default
`<store root>/age.key`) — the same self-minting pattern the
per-workspace ssh identities use (#138). Plaintext, because the
daemon must decrypt unattended; a copy of the store root without
the identity file cannot read the agefile. Back the identity up
beside the state dir (the file carries its recipient on a comment
line, so a backup can be checked without decrypting anything); a
lost identity takes the values with it — re-mint them.

msksd never decrypts anything itself: every store operation
spawns `secretspec get/set/delete --provider
"age://<root>/secrets.age?identity=<path>"` and the agefile is
decrypted inside that child process. Decryption runs once per ref
per daemon lifetime — the first tap-time `get`, with the
in-memory value cache serving every later rewrite — and the store
root (`0700`) holds the agefile, the identity, and the generated
manifest (`secretspec.toml`) beside them, readable only by the
daemon's user.

### Worked example

```yaml
secret_store_root: "" # default <state_dir>/secrets
secret_store_age_identity:
  "" # default <store root>/age.key,
  # minted when absent
```

## At-rest encryption

The agefile is the store: every value msks mints lands in it
encrypted, and no configuration choice changes that posture — the
`age` provider is the one store. The ssh identities, the
database, and the token hashes keep their own house postures; the
agefile names the secrets' one.

Moving a vault to another daemon host means moving the store root
whole — agefile and identity together — while the daemon is
stopped; the root's manifest regenerates from the database, and
the values answer the moved identity.

## The audit trail

Mint, revoke, and expiry append a row to the daemon database:
the placeholder's coverage (the daemon-wide row, or the workspace
list), name, destination allowlist, and a timestamp (`msks
secret ls` lists placeholders; the audit view is
`/api/v1/secrets/audit`). Expiry fires from the status watcher's
periodic sweep, which publishes a `secret.expiry` event and
re-evaluates the covered workspaces' redirects in the same pass —
the last placeholder's retirement stands the interception down,
and one revoke or expiry retires the whole row everywhere at
once. The secret value and the sentinel appear nowhere in the
audit or the events: the value answers its single mint-time
reply alone, and the sentinel is never shown past its own.
