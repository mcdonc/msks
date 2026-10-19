# msks: microvm workspaces for AI coding agents

msks runs each workspace as its own microvm — a cloud-hypervisor
VM with a per-VM network tap, its own kernel, and a state
directory an operator can point at any disk. An AI coding agent
works inside a workspace with the permissions it needs; the host
and every other workspace stay out of reach.

## The reading order

1. [**The CLI client**](cli.md) — every command, the TUI, and
   the client's settings: `msks` against the daemon's HTTP API,
   the workspace tree, the image page, ssh and rsync over the
   forward.
2. [**Configuration**](config.md) — the daemon's settings: every
   key, its environment variable, and its default, from the
   listener's socket to the egress stack's ceilings.
3. [**Workspace images**](images.md) — the image contract, the
   shipped builder, and the published channel: import from a
   manifest URL, the descriptions the builds bake, the hash-keyed
   releases.
4. [**Guest networking**](networking.md) — the per-VM tap, the
   egress consent postures, the interceptor, and the forward that
   carries ssh and rsync.
5. [**Storage**](storage.md) — the state disk, the per-workspace
   volumes, and the capacity report that prices them.
6. [**Secrets**](secrets.md) — placeholders, the swap on the
   wire, and the sentinel discipline.
7. [**Changelog**](changes.md) — what changed, release by
   release.

## The quick start

A Debian host prepares itself one command wide
(`msks-setup-debian-host`), the daemon serves two listeners —
TLS on a port for remote clients, a unix socket for same-host
ones that authenticate by peer credential — and a bare `msks`
finds the socket, lists the workspaces, and creates the next one.
[The CLI chapter](cli.md) walks the whole loop.
