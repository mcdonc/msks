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
TEXTUAL_PKG = REPO_ROOT / "nix" / "textual-pkg.nix"


def strip_comments(block: str) -> str:
    """Drop `# …` tails so a bracket or name inside a comment cannot
    confuse the parses below."""
    return "\n".join(line.split("#", 1)[0] for line in block.splitlines())


def dependency_names() -> set[str]:
    deps = tomllib.loads(PYPROJECT.read_text())["project"]["dependencies"]
    return {re.split(r"[><=~!;\[]", d)[0].strip().lower() for d in deps}


def version_tuple(text: str) -> tuple[int, ...]:
    """`"8.2.8"` → `(8, 2, 8)`: numeric compare, so `8.2.10` sorts
    above `8.2.9` where the strings compare below it."""
    return tuple(int(part) for part in text.split("."))


def nix_dependency_block() -> str:
    """The lines of msks-pkg.nix's dependencies list, sans brackets."""
    nix = strip_comments(MSKS_PKG.read_text())
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


def test_the_hand_pinned_textual_floor_covers_pyproject() -> None:
    """pyproject's textual floor stays within what textual-pkg.nix
    pins. textual is the one dependency this repo builds by hand from
    an exact wheel, so a floor bump in pyproject that the nix pin
    misses cannot fail the runtime check (8.2.9 required, 8.2.8
    shipped) and no smoke covers the TUI focus semantics the floor
    exists for — this test is that net."""
    specifier = next(
        d
        for d in tomllib.loads(PYPROJECT.read_text())["project"][
            "dependencies"
        ]
        if re.split(r"[><=~!;\[]", d)[0].strip().lower() == "textual"
    )
    floor = re.search(r">=\s*(\d+(?:\.\d+)*)", specifier)
    assert floor is not None, (
        f"pyproject's textual specifier {specifier!r} declares no "
        f">= floor for nix/textual-pkg.nix to cover"
    )
    pin = re.search(
        r'version = "([^"]+)"', strip_comments(TEXTUAL_PKG.read_text())
    )
    assert pin is not None, "nix/textual-pkg.nix lost its version pin"
    pin_v = version_tuple(pin.group(1))
    floor_v = version_tuple(floor.group(1))
    assert pin_v >= floor_v, (
        f"pyproject wants textual>={floor.group(1)}; "
        f"nix/textual-pkg.nix pins {pin.group(1)} — bump the pin "
        f"(wheel URL, hash, and propagatedBuildInputs from the new "
        f"METADATA) or the TUI runs on a textual older than its "
        f"declared floor"
    )
