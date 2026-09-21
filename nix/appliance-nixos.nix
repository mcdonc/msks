# The NixOS appliance build (#212): evaluates nix/appliance-config.nix
# (both store shapes live there) against the pinned nixpkgs and packs
# the boot artifacts the host run script consumes.
#
#   $out/vmlinux    - the nixpkgs kernel (bzImage; direct boot)
#   $out/initrd     - NixOS's generated initial ramdisk
#   $out/cmdline    - the full kernel command line (init= included;
#                     diagnostic convenience — the manifest is the
#                     contract the run script reads)
#   $out/toplevel   - the system toplevel store path, one line
#                     (same status: convenience, not the contract)
#   $out/state.ext4 - the blank persistent-state disk template (the
#                     same 40G sparse template the Debian appliance
#                     ships, the same capacity model)
#   $out/base-store.erofs   - deployed mode only: the system closure
#                     AND its nix database as an erofs image (spike
#                     2's recipe — the db baked in, no boot-time
#                     load-db pass)
#   $out/store-volume.ext4  - deployed mode only: the pristine ext4
#                     volume carrying the overlayfs upper layer, the
#                     workdir, and the upper store's state directory
#   $out/appliance-manifest.json - the artifact contract the run
#                     script reads (mode, network plan, store paths)
#
# Supported entry: the msks-appliance-build script evaluates this file
# with nixpkgs pinned to the devenv.lock revision (MSKS_GUEST_NIXPKGS
# via -I nixpkgs=). The MODE rides the environment the same way the
# config reads it: export MSKS_APPLIANCE_MODE=dev for the dev-shape
# artifacts; a plain build is the deployed shape.
{
  pkgs ? import <nixpkgs> {
    config = { };
    overlays = [ ];
  },
}:

