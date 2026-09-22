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
  guest = pkgs.callPackage ./guest-assets.nix { };
in
{
  inherit guest;

  # The workspace image archive on its own (#141): the bare-host dev
  # daemon's default image — same derivation the `guest` build embeds,
  # buildable without the kernel/rootfs copies that shape serves.
  image-archive = guest.imageArchive;
}
