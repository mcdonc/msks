# The agent reference card

This card is for a coding harness that operates msks from the host.
Given only this text, a harness can enumerate workspaces, create and
boot one, run commands inside it, sync files, consent to egress, and
interpret every status and exit code it sees. Every report command
below takes `--json`. Three commands stream instead: `msks ssh` and
`msks rsync` stream the child's own output, and `msks egress watch`
streams daemon lines. `msks agent-card` prints this card. The full
references are [the CLI chapter](cli.md) and [the HTTP API
chapter](api.md).

## Read state

```bash
msks ls              # every workspace: id, name, status, image
msks storage         # disk artifacts and sizes per workspace
msks image ls        # the image catalog
msks secret ls       # minted secrets and their coverage
```

## Lifecycle

The lifecycle verbs block until the transition settles. When a verb
exits, the status it reports is the workspace's status.

```bash
msks create ws --pubkey key.pub --image debian:13 --start
msks start ws
msks stop ws
msks clone ws fork
msks rm ws           # takes several workspaces, removed in order
```

`create` plants an ssh identity into the workspace. msks generates
no key: pass `--pubkey` with your public key file, or configure
`identity_file` once in the client config. An unconfigured client
refuses the create. A name is unique per daemon: a create over a
name another workspace already holds exits 4.

A workspace's `status` is one of these values:

| status     | meaning                                      |
| ---------- | -------------------------------------------- |
| `created`  | the row exists; it has never booted          |
| `starting` | boot in progress                             |
| `running`  | up                                           |
| `paused`   | the hypervisor holds the VM paused           |
| `stopping` | stop in progress                             |
| `stopped`  | shut down; `start` boots it again            |
| `absent`   | the row exists; its artifacts are gone       |
| `unknown`  | the daemon lost track; re-read before acting |

## Run commands inside

```bash
msks ssh ws -- whoami
msks ssh ws -- python3 -m pip list
msks rsync -av ./src :/src
msks rsync -av :/src ./src
```

Everything after the workspace reaches ssh or rsync verbatim. The
command exits with the child's own exit code. An rsync path with an
empty host (`:/src`, `user@:/src`) targets this workspace; the push
and pull examples above are the pair.

## Egress consent from a script

A workspace in the `allow` posture (the default) needs no consent:
flows pass. Consent exists in the `interactive` posture, where each
new flow's first packet holds until a verdict. A held flow with no
connected decider is denied at once, so the decider must be
connected while the flow fires.

```bash
msks create ws --egress-mode interactive --pubkey key.pub
msks egress mode ws interactive
msks egress watch ws
msks egress requests ws --decision pending --json
msks egress decide ws REQID allow --duration 15m
msks egress rules ws
```

`watch ws` registers this client as that workspace's decider and
stays connected. It prints three line shapes: a new request
(`<id>  <destination>  <decision>  <epoch>`), a verdict landing
(`<id>  resolved: <decision>`), and a rules block when the rules
change. The request id prints in full; `decide` takes it as REQID
above, from the stream or from `requests`. `--duration` is one of
`once`, `5m`, `15m`, `tilrestart`, `forever`.

## Exit codes

| code | meaning                                                         |
| ---- | --------------------------------------------------------------- |
| 0    | success                                                         |
| 1    | a failure no other row covers: network, timeout, other refusals |
| 2    | bad invocation                                                  |
| 3    | not found                                                       |
| 4    | name conflict: a create or clone over a held name               |
| 5    | wrong state: the row's state or placement refuses the action    |
| 130  | interrupted                                                     |

Under `--json` a failure prints one object on stderr:

```json
{ "error": { "status": 404, "detail": "no such workspace" } }
```

`rm` is the exception: it prints one JSON line per removed
workspace as the removals land, so a failure mid-sequence keeps the
earlier confirmations on stdout.

## The daemon connection

The client dials the daemon's local socket by default. A remote
daemon needs `MSKSC_URL`, `MSKSC_TOKEN` (a token file), and
`MSKSC_CAFILE` (the daemon's `msks-ca.pem`).
