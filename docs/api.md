# The HTTP API

Everything the client does, the daemon does through one
versioned HTTP surface: `/api/v1` — HTTPS with a bearer token
over TCP, or a unix socket with peer-credential authentication
for same-host clients. The CLI and TUI chapters describe the
commands that wrap these endpoints; this chapter is the surface
itself, for scripts and integrations that speak to msksd
directly.

The machine-readable schema ships with the daemon: `GET
/openapi.json` answers an OpenAPI 3.1 document covering every
REST route below, served unauthenticated alongside the
interactive `/docs` — the schema names routes, never data. The
prose here owns the behavior; the schema owns the shapes.

## Authentication

Two listeners, one API.

**The TCP listener** serves HTTPS and WSS on the `host`/`port`
settings (`MSKSD_HOST`/`MSKSD_PORT`, default
`127.0.0.1:8660`). Every request carries a bearer token in the
`Authorization` header, and TLS is verified against the CA the
daemon mints at first
serve (`msks-ca.pem` in the state dir) — the same CA that signs
the daemon's serving certificate. The first token is seeded by
the `bootstrap_token` setting at first boot (the [Configuration
chapter](config.md) carries the setting and its environment
variable); after that, a token holder mints tokens through the
API itself (below). The dev daemon seeds its own first token
from a file the dev script writes — a dev-workflow artifact,
not a deployment's path.

**The unix socket** binds at `MSKSD_SOCKET` (default
`$XDG_RUNTIME_DIR/msks/msksd.sock`) and authenticates by peer
credentials: the daemon's own uid passes, and so does any
member of the socket file's group — the file is mode 0660, so
`chgrp` it to a shared group to admit that group's members
without a token or TLS. A daemon without a runtime dir (a
container, a hand-written unit) serves TCP alone until the
setting names a path. The socket is the same-host path — a
local script or the client's default candidate path — and the
TCP listener is the remote path. Neither weakens the other:
TCP without a token answers 401.

A missing, expired, or revoked token answers 401; the console,
forward, and events websockets take the same bearer token in
the handshake's `Authorization` header — never a URL
parameter, so the credential cannot land in a proxy's access
log — and close **4401** when it fails (4404 names an unknown
workspace on the console and forward).

## Enabling the TCP interface

The TCP listener serves loopback from the first start — the
dev daemon and the NixOS module both do. Reaching it from
another machine is three settings and one file:

1. **Bind beyond loopback.** Set `host` to the address the
   remote client reaches — a specific interface's address, or
   `0.0.0.0` for every interface. The setting is read at
   startup; a reload applies it at restart.
2. **Ship the CA to the client.** Copy `msks-ca.pem` from the
   daemon's state dir to the client machine and point the
   client at it — `MSKSC_CAFILE` for the msks client, or
   `--cacert` for curl. The daemon's certificate is signed by
   this CA alone; the system roots know nothing about it.
3. **Ship a token.** Mint one through the API itself (`POST
/api/v1/tokens`) — the first one comes from the
   `bootstrap_token` setting above — and hand it to the client:
   `MSKSC_TOKEN` for the msks client, or the `Authorization:
Bearer` header for anything else.

Then the first call, from the client machine:

```bash
export MSKSC_URL=https://msksd.internal:8660
export MSKSC_TOKEN=mskstok_...
export MSKSC_CAFILE=/etc/msks/msks-ca.pem
curl --cacert "$MSKSC_CAFILE" \
  -H "Authorization: Bearer $MSKSC_TOKEN" \
  "$MSKSC_URL/api/v1/health"
{"status": "ok", "version": "0.3.0", ...}
```

