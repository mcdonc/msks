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
   commands that prepare hosts.
2. [**The host doctor**](doctor.md): the command that
   checks the host for every tool the daemon and client executes,
   and suggests a fix for each problem found.
3. [**First run**](first-run.md): configuring identity and importing an image.
4. [**The TUI**](tui.md): the full-screen workspace tree: the
   workspace, secrets, and image pages, the consent decider, and
   terminal windows.
5. [**The CLI client**](cli.md): every command line option: `msks` against
   the daemon's HTTP API, ssh and rsync over a forward.
6. [**Host share mounts**](mounts.md): the host trees a
   workspace mounts into its guest.
7. [**Cloning workspaces**](cloning.md): cloning workspaces --
   what a clone carries, what stays behind.
8. [**Guest networking**](networking.md): details about VM guest networking.
9. [**Egress consent**](egress-consent.md): the consent
   postures, verdicts and durations, switching modes live.
10. [**LLM Proxy**](llm.md): how to make use of msksd's built-in
    LLM proxy.
11. [**Secrets**](secrets.md): creating secret placeholders, and how
    the swap of placeholders for secret values happens on the wire.
12. [**Daemon Configuration**](config.md): the msksd daemon's settings: every
    key, its environment variable, and its default.
13. [**Workspace images**](images.md): the image contract, the
    shipped builder, and the published channel: import from a
    manifest URL.
14. [**Storage**](storage.md): reports about the per-workspace
    volumes (e.g. capacity).
15. [**The HTTP API**](api.md): msksd HTTP API surface --
    the endpoints the commands wrap and the authentication model.
16. [**Development environment**](development.md): The Nix/devenv development
    environment.
17. [**Changelog**](changes.md): what changed, release by
    release.
