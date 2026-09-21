# Spike #211-3: `--target-host` updates over bridge ssh, generation pointer and fallback

Timeboxed spike, closed 2026-09-21 (issue
[#211](https://github.com/mcdonc/msks/issues/211), third of three for
[#205](https://github.com/mcdonc/msks/issues/205)). The question: does
the deployed update path hold — `nixos-rebuild boot --target-host`
against the appliance over the msks bridge, copying only missing store
paths into the spike-2 overlay's upper layer, the appliance-side
system profile as the generation pointer, and the previous generation
as the boot fallback?

**Answer: yes.** The harness is `nix/spike-211-3-config.nix` (the
NixOS configuration — the same module `nixos-rebuild` evaluates),
`nix/spike-211-3.nix` (the first-boot artifact set), and
`scripts/spike-211-3.sh` (the orchestrated cycle). Run from a devenv
shell: `bash scripts/spike-211-3.sh`.

## What ran

- The spike-2 store shape unchanged: embedded erofs base with shipped
  database, ext4 store volume upper, `/nix/store` the overlay.
- **sshd, bridge-only, key-only**: one listen address — the bridge
  address `192.168.77.2` — `PermitRootLogin prohibit-password`,
  passwords off, one harness-generated Ed25519 key (delivered through
  a `spike3-ssh-key` NIX_PATH entry). Host keys mint at activation on
  the tmpfs root, so every fresh volume presents new ones and the
  harness pins none.
- **Every ssh session speaks the overlay store**: sshd `SetEnv
NIX_REMOTE=local-overlay://...` points root's sessions (nix-copy's
  remote side, switch-to-configuration) at the overlay store — reads
  consult both databases, writes land only in the upper layer, and
  delta computation knows the lower paths. A plain local store would
  re-send the whole base closure: its database has never heard of the
  lower layer's 4491 paths. No nix-daemon runs at all — the update
  path needs none.
- **The profile survives reboots**: `systemd.tmpfiles` symlinks
  `/nix/var/nix` to the volume's `nix-var`, so the system profile
  (`/nix/var/nix/profiles/system`, where `switch-to-configuration
boot` points it) and its `system-*-link` siblings live on the store
  volume, on the same directory the overlay store treats as its
  state.
- `system.switch.enable = true` — microvm.nix disables
  switch-to-configuration when the host store is not a share; this
  system's store is its own overlay and updates arrive over ssh, so
  switching stays on.
- Networking: systemd-networkd, static `192.168.77.2/24` on `eth0`
  (predictable names off), CH attached to the operator-installed
  `mskstap0`/`msksbr0` bridge the msks appliance itself uses — the
  spike refuses to start while any cloud-hypervisor (the appliance
  included) is running.

## The cycle, measured (reference host, warm caches)

| Step                                                    | Wall time   | Bytes copied to upper         |
| ------------------------------------------------------- | ----------- | ----------------------------- |
| boot gen A (first boot, artifacts shipped) → ssh        | 9.4s        | —                             |
| gen B: marker-only config change, `nixos-rebuild boot`  | 14.8s       | 203,505 B (~0.2 MiB)          |
| gen B reboot from the appliance profile → ssh, marker B | 10.4s       | —                             |
| gen C: adds `hello` to `systemPackages`, rebuild + copy | 37.4s       | 3,436,718 B (~3.3 MiB)        |
| gen C reboot → ssh, marker C, `hello` runs              | 10.5s       | —                             |
| broken gen (sshd on an unroutable address): rebuild     | 34.1s       | (delta of an etc-only change) |
| broken gen boot → misses the 60s boot-to-ssh budget     | 60s timeout | —                             |
| fallback: boot the last good generation → ssh, marker C | 10.1s       | —                             |

For scale, a **full image rebuild** for the same marker-only change —
rebuilding the 932 MiB base store tree plus the erofs pack — takes
**70–85s of host build time** (83s in the recorded run; 69.7s in an
independent repack of a never-built generation on the same host)
before any VM restart, and the delta path moved 0.2 MiB. The comparison the issue asked for: an update that
changes only configuration costs ~20s and kilobytes through ssh; one
that pulls a small package costs ~21s and the package's closure; a
full repack costs minutes and a fresh appliance image.

Generation retention: each `boot` switch adds a `system-N-link` on
the volume (two after two switches in the recorded run; the broken
deploy adds a third); nothing GCs them — the upper-scoped GC policy
from #205 applies, and the retained links are what the fallback
pointer reads. After the fallback the appliance profile still points
at the broken toplevel — the harness booted the last good generation
from its own cache. The production run script resolves this by
rewinding the profile to the generation it actually booted (or
equivalently keying the boot source on the harness-side cache, as
here); the decision belongs to #212.

## The privileged surface

- sshd listens on the bridge address only — one port, one address,
  key-only root, one key. Password auth off.
- No nix-daemon: `nix.enable` would wire `nix-daemon.socket` into
  `sockets.target` — the socket listens from boot (world-connectable,
  `srw-rw-rw-`) and any local connect starts the root daemon serving
  the plain-local-store view. The config disables the socket
  (`systemd.sockets.nix-daemon.wantedBy = []`); store access happens
  in-session under `NIX_REMOTE` with the overlay's own permissions.
- The broken-generation demonstration doubles as the surface check:
  sshd configured for an address the bridge never routes fails to
  bind at activation, and the machine boots to a login prompt with no
  way in — the constraint is real, not advisory.

## Landmines found (all handled in the harness)

1. **`kernel-params` carries no `init=`** — bootspec keeps the init
   path separate, and the file has no trailing newline either. A
   direct-kernel boot built from the profile needs
   `kernel-params + " init=<profile>/init"`; gluing them naively
   produces `bpfinit=` and an emergency shell.
2. **nixos-rebuild-ng defaults to flake mode** on hosts whose
   `/etc/nixos` is a flake — pass `--no-flake` (the devenv host's
   `nixos-rebuild` is the Rust rewrite; `-I nixos-config=...` alone
   does not steer it).
3. **nix-copy-closure spawns its own ssh** with the default config:
   it offers the invoking user's agent keys (triggering their
   passphrase prompts!) and fails host-key verification because each
   fresh volume mints new host keys. The harness puts a wrapper ssh
   on PATH with a dedicated config (`IdentitiesOnly`, the spike key,
   `StrictHostKeyChecking no` pinned to a throwaway known_hosts).
4. **`/nix/var/nix` is tmpfs** on a diskless system — the system
   profile lives there by default and vanishes on reboot. The symlink
   to the volume's state directory is load-bearing, and it must exist
   before any `nix-env -p` runs (tmpfiles ordering puts it in boot,
   before sshd).
5. **The first boot has no system profile** — the initial generation
   ships in the base image; `/nix/var/nix/profiles/system` appears
   only after the first switch. A boot-source reader must fall back to
   `/run/current-system`.
6. **overlayfs reports directory sizes as 0** — `du -sb` of the empty
   upper store reads 0, which makes delta arithmetic (before/after
   snapshots) still correct but absolute "size of upper" misleading
   without the note.
7. **The agent-prompt side effect** (see landmine 3): any harness ssh
   loop that retries while the guest is unreachable must run with
   `IdentitiesOnly` — otherwise it interrogates the user's agent
   twice a second, and on a desktop that is a passphrase dialog every
   retry.

## Not answered here

- The `switch` (live activation) cost — `boot` is the default verb;
  a live switch carries the workspace VMM tree with any daemon
  change, and measuring that belongs with #212's real units.
- msksd inside the config, capability units, the real run script's
  integration of the generation pointer — all #212.
