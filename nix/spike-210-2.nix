# Spike 210-2 for #210/#205: the deployed store shape. An embedded,
# immutable base store (tree AND database) packs into an erofs image
# that microvm.nix mounts read-only at /nix/.ro-store; a dedicated
# ext4 store volume (its own disk) carries the overlayfs upper layer,
# the workdir, and the upper store's database; stage 1 mounts all of
# it and the guest's nix reads and writes through the local-overlay
# store.
#
# The wiring follows microvm.nix's own writableStoreOverlay mode —
# the production config lifts the same path — with one deliberate
# replacement: the store disk ships a complete SQLite database built
# ahead of time (microvm's default ships no database and registers at
# boot; spike 1's landmine 5).
#
# Run scripts/spike-210-2.sh from a devenv shell.
{
  pkgs ? import <nixpkgs> { },
}:

let
  lib = pkgs.lib;
  microvm =
    (builtins.getFlake "github:microvm-nix/microvm.nix/187b0a390ee054028106a674e7b01b1cb940cbba")
    .nixosModules.microvm;

  nixos = import (pkgs.path + "/nixos") {
    configuration =
      { config, lib, ... }:
      {
        imports = [ microvm ];

        microvm = {
          hypervisor = "cloud-hypervisor";
          # microvm's store-disk + writable-overlay mode: the
          # store disk mounts ro at /nix/.ro-store, the store
          # volume mounts at /nix/.upper-volume, and /nix/store
          # becomes the overlayfs merged view.
          storeOnDisk = true;
          storeDiskType = "erofs";
          writableStoreOverlay = "/nix/.upper-volume";
          # The base store ships a complete database; nothing
          # registers at boot.
          registerClosure = false;
          shares = [ ];
          volumes = [
            {
              image = "spike210-store-volume.img";
              label = "SPIKE2UPPER";
              mountPoint = "/nix/.upper-volume";
              autoCreate = false;
            }
          ];
        };

        boot.kernelParams = [ "console=ttyS0" ];
        boot.initrd.availableKernelModules = [
          "virtio_blk"
          "virtio_pci"
          "ext4"
          "overlay"
        ];

        # The embedded base packs a whole local-store tree
        # (nix/store + nix/var), so the overlay's lower layer is
        # the store DIRECTORY inside that tree, not the erofs
        # root microvm's default store disk uses.
        fileSystems."/nix/store".overlay.lowerdir = lib.mkForce [
          "/nix/.ro-store/nix/store"
        ];

        # The deployed shape ships nix itself; the overlay store is
        # reachable through the experimental local-overlay store.
        nix.enable = true;
        nix.settings.experimental-features = [
          "nix-command"
          "flakes"
          "local-overlay-store"
          "read-only-local-store"
        ];
        documentation.enable = false;
        services.openssh.enable = false;
        # The evidence collector: runs once the store mounts are
        # up, exercises the overlay store, and prints PROBE lines
        # to the serial log (journal+console, spike 1's landmine
        # 3) for the boot script to grep. Forward reference to the
        # probe binding below — let is recursive.
        systemd.services.spike2-probe = {
          description = "Spike 210-2 overlay-store probes";
          after = [ "local-fs.target" ];
          wantedBy = [ "multi-user.target" ];
          serviceConfig = {
            Type = "oneshot";
            StandardOutput = "journal+console";
            StandardError = "journal+console";
          };
          unitConfig.ConditionPathIsMountPoint = "/nix/store";
          script = ''
            exec ${probe}
          '';
        };

        system.stateVersion = lib.trivial.release;
      };
  };

  cfg = nixos.config;
  toplevel = cfg.system.build.toplevel;
  regInfo = pkgs.closureInfo { rootPaths = [ toplevel ]; };

  # The overlay-store URI the probes use. `real` is the merged view
  # (the overlayfs mount at /nix/store); `lower-store` is the embedded
  # base as a plain local store rooted at its erofs mount; `upper-layer`
  # is the overlayfs upperdir on the store volume; the upper database
  # lives at ${state}/db, on the volume.
  overlayStoreUri = "local-overlay://?real=/nix/store&lower-store=local%3Froot%3D%2Fnix%2F.ro-store%26read-only%3Dtrue&upper-layer=/nix/.upper-volume/store&state=/nix/.upper-volume/nix-var&check-mount=false";
  # Decoded: lower-store=local?root=/nix/.ro-store&read-only=true — the
  # nested URI's own parameters ride percent-encoded inside the outer
  # query (nix decodes each value), because a bare & would split the
  # outer query. read-only=true stops the lower store from trying to
  # remount the erofs writable. check-mount=false: the initrd mounts
  # the overlay with /sysroot-prefixed layer paths, and the check
  # compares mount-option STRINGS (probe 1 prints the real mount line
  # as evidence instead).

  probe = pkgs.writeShellScript "spike2-probe" ''
    set -u
    OV='${overlayStoreUri}'
    # systemd service PATH is bare; use the absolute store paths.
    nix='${pkgs.nix}/bin/nix'
    nixstore='${pkgs.nix}/bin/nix-store'
    say() { printf 'PROBE %s\n' "$*"; }
    fail=0
    bad() { printf 'PROBE %s\n' "$*"; fail=1; }
    # The booted system, resolved at runtime: embedding the toplevel
    # at build time would recurse (this service is part of the
    # toplevel's own unit graph).
    TL=$(readlink -f /run/current-system)

    # 0. Topology diagnostics: how many times is the volume mounted,
    # and what does the upper layer hold?
    say "topology: $(grep -c ' /nix/.upper-volume ' /proc/mounts) volume mount(s): $(grep 'upper-volume' /proc/mounts | tr '\n' ';')"
    say "upper-layer-ls: $(ls /nix/.upper-volume/store 2>&1 | tr '\n' ' ')"

    # 1. The merged store is an overlayfs with exactly the built layers
    # (two mount lines exist: the rw overlay and stage 2's ro bind of it).
    m=$(grep ' /nix/store ' /proc/mounts || true)
    case "$m" in
      *"overlay"*) while read -r line; do say "mount: $line"; done <<< "$m" ;;
      *) bad "mount: MISSING ($m)" ;;
    esac

    # 2. Reads resolve through the lower database (no load, no daemon).
    if out=$($nix --store "$OV" path-info "$TL" 2>/dev/null); then
      say "lower-db-read: $out"
    else
      bad "lower-db-read: FAILED ($out)"
    fi

    marker=/nix/.upper-volume/spike2-marker
    if [ -f "$marker" ]; then
      # 3a. A path written by an earlier boot resolves from the upper
      # database alone (persistence across reboots).
      h=$(cat "$marker")
      if out=$($nix --store "$OV" path-info "$h" 2>/dev/null); then
        say "persistence: $out"
      else
        bad "persistence: FAILED ($out)"
      fi
    else
      # 3b. A store write lands ONLY in the upper layer and registers
      # only in the upper database.
      # stdout is the added path; a warning on stderr would corrupt it.
      # The added object may be a symlink (nix-store --add archives
      # /etc/hostname's symlink as-is), so existence, not -d, is the
      # check below.
      h=$($nixstore --store "$OV" --add /etc/hostname 2>/dev/null) || h=""
      case "$h" in
        /nix/store/*-*) ;;
        *)
          # Re-run with stderr for the message, then continue.
          bad "write: FAILED ($($nixstore --store "$OV" --add /etc/hostname 2>&1))"
          h=""
          ;;
      esac
      if [ -n "$h" ]; then
        if [ -e "/nix/.upper-volume/store/$(basename "$h")" ]; then
          say "write-upperdir: $h"
        else
          bad "write-upperdir: MISSING in upper layer ($h)"
        fi
        if [ -e "/nix/.ro-store/nix/store/$(basename "$h")" ]; then
          bad "not-in-lower: VIOLATION (path leaked into the erofs)"
        else
          say "not-in-lower: confirmed"
        fi
        if out=$($nix --store "$OV" path-info "$h" 2>/dev/null); then
          say "upper-db-registered: $out"
        else
          bad "upper-db-registered: FAILED ($out)"
        fi
        echo "$h" > "$marker"
      fi
    fi

    # 4. Registering a path the lower store already holds copies no
    # bytes: the upper layer stays byte-identical.
    before=$(ls /nix/.upper-volume/store | sort)
    if out=$($nix --store "$OV" copy --from 'local?root=/nix/.ro-store&read-only=true' "$TL" 2>&1); then
      after=$(ls /nix/.upper-volume/store | sort)
      if [ "$before" = "$after" ]; then
        say "lower-reg-no-copy: confirmed (the upper-db rows the gc later deletes are the registration evidence)"
      else
        bad "lower-reg-no-copy: COPIED (upper layer grew)"
      fi
    else
      bad "lower-reg-no-copy: FAILED ($out)"
    fi

    # 5. Garbage collection stays upper-scoped: a rooted upper path
    # survives, and the lower store is untouched.
    mkdir -p /nix/.upper-volume/nix-var/gcroots
    [ -e /nix/.upper-volume/nix-var/gcroots/spike2 ] \
      || ln -s "$(cat "$marker" 2>/dev/null || echo /nix/store)" \
        /nix/.upper-volume/nix-var/gcroots/spike2
    rooted=$(readlink -f /nix/.upper-volume/nix-var/gcroots/spike2 2>/dev/null || true)
    if out=$(timeout 120 $nix --store "$OV" store gc 2>&1); then
      if [ -n "$rooted" ] && $nix --store "$OV" path-info "$rooted" >/dev/null 2>&1; then
        say "gc-upper-scoped: rooted upper path survived ($rooted)"
      else
        bad "gc-upper-scoped: ran but dropped the rooted path ($rooted)"
      fi
      $nix --store "$OV" path-info "$TL" >/dev/null 2>&1 \
        && say "gc-lower-intact: toplevel still resolvable" \
        || bad "gc-lower-intact: FAILED"
    else
      bad "gc-upper-scoped: FAILED ($out)"
    fi

    if [ "$fail" = 0 ]; then
      printf 'SPIKE2-READY\n'
    else
      printf 'SPIKE2-FAILED\n'
      exit 1
    fi
  '';

  spike = pkgs.runCommand "msks-spike-210-2" { } ''
    set -eu
    mkdir -p "$out"
    cp -L "${cfg.system.build.kernel}/bzImage" "$out/vmlinux"
    cp -L "${cfg.system.build.initialRamdisk}/initrd" "$out/initrd"
    printf '%s\n' "${pkgs.lib.concatStringsSep " " cfg.microvm.kernelParams}" \
      > "$out/cmdline"
    printf '%s\n' "${toplevel}" > "$out/toplevel"
  '';

  # The embedded base: a complete local store — tree plus database —
  # built entirely sandboxed. load-db creates the SQLite database
  # inside $out from the closure's registration file (the same file
  # microvm.nix ships for boot-time loading; here it is baked in
  # ahead of time instead).
  baseStore =
    pkgs.runCommand "msks-spike-210-2-base"
      {
        __structuredAttrs = true;
        nativeBuildInputs = [ pkgs.nix ];
        unsafeDiscardReferences.out = true;
      }
      ''
        set -eu
        mkdir -p "$out/nix/store" "$out/nix/var/nix"
        while read -r p; do
          cp -a --reflink=auto "$p" "$out/nix/store/"
        done < ${regInfo}/store-paths
        NIX_REMOTE="local?root=$out" nix-store --load-db < ${regInfo}/registration
      '';

  # The immutable lower image: erofs over the base tree, so the erofs
  # root holds nix/store and nix/var — the exact layout a local store
  # rooted at /nix/.ro-store expects. The label is microvm.nix's
  # fixed one, so its /nix/.ro-store mount finds the disk unchanged.
  lowerImage =
    pkgs.runCommand "msks-spike-210-2-lower"
      {
        __structuredAttrs = true;
        nativeBuildInputs = [ pkgs.erofs-utils ];
        unsafeDiscardReferences.out = true;
      }
      ''
        set -eu
        # Root ownership is forced: the image packs whatever the build
        # sandbox produced, and the guest must not inherit build-user
        # ownership (microvm.nix's squashfs path passes --all-root for
        # the same reason).
        mkfs.erofs --force-uid=0 --force-gid=0 -T 0 -L nix-store \
          "$out" "${baseStore}"
      '';

  # The pristine store volume: ext4 carrying the (empty) overlayfs
  # upper layer (store/), the overlayfs workdir (work/), and the upper
  # store's state directory (nix-var/). The boot script copies this
  # once into its state dir and keeps it across runs — the volume IS
  # the appliance's persistence.
  upperVolume =
    pkgs.runCommand "msks-spike-210-2-upper"
      {
        __structuredAttrs = true;
        nativeBuildInputs = [ pkgs.e2fsprogs ];
        unsafeDiscardReferences.out = true;
      }
      ''
        set -eu
        mkdir -p root/store root/work root/nix-var
        truncate -s 512M "$out"
        mkfs.ext4 -F -L SPIKE2UPPER -d root "$out"
        # mkfs -d leaves the journal flagged for recovery; a read-only
        # stage-1 mount would abort on it. Settle the filesystem.
        e2fsck -fy "$out" >/dev/null 2>&1 || test $? -le 1
      '';

in
{
  inherit spike lowerImage upperVolume;
}
