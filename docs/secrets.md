# Placeholder secrets

A workspace never holds a real secret. It holds a **placeholder** —
a sentinel token (`mskssec1_…`) the operator mints once and pastes
into the workspace — and the daemon swaps the sentinel for the real
secret on the wire, in flight, only toward the destinations the
mint named. The real secret lives in msksd's **secret store** and in
the daemon's memory, nowhere else.

This chapter is about the store and the mint/revoke/renew commands.
The in-flight swap itself is the **egress interceptor** — the next
section.

## The interceptor (the swap on the wire)

A workspace with at least one live placeholder — minted, unrevoked,
and unexpired — is **armed**: the daemon redirects that workspace's
TCP flows toward ports 80 and 443 into an in-process HTTPS/HTTP
proxy, and the swap happens there. The proxy is
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
  event on the events channel. A revoked or expired sentinel
  passes through the same way; its placeholder is gone, so there
  is nothing to swap and nothing to report.
- **QUIC stays down while armed.** The workspace's UDP flows toward
  port 443 are dropped, so browsers fall back to the TCP flow the
  redirect owns — nothing routes around the interceptor.

While a workspace is armed, the redirect takes its web egress
(TCP 80 and 443) **before** the egress-consent gates see it: the
interceptor's own allowlist is what gates web traffic during that
time, and a static or interactive workspace with a live
placeholder reaches any web destination through the splice tier.
The consent modes keep gating every other port. A connection the
guest opened **before** the first placeholder armed keeps flowing
unintercepted until it ends — the kernel's connection tracking
outlives the rule swap — which places it in the same accepted
blind-spot class as the splice tier.

Each swap publishes a `secret.swap` event; mint, revoke, and expiry
publish their own (`secret.mint`, `secret.revoke`, `secret.expiry`).
All five ride the events websocket beside the workspace lifecycle
events.

Fail-closed on the swap path: a secret the store cannot serve, or a
row the database cannot read, answers the request locally with a
502 instead of forwarding it — a request that still carries the
sentinel never leaves the host.

