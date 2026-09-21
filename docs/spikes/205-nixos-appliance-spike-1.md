# Spike #205-1: stock NixOS on cloud-hypervisor, store over the ro share

Timeboxed spike, closed 2026-09-21. The question (issue
[#205](https://github.com/mcdonc/msks/issues/205), spike 1 of 3): does
the dev-mode store shape work — a NixOS system booted directly by
cloud-hypervisor whose entire `/nix/store` arrives over a read-only
virtiofs share of the host's store, with no local nix database and no
disks at all?

**Answer: yes, and it is fast.** The harness is `nix/spike-205-1.nix`
(the evaluation) and `scripts/spike-205-1.sh` (build, boot, measure).
Run it from a devenv shell: `bash scripts/spike-205-1.sh`. The first
evaluation fetches the pinned microvm.nix tarball and needs
`experimental-features = flakes` in the host nix.conf (the devenv
shell's nix carries it); later builds are cached.

## What booted

- nixpkgs pinned by devenv.lock (NixOS 26.05pre, kernel 6.18.50,
  systemd 260.2), evaluated through `import (pkgs.path + "/nixos")`.
- microvm.nix's guest module, pinned rev `187b0a39`, imported via
  `builtins.getFlake` — zero friction against our nixpkgs pin. It
  supplies the ro-store share wiring (`/nix/.ro-store` + a read-only
  bind at `/nix/store`, both `neededForBoot`), the virtio initrd
  modules, and the `init=<toplevel>/init` kernel param. This is the
  wiring the production config will lift (recorded decision on the
  issue).
- One virtiofsd (`--shared-dir /nix/store --readonly --sandbox none`),
  one cloud-hypervisor instance, `memory.shared=true` (see landmines),
  serial to a file, **no disks** — tmpfs root, everything resolves
  through the share.

## Numbers (reference host, warm host page cache)

| Measurement                                   | Value               |
| --------------------------------------------- | ------------------- |
| boot-to-ready (vm.boot → SPIKE1-READY marker) | 11.7–11.8s (4 runs) |
| NixOS system toplevel closure                 | 699.5 MiB           |
| initrd                                        | 21.2 MiB            |

Context for the budget on the issue: the Debian appliance's warm p50
boot-to-API is ~27.5s, of which ~25s is msksd's Python closure
resolving cold over the share — OS bring-up is the small part in both
worlds. This spike reaches full multi-user (getty, journald, udev, the
unit graph) in ~12s with no msksd at all, so the +5s budget has ample
headroom for adding msksd, networkd's static plan, and the capability
unit. The comparison is honest about one gap: the spike's store paths
were warm in the host page cache from the build; a cold-host first
boot pays more, exactly as the Debian appliance's does (its ~15s fully
warm vs ~27.5s cold-share numbers in `docs/boot-speed.md`).

The closure carries a full systemd, a getty, and udev at 699.5 MiB —
inside the 1.1–1.6G estimate recorded on the issue (the estimate
assumed nix itself inside the closure; see landmine 5). The dev
artifact set stays tiny: a kernel, an initrd, a cmdline file, and the
toplevel path. The dev mode needs no rootfs at all — the root is a
tmpfs and `/etc` materializes through activation.

## Evidence for the no-database design

The system booted to multi-user with no nix database anywhere on the
machine: `nix.enable = false` keeps nix out of the closure (verified:
`nix path-info -r` on the toplevel lists no nix package), and
`microvm.registerClosure = false` keeps microvm.nix's boot-time
`nix-store --load-db` step and `regInfo=` kernel parameter out
(verified: the artifact cmdline ends at `init=…`). Nothing on the boot
or activation path constructs, loads, or consults a database — every
store path resolved through the share. This is the core fact the
issue's dev-mode design rests on, demonstrated against our own pin and
VMM.

Scope of the claim: the spike proves the **boot and activation path**
needs no database. It did not run any nix command inside the guest —
the deployed mode (spike 2) is exactly the one that does, and that is
where the two-database `local-overlay-store` design applies.

## Landmines found (all fixed or recorded in the harness)

1. **vhost-user needs shared memory.** `vm.create` without
   `memory.shared=true` succeeds; `vm.boot` returns 500. The
   appliance-run script already sets it; any hand-rolled payload must
   too.
2. **`config.system.build.initialRamdisk` is a directory** in current
   NixOS — the initrd file is `$out/initrd` (`initrd.zst` is the same
   content).
3. **A service's stdout lands in the journal, not on the console**, by
   default — a boot marker needs `StandardOutput=journal+console`
   (the same setting msksd.service carries).
4. **Kernel-cmdline quoting through `printf`**: an interpolated
   space-separated string word-splits under `printf '%s\n'`, writing
   one parameter per line and breaking JSON payloads downstream. Quote
   interpolations in generated scripts.
5. **microvm.nix registers the closure into a fresh nix db at every
   boot, by default.** `microvm.registerClosure` defaults on and is
   gated by `nix.enable`: the guest gets `regInfo=` on its kernel
   command line and a stage-2 `nix-store --load-db < registration`
   step — 4491 paths in this spike's first builds — and nix itself
   rides the closure (~224 MiB of the first build's 923.7 MiB
   toplevel). The first four measurements (11.7–12.4s) ran with that
   load-db pass inside them; the numbers above are re-measured with
   both options off, and the pass cost under a second against a tmpfs
   db — but a production config lifting microvm.nix's wiring must
   decide this deliberately, not inherit it silently.

## Not answered here (spike 2 and 3)

- The deployed store shape: overlayfs over an embedded lower,
  `local-overlay://`, the two-database behavior, upper layer on a
  dedicated volume.
- sshd-on-bridge + `nixos-rebuild --target-host boot` round trip and
  its delta-copy size.
- msksd itself inside a NixOS config (units, capabilities, networkd
  plan) — lands with the production config work, measured against the
  boot budget at parity time.
