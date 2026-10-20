# Development environment

This chapter collects the devenv side of msks: everything a
checkout of this repository presets, seeds, and runs for
development. The published chapters describe the deployed
product; this one describes the developer's tree — the shell,
the dev daemon, the client presets, and the dev identity.

## The dev daemon

A development host (this repository's checkout) runs the daemon
through devenv. `msks-dev` seeds a per-worktree state dir with
its own port, token, and socket, and execs `msksd` through
`/run/wrappers/bin/msks-caps`, the wrapper the host's NixOS
configuration installs. When the dev tree runs on a host NixOS
does not manage, `DEV_CAPS` points at the Debian setup's
`/usr/local/bin/msks-caps`.

The same daemon is the default `devenv processes up` process:
the manager runs `scripts/dev-daemon.sh` in the foreground, and
Ctrl-C stops it through the daemon's own graceful path.

## The daemon's config file

The dev daemon runs bare: `scripts/dev-daemon.sh` points
`MSKSD_CONFIG_DIR` at the devenv root. The daemon's durable
settings therefore live in `<repo>/msksd.yaml`, generated as
the commented template on first daemon start and untracked, so
local edits stay personal. The environment the dev script
exports (API port, state dir, egress wiring) wins over file
values under the precedence the [Configuration](config.md)
chapter documents.

## The client presets

The devenv shell presets the client's connection to the
worktree's own dev daemon. Once the dev daemon has served once,
a fresh devenv shell needs no exports: the token and CA come
from the dev-daemon state dir, `.devenv/state/msksd/`:

```bash
msks ls        # presets: https://127.0.0.1:8660, the worktree's
               # token, and .devenv/state/msksd/msks-ca.pem
               # (verified TLS)
```

The same shell presets `MSKSC_CACHE_DIR` at the worktree's own
`.devenv/state/msksc/cache/`, so each checkout's client state
stays its own and one tree's cache entry is never read by
another. A non-empty value exported before entering the shell
survives the preset; an empty one counts as unset.

Targeting a hand-run bare-host daemon instead (no managed
process; `msks-dev-ready` then `msksd` from a shell) takes the
explicit pair from its state, with its own CA:

```bash
export MSKSC_URL=https://127.0.0.1:8660
export MSKSC_TOKEN=$(cat .devenv/state/msksd/bootstrap-token)
export MSKSC_CAFILE=.devenv/state/msksd/msks-ca.pem
```

## The dev identity

A checkout's devenv shell presets `MSKSC_IDENTITY_FILE` at the
worktree's own dev identity (`.devenv/state/msksc/identity`),
minted passphrase-less the first time the shell opens, so the
smokes and `msks-dev` have a key without touching yours. The
preset fills an unset variable: a non-empty
`MSKSC_IDENTITY_FILE` you exported before entering the shell
survives, and so does the file's `identity_file` (the
environment preset lands only when you named nothing). Pointing the
dev identity at your own key (a symlink over the minted file)
works, with the identity chapter's rules: the passphrase prompt
appears when the daemon's rows are locked under it and no agent
holds it. See [The identity file](first-run.md#the-identity-file).

## Doctor on the dev host

The dev daemon runs bare: `scripts/dev-daemon.sh` points
`MSKSD_CONFIG_DIR` at the devenv root. A hand-run `msksd doctor`
in the shell therefore resolves the same `<repo>/msksd.yaml`
the daemon serves with. The environment the dev script exports
(API port, state dir, egress wiring) wins over file values
under the precedence the [Configuration](config.md) chapter
documents.
