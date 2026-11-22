# The eval-time gate over a composed pkgs set (#785): its python
# 3.14 certifi must clear the floor pyproject.toml declares
# (nix/certifi-floor.nix reads it there). A set below the floor
# fails the build deep in the runtime-deps check; this gate names
# the cause at eval time instead. The two usual causes: a
# composition dropped nix/certifi-overlay.nix while its nixpkgs
# still sits below the floor, or nix/certifi-pkg.nix pins a wheel
# below the floor the overlay applies it to cover.
#
# Applied by flake.nix, devenv.nix, and nix/module.nix — the same
# three compositions that apply the overlay.
pkgs:
let
  floor = import ./certifi-floor.nix;
  version = floor.certifiVersion pkgs;
in
if floor.metBy pkgs then
  pkgs
else
  abort ''
    msks: this build's python 3.14 set carries certifi ${version},
    below pyproject.toml's certifi>=${floor.version} floor. The
    build would fail deep in the runtime-deps check; the two usual
    causes:
    - the composition dropped nix/certifi-overlay.nix while its
      nixpkgs still sits below the floor — restore it where the
      composition lives (flake.nix: overlays = [...]; devenv.nix
      and nix/module.nix: pkgs.extend (import ...));
    - nix/certifi-pkg.nix pins a wheel below the floor — bump it
      (wheel URL and hash from the new uv.lock entry).
  ''
