# Placeholder secrets

A workspace never holds a real secret. It holds a **placeholder** —
a sentinel token the operator mints once and pastes into the
workspace — and the daemon swaps the sentinel for the real secret
on the wire, in flight, only toward the destinations the mint
named. The real secret is the operator's own value, supplied at
mint (a file or stdin on the CLI, a masked field on the TUI
form); it lives in msksd's **secret store** — the database's
per-recipient age-encrypted blobs — and in the daemon's unlock
cache for its window, and it is never echoed back: the mint reply
carries the sentinel alone.

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
(TCP 80 and 443) **before** the kernel's consent queue, so the
interceptor runs the consent gate for exactly those flows (#452):
every web connection is decided on the name the wire carries — the
TLS handshake's SNI on HTTPS, the Host header on plain HTTP —
bound to the address the naming memory holds for the connection
(a name claiming an address it never resolved to keys by the
address, so it borrows nothing). A `static` workspace's
allowlist gates its web traffic by that name; an `interactive`
workspace's web connections hold through the same decider
endpoints, rows, durations, and session memory every other port
uses — `msks egress watch` and the TUI answer them like any other
hold. An allowed web connection takes the splice or swap path
above; a denied one is answered locally with an HTTP 403 naming
consent — the handshake completes against the daemon's
interception CA, the
request refuses, and nothing forwards (a client that pins the
origin's real certificate fails its handshake instead; refused
closed either way). See
[networking](networking.md#egress-consent-69) for the gate's
precedence and its per-connection boundaries. The consent modes
keep gating every other port at the kernel queue as before, and
the verdict pins and resolver-learned allows they hold in the
kernel carry across the
interceptor's arm/disarm table swaps — re-pinned with their
remaining lifetimes in the same transaction that swaps the table.

Each swap publishes a `secret.swap` event; mint, revoke, expiry,
and unlock publish their own (`secret.mint`, `secret.revoke`,
`secret.expiry`, `secret.unlock`). All six ride the events
websocket beside the workspace lifecycle events, each naming the
placeholder's row id, its workspace and name, and a timestamp —
the row id is the durable handle, because the row itself retires
on revoke and expiry. A swap or sighting also names the
destination the wire saw; a mint names the allowlist it was
minted with; revoke, expiry, and unlock carry the identity
alone. The `msks tui` secrets page's audit view (`e` from the
page) shows every workspace's events newest first, daemon-wide:
the newest hundred recorded mints, revokes, expiries, and
unlocks replay onto it when the view opens, and swaps and
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
by the daemon's one interception CA (#485) — every workspace's
guest trusts the same certificate its seed staged (#424: the
identity seed writes `/etc/msks/interceptor-ca.crt` at first boot,
and the trust-store exports and the NixOS fold register it). A
workspace whose guest already trusts that CA sees HTTPS toward
allowlisted destinations validate normally from the first
placeholder. When the daemon's certificate changes under a
workspace that already booted — the move from a pre-#485
per-workspace CA, or a future CA rotation — the next boot renews
the seed: it is rebuilt carrying the daemon's certificate with an
instance-id suffix keyed to it, cloud-init re-provisions (the seed
script is idempotent, and the operator's payload re-executes with
it), and the fold re-registers the fresh certificate. The splice
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
the table — or mint it back by hand: `msks secret mint --name
probe --dest secretprobe.msks --secret-file -` with the fixed
credential blob `bXNrczptc2tz` (the value the probe endpoint
itself serves). The seed's mint rides the audit trail like any
other.

Under the unlock window (#514) the probe row is armed for the
window at every boot and again on every `GET /api/v1/probe` read:
the credential is public, so the daemon re-arms its own row
rather than leaving the probe locked after the window expires —
the same rationale that lets the endpoint serve the credential
at all. Between the window's expiry and the next read (or boot,
or an operator's unlock with the public value) the probe's swap
answers the locked refusal like any other row.

## The mint flow

```console
$ op read 'op://Vault/github/credential' \
    | msks secret mint --name github_api \
        --dest api.github.com --secret-file -
minted */github_api for api.github.com
sentinel: mskssec2_9Jm3...kQ
```

- The value is the operator's own secret: a file's contents, or
  stdin for `--secret-file -` (the example pipes it from a
  password manager, so it never touches disk). It is
  whitespace-stripped at both ends, never accepted as a
  command-line argument (argv lands in process lists and shell
  history), stripped at both ends (a UTF-8 BOM drops too) and
  stored as ciphertext blobs, and never echoed —
  the mint reply carries the sentinel, not the value. The client and the
  daemon may sit on different machines: the value rides the
  token-authenticated TLS API. The mint stores **ciphertext
  only** (#514): one age-encrypted blob per covering workspace's
  identity key, and the plaintext rides the daemon's unlock
  cache until its window ends (below). A covering workspace
  whose key is outside the store's capable set (`ssh-ed25519`,
  `ssh-rsa`) — or that carries no identity key at all — refuses
  the mint with the workspace named.
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
- The CLI prints the sentinel at mint. The listing and the
  audit omit it (`msks secret ls` never shows one), and the
  daemon serves the row's sentinel back on demand over the
  token-authenticated API — the `msks tui` secrets page's
  Enter-on-a-row panel (#440) — so a sentinel that scrolled
  away reads back from the daemon; the operator's value alone
  is never echoed back.

The `msks tui` secrets page mints too (#393): `c` opens the form
— name, repeatable destinations, coverage (the daemon-wide row,
or a multi-select of the workspaces the tree's own list offers),
the lifetime (`unbounded` by default, an hour to thirty days
beside it), and the value: a masked field the operator types or
pastes into — the form takes the secret's
bytes directly. The submit checks the store (`msks secret
check`'s endpoint) before it mints, and a refusal — a store that
cannot answer writes, a name collision on the chosen coverage
set, a blank value — names itself on the form with the fields
kept for a retry. A successful mint answers with the sentinel's
panel: the sentinel opens masked — its prefix names the reach,
the body renders as bullets — with a Show action that reveals
and hides it again, an OSC 52 clipboard copy (over ssh included,
where the terminal honors it — the copy needs no reveal), and
the rule that the display ends with the panel.
Enter (or a click) on a listing row opens the same panel again
(#440): the page fetches the row's sentinel from the daemon and
shows it, so the sentinel reads back on demand — the panel is
one surface for the row, opened at mint and after. Closing the
panel clears its text.

`msks secret revoke --name github_api` retires the daemon-wide row
of that label everywhere at once; `msks secret revoke
--workspace myws --name github_api` retires the scoped row whose
coverage is exactly that set — the targeting mirrors the mint's,
and `msks secret renew` selects its row the same way. `msks
secret ls` lists every row with its coverage (`*` for the
daemon-wide row, the workspace list scoped).

A workspace can exempt itself from daemon-wide placeholders
(#339): `msks create --secret-coverage scoped` (or
`msks secret coverage myws scoped` later) means only placeholders
minted directly at that workspace arm it. A daemon-wide sentinel
used from a scoped workspace publishes an off-allowlist sighting
and swaps nothing; every new workspace accepts daemon-wide
coverage unless its own setting says scoped, and flipping the
setting back to `all` arms it with any live daemon-wide row.

## The unlock window (#514)

The daemon holds no plaintext of its own: a mint leaves only the
per-recipient ciphertext blobs, and the plaintext returns through
an operator's unlock — the client (#515) decrypts the blobs with
the workspace identity's private half and posts the values back
over the token-authenticated API (`POST /api/v1/secrets/unlock`,
after `GET /api/v1/secrets/pending` serves the ciphertext and
opens the session window the unlock requires).

### The client's half (#515)

The unlock reads the identity key file the client already uses
for ssh — `identity_file` (the client config,
`~/.config/msks/msks.yaml`) or `MSKSC_IDENTITY_FILE` — and
decrypts only the blobs that key opens: possession of the
private key is the authority for a value, proven by the unwrap
itself. An operator without the key receives ciphertext, uploads
nothing, and the daemon stays cold for those rows; two operators
keyed to different workspaces each unlock their own.

An agent-held key cannot unlock: ssh-agent exposes signing, and
the unwrap needs the private key material directly, so the
unlock reads the file where it lives (ssh login still resolves
through the agent, #490). A passphrase-encrypted key file
prompts once per session — the passphrase loads the identity and
every row then decrypts through it.

Three surfaces run the flow:

- **The TUI at start.** The tree's mount offers the unlock
  against the daemon's locked rows: a warm cache stays silent, a
  cold one with the key present unlocks itself, and the
  passphrase prompt (when the file is encrypted) is the modal
  the session opens with. The secrets page's `u` action runs
  the same flow on demand — a window that lapsed mid-session
  heals the moment it is seen — and the page's STATE column
  (`locked` / `open`) and `msks secret ls`'s state column
  carry the lock state row by row.
- **`msks secret unlock`** — the scripting surface: fetch
  pending, decrypt, upload, and print one line per unlocked row
  plus the rows the key could not open — rows whose ciphertext
  failed to open name the re-mint they need, rows with no stanza
  for this key are another operator's and stay ciphertext. The
  command without a configured identity names `identity_file`
  and exits nonzero.
- **A cold refusal is a named condition.** The daemon's
  refusals surface verbatim — the locked swap error (423), and
  on a zero-window daemon the `422` that answers an unlock while
  no operator websocket stands. The websocket lives on a
  workspace page or the secrets page's audit view: open either
  (the TUI holds none at its root list) and the same flow
  works — never a retry loop.

The window is `MSKSD_SECRET_STORE_UNLOCK_TIMEOUT_S` (default
86400 seconds — one day), the `ssh-add -t` pattern. The timer resets on an
unlock POST itself — an operator action carrying plaintext — and
on nothing else: websocket traffic, keepalives, and scheduled
tasks never extend it. Zero holds plaintext only while at least
one operator websocket stays connected — the last disconnect
drops it, and an unlock over HTTP alone holds nothing past the
reply. Expiry flushes the cache, observable through the row
fetch and the pending list.

Until an unlock stands, the interceptor's swap path refuses a
carried sentinel with a named locked error — HTTP 423,
`msks: locked: no operator session within the unlock window` —
and the request never reaches the origin: the sentinel never
passes through as a value. An unlock also re-encrypts the row's
blobs to its current recipient set, so a coverage edit or a
workspace key rotation heals at the next operator session.

## Where the real secret lives

A mint stores **ciphertext blobs in the daemon's database**
(#514): one age file per covering workspace identity key, wrapped
to that workspace's ssh public half — the same file format the
reference `age` tool writes. The daemon holds no key that opens
them: only the workspace identity's private half (kept on the
operator's client machine, #486) unwraps a blob, which is the
whole design — the client decrypts, the daemon swaps. Values
exist in plaintext only in the operator's memory, the
token-authenticated request, and the daemon's unlock cache for
the window.

The per-recipient blobs are the store. The workspace identity
keys decide who can read: back up the client machine's identity
files alongside the daemon's database — a database copy alone
carries ciphertext nobody can open, and a lost identity file
means re-minting the values.

### Worked example

```yaml
# the unlock window: how long unlocked plaintext lives in the
# daemon after the unlock POST that supplied it
secret_store_unlock_timeout_s: 86400.0
```

The store itself needs no configuration: the blobs live in the
daemon's database beside the placeholder rows, and the only
secret-bearing setting is the window above.

## At-rest encryption

Every minted value is ciphertext at rest — a per-recipient age
blob in the database (#514) — and no configuration choice
changes that posture. The ssh identities, the database, and the
token hashes keep their own house postures; the blobs name the
secrets' one, and the blob's recipient list is exactly the row's
coverage.

Moving a daemon to another host means moving its database (the
blobs ride the placeholder rows) while the daemon is stopped.

## The audit trail

Mint, revoke, expiry, and unlock append a row to the daemon
database:
the placeholder's coverage (the daemon-wide row, or the workspace
list), name, destination allowlist, and a timestamp (`msks
secret ls` lists placeholders; the audit view is
`/api/v1/secrets/audit`). Expiry fires from the status watcher's
periodic sweep, which publishes a `secret.expiry` event and
re-evaluates the covered workspaces' redirects in the same pass —
the last placeholder's retirement stands the interception down,
and one revoke or expiry retires the whole row everywhere at
once. The secret value and the sentinel appear nowhere in the
audit or the events: the value rides its mint request alone and
is never echoed, and the sentinel appears only in the mint reply
and the on-demand row fetch — never in the audit or the event
stream.
