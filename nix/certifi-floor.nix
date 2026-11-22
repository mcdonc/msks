# The one definition of the certifi floor (#785): pyproject.toml's
# `certifi>=X.Y.Z` line, read at eval time. Both halves of the
# overlay machinery read it here — nix/certifi-overlay.nix applies
# the pinned wheel only while a set's own certifi sits below the
# floor, and nix/certifi-gate.nix refuses a composed set that
# misses it — so the nix side cannot drift from the floor the
# wheel's runtime-deps check enforces.
rec {
  # The floor pyproject declares, e.g. "2026.10.23".
  version =
    let
      floorLines = builtins.filter (
        line:
        builtins.isString line && builtins.match ''.*"certifi>=[0-9.]+".*'' line != null
      ) (builtins.split "\n" (builtins.readFile ../pyproject.toml));
    in
    if floorLines == [ ] then
      throw "pyproject.toml declares no certifi>= floor — nix/certifi-floor.nix cannot read it"
    else
      builtins.head (
        builtins.match ''.*"certifi>=([0-9.]+)".*'' (builtins.head floorLines)
      );

  # The python 3.14 certifi a pkgs set carries — "0" when the set
  # has no certifi at all, so the gate refuses the set instead of
  # erroring on the missing attribute.
  certifiVersion = pkgs: pkgs.python314.pkgs.certifi.version or "0";

  # True when the set's own certifi clears the floor.
  metBy = pkgs: builtins.compareVersions (certifiVersion pkgs) version >= 0;
}
