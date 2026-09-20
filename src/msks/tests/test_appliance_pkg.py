"""Guards over the appliance closure's nix mirror (#203).

`nix/msks-pkg.nix` hand-mirrors `[project.dependencies]`; when #196
added textual the mirror drifted and the failure surfaced only as the
supervised appliance's asset build restart-looping (the
pythonRuntimeDepsCheckHook refuses the wheel). These checks compare
the two lists by name so the next dependency addition fails a test
instead of the appliance boot.
"""

import re
import tomllib
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
PYPROJECT = REPO_ROOT / "pyproject.toml"
MSKS_PKG = REPO_ROOT / "nix" / "msks-pkg.nix"


def dependency_names() -> set[str]:
    deps = tomllib.loads(PYPROJECT.read_text())["project"]["dependencies"]
    return {re.split(r"[><=!;\[]", d)[0].strip().lower() for d in deps}


def nix_dependency_block() -> str:
    """The lines of msks-pkg.nix's dependencies list, sans brackets."""
    nix = MSKS_PKG.read_text()
    start = nix.index("dependencies = [")
    body = nix[start + len("dependencies = [") : nix.index("]", start)]
    return body


def test_every_pyproject_dependency_is_in_the_nix_package() -> None:
    """Each [project.dependencies] name appears in msks-pkg.nix's
    dependencies list — the mirror the appliance closure builds from."""
    have = {
        line.strip()
        for line in nix_dependency_block().splitlines()
        if line.strip() and not line.strip().startswith("#")
    }
    missing = dependency_names() - have
    assert not missing, (
        f"pyproject dependencies missing from nix/msks-pkg.nix: "
        f"{sorted(missing)} — the appliance build fails its runtime "
        f"deps check without them (#203)"
    )


def test_no_extra_nix_dependencies_beyond_pyproject() -> None:
    """The nix list carries nothing pyproject does not name, so the
    mirror stays a mirror and the two cannot drift apart silently."""
    have = {
        line.strip()
        for line in nix_dependency_block().splitlines()
        if line.strip() and not line.strip().startswith("#")
    }
    extra = have - dependency_names()
    assert not extra, (
        f"nix/msks-pkg.nix dependencies pyproject does not declare: "
        f"{sorted(extra)}"
    )
