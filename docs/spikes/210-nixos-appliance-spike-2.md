# Spike #210-2: overlay store via local-overlay-store over an embedded base

Timeboxed spike, closed 2026-09-21 (issue
[#210](https://github.com/mcdonc/msks/issues/210), second of three for
[#205](https://github.com/mcdonc/msks/issues/205)). The question: does
the deployed store shape hold together — `/nix/store` as an overlayfs
whose lower layer is an embedded, immutable base store **with its
database**, whose upper layer and second database live on a dedicated
store volume, all driven by Nix's experimental `local-overlay` store?

**Answer: yes.** Every probe the issue demanded passes, from inside the
booted system. The harness is `nix/spike-210-2.nix` (the evaluation,
the embedded base, the pristine volume) and `scripts/spike-210-2.sh`
(build, boot, collect). Run from a devenv shell:
`bash scripts/spike-210-2.sh`; a second invocation exercises the
persistence probe, because the store volume persists across runs.

## The shape that booted

- The same pinned evaluation as spike 1 (nixpkgs 26.05pre, kernel
  6.18.50, systemd 260.2, microvm.nix at `187b0a39`), this time using
  microvm.nix's own `storeOnDisk` + `writableStoreOverlay` wiring —
  the mode its author built for writable overlays. The production
  config lifts this path unchanged.
- The embedded base (`msks-spike-210-2-base`): a complete local store
  — tree plus SQLite database — built entirely sandboxed by
  `nix-store --load-db` from the closure's registration file inside
  the output. The base packs into an erofs image (label `nix-store`,
  microvm.nix's fixed label) that stage 1 mounts read-only at
  `/nix/.ro-store`.
- The store volume (`msks-spike-210-2-upper`): a 512 MiB ext4 image,
  its own disk, carrying the overlayfs upper layer (`store/`), the
  workdir (`work/`), and the upper store's state (`nix-var/`, database
  at `nix-var/db`). Mounted at `/nix/.upper-volume`, neededForBoot.
- `/nix/store` is the overlayfs merged view of the two, mounted in
  stage 1 (one correction to microvm.nix's default wiring: the lower
  layer is `nix/store` **inside** the base tree, because the base
  ships `var/` beside it — `fileSystems."/nix/store".overlay.lowerdir`
  is forced accordingly).
- The guest ships nix (929.4 MiB closure — nix is back in, as the
  deployed mode requires) with `local-overlay-store` and
  `read-only-local-store` experimental features enabled. The store
  URI the probes exercise:

  ```
  local-overlay://?real=/nix/store
    &lower-store=local?root=/nix/.ro-store&read-only=true   # nested, percent-encoded
    &upper-layer=/nix/.upper-volume/store
    &state=/nix/.upper-volume/nix-var
    &check-mount=false
  ```

## Evidence (from the booted system, first and second boots)

| Probe                                      | Result                                                                                                         |
| ------------------------------------------ | -------------------------------------------------------------------------------------------------------------- |
| merged store is the overlay (mount line)   | rw overlay at /nix/store, lowerdir the erofs store, upperdir/workdir the volume                                |
| read through the **lower database**        | toplevel resolves via `path-info` with no load step, no daemon                                                 |
| store write lands only in upper            | `nix-store --add` object appears in `nix-var`'s store layer, absent from the erofs, registered in the upper db |
| existing lower path registers without copy | `copy --from` of the toplevel: upper layer listing byte-identical before/after                                 |
| GC stays upper-scoped                      | rooted upper path survives `nix store gc`; toplevel still resolvable after                                     |
| persistence across reboots                 | second boot resolves the first boot's written path from the upper database alone                               |

## Numbers (reference host, warm host page cache)

| Measurement                                  | Value                                                                              |
| -------------------------------------------- | ---------------------------------------------------------------------------------- |
| boot-to-probes-done (vm.boot → SPIKE2-READY) | 9.0–10.3s (7 runs; the probes themselves cost ~1.5s, so boot-to-multi-user is ~8s) |
| NixOS system toplevel closure (with nix)     | 929.4 MiB                                                                          |
| embedded base erofs image                    | 920.0 MiB                                                                          |
| initrd                                       | 23.5 MiB                                                                           |
| store volume (pristine)                      | 512 MiB (effectively empty)                                                        |

Boot sits **under** spike 1's ro-share shape (11.7–11.8s) — the erofs
lower reads locally from disk instead of crossing virtiofs. The
shipped artifact set grows to: kernel, initrd, the erofs base, and a
template volume — within the 1.1–1.6G estimate recorded on #205.

## Landmines found (all handled in the harness; each is a decision the production config inherits)

1. **cloud-hypervisor disk auto-detection breaks writes on ZFS hosts.**
   Without an explicit `"image_type": "Raw"`, the API probes the image
   and the resulting backend fails every guest WRITE with I/O errors
   (reads work — the failure surfaces as ext4 superblock-write EIO at
   mount). `appliance-run.sh` already sets `image_type` on both disks;
   any hand-rolled payload must too. Explicit `readonly` flags on both
   disks belong beside it.
2. **`mkfs.ext4 -d` ships an unreplayed journal.** A read-only stage-1
   mount then aborts the journal and the boot drops to emergency mode.
   `e2fsck -fy` after mkfs settles the filesystem.
3. **Store-path copies keep the read-only mode.** The pristine volume
   is a store output (mode 444); the boot script's writable copy needs
   an explicit `chmod u+w`.
4. **A read-only lower store must SAY so, twice.** The lower store URI
   takes `read-only=true` (else the local store tries to remount the
   erofs writable: `EINVAL`), and that flag requires the
   `read-only-local-store` experimental feature. The nested URI rides
   percent-encoded inside the outer query — a bare `&` would split the
   outer parser. Every URI that opens the lower store (probe, `copy
--from`) needs the same treatment.
5. **`check-mount` compares option STRINGS, not mounts.** The initrd
   (systemd fstab generator) mounts the overlay with `/sysroot`-
   prefixed layer paths; after switch_root nix's check reads those
   strings, compares them against the configured absolute paths, and
   rejects a correctly-mounted store. `check-mount=false` plus
   printing the real mount line as evidence is the spike's answer;
   production should re-examine (a stage-2 remount with final paths,
   or upstreaming the prefix comparison) before trusting the check.
6. **NixOS stage 2 bind-remounts `/nix/store` read-only** (standard
   behavior). The local-overlay store still writes — it operates on
   the upper layer directly, not through the merged mount — so this is
   the deployed mode's desired posture: VFS users get a read-only
   `/nix/store`, nix gets a writable store through the volume. Worth
   knowing it is NixOS doing it, not us.
7. **A service embedding `${toplevel}` at build time recurses** — the
   service is part of the toplevel's own unit graph. Guest-side
   scripts resolve the booted system at runtime
   (`readlink -f /run/current-system`).
8. **`unsafeDiscardReferences` needs `__structuredAttrs = true`** in
   nixpkgs 26.05 — plain runCommand attrsets fail coercion.
9. **systemd service PATH is bare** — `/run/current-system/sw/bin` is
   not on it; unit scripts reference store paths absolutely.

## Not answered here (spike 3, #211)

- The ssh transport and `nixos-rebuild --target-host boot` round trip:
  copy sizes, generation pointer, boot fallback.
- nix-daemon wiring (this spike used client-side `--store` URIs; the
  daemon's unix-socket constraint is production work, #212).
- Cold-host first boot (page-cache-warm numbers only, as in spike 1).
