# The client-side msks wheel build (#537): the same source and the
# same entry points as the daemon package (nix/msks-pkg.nix builds
# the one wheel; hatchling reads pyproject.toml), with the
# dependency closure the client subtree imports. The layering
# contract (CLIENT_ALLOWED in test_layering.py) holds the client to
# its own siblings, stdlib, third-party packages, and the shared
# leaves (agecrypto, configio, conformance_args, identity, spec),
# so the closure needs exactly those trees' third-party names:
# cryptography (agecrypto, and the client tree itself), httpx,
# pyyaml (configio), rich, textual, typer, websockets.
# test_pkg_mirror.py pins the dependency list against the tree's
# imports and the remove list against pyproject — the same drift
# guards the daemon package has (#203).
#
# The wheel installs every console script (msksd included): the
# split is the closure, not the entry points. The client output
# serves `msks` and `msks-term-popup`; a daemon started from it
# fails at import naming the first daemon-only module missing, so
# run msksd from the server output.
{
  lib,
  buildPythonPackage,
  hatchling,
  cryptography,
  httpx,
  pyyaml,
  rich,
  textual,
  typer,
  websockets,
}:

buildPythonPackage {
  pname = "msks";
  version = "0.1.0";
  pyproject = true;
  src = import ./source.nix { inherit lib; };

  build-system = [ hatchling ];

  dependencies = [
    cryptography
    httpx
    pyyaml
    rich
    textual
    typer
    websockets
  ];

  # The wheel's recorded requirements name the full set
  # (pyproject.toml is the one source); pythonRemoveDeps drops the
  # daemon-only names so the install keeps the client closure. The
  # hook matches by name prefix — the plain "uvicorn" entry drops
  # the whole bracketed `uvicorn[standard]` requirement line (a
  # bracketed entry would be a sed bracket expression and match
  # nothing). test_pkg_mirror.py pins the list against pyproject's
  # dependencies minus the client list.
  pythonRemoveDeps = [
    "aiosqlite"
    "alembic"
    "certifi"
    "fastapi"
    "litellm"
    "mitmproxy"
    "netfilterqueue"
    "pydantic"
    "sqlalchemy"
    "uvicorn"
  ];

  # No nix-side test run: the unit suite runs in the devenv shell
  # and CI, not in this build.
  doCheck = false;

  meta = {
    description = "msks client (CLI/TUI): workspaces on a running msksd, over its HTTP API";
    mainProgram = "msks";
  };
}
