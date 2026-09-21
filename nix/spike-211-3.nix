# Spike 211-3 for #211/#205: first-boot artifacts for the deployed
# update path. The guest configuration is nix/spike-211-3-config.nix —
# the same module nixos-rebuild evaluates for updates — and this
# wrapper packages its initial generation the way the appliance
# manifest would ship it: kernel, initrd, cmdline, the erofs base
# store with its database, and the pristine store volume.
#
# Run scripts/spike-211-3.sh from a devenv shell.
{
  pkgs ? import <nixpkgs> { },
}:

let
  lib = pkgs.lib;

  nixos = import (pkgs.path + "/nixos") {
    configuration = import ./spike-211-3-config.nix;
  };

  cfg = nixos.config;
  toplevel = cfg.system.build.toplevel;
  regInfo = pkgs.closureInfo { rootPaths = [ toplevel ]; };

  spike = pkgs.runCommand "msks-spike-211-3" { } ''
    set -eu
    mkdir -p "$out"
    cp -L "${cfg.system.build.kernel}/bzImage" "$out/vmlinux"
    cp -L "${cfg.system.build.initialRamdisk}/initrd" "$out/initrd"
    printf '%s\n' "${pkgs.lib.concatStringsSep " " cfg.microvm.kernelParams}" \
      > "$out/cmdline"
    printf '%s\n' "${toplevel}" > "$out/toplevel"
  '';

  # The embedded base: complete local store — tree plus database —
  # built sandboxed (spike 2's recipe).
  baseStore =
    pkgs.runCommand "msks-spike-211-3-base"
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

  # The immutable lower image (microvm.nix's fixed label).
  lowerImage =
    pkgs.runCommand "msks-spike-211-3-lower"
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

  # The pristine store volume (spike 2's recipe, settled with e2fsck).
  upperVolume =
    pkgs.runCommand "msks-spike-211-3-upper"
      {
        __structuredAttrs = true;
        nativeBuildInputs = [ pkgs.e2fsprogs ];
        unsafeDiscardReferences.out = true;
      }
      ''
        set -eu
        mkdir -p root/store root/work root/nix-var
        truncate -s 2G "$out"
        mkfs.ext4 -F -L SPIKE211UPPER -d root "$out"
        e2fsck -fy "$out" >/dev/null 2>&1 || test $? -le 1
      '';

in
{
  inherit spike lowerImage upperVolume;
}
