# Entry point for the msks guest-asset builds (#5).
#
# Supported entry: the msks-build-guest script evaluates this file
# with nixpkgs pinned to the devenv.lock revision, so the guest
# toolchain matches the dev shell exactly. A bare
# `nix-build nix/guest.nix -A guest` also works and falls back to
# `<nixpkgs>` from NIX_PATH.
#
# Overlays and config are pinned explicitly so a host's channels or
# nixpkgs-config.nix cannot leak into the guest build.
{
  pkgs ? import <nixpkgs> {
    config = { };
    overlays = [ ];
  },
}:

let
  # The arch attrs (#637): the default follows the evaluating
  # host (an aarch64 dev host builds arm64 guests from the bare
  # names); the pinned attrs serve the channel legs and any host
  # building the other arch deliberately (the Debian flavor is
  # fetch-and-pack, so cross-building its artifacts works; the
  # NixOS closure wants its native runner — #638's legs).
  guest = pkgs.callPackage ./guest-debian.nix { };
  guest-x86_64 = pkgs.callPackage ./guest-debian.nix { arch = "x86_64"; };
  guest-aarch64 = pkgs.callPackage ./guest-debian.nix { arch = "aarch64"; };
  guest-fedora = pkgs.callPackage ./guest-fedora.nix { };
  guest-fedora-x86_64 = pkgs.callPackage ./guest-fedora.nix { arch = "x86_64"; };
  guest-fedora-aarch64 = pkgs.callPackage ./guest-fedora.nix {
    arch = "aarch64";
  };
  guest-nixos = pkgs.callPackage ./guest-nixos.nix { };
  guest-nixos-x86_64 = pkgs.callPackage ./guest-nixos.nix {
    system = "x86_64-linux";
  };
  guest-nixos-aarch64 = pkgs.callPackage ./guest-nixos.nix {
    system = "aarch64-linux";
  };
in
{
  inherit guest;

  # The pinned-arch Debian guests (#637).
  inherit guest-x86_64 guest-aarch64;

  # The Fedora 44 workspace guest (#651): same output surface and
  # archive contract as the Debian `guest`, built from Fedora's
  # Generic cloud image (btrfs extraction legs; see
  # guest-fedora.nix).
  inherit guest-fedora;

  # The pinned-arch Fedora guests (#651, #637).
  inherit guest-fedora-x86_64 guest-fedora-aarch64;

  # The NixOS workspace guest (#250): same output surface and
  # archive contract as the Debian `guest`, built from a NixOS
  # system evaluated against the same pinned nixpkgs.
  inherit guest-nixos;

  # The pinned-arch NixOS guests (#637): the aarch64 one carries
  # the 16K-page kernel variant (see guest-nixos.nix).
  inherit guest-nixos-x86_64 guest-nixos-aarch64;

  # The Debian workspace image archive on its own (#141): the
  # bare-host dev daemon's default image — same derivation the
  # `guest` build embeds, buildable without the kernel/rootfs
  # copies that shape serves.
  image-archive-debian = guest.imageArchive;
  image-archive-debian-x86_64 = guest-x86_64.imageArchive;
  image-archive-debian-aarch64 = guest-aarch64.imageArchive;

  # The Fedora image archive on its own (#651).
  image-archive-fedora = guest-fedora.imageArchive;
  image-archive-fedora-x86_64 = guest-fedora-x86_64.imageArchive;
  image-archive-fedora-aarch64 = guest-fedora-aarch64.imageArchive;

  # The NixOS image archive on its own (#250).
  image-archive-nixos = guest-nixos.imageArchive;
  image-archive-nixos-x86_64 = guest-nixos-x86_64.imageArchive;
  image-archive-nixos-aarch64 = guest-nixos-aarch64.imageArchive;
}
