# The msks python package built by nixpkgs' python machinery (#10):
# the daemon closure as store paths, resolved against the same
# pinned nixpkgs that builds the guest assets.
#
# Version pinning is looser than uv.lock (nixpkgs carries its own
# fastapi/uvicorn/sqlalchemy minor versions within the >= floors of
# pyproject.toml); test_pkg_mirror.py pins the dependency NAME set
# against pyproject, so a missing dependency fails a test, not a
# build. Nothing boots the nix-built daemon in CI today.
{
  lib,
  buildPythonPackage,
  hatchling,
  # Runtime dependencies, mirroring [project.dependencies]:
  fastapi,
  httpx,
  pydantic,
  pyyaml,
  sqlalchemy,
  aiosqlite,
  uvicorn,
  cryptography,
  alembic,
  websockets,
  # The egress interceptor's engine (#199), straight from the
  # pinned nixpkgs (12.2.3 — the version the #194 spike ran);
  # test_pkg_mirror.py keeps the name set honest against
  # pyproject.toml.
  mitmproxy,
  # The consent decider TUI framework (#195), pinned to the
  # >=8.2.8 floor pyproject declares — nixpkgs' 8.2.6 predates the
  # ListView focus semantics it relies on (nix/textual-pkg.nix).
  textual,
  # The NFQUEUE binding (#69), built from the sdist against
  # nixpkgs' libnetfilter_queue (nix/netfilterqueue-pkg.nix).
  netfilterqueue,
  # The LLM router's multi-provider mode (#259), lazily imported
  # (the passthrough mode runs without it).
  litellm,
}:

let
  # Only what hatchling reads: pyproject context at the root plus the
  # package tree. Keeps .devenv/worktree noise out of the hash
  # so unrelated edits cannot rebuild the package closure.
  src = lib.cleanSourceWith {
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
  };
in
buildPythonPackage {
  pname = "msks";
  version = "0.1.0";
  pyproject = true;
  inherit src;

  build-system = [ hatchling ];

  dependencies = [
    fastapi
    httpx
    pydantic
    pyyaml
    sqlalchemy
    aiosqlite
    uvicorn
    cryptography
    alembic
    websockets
    mitmproxy
    textual
    netfilterqueue
    litellm
  ];

  # No nix-side test run: the unit suite runs in the devenv shell
  # and CI, not in this build.
  doCheck = false;

  pythonRemoveDeps = [
    # uvicorn[standard] extra: the speedups resolve via the plain
    # nixpkgs uvicorn package here.
    "uvicorn[standard]"
  ];

  meta = {
    description = "Microvm workspace daemon (klangkd analogue on cloud-hypervisor)";
    mainProgram = "msksd";
  };
}
