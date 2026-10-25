# msks: microvm workspaces

_msks_ is a tool that easily allows you to create microvm workspaces. It can be
used to run AI harnesses securely.

Each microvm is a cloud-hypervisor VM with its own kernel. It also provides
services which allow you to avoid keeping secrets and keys within the VM and a
facility to allow you to approve network egress.

_msks_ runs on Linux hosts, and provides tooling to install its host
dependencies on Debian-based systems.

## The reading order

1. [**Installation**](installation.md): install the Python wheel, and the one
   command that prepares a Debian host: the capability wrapper, the routing
   kernel, `/dev/kvm`, and the lingered service that keeps the daemon running.
2. [**The TUI**](tui.md): the full-screen workspace tree: the
   workspace, secrets, and image pages, the failure panels, the
   consent decider, and the terminal windows the shell action
   opens.
3. [**The CLI client**](cli.md): every command and the
   client's settings: `msks` against the daemon's HTTP API, ssh
   and rsync over the forward.
4. [**Configuration**](config.md): the daemon's settings: every
   key, its environment variable, and its default, from the
   listener's socket to the egress stack's ceilings.
5. [**The host doctor**](doctor.md): the one command that
   checks the host for every tool the daemon and client exec,
   and names the fix for each miss.
6. [**First run**](first-run.md): what the daemon lands on its
   first serve — the CA, the config template, the socket — and
   the first workspace.
7. [**Workspace images**](images.md): the image contract, the
   shipped builder, and the published channel: import from a
   manifest URL, the descriptions the builds bake, the hash-keyed
   releases.
8. [**Guest networking**](networking.md): the per-VM tap, the
   DHCP and DNS the guest sees, the NAT path out, and the
   forward that carries ssh and rsync.
9. [**Egress consent**](egress-consent.md): the consent
   postures, verdicts and durations, switching modes live, and
   the probe endpoint that proves the chain end to end.
10. [**Storage**](storage.md): the state disk, the per-workspace
    volumes, and the capacity report that prices them.
11. [**LLM Proxy**](llm.md): how to make use of msksd's built-in
    LLM proxy.
12. [**Secrets**](secrets.md): placeholders, the swap on the
    wire, and the sentinel discipline.
13. [**The HTTP API**](api.md): the `/api/v1` surface itself —
    the endpoints the commands wrap, the authentication model,
    and the TCP listener a remote client reaches.
14. [**Changelog**](changes.md): what changed, release by
    release.
15. [**Development environment**](development.md): the devenv
    side of a checkout — the dev daemon, the shell's client
    presets, the worktree identity.
