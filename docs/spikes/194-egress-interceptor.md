# Spike #194: the egress interceptor — decision doc

Timeboxed spike, closed 2026-09-20. Every open decision on
[#194](https://github.com/mcdonc/msks/issues/194) is answered below with
evidence from a working harness (`spike194.py`, this directory) that ran
mitmproxy 12.2.3 in-process on the project's Python 3.14.7 venv.

## Answers to the open decisions

### 1. CA injection — decided: mint at create, deliver on the cidata seed

The channel already exists end to end. Every workspace boot consumes a
per-workspace `cidata` seed disk (#41's `user_data` channel) that the
daemon composes at create and cloud-init (NoCloud, the only datasource)
applies on first boot; the ssh identity rides the same channel (#111).

The interceptor's CA joins that composition: msksd mints a per-workspace
CA at **create** — beside the identity — writes it into the seed as a
`write_files` entry (`/usr/local/share/ca-certificates/msks-ws.crt` +
`update-ca-certificates`), and the guest trusts it from its first boot.
Consequences:

- Workspaces created after this feature never see the "HTTPS toward
  allowlisted destinations fails until reboot" wrinkle — the CA predates
  the first placeholder.
- A workspace created **before** this feature carries a seed without the
  CA. Rebuilding the seed while such a workspace is stopped is possible
  (the seed is a disk image the daemon owns); while it runs, minting
  keeps the recorded decision: succeed with a warning, HTTPS toward
  allowlisted destinations fails visibly until the next boot.
- The CA stays inert until interception arms: trust-store presence
  grants nothing without the nft redirect, so an armed-less workspace
  carrying its CA is safe.

The candidate "agent-channel import" mechanism is dropped: the seed
needs nothing new.

### 2. mitmproxy gates — all pass, with one posture finding

Ran on Python 3.14.7 with prebuilt wheels (`mitmproxy 12.2.3`,
`mitmproxy-rs 0.12.11`, `mitmproxy-linux 0.12.11`):

- **Dependency footprint**: 35 packages on top of the project's 44,
  including pyopenssl, tornado, flask, urwid, kaitaistruct, ldap3,
  aioquic, zstandard, and a `cryptography` bump 48 → 50 (still within
  the project floor, `>=44`). Acceptable inside the nix appliance; the
  runtime list for the appliance image grows by this set.
- **Per-workspace CA**: works, through the documented override point.
  The addon's `tls_start_client` hook builds its own
  `SSL.Connection` from a per-workspace `CertStore`
  (`certs.CertStore.from_store`) and the `tlsconfig` addon hands off
  when it finds a user-provided `ssl_conn`. Three integration facts the
  harness established (each cost a debugging round — record them):
  1. The interceptor addon must register **before** the default addons;
     hook dispatch follows registration order and the default context
     lands first otherwise.
  2. The hook must call `set_accept_state()` itself — the proxy layer
     never does.
  3. `cipher_list` must be non-empty; an empty list is a hard OpenSSL
     error (`no cipher match`), and mitmproxy has **no "platform
     defaults" path** — `_default_ciphers()` always returns its curated
     list. This is the FIPS finding below.
- **FIPS posture — deviation found**: mitmproxy sets a hardcoded cipher
  list on every client- and server-facing context (`addons/tlsconfig.py:
_default_ciphers`; the `ciphers_client`/`ciphers_server` options being
  unset does _not_ leave OpenSSL defaults in force, it selects
  mitmproxy's list). TLS floor defaults to TLS 1.2, floor and list are
  per-connection overridable via options. The project rule ("pin
  nothing; algorithm selection belongs to the platform") therefore does
  not hold automatically under mitmproxy. Disposition for the
  implementation issue: accept the curated list as the platform-behavior
  baseline for v1, keep `ciphers_client`/`ciphers_server` unset in msks
  config (the FIPS certification effort later sets them as a setting,
  never in code — the same posture the ssh directives follow).
- **Splice tier**: `tls_clienthello` + `ignore_connection = True`
  relays undecrypted. The harness proves the guest-visible property:
  a client that trusts only the origin's real CA completes the
  connection, and the sentinel inside that flow arrives at the origin
  unchanged — detection of off-allowlist sightings stays limited to
  decrypted flows exactly as decision A recorded (the harness
  inadvertently re-demonstrated the blind spot: no audit event fires
  for the sentinel carried inside a spliced flow).
- **Performance** (single event loop, proxy and origin in one process,
  loopback; relative numbers are the signal):

  | path           | best of 3, 10 MB | MB/s | vs direct |
  | -------------- | ---------------- | ---- | --------- |
  | direct         | 0.10 s           | 104  | 1.00      |
  | spliced relay  | 0.12 s           | 90   | 0.86      |
  | MITM + rewrite | 0.14 s           | 73   | 0.70      |

  Echo request latencies: 18 ms spliced, 30–90 ms MITM (first hit
  includes leaf minting). A workspace's real traffic is small API
  calls; both tiers clear that bar with wide margin.

### 3–8. Already decided on the issue

Process topology (one master, per-tap dispatch), sentinel format
(`mskssec1_` + 32 bytes base64url), query-string rewrite (in), TLS
floors, lifetime (unbounded default, `--ttl` opt-in), allowlist grammar
(exact + label-anchored suffix), residual risks (accepted) — see the
issue for the recorded wording; the harness implements and confirms
each.

## Harness evidence summary

`python docs/spikes/spike194.py` (devenv shell; installs mitmproxy into
the venv first) reproduces, against two loopback source addresses
standing in for two workspaces:

1. ws-a request with the sentinel in `Authorization` and in
   `?api_key=` toward the allowlisted host → the origin sees the real
   secret in both places; the workspace never holds it.
2. ws-b's connection receives a leaf from ws-b's own CA — validated by
   a client that trusts only that CA.
3. The same workspace toward a host outside every allowlist →
   undecrypted relay; the origin's real certificate reaches the client
   and the sentinel arrives unreplaced.
4. Mapping revoked → the next request toward the allowlisted host is
   relayed unreplaced (in production: the upstream answers 401; in the
   harness the origin is self-signed so the failure surfaces at TLS).
5. Audit trail: `swap` events per workspace address, `splice` events
   per connection.

## Follow-up issues to file from this doc

1. Secret store + mint/revoke/renew API and CLI (placeholder rows,
   TTL predicate, audit events).
2. Interceptor embedding: nft arm/disarm per active placeholder,
   mitmproxy in-process with the per-tap dispatch addon, sentinel
   rewrite (headers + query), splice tier.
3. CA at create + cidata seed composition (+ seed rebuild for stopped
   pre-existing workspaces; warning path for running ones).
4. Audit events into the event stream and the TUI.
