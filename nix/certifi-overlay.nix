# The pkgs-level overlay that puts nix/certifi-pkg.nix's build under
# the `certifi` name in the python 3.14 package set (#606) — the one
# set the msksd and client wheels build against. httpx, mitmproxy,
# and every other dependent in that set resolve this one derivation
# instead of nixpkgs' older one, so the closure carries a single
# certifi and both the runtime deps check (the wheel's >=2026.10.23
# floor) and catch-conflicts pass.
#
# Scoped to python314 through packageOverrides on purpose: the
# set-wide pythonPackagesExtensions mechanism would replace certifi
# in every python version's set, and the doc-tool closures that ride
# other versions (python3.13's sphinx, behind libtiff's docs build)
# would rebuild off the binary cache — a C-chain rebuild whose
# sandbox test failures have nothing to do with msks. The override
# composes with whatever packageOverrides the host nixpkgs already
# carries.
#
# Applied by flake.nix (the nix build path), devenv.nix (the release
# outputs' twin path), and module.nix (a consuming host's own
# nixpkgs).
final: prev: {
  python314 = prev.python314.override (old: {
    packageOverrides =
      prev.lib.composeExtensions (old.packageOverrides or (_: _: { }))
        (
          python-final: python-prev: {
            certifi = python-final.callPackage ./certifi-pkg.nix { };
          }
        );
  });
}