The interceptor presents each connection a leaf certificate signed
by the workspace's own CA, and each workspace's CA is its own: one
workspace's leaves never validate under another's. A workspace
whose guest already trusts its CA — the first-boot seeding is
[#200]'s composition — sees HTTPS toward allowlisted destinations
validate normally from the first placeholder. Until #200 lands,
the CA is minted at the first arm: a workspace minted a
placeholder against sees HTTPS toward allowlisted destinations
fail visibly until its next boot (the recorded decision — the
trust-store entry rides the seed, and a running guest cannot be
retro-trusted). Arming and disarming swap the workspace's firewall
table in one nft transaction — the redirect, the widened input
rule for the listener, and the QUIC drop appear and disappear
together, with no window in between where the table is absent.

The listeners bind one shared port on each armed tap address
(`interceptor_port`, default 8643 — see the
[key reference](config.md)). Upstream connections are verified
against the platform's trust store. The daemon pins no cipher
list of its own anywhere: the contexts it builds take mitmproxy's
curated default list, and any override arrives as a setting with
the certification effort, never as code. One functional limit
rides that posture: the client-facing leg negotiates HTTP/1.1
only (no ALPN callback is installed), so guests with HTTP/2
fall back — the swap, the splice, and the detection behave the
same over HTTP/1.1.

## The mint flow

```console
$ op read 'op://Vault/github/credential' \
    | msks secret mint myws --name github_api \
        --dest api.github.com --secret-file -
minted myws/github_api for api.github.com
sentinel (shown once): mskssec1_9Jm3...kQ
```

- `--secret-file` takes a path, or `-` to read the secret from a
  pipe. The secret is never accepted as a command-line argument:
  arguments land in process lists and shell history.
- `--dest` repeats: an exact host (`api.github.com`) binds the swap
  to that host; a suffix (`.github.com`) binds it to every host
  under that domain. A placeholder carries one workspace, one
  allowlist, one secret; the same secret in N workspaces is N mints.
- `--ttl SECONDS` gives the placeholder a lifetime; without it the
  placeholder lives until revoked. `msks secret renew` extends a
  lifetime in place — the sentinel never changes and nothing is
  re-delivered.
- The sentinel is printed once, at mint. Every later view (list,
  audit, logs) omits it; a lost sentinel is re-minted, not recalled.

`msks secret revoke myws --name github_api` takes effect on the
next request. `msks secret check` verifies the configured store
answers writes before the first mint — a typo'd setting fails there,
loudly.

## Where the real secret lives

The store is [SecretSpec](https://secretspec.dev) driven through its
CLI (`secretspec`, named by `secret_store_cli`). The **provider is a
setting** — where the bytes live is a configuration choice, not
code, so moving from a local file to at-rest encryption or a managed
vault changes no msks code:

| Provider | Setting value    | Where the secret lives                                                                                                | The credential the daemon holds                                                                   |
| -------- | ---------------- | --------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------- |
| `file`   | `file` (default) | One file per secret under the store root (`<state_dir>/secrets`), filesystem permissions only                         | none                                                                                              |
| `age`    | `age`            | One age-encrypted file under the store root; the identity lives outside it, so a copy of the root alone is ciphertext | an age identity file beside the state dir                                                         |
| `awssm`  | `awssm`          | AWS Secrets Manager, under the `secretspec/msks/` name prefix                                                         | the AWS SDK credential chain — on AWS hosts an instance profile role, no stored credential at all |
| `bws`    | `bws`            | A Bitwarden Secrets Manager project                                                                                   | a machine-account access token (`BWS_ACCESS_TOKEN`) in the daemon's environment                   |

Plain Bitwarden Password Manager (`bw`) keeps its session only
while an operator holds it unlocked, so it serves interactive use;
an unattended daemon uses `bws`.

Provider credentials are deliberately **not msksd settings**: the
`secretspec` subprocess inherits the daemon's environment, so each
provider resolves its own chain (the AWS SDK chain, `BWS_ACCESS_TOKEN`).
Nothing doubles through msksd's configuration.

### Worked examples

`file` — the default; one file per secret under the state dir:

```yaml
secret_store_provider: file
# secret_store_root defaults to <state_dir>/secrets
```

`age` — at-rest encryption of the store (protects copies of the
state dir; the identity file itself is the daemon's one bootstrap
secret):

```yaml
secret_store_provider: age
secret_store_age_identity: /var/lib/msksd/secrets/age.key
# generate once: age-keygen -o /var/lib/msksd/secrets/age.key
```

`awssm` — secrets in AWS; an instance profile role scopes to the
`secretspec/msks/*` names and no credential is stored on the host:

```yaml
secret_store_provider: awssm
secret_store_region: eu-west-1
# optional: secret_store_profile and secret_store_prefix
```

`bws` — a Bitwarden Secrets Manager project, token via the daemon's
environment:

```yaml
secret_store_provider: bws
secret_store_project: 5f8a-...-project-uuid
# systemd unit: Environment=BWS_ACCESS_TOKEN=...
```

## At-rest encryption is a provider choice

The `file` provider stores plaintext bytes behind filesystem
permissions — the same posture as every other secret-bearing
artifact in the state dir (the database, the ssh identities). When
that is not enough, `age` encrypts the store in place and `awssm` /
`bws` move the bytes off the daemon host entirely. All three are
settings; msksd never picks an algorithm itself.

## The audit trail

Mint, revoke, and expiry append a row to the daemon database:
the placeholder's workspace, name, destination allowlist, and a
timestamp. The secret value and the sentinel appear nowhere in the
audit — the value is not msks's to log, and the sentinel is never
shown past its single mint-time print. `msks secret ls` lists
placeholders; expiry fires from the status watcher's periodic sweep
and publishes a `secret.expiry` event on the events channel.
