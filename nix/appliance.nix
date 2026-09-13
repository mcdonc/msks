# Entry point for the msksd appliance builds (#10).
#
# Supported entry: the devenv tasks (`msks:appliance-build`) evaluate
# this file with nixpkgs pinned to the devenv.lock revision. A bare
# `nix-build nix/appliance.nix -A appliance` also works and falls back
# to `<nixpkgs>` from NIX_PATH.
#
# No NixOS anywhere (issue #10, revised scope): the appliance is built
# exactly like the workspace guest — pure derivations from the pinned
# nixpkgs, direct kernel boot, an ext4 rootfs whose init is a shell
# script. The host OS is irrelevant beyond having nix + KVM.
{
  pkgs ? import <nixpkgs> { config = { }; overlays = [ ]; },
}:

let
  appliance = pkgs.callPackage ./appliance-image.nix { };
in
{
  inherit appliance;
}
