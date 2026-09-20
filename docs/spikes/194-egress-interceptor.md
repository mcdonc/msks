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
  grants nothing without the nft redirect, so a workspace that carries
  its CA but has no placeholders is safe.

The candidate "agent-channel import" mechanism is dropped: the seed
needs nothing new.

### 2. mitmproxy gates — all pass, with two posture findings

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
  when it finds a user-provided `ssl_conn`. Four integration facts the
  harness established (each cost a debugging round — record them):
  1. The interceptor addon must register **before** the default addons;
     hook dispatch follows registration order and the default context
     lands first otherwise.
  2. The hook must call `set_accept_state()` itself — the proxy layer
     never does.
  3. `cipher_list` accepts `None` (platform/OpenSSL defaults; the
     `set_cipher_list` call is skipped) but rejects an empty tuple — a
     hard OpenSSL error (`no cipher match`). mitmproxy's **own**
     contexts never take the `None` path: `_default_ciphers()` always
     returns its curated list, so `ciphers_*` options being unset means
     "mitmproxy's list", never "platform defaults". The addon's
     override path is the one place platform defaults are reachable.
  4. The harness dispatches by client `peername` address because the
     stand-ins share one listener; production keys on the accepting
     per-tap listener instead (#199). The dispatch lookup is the one
     piece of addon logic that does not carry over verbatim.
- **FIPS posture — finding, scoped**: contexts the interceptor builds
  itself run with platform defaults (`cipher_list=None`; the
  `set_cipher_list` call is skipped in `_create_ssl_context` — true by
  code path in `net/tls.py`, and the harness's connections run on that
  path). Contexts mitmproxy builds itself — including every
  proxy→upstream connection (`tls_start_server`) — carry the curated
  list, and no option reaches the platform-default path there.
  Disposition: the interceptor passes `None` on the client-facing side
  (posture holds where we build); the upstream side either accepts the
  curated list for v1 or overrides `tls_start_server` the same way —
  decided in #199, with any override a setting, never code (the same
  posture the ssh directives follow).
- **Key-type posture — finding**: `CertStore.from_store` mints an
  RSA-2048 CA (`create_ca` is RSA-only), so the harness's CAs are RSA
  where the #111/#138 posture defaults Ed25519. Handing the store an
  Ed25519 CA does not work either: `get_cert` → `dummy_cert` signs
  with SHA-256 (a hard `ValueError` with Ed25519) and builds the leaf
  from the CA's own public key. #200 therefore mints both the
  Ed25519 CA **and the per-connection leaves** in msks code,
  bypassing `CertStore` leaf minting entirely — recorded there as the
  requirement.
- **Splice tier**: `tls_clienthello` + `ignore_connection = True`
  relays undecrypted. The harness proves the guest-visible property:
  a client that trusts only the origin's real CA completes the
  connection, and the sentinel inside that flow arrives at the origin
  unchanged — detection of off-allowlist sightings stays limited to
  decrypted flows, exactly as the recorded detection decision on #194
  (parsed flows only) says. The harness re-demonstrated the blind spot:
  no audit event fires for the sentinel carried inside a spliced flow.
- **Performance** (single event loop, proxy and origin in one process,
  loopback; relative numbers are the signal):

  | path           | best of 3, 10 MiB | MiB/s | vs direct |
  | -------------- | ----------------- | ----- | --------- |
  | direct         | 0.10 s            | 102   | 1.00      |
  | spliced relay  | 0.12 s            | 83    | 0.81      |
  | MITM + rewrite | 0.15 s            | 65    | 0.63      |

  The MITM row carries the sentinel, so it measures the full
  decrypt + rewrite + re-encrypt path. Echo latencies (best of 3):
  direct 20 ms, spliced 27 ms, MITM 47 ms (first hit includes leaf
  minting).

  **Verdict against the issue's gate** ("holdback ≈ 0 on non-matching
  flows"): first-byte latency holds back ~7 ms on the splice tier
  against a 20 ms direct baseline; throughput on bulk transfers holds
  back ~19% through the splice. For the workspace traffic profile this
  feature serves — small API calls — the gate passes with wide margin
  (the 10 MiB body is far outside that profile). A deployment that
  pushes bulk transfers through armed workspaces pays the relay cost,
  and the mitigation is selective steering (redirect only flows toward
  destinations with placeholders) — noted as an option inside #199,
  not scheduled.

### 3–8. Already decided on the issue

Process topology (one master, per-tap dispatch), sentinel format
(`mskssec1_` + 32 bytes base64url), query-string rewrite (in), TLS
floors, lifetime (unbounded default, `--ttl` opt-in), allowlist grammar
(exact + label-anchored suffix), residual risks (accepted) — see the
issue for the recorded wording. The harness implements and confirms the
subset those decisions exercise: per-tap-style dispatch, the sentinel
swap (headers + query with duplicate keys preserved, per-placeholder
allowlist binding, exact and suffix matching), the splice tier, the
off-allowlist sighting with its audit event, revocation while still
armed, and the audit trail. Lifetime/TTL expiry, TLS-floor probes, and
sentinel validation remain in the implementation issues.

## Harness evidence summary

One devenv shell invocation (the uv-sync task strips mitmproxy from the
venv on every shell entry) and public DNS (the proxy resolves the
origin names itself, via `*.127.0.0.1.sslip.io`):

```bash
devenv shell -- bash -c \
  'uv pip install -q --python .devenv/state/venv/bin/python \
   mitmproxy && python docs/spikes/spike194.py'
```

Two loopback source addresses stand in for two workspaces; ports
19443/19444/19800 are hardcoded; the scratch dir (CA keys, mitmproxy
confdir, origin certs) prints at the end and is left in place for
inspection. The harness points mitmproxy's confdir at the scratch dir,
so nothing touches `~/.mitmproxy`. Public DNS is needed on both sides:
the proxy resolves the origin names itself (`*.127.0.0.1.sslip.io`
for the proxied legs), and curl resolves client-side for the direct
benchmark leg. Reproduced output:

1. ws-a request with the sentinel in `Authorization` and in
   `?api_key=` toward the allowlisted host → the origin sees the real
   secret in both places; the workspace never holds it.
2. ws-b's connection — matched by the suffix allowlist — receives a
   leaf from ws-b's own CA, validated by a client that trusts only that
   CA.
3. The same workspace toward a host outside every allowlist →
   undecrypted relay; the origin's real certificate reaches the client
   and the sentinel arrives unreplaced.
4. Off-allowlist sighting: the sentinel's entry exists but its own
   allowlist misses the host while a second placeholder covers it →
   decrypted, unrewritten, `off-allowlist-sighting` audit event; the
   echo shows the raw sentinel.
5. Revocation while a second placeholder still arms the workspace →
   the revoked sentinel passes through decrypted but unrewritten; the
   origin answers and the echo shows the raw sentinel (in production
   the upstream answers 401).
6. Audit trail: `swap` events per workspace address, `splice` events
   per connection, the sighting event.

## Follow-up issues filed from this doc

[#198](https://github.com/mcdonc/msks/issues/198) secret store + API +
CLI · [#199](https://github.com/mcdonc/msks/issues/199) interceptor
embedding · [#200](https://github.com/mcdonc/msks/issues/200) CA at
create + seed composition ·
[#201](https://github.com/mcdonc/msks/issues/201) audit events + TUI.
