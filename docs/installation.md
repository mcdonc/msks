# Installation

msks ships as one Python wheel with everything inside: the
client (`msks` — the TUI and the commands), the daemon
(`msksd`), the consent-decider terminal (`msks-term-popup`),
and the Debian host setup this chapter walks
(`msks-setup-debian-host`). The wheel needs Python 3.14 or
newer.

```console
pipx install msks        # or: pip install msks
```

The daemon runs the workspaces first-level — cloud-hypervisor
on the host's own `/dev/kvm`, per-VM network taps, the egress
consent stack in the host kernel — so the host needs pieces the
wheel cannot carry: kernel modules, a routing sysctl, two
capabilities for the daemon's user, and a service that keeps
`msksd` running. On a Debian deployment host, one command lands
all of it:

```console
sudo msks-setup-debian-host
```

Run it as the user the daemon will serve — under `sudo`, the
`SUDO_USER` account is the executing user, and the service the
tool registers belongs to that account. The run is idempotent:
re-run any time, and re-run after a util-linux upgrade (the
wrapper keeps a copy of `setpriv`; see below).

## What the setup lands

Each phase runs in order, and the first refusal stops the run
with a named error.

**The packages.** Every tool the daemon execs by bare name
installs through apt when missing: `iproute2` (`ip`),
`nftables` (`nft`), `conntrack`, `kmod` (`modprobe`), `procps`
(`sysctl`), `libcap2-bin` (`setcap`), and `util-linux`
(`setpriv`).

**The capability wrapper.** `/usr/local/bin/msks-caps` — a
root-owned copy of util-linux `setpriv` carrying file
capabilities `cap_net_admin` and `cap_net_bind_service`. The
daemon's per-VM taps and nftables chains need `net_admin`; the
egress stack's DHCP (port 67) and DNS (port 53) need
`net_bind_service`. The wrapper raises both caps ambient, so
they flow to the daemon and every tool it execs, and any local
user able to execute the wrapper holds them through it — the
same grant surface the NixOS deployment host's wrapper makes.
After installing it the setup runs the wrapper as the invoking
user and verifies the caps arrive ambient.

**The routing kernel.** `/etc/sysctl.d/msks-egress.conf` sets
`net.ipv4.ip_forward=1` at boot (and the value applies
immediately): route packets between the per-VM taps and the
host uplink. The daemon reads the sysctl at startup and refuses
every egress workspace, with the cause named, while it reads 0.
`/etc/modules-load.d/msks.conf` loads the netfilter and tun
modules at boot — the nftables machinery, NAT, conntrack, the
deny RST, and the interactive consent queue. A module the
kernel tree lacks lands a warning with the kernel package that
carries it; interactive egress consent needs its whole module
set, and the run's summary names the verdict.

**`/dev/kvm`.** Access arrives through the `kvm` group on the
daemon's user — the setup adds the executing user to the group
when missing. The account picks the group up at the next login
(a bare re-run of an already-logged-in shell keeps the old
groups); `msksd doctor` reports the membership as it stands for
the daemon's own account.

**The systemd user service.** On a host systemd manages, the
setup writes `~/.config/systemd/user/msksd.service` for the
executing user and enables it: the unit execs `msksd` through
the wrapper (the file capabilities grant any executor the same
posture a root unit would), runs with
`MSKSD_EGRESS_ENABLED=true` and the uplink interface the setup
detected, creates the unix listener's runtime directory under
the user's own `$XDG_RUNTIME_DIR` (`RuntimeDirectory=msks` —
the client's default socket candidate), restarts the daemon on
failure, and `loginctl enable-linger` keeps the user manager —
and the daemon — running across logouts and boots. A user
manager that was already running forked before the kvm group
write reaches it; the daemon holds the old groups until that
manager restarts, and the run's summary says so. A re-run
rewrites the unit and restarts a running daemon onto it when
the unit changed.

The run ends with a summary: the wrapper and its caps, the
sysctl, the modules file and the consent verdict, the tools'
paths, `/dev/kvm`, the detected uplink (the interface the
daemon should point at through `MSKSD_EGRESS_UPLINK`), and the
service line. When the service started, the same-user socket
path prints beside it.

## Hosts the unit does not cover

`--no-service` lands every host piece above and skips the
unit — for a container, a non-systemd init, or a deployment
that runs `msksd` its own way (the setup also skips the unit on
its own when it cannot find an `msksd` beside its own install,
or when systemd is not the manager, and says so). On such a
host the daemon runs whatever way the deployment chooses, with
the wrapper on its exec line for the two capabilities; the
[Configuration](config.md) chapter documents the settings a
hand-written unit needs.

## The alternative paths

**A NixOS deployment host** needs no setup tool: the module in
`nix/module.nix` sets the sysctl, names the uplink, grants the
daemon the same two capabilities through its own wrapper, and
runs the daemon as a system service — a `nixos-rebuild switch`
is the whole host setup.

**A development host** (this repository's checkout) runs the
daemon through devenv: `msks-dev` seeds a per-worktree state
dir (its own port, token, and socket) and execs `msksd` through
the host's `msks-caps` wrapper — the same wrapper the Debian
setup installs, relocated through `DEV_CAPS` when it lives
somewhere else.

## First run

A bare `msksd` (no subcommand) serves. On a fresh state dir
(`~/.local/state/msksd`, relocated by `MSKSD_STATE_DIR`) the
daemon mints its CA and certificate on first serve and prints
the CA fingerprint to pin on first connect; the config file
generates as a commented template on first run (see
[Configuration](config.md)); and the unix listener binds
`$XDG_RUNTIME_DIR/msks/msksd.sock` — same-host clients connect
through it with peer-credential authentication, no token, no
TLS, while remote ones dial the TLS listener with a bearer
token. A workspace image arrives either from the published
channel (`msks image import` in [the TUI](tui.md), or the
[images](images.md) chapter's URL form) or from a local build.

With the host prepared, the service lingered, and an image
imported, a bare `msks` on the same account finds the socket,
lists the workspaces, and creates the next one —
[the TUI](tui.md) walks the whole loop.
