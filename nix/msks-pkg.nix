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
  # The platform CA bundle (#424): the interceptor's upstream
  # verification loads the probe CA appended to certifi's platform
  # roots. Resolved through nix/certifi-overlay.nix while a set's
  # own certifi sits below the pyproject floor (#606, #785).
  certifi,
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
  # The listings' and help's measured columns (#271): textual
  # already ships it, and the CLI imports it directly on every
  # path — nixpkgs' own rich serves the closure (the pyproject
  # floor is >=13; test_pkg_mirror.py keeps the name set honest).
  rich,
  # The client's command tree (#315): the typer app renders the
  # help screens and parses the flags (nixpkgs' own typer; the
  # vendored-click import in cli falls back to the plain click
  # when a typer without one serves the closure).
  typer,
  # The LLM router's multi-provider mode (#259), lazily imported
  # (the passthrough mode runs without it), pinned to the exact
  # wheel uv.lock vets — the hand-built package records the
  # range survey and the bump workflow (nix/litellm-pkg.nix).
  litellm,
}:

let
  src = import ./source.nix { inherit lib; };
in
buildPythonPackage {
  pname = "msks";
  version = "0.10.2";
  pyproject = true;
  inherit src;

  build-system = [ hatchling ];

  dependencies = [
    fastapi
    httpx
    certifi
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
    rich
    typer
    litellm
  ];

  # No nix-side test run: the unit suite runs in the devenv shell
  # and CI, not in this build.
  doCheck = false;

  # uvicorn[standard] extra: the plain nixpkgs uvicorn package
  # serves the speedups here. The remove entry matches by name
  # prefix — the plain "uvicorn" drops the whole bracketed
  # requirement line (a bracketed entry would be a sed bracket
  # expression and match nothing).
  #
  # The GUI dependencies (#1023) — pyside6-essentials and the
  # exactly-pinned pyghostty dev snapshot — are stripped the
  # same way: the nix package is the daemon's closure
  # (mainProgram msksd), and the pinned nixpkgs carries neither
  # name (pyghostty only ships as the wheel the base install
  # pins). The GUI stays a pip surface on a nix host: install
  # the wheel (`pip install msks`) for `msks gui`. The entries
  # are lowercase because the match is case-sensitive: the
  # remove hook's sed compares against the wheel METADATA, and
  # hatchling normalizes every dependency name to lower case
  # there (PEP 503) — a mixed-case entry matches nothing and
  # the runtime-deps check fails the build.
  pythonRemoveDeps = [
    "uvicorn"
    "pyside6-essentials"
    "pyghostty"
  ];

  meta = {
    description = "Microvm workspace daemon (klangkd analogue on cloud-hypervisor)";
    mainProgram = "msksd";
    license = lib.licenses.mit;
  };
}
