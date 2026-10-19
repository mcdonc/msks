# Installation

msks ships as one Python wheel with everything inside: the client (`msks`), the
daemon (`msksd`), the consent-decider terminal (`msks-term-popup`), and the
Debian host setup this chapter walks (`msks-setup-debian-host`).

The wheel needs Python 3.14 or newer.

```console
pipx install msks        # or: pip install msks
```

The daemon runs the workspaces directly on the host using cloud-hypervisor on
`/dev/kvm`, one network tap per VM, and the egress consent stack in the host
kernel

The host therefore needs pieces the wheel cannot carry. These are software
dependency packages, kernel modules, a routing sysctl, two capabilities for the
daemon's user, and a service that keeps `msksd` running.

On a Debian deployment host, one command lands all of it:

```console
sudo "$(command -v msks-setup-debian-host)"
```

The quoted form matters after a `pipx` install: the console
script lands in `~/.local/bin`, which sudo's `secure_path` does
not carry — the bare name would answer "command not found".

Run it as the user the daemon will serve — under `sudo`, the `SUDO_USER`
account is the executing user, and the service the tool registers belongs to
that account.

The run is idempotent: re-run any time, and re-run after a util-linux upgrade
(the wrapper keeps a copy of `setpriv`; see below).

## What the setup lands

Each phase runs in order, and the first refusal stops the run
with a named error.

**The packages.** The egress plumbing the daemon execs by bare
name installs through apt when missing: `iproute2` (`ip`),
`nftables` (`nft`), `conntrack`, and `procps` (`sysctl`),
beside the tools the setup itself runs: `util-linux` (`setpriv`),
`libcap2-bin` (`setcap`), `kmod` (`modprobe`). The daemon's
workspace tools install separately (below).

**The capability wrapper.** `/usr/local/bin/msks-caps` — a
root-owned copy of util-linux `setpriv` carrying file
capabilities `cap_net_admin` and `cap_net_bind_service`. The
daemon's per-VM taps and nftables chains need `net_admin`; the
egress stack's DHCP (port 67) and DNS (port 53) need
`net_bind_service`. The wrapper raises both caps ambient, so
they flow to the daemon and every tool it execs — and the grant
surface is the wrapper's execute permission: any local user
able to run it holds the two capabilities through it. After
installing it the setup runs the wrapper and verifies the caps
arrive ambient.

**The routing kernel.** `/etc/sysctl.d/msks-egress.conf` sets
`net.ipv4.ip_forward=1` at boot, and the value applies
immediately: route packets between the per-VM taps and the
host uplink. The daemon reads the sysctl at startup and refuses
every egress workspace, with the cause named, while it reads 0.
`/etc/modules-load.d/msks.conf` loads the netfilter and tun
modules at boot: the nftables machinery, NAT, conntrack, the
deny RST, and the interactive consent queue. A module the
kernel tree lacks lands a warning with the kernel package that
carries it; interactive egress consent needs its whole module
set, and the run's summary names the verdict.

**`/dev/kvm`.** Access arrives through the `kvm` group on the
daemon's user; the setup adds the executing user to the group
when missing. The account picks the group up at the next login; an
already-running session keeps the old groups until it starts
anew.

**The systemd user service.** On a host systemd manages, the
setup writes `~/.config/systemd/user/msksd.service` for the
executing user and enables it. The unit execs `msksd` through
the wrapper, so the file capabilities grant it the same
posture a root unit would have. It runs with
`MSKSD_EGRESS_ENABLED=true` and the uplink interface the setup
detected, and it creates the unix listener's runtime directory
under the user's own `$XDG_RUNTIME_DIR` (`RuntimeDirectory=msks`,
the client's default socket candidate). It restarts the daemon
on failure. Finally, `loginctl enable-linger` keeps the user
manager, and the daemon, running across logouts and boots. A user
manager that was already running may have forked before the kvm
group write reached it; the daemon then holds the old groups
until that manager restarts, and the run's summary says so. A re-run
rewrites the unit and restarts a running daemon onto it when
the unit changed.

