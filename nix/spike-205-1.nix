# Spike 1 for #205: stock NixOS booted by cloud-hypervisor with
# /nix/store arriving over a read-only virtiofs share of the host's
# store — the dev-mode store shape decided on the issue.
#
# The evaluation imports microvm.nix's guest module (pinned rev; the
# recorded decision: spike against it directly, lift the wiring for
# the production config). The module supplies the ro-store share
# mount (/nix/.ro-store + a ro bind at /nix/store, both
# neededForBoot), the virtio initrd modules, and the
# init=<toplevel>/init kernel param. Everything else is a stock
# minimal NixOS on a tmpfs root: no disk, no state, no sshd — the
# spike measures the boot path itself.
#
# Outputs (attr `spike`):
#   vmlinux   - the nixpkgs kernel (bzImage)
#   initrd    - NixOS's generated initial ramdisk
#   cmdline   - the full kernel command line (init= included)
#   toplevel  - the system toplevel store path, one line
#
# Boot shape (scripts/spike-205-1.sh): cloud-hypervisor, one virtiofs
# fs device tagged ro-store, serial to a file, no disks.
{
  pkgs ? import <nixpkgs> {
    config = { };
    overlays = [ ];
  },
}:

let
  # Pinned master at spike time; the flake's guest module is imported
  # against our own nixpkgs pin (MSKS_GUEST_NIXPKGS / -I nixpkgs=).
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
          # The host store serves everything; nothing is packed to disk.
          storeOnDisk = false;
          writableStoreOverlay = null;
          # microvm.nix registers the system closure into a fresh
          # tmpfs nix db at every boot by default (regInfo= on the
          # cmdline + a `nix-store --load-db` step in stage 2). The
          # spike measures the shape WITHOUT any database, so the
          # registration stays off — the first measurements silently
          # included a 4491-path load-db pass (landmine 5 in the doc).
          registerClosure = false;
          shares = [
            {
              proto = "virtiofs";
              tag = "ro-store";
              source = "/nix/store";
              mountPoint = "/nix/.ro-store";
            }
          ];
          volumes = [ ];
          interfaces = [ ];
        };

        # Serial console, for boot status on the serial log. The
        # spike's success marker below is the real signal; serial runs
        # in File mode, so there is no input path to poke through
        # interactively.
        boot.kernelParams = [ "console=ttyS0" ];

        # The spike's success marker: one line on the serial console
        # after multi-user.target is reached.
        systemd.services.spike-ready = {
          wantedBy = [ "multi-user.target" ];
          after = [ "multi-user.target" ];
          script = ''
            echo "SPIKE1-READY multi-user.target reached"
          '';
          serviceConfig = {
            Type = "oneshot";
            RemainAfterExit = true;
            StandardOutput = "journal+console";
            StandardError = "journal+console";
          };
        };

        # A lean closure: no docs, no sshd, and no nix at all — the
        # store is read-only and nothing inside manages it (this also
        # gates microvm.nix's regInfo cmdline parameter; see the
        # registerClosure comment above).
        documentation.enable = false;
        nix.enable = false;
        services.openssh.enable = false;

        system.stateVersion = lib.trivial.release;
      };
  };

  cfg = nixos.config;
  toplevel = cfg.system.build.toplevel;

  # The artifact set the boot script consumes, mirroring the
  # appliance manifest's shape (kernel, initrd, cmdline) without the
  # disks a diskless spike does not have.
  spike = pkgs.runCommand "msks-spike-205-1" { } ''
    set -eu
    mkdir -p "$out"
    cp -L "${cfg.system.build.kernel}/bzImage" "$out/vmlinux"
    cp -L "${cfg.system.build.initialRamdisk}/initrd" "$out/initrd"
    # boot.kernelParams already carries console=ttyS0 and
    # microvm.kernelParams includes boot.kernelParams, so no
    # append is needed here.
    printf '%s\n' "${pkgs.lib.concatStringsSep " " cfg.microvm.kernelParams}" \
      > "$out/cmdline"
    printf '%s\n' "${toplevel}" > "$out/toplevel"
  '';
in
{
  inherit spike toplevel;
}