let
  mode =
    let
      env = builtins.getEnv "MSKS_APPLIANCE_MODE";
    in
    if env != "" then env else "deployed";

  nixos = import (pkgs.path + "/nixos") {
    configuration = {
      imports = [ ./appliance-config.nix ];
    };
    # The module reads the mode from the same environment; passing it
    # through specialArgs would drift from the config's own default
    # logic, so the environment is the single channel.
  };

  cfg = nixos.config;
  toplevel = cfg.system.build.toplevel;

  # The workspace guest build: the default workspace image
  # (containerDisk archive) GC-rooted by this manifest and — in
  # deployed mode — packed into the erofs base, so the appliance is
  # self-contained: a bare workspace create works with nothing else
  # built, no host store in sight.
  guest = pkgs.callPackage ./guest-assets.nix { };
  defaultImage = "${guest.imageArchive}";

  # The closure list for the deployed base: the system plus the
  # default workspace image (a file, not a tree — it packs into the
  # store like any path).
  regInfo = pkgs.closureInfo {
    rootPaths = [
      toplevel
    ]
    ++ pkgs.lib.optionals (mode == "deployed") [ guest.imageArchive ];
  };

  # The embedded base: a complete local store — tree plus database —
  # built entirely sandboxed (spike 2's recipe, verbatim): load-db
  # creates the SQLite database inside $out from the closure's
  # registration file, baked in ahead of time instead of microvm.nix's
  # boot-time loading.
  baseStore =
    pkgs.runCommand "msks-appliance-nixos-base"
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
  # rooted at /nix/.ro-store expects. The label is microvm.nix's fixed
  # one, so the config's /nix/.ro-store mount finds the disk
  # unchanged. Root ownership forced: the guest must not inherit
  # build-user ownership.
  lowerImage =
    pkgs.runCommand "msks-appliance-nixos-lower"
      {
        __structuredAttrs = true;
        nativeBuildInputs = [ pkgs.erofs-utils ];
        unsafeDiscardReferences.out = true;
      }
      ''
        set -eu
        mkfs.erofs --force-uid=0 --force-gid=0 -T 0 -L nix-store \
          "$out" "${baseStore}"
      '';

  # The pristine store volume (spike 2's recipe): ext4 carrying the
  # (empty) overlayfs upper layer (store/), the workdir (work/), and
  # the upper store's state directory (nix-var/). 4G in production —
  # updates copy only missing paths (spike 3's measurements: a
  # package pull cost ~3.4 MiB), and the manifest records the size so
  # the host can grow it. e2fsck after mkfs -d: the journal is left
  # flagged for recovery and a read-only stage-1 mount would abort.
  storeVolume =
    pkgs.runCommand "msks-appliance-nixos-volume"
      {
        __structuredAttrs = true;
        nativeBuildInputs = [ pkgs.e2fsprogs ];
        unsafeDiscardReferences.out = true;
      }
      ''
        set -eu
        mkdir -p root/store root/work root/nix-var
        truncate -s 4G "$out"
        mkfs.ext4 -F -L MSKSSTORE -d root "$out"
        e2fsck -fy "$out" >/dev/null 2>&1 || test $? -le 1
      '';

  # The persistent-state template: the same 40G sparse blank the
  # Debian appliance ships (the capacity model's comments live in
  # appliance-image.nix): the run script seeds the state disk from it
  # once per install, and the guest's preparation unit converges
  # blank, foreign, and existing disks.
  stateDisk =
    pkgs.runCommand "msks-appliance-nixos-state"
      {
        nativeBuildInputs = [
          pkgs.e2fsprogs
          pkgs.fakeroot
        ];
        fakeEpoch = 1262304000;
      }
      ''
        set -eu
        mkdir -p "$out"
        truncate -s 40G "$out/state.ext4"
        mkdir -p stage/var/log/journal
        ln -s /run stage/var/run
        ln -s /run/lock stage/var/lock
        fakeroot -- sh -c '
          chown -R 0:0 stage
          E2FSPROGS_FAKE_TIME="$fakeEpoch" mke2fs -q -F -t ext4 -b 4096 -I 256 \
            -L msks-state \
            -E hash_seed=00000000-0000-0000-0000-000000000003 \
            -d stage \
            "$out/state.ext4"
        '
        E2FSPROGS_FAKE_TIME="$fakeEpoch" tune2fs -U 00000000-0000-0000-0000-000000000004 "$out/state.ext4" >/dev/null
      '';

  # The deployed-only fields merge as an attrset: the optionalString
  # spelling forces the string contexts through toJSON and drags the
  # erofs/volume builds into DEV's plan (minutes of build for bytes
  # dev never reads); the attrset branch keeps the fields ABSENT in
  # dev, and with them the derivations out of the plan entirely.
  manifest = pkgs.writeText "appliance-manifest.json" (
    builtins.toJSON (
      {
        mode = mode;
        kernel = "${cfg.system.build.kernel}/bzImage";
        initrd = "${cfg.system.build.initialRamdisk}/initrd";
        cmdline = pkgs.lib.concatStringsSep " " cfg.microvm.kernelParams;
        toplevel = "${toplevel}";
        stateDisk = "${stateDisk}/state.ext4";
        network = {
          address = "192.168.77.2";
          prefixLength = 24;
          gateway = "192.168.77.1";
        };
        msksd = "${cfg.msks.daemon}";
        vmm = "${pkgs.cloud-hypervisor}";
        defaultImage = defaultImage;
      }
      // (pkgs.lib.optionalAttrs (mode == "deployed") {
        baseStore = "${lowerImage}";
        storeVolume = "${storeVolume}";
      })
    )
  );
in
pkgs.runCommand "msks-appliance-nixos"
  {
    inherit
      manifest
      ;
    # The outputs referenced only by the manifest text: making them
    # build-input-style deps keeps them realized on the host (dev
    # mode serves them through the virtiofs share).
    deps = [
      toplevel
      guest
    ]
    ++ pkgs.lib.optionals (mode == "deployed") [
      lowerImage
    ];
  }
  ''
    set -eu
    mkdir -p "$out"
    cp -L "${cfg.system.build.kernel}/bzImage" "$out/vmlinux"
    cp -L "${cfg.system.build.initialRamdisk}/initrd" "$out/initrd"
    printf '%s\n' "${pkgs.lib.concatStringsSep " " cfg.microvm.kernelParams}" \
      > "$out/cmdline"
    printf '%s\n' "${toplevel}" > "$out/toplevel"
    cp -L "${manifest}" "$out/appliance-manifest.json"
    ${pkgs.lib.optionalString (mode == "deployed") ''
      cp -L "${lowerImage}" "$out/base-store.erofs"
    ''}
  ''