The run ends with a summary: the wrapper and its caps, the
sysctl, the modules file and the consent verdict, the tools'
paths, `/dev/kvm`, the detected uplink (the interface the
daemon's `egress_uplink` key should point at), and the
service line. When the service started, the same-user socket
path prints beside it.

## The same host by hand (if you'd rather not trust `msks-setup-debian-host`)

`msks-setup-debian-host` is a program the wheel ships, and
running it means running shipped code as root. An operator who
prefers to read every step first can land the same host in an
interactive root shell — each command below is one phase of the
walk above, pasted and run by hand.

Start a root shell and install the package set:

```console
sudo -i apt install iproute2 \
                    nftables \
                    conntrack \
                    procps \
                    util-linux \
                    libcap2-bin \
                    kmod
```

The capability wrapper — a fresh root-owned copy of `setpriv`,
with the two capabilities set and read back (a filesystem
without capability xattrs takes the `setcap` and loses it, so
the readback is the proof):

```console
install -o root -g root -m 0755 "$(command -v setpriv)" /usr/local/bin/msks-caps
setcap cap_net_admin,cap_net_bind_service+ep /usr/local/bin/msks-caps
getcap /usr/local/bin/msks-caps
```

The functional proof — the daemon's exact exec line, with the
ambient set inspected in the process it starts (the `CapAmb`
line must carry a nonzero mask):

```console
/usr/local/bin/msks-caps --inh-caps=+net_admin,+net_bind_service \
  --ambient-caps=+net_admin,+net_bind_service \
  -- sh -c "sed -n 's/^CapAmb:[[:space:]]*//p' /proc/self/status"
```

Routing, at boot and now:

```console
printf '%s\n' \
  '# route packets between per-VM taps and the host uplink' \
  'net.ipv4.ip_forward = 1' > /etc/sysctl.d/msks-egress.conf
sysctl -p /etc/sysctl.d/msks-egress.conf
```

The module set, at boot and now:

```console
printf '%s\n' tun nf_tables nft_chain_nat nft_masq nft_ct nft_reject \
  nf_reject_ipv4 nf_nat nf_conntrack nfnetlink_queue \
  > /etc/modules-load.d/msks.conf
modprobe tun nf_tables nft_chain_nat nft_masq nft_ct nft_reject \
  nf_reject_ipv4 nf_nat nf_conntrack nfnetlink_queue
```

Interactive egress consent needs `nft_reject`,
`nf_reject_ipv4`, and `nfnetlink_queue` loaded — check with
`grep -w <module> /proc/modules` (or `lsmod`) when a load
landed quietly.

`/dev/kvm` for the daemon's user, picked up at that account's
next login:

```console
usermod -aG kvm <the-user-the-daemon-runs-as>
```

The systemd user service is the daemon user's own piece — leave
the root shell and run it as that user. Write
`~/.config/systemd/user/msksd.service` with the unit below
(adjust the `msksd` path to where the install put it, and the
uplink to this host's default-route interface — `ip route show
default` names it):

```ini
[Unit]
Description=msks daemon (workspaces as cloud-hypervisor microvms)

[Service]
ExecStart=/usr/local/bin/msks-caps --inh-caps=+net_admin,+net_bind_service --ambient-caps=+net_admin,+net_bind_service -- /path/to/msksd
Environment=MSKSD_EGRESS_ENABLED=true
Environment=MSKSD_EGRESS_UPLINK=<uplink>
RuntimeDirectory=msks
Restart=always
RestartSec=1
TimeoutStopSec=180s

[Install]
WantedBy=default.target
```

Then enable it, as that user:

```console
systemctl --user enable --now msksd.service
loginctl enable-linger <the-user-the-daemon-runs-as>
```

`loginctl enable-linger` is the one root command among these —
it keeps the user manager, and the daemon, running across
logouts and boots.

## The VMM (`cloud-hypervisor`) and the workspace tools

The daemon's workspace machinery execs three more tools by bare
name, and the wheel carries none of them: `cloud-hypervisor`
(the VMM), `qemu-img` (the root overlay), and `mkisofs` (the
seed metadata).

`msksd doctor` verifies all three and names the install for a miss. On Debian,
`qemu-img` and `mkisofs` come from `qemu-utils` and `genisoimage`; the egress
consent stack's library (`libnetfilter_queue`) comes from
`libnetfilter-queue1`. Cloud Hypervisor's static release binary is the
sanctioned VMM install — Debian's archive carries it not:

```console
curl -L -o cloud-hypervisor-static \
  https://github.com/cloud-hypervisor/cloud-hypervisor/releases/latest/download/cloud-hypervisor-static
sudo install -m755 cloud-hypervisor-static /usr/local/bin/cloud-hypervisor
```

## The alternative paths

**A NixOS deployment host** needs no setup tool. The module
in `nix/module.nix` sets the sysctl, names the uplink, and
grants the daemon the same two capabilities through the unit
itself (bounded `AmbientCapabilities`). A dedicated `msksd`
user holds them, with `kvm` among its supplementary groups.
This grant is narrower than the wrapper's execute permission:
it is scoped to the one service.
A `nixos-rebuild switch` is the whole host setup.

**A development host** (this repository's checkout) runs the
daemon through devenv. `msks-dev` seeds a per-worktree state
dir with its own port, token, and socket, and execs `msksd`
through `/run/wrappers/bin/msks-caps`, the wrapper the host's
NixOS configuration installs. When the dev tree runs on a host
NixOS does not manage, `DEV_CAPS` points at the Debian setup's
`/usr/local/bin/msks-caps`.

## Distributions the `msks-setup-debian-host` unit does not cover

The `--no-service` flag to `msks-setup-debian-host` lands every host piece
above and skips the unit. Choose it for a container, a non-systemd init, or a
deployment that runs `msksd` its own way.

The setup also skips the unit on its own when it cannot find an `msksd` beside
its own install, or when systemd is not the manager, and it says so. On such a
host the daemon runs whatever way the deployment chooses, with the wrapper on
its exec line for the two capabilities; the [Configuration](config.md) chapter
documents the settings a hand-written unit needs.

## First run

A bare `msksd` (no subcommand) serves. A fresh state dir (`~/.local/state/msksd`, relocated by the
`state_dir` key) gets three things on first serve. The daemon
mints its CA and certificate and prints the CA fingerprint to
pin on first connect. The config file generates as a commented
template (see [Configuration](config.md)). The unix listener
binds `$XDG_RUNTIME_DIR/msks/msksd.sock`: same-host clients
connect through it on peer-credential authentication alone,
while remote ones dial the TLS listener with a bearer token. A workspace image arrives either from the published
channel (`msks image import` in [the TUI](tui.md), or the
[images](images.md) chapter's URL form) or from a local build.

With the host prepared, the service lingered, and an image
imported, a bare `msks` on the same account finds the socket,
lists the workspaces, and creates the next one. [The TUI](tui.md)
walks the whole loop, and [the identity file](identity.md)
names the key the workspaces will answer to.
