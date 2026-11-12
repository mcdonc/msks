"""The agent reference card (#842).

One markdown text with three surfaces: this constant,
``docs/agents.md`` (the site chapter, byte for byte the same text —
a test holds the two together), and ``msks agent-card`` (the client
command that prints it, so a harness captures the card in one call).

The card is addressed to the harness persona: the machine that
operates msks from the host. It groups commands by task, carries the
status vocabulary and the exit-code table (#841) whole, and names
only invocations that parse — a test re-parses every ``msks`` line
against the CLI.
"""

AGENT_CARD_MD = """\
# The agent reference card

This card is for a coding harness that operates msks from the host.
Given only this text, a harness can enumerate workspaces, create and
boot one, run commands inside it, sync files, consent to egress, and
interpret every status and exit code it sees. Every command below
takes `--json`. `msks agent-card` prints this card. The full
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
msks create ws --image debian:13 --mem-mib 2048 --start
msks start ws
msks stop ws
msks clone ws fork
msks rm ws           # takes several workspaces, removed in order
```

`create` plants your ssh identity into the workspace. The first
`msks` run wrote the client config that carries it. A name is unique
per daemon: a create over a live name exits 4.

A workspace's `status` is one of these values:

| status    | meaning                                       |
| --------- | --------------------------------------------- |
| `created` | the row exists; it has never booted           |
| `starting`| boot in progress                              |
| `running` | up                                            |
| `paused`  | the hypervisor holds the VM paused            |
| `stopping`| stop in progress                              |
| `stopped` | shut down; `start` boots it again             |
| `absent`  | the row exists; its artifacts are gone        |
| `unknown` | the daemon lost track; re-read before acting  |

## Run commands inside

```bash
msks ssh ws -- whoami
msks ssh ws -- python3 -m pip list
msks rsync -av ./src ws:/src
```

Everything after the workspace reaches ssh or rsync verbatim. The
command exits with the child's own exit code. In an rsync path the
empty host (`ws:/src`) is this workspace.

## Egress consent from a script

```bash
msks egress requests ws --decision pending --json
msks egress decide ws REQID allow --duration 15m
msks egress rules ws
msks egress watch
```

`watch` streams one line per request: the request id, the
destination, the decision, and the requested-at epoch. The id
prints in full; `decide` takes it as REQID above. `--duration` is
one of `once`, `5m`, `15m`, `tilrestart`, `forever`.

## Exit codes

| code | meaning                                                        |
| ---- | -------------------------------------------------------------- |
| 0    | success                                                        |
| 1    | a failure no other row covers: network, timeout, other refusals |
| 2    | bad invocation                                                 |
| 3    | not found                                                      |
| 4    | name conflict: a create or clone over a live name              |
| 5    | wrong state: the row's state or placement refuses the action   |
| 130  | interrupted                                                    |

Under `--json` a failure prints one object on stderr and nothing on
stdout:

```json
{ "error": { "status": 404, "detail": "no such workspace" } }
```

## The daemon connection

The client dials the daemon's local socket by default. A remote
daemon needs `MSKSC_URL`, `MSKSC_TOKEN` (a token file), and
`MSKSC_CAFILE` (the daemon's `msks-ca.pem`).
"""
