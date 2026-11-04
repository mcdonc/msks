# msks: microvm workspaces

_msks_ is a tool that easily allows you to create microvm workspaces. It can be
used to run AI harnesses securely.

Each microvm is a cloud-hypervisor VM with its own kernel. It also provides
services which allow you to avoid keeping secrets and keys within the VM, a
facility to allow you to approve network egress, and a system that allows
you to proxy LLM requests without keeping your API keys on the agent host.

_msks_ runs on:

- Linux on Intel and ARM systems (Debian/Fedora)

- macOS on M3+ systems

## The reading order

1. [**Installation**](installation.md): install the Python wheel, and the
   command that prepares a host.
2. [**The host doctor**](doctor.md): the command that
   checks the host for every tool the daemon and client executes,
   and suggests the fix for each problem.
3. [**First run**](first-run.md): what the daemon lands on its
   first serve — the CA, the config template, the socket — and
   the first workspace.
4. [**The TUI**](tui.md): the full-screen workspace tree: the
   workspace, secrets, and image pages, the consent decider, and
   terminal windows.
5. [**The CLI client**](cli.md): every command line option: `msks` against
   the daemon's HTTP API, ssh and rsync over a forward.
6. [**Host share mounts**](mounts.md): the host trees a
   workspace mounts into its guest.
7. [**Guest networking**](networking.md): the per-VM tap, the
   DHCP and DNS the guest sees, the NAT path out, and the
   forward that carries ssh and rsync.
8. [**Egress consent**](egress-consent.md): the consent
   postures, verdicts and durations, switching modes live.
9. [**LLM Proxy**](llm.md): how to make use of msksd's built-in
   LLM proxy.
10. [**Secrets**](secrets.md): placeholders, the swap on the
    wire, and the sentinel discipline.
11. [**Configuration**](config.md): the daemon's settings: every
    key, its environment variable, and its default.
12. [**Workspace images**](images.md): the image contract, the
    shipped builder, and the published channel: import from a
    manifest URL.
13. [**Storage**](storage.md): the state disk, the per-workspace
    volumes, and a capacity report.
14. [**The HTTP API**](api.md): the `/api/v1` surface itself —
    the endpoints the commands wrap and the authentication model.
15. [**Development environment**](development.md): the devenv
    side of a checkout — the dev daemon, the shell's client
    presets, the worktree identity.
16. [**Changelog**](changes.md): what changed, release by
    release.
