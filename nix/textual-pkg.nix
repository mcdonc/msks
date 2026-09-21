# The textual TUI framework for the appliance closure (#195/#203).
#
# nixpkgs (nixos-26.05) carries 8.2.6, below the >=8.2.8 floor the
# consent decider TUI declares: the ListView focus semantics the TUI
# depends on are pinned at 8.2.8 (wholesale rebuilds mount a fresh
# list and set focus after mount — an 8.2.6 ListView drops that
# focus). Built here from the PyPI wheel — pure python, no compiled
# extensions — against nixpkgs' own rich/pygments/… so the daemon
# closure stays on the platform's package set wherever the floor
# allows it.
#
# Bump workflow: bump `version` and swap `src.url`/`hash` to the
# new wheel uv.lock pins (nix prints the real hash on mismatch),
# then diff the wheel's METADATA Requires-Dist against
# propagatedBuildInputs — a base entry the new wheel adds needs a
# hand-edit here, or the runtime check fails only at appliance
# build time (#203's blast radius). test_appliance_pkg.py fails
# when pyproject's floor moves past this pin.
{
  lib,
  buildPythonPackage,
  fetchurl,

  # Runtime dependencies, mirroring textual 8.2.8's wheel metadata
  # (the `syntax` tree-sitter extra is not used and stays off):
  markdown-it-py,
  linkify-it-py,
  mdit-py-plugins,
  platformdirs,
  pygments,
  rich,
  typing-extensions,
}:

buildPythonPackage rec {
  pname = "textual";
  version = "8.2.8";
  format = "wheel";

  # The exact wheel uv.lock pins (hash and all), fetched directly:
  # fetchPypi's JSON-API file pick mis-fires on this release and 404s.
  src = fetchurl {
    url = "https://files.pythonhosted.org/packages/fb/be/35261223d9416a0751cdff1c7b4a6f881387218a12d439fe22fefebc8c04/textual-8.2.8-py3-none-any.whl";
    hash = "sha256-JnN1/UAtyNmBRXIS76cfDjNl/Re7oUS6m7PtdWPLN0o=";
  };

  # markdown-it-py rides along for its [linkify] extra; linkify-it-py
  # is that extra's payload, listed directly so the runtime check
  # resolves `markdown-it-py[linkify]` either way it looks it up.
  propagatedBuildInputs = [
    markdown-it-py
    linkify-it-py
    mdit-py-plugins
    platformdirs
    pygments
    rich
    typing-extensions
  ];

  pythonImportsCheck = [ "textual" ];

  meta = {
    description = "TUI framework used by msks' consent decider screen";
    homepage = "https://github.com/Textualize/textual";
    license = lib.licenses.mit;
  };
}