A deployment that wants the listener reachable only from the
same host keeps the loopback bind and uses the socket beside
it — the [Configuration chapter](config.md#the-api-listener)
carries both settings and the socket's tradeoffs.

## Health and defaults

- `GET /api/v1/health` — the daemon's version and the image it
  itself booted from (the `msksd.image=` command-line fact, for
  drift checking), unauthenticated by design: the route a load
  balancer can probe without a token.
- `GET /api/v1/create-defaults` — the sizes a create lands on
  when the request names none (`root_mib`, `home_mib`).
- `GET /api/v1/probe` — the secrets-interception probe row: the
  seeded placeholder's id, sentinel, host and port, username, and the
  fixed credential. The [egress consent
  chapter](egress-consent.md#the-probe-endpoint) owns the
  verification flow.

## Workspaces

The row's lifecycle is four verbs and a queue of actions:

```bash
# create: the name is the everyday reference; egress arms the
# network posture; image resolves against the catalog
curl -X POST "$MSKSC_URL/api/v1/workspaces" \
  -H "Authorization: Bearer $MSKSC_TOKEN" \
  -H 'content-type: application/json' \
  -d '{"name": "ws1", "egress": true}'
```

- `GET /api/v1/workspaces` — every row, with its image paths,
  status, and recorded facts. `GET …/workspaces/{id}` is one
  row; the id also accepts the name.
- `POST` creates (the [create defaults](#health-and-defaults)
  fill unnamed sizes); `DELETE` removes the row and its volumes.
  `PATCH`/`PUT` answer 405: a workspace is immutable after
  create, and sizes and topology move through `…/resize`.
- `POST …/start` and `…/stop` drive the VM; `…/reset` wipes the
  root overlay to its first-boot state; `…/resize` grows the
  root overlay, grows or shrinks the home volume, and moves
  the cpu and memory sizes; `…/ssh-key` reads the workspace's
  planted public identity;
  `…/secret-coverage` flips the daemon-wide placeholder posture;
  `POST …/mounts` sets the workspace's [share
  mounts](mounts.md) — the full replacement list, applied at the
  next start:

```bash
curl -X POST "$MSKSC_URL/api/v1/workspaces/ws1/mounts" \
  -H "Authorization: Bearer $MSKSC_TOKEN" \
  -H 'content-type: application/json' \
  -d '{"mounts": [{"host": "/srv/projects", "guest": "/mnt/projects"}]}'
```

The daemon validates the list against its #667 gates (a host
path outside the allowed roots, and one that does not resolve
under them, are named 400s) and answers with the refreshed row;
every row the daemon serves carries `mounts` and the derived
`mount_issues` — the skip state `msks ls` and the TUI render.
`msks mount`/`umount` and the TUI's editor ride this route.

- `GET`/`PUT …/home` export and import the home volume as a
  byte stream — the same stream `msks home import`/`export`
  ride.

The behaviors each verb drives are the CLI chapter's subjects:
[create](cli.md#msks-create) and its fields,
[resize](cli.md#msks-resize), and the home stream through
[msks home](cli.md#msks-home); the reset surface is
API-direct.

## Images

The catalog the workspaces boot from:

- `GET /api/v1/images` — every row: name, version, hash, the
  default designation, kernel facts, description, import
  time.
- `POST /api/v1/images` — import: a URL (the published
  channel's rows are URLs) or a local path the daemon reads.
  The compressed form (`tar.zst`) is accepted directly and
  hashes to the shipped bytes.
- `POST/DELETE …/images/default` — designate or clear the
  default image a create lands on when its request names none.
- `PATCH …/images/{digest}` — rename a row; `DELETE` removes
  one (refused while a workspace still boots it).

The import, in the form the channel's rows name:

```bash
curl -X POST "$MSKSC_URL/api/v1/images" \
  -H "Authorization: Bearer $MSKSC_TOKEN" \
  -H 'content-type: application/json' \
  -d '{"source": "https://…/debian.tar.zst"}'
```

The [images chapter](images.md) owns the archive layout, the
manifest, and the published channel.

## Secrets

The placeholder store, per the [secrets chapter](secrets.md):

```bash
# mint: daemon-wide with no workspaces target, the destinations
# the swap binds, the value itself
curl -X POST "$MSKSC_URL/api/v1/secrets" \
  -H "Authorization: Bearer $MSKSC_TOKEN" \
  -H 'content-type: application/json' \
  -d '{"name": "github_api", "dests": ["api.github.com"],
       "value": "ghp_..."}'
```

The mint answer carries the **sentinel**, never the value.
`GET /api/v1/secrets` lists rows with coverage and expiry;
`GET …/secrets/{id}` reads one row's sentinel back;
`POST …/renew` extends a lifetime; `DELETE` revokes. The audit
trail is `GET /api/v1/secrets/audit`; a locked store answers
`GET /api/v1/secrets/pending` and takes `POST
/api/v1/secrets/unlock` — the operator's passphrase window the
secrets chapter describes.

## Tokens

`GET`/`POST /api/v1/tokens`, and `DELETE
/api/v1/tokens/{token_id}` — a token holder mints, lists, and
revokes bearer tokens; the token surface is API-direct (the
client ships no token command). A revoked token's next request
answers 401.

```bash
curl -X POST "$MSKSC_URL/api/v1/tokens" \
  -H "Authorization: Bearer $MSKSC_TOKEN" \
  -H 'content-type: application/json' \
  -d '{"name": "ci"}'
```

## Storage

`GET /api/v1/storage` — the state disk's capacity report: the
volumes' sizes, the images' weight, the headroom. The
[storage chapter](storage.md) owns the accounting.

```bash
curl --cacert "$MSKSC_CAFILE" \
  -H "Authorization: Bearer $MSKSC_TOKEN" \
  "$MSKSC_URL/api/v1/storage"
```

## Egress consent

The consent surface the decider drives, per the [egress consent
chapter](egress-consent.md):

- `GET …/workspaces/{id}/egress` — the rules view: mode,
  allowlist, the in-effect verdicts.
- `PUT …/egress/policy` — the posture switch
  (`{"mode": "static", "allow_list": [".deb.debian.org"]}`),
  the endpoint behind `msks egress mode`.
- `GET …/egress/requests` — the held requests and their
  trail; `POST …/egress/requests/{rid}` records a verdict
  (allow/deny, with a duration); `DELETE` revokes an in-effect
  one.

## The websockets

Three streams ride the same listener and the same token:

- `wss://…/api/v1/events` — the event hub: workspace status
  frames, the egress consent frames (`egress.rules`,
  `egress.request`, `egress.resolved`), and the secret
  lifecycle events. A client that announces itself as a
  decider receives that workspace's pending holds —
  `msks egress watch` prints the frames as lines.
- `wss://…/api/v1/workspaces/{id}/console` — the guest's
  serial console as a bidirectional byte stream. Ctrl-]
  detaches in the client; the [console
  chapter](cli.md#msks-console) covers the interactive form.
- `wss://…/api/v1/workspaces/{id}/forward/{port}` — raw TCP
  bytes between the client and that port on the workspace's
  tap: the pipe `msks forward` and ssh's ProxyCommand ride. A
  workspace without egress is refused with close code 4501
  before any dial.
