# The filtered source the msks wheel build compiles from (#537,
# factored out of msks-pkg.nix; nix/module.nix's own callPackage
# resolves the same tree, so the two builds cannot drift to two
# different source shapes).
#
# Only what hatchling reads: pyproject context at the root plus the
# package tree. Keeps .devenv/worktree noise out of the hash
# so unrelated edits cannot rebuild the package closures.
{ lib }:

lib.cleanSourceWith {
  src = ./..;
  filter =
    path: type:
    let
      rel = lib.removePrefix (toString ./.. + "/") (toString path);
    in
    # Directories prune their whole subtree when filtered out, so
    # every ancestor of the package tree must pass too.
    rel == "pyproject.toml"
    || rel == "README.md"
    || (type == "directory" && (rel == "src" || rel == "src/msks"))
    || (
      lib.hasPrefix "src/msks/msks" rel
      && !lib.hasSuffix "__pycache__" rel
      && !lib.hasSuffix ".pyc" rel
    );
}
