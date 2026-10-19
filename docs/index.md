# msks: microvm workspaces for AI coding agents

msks runs each workspace as its own microvm: a cloud-hypervisor
VM with a per-VM network tap, its own kernel, and a state
directory an operator can point at any disk. An AI coding agent
works inside a workspace with the permissions it needs; the host
and every other workspace stay out of reach.

## The reading order

1. [**Installation**](installation.md): the wheel, and the
   one command that prepares a Debian host: the capability
   wrapper, the routing kernel, `/dev/kvm`, and the lingered
   service that keeps the daemon running.
2. [**The identity file**](identity.md): the operator's ssh
   key: which file msks uses and why, the two config keys, the
   agent-first resolution, and the passphrase prompt.
3. [**The TUI**](tui.md): the full-screen workspace tree: the
   workspace, secrets, and image pages, the failure panels, the
   consent decider, and the terminal windows the shell action
   opens.
4. [**The CLI client**](cli.md): every command and the
   client's settings: `msks` against the daemon's HTTP API, ssh
   and rsync over the forward.
5. [**Configuration**](config.md): the daemon's settings: every
   key, its environment variable, and its default, from the
   listener's socket to the egress stack's ceilings.
6. [**Workspace images**](images.md): the image contract, the
   shipped builder, and the published channel: import from a
   manifest URL, the descriptions the builds bake, the hash-keyed
   releases.
7. [**Guest networking**](networking.md): the per-VM tap, the
   egress consent postures, the interceptor, and the forward that
   carries ssh and rsync.
8. [**Storage**](storage.md): the state disk, the per-workspace
   volumes, and the capacity report that prices them.
9. [**Secrets**](secrets.md): placeholders, the swap on the
   wire, and the sentinel discipline.
10. [**Changelog**](changes.md): what changed, release by
    release.
