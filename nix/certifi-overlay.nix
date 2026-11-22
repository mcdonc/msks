# The pkgs-level overlay that puts nix/certifi-pkg.nix's build under
# the `certifi` name in the python 3.14 package set (#606) — the one
# set the msksd wheel builds against. httpx, mitmproxy, and every
# other dependent in that set resolve this one derivation instead of
# nixpkgs' older one, so the closure carries a single certifi and
# both the runtime deps check (the wheel's >=2026.10.23 floor) and
# catch-conflicts pass.
#
# The composition decides whether to apply it (#785), reading the
# floor pyproject.toml declares through nix/certifi-floor.nix: a
# set whose own certifi already clears the floor skips this overlay
# and every dependent comes from the binary cache instead of
# rebuilding around the swapped wheel. The overlay itself stays
# unconditional on purpose — reading the set's certifi version from
# inside the overlay body recurses on the pkgs fixpoint, so the
# condition cannot live here.
#
# Scoped to python314 through packageOverrides on purpose: the
# set-wide pythonPackagesExtensions mechanism would replace certifi
# in every python version's set, and the doc-tool closures that ride
# other versions (python3.13's sphinx, behind libtiff's docs build)
# would rebuild off the binary cache — a C-chain rebuild whose
# sandbox test failures have nothing to do with msks. The override
# composes with whatever packageOverrides the host nixpkgs already
# carries, and whatever certifi the host's set holds — older or
# newer — the pinned wheel is the one every python 3.14 dependent
# resolves.
#
# Applied by flake.nix (the nix build path), devenv.nix (the release
# outputs' twin path), and module.nix (a consuming host's own
# nixpkgs) — each only while its set's own certifi sits below the
# floor.
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
