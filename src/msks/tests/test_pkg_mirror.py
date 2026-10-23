"""Guards over the msks wheel builds' nix mirrors (#203, #537).

`nix/msks-pkg.nix` hand-mirrors `[project.dependencies]`; when #196
added textual the mirror drifted and the failure surfaced only at
package build time (the pythonRuntimeDepsCheckHook refuses the
wheel). These checks compare the two lists by name so the next
dependency addition fails a test instead of a build.

`nix/msks-client-pkg.nix` mirrors the subset the client closure
imports (#537): the same wheel, the client-only dependency list.
Its guards walk the client tree and the CLIENT_ALLOWED shared
leaves — the same contract test_layering holds — so a widening
allowlist or a new client import fails a test when the nix list
misses it; the remove list is pinned against pyproject's
full set, and the two locks are pinned to each other so the
flake/devenv same-path claim cannot rot silently.
"""

import ast
import json
import re
import sys
import tomllib
from pathlib import Path

from test_layering import CLIENT_ALLOWED

REPO_ROOT = Path(__file__).resolve().parents[3]
PYPROJECT = REPO_ROOT / "pyproject.toml"
PKG_NIX = REPO_ROOT / "nix" / "msks-pkg.nix"
CLIENT_PKG_NIX = REPO_ROOT / "nix" / "msks-client-pkg.nix"
TEXTUAL_PKG = REPO_ROOT / "nix" / "textual-pkg.nix"
CERTIFI_PKG = REPO_ROOT / "nix" / "certifi-pkg.nix"
DEVENV_LOCK = REPO_ROOT / "devenv.lock"
FLAKE_LOCK = REPO_ROOT / "flake.lock"
PKG_ROOT = REPO_ROOT / "src" / "msks" / "msks"

# Import names whose distribution name differs: pyproject and the
# nix dependency lists carry the distribution name.
IMPORT_TO_DIST = {"yaml": "pyyaml"}


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


def nix_dependency_block(nix_path: Path) -> str:
    """The lines of a package file's dependencies list, sans brackets."""
    nix = strip_comments(nix_path.read_text())
    start = nix.index("dependencies = [")
    body = nix[start + len("dependencies = [") : nix.index("]", start)]
    return body


def nix_list_names(nix_path: Path, attribute: str) -> set[str]:
    """The names an attribute's bracketed list in a package file
    carries (dependencies, pythonRemoveDeps, …)."""
    nix = strip_comments(nix_path.read_text())
    start = nix.index(f"{attribute} = [")
    body = nix[start + len(f"{attribute} = [") : nix.index("]", start)]
    return {
        line.strip().strip('"') for line in body.splitlines() if line.strip()
    }


def nix_dependency_names(nix_path: Path) -> set[str]:
    """The dependency names a package file's nix list carries."""
    return {
        line.strip()
        for line in nix_dependency_block(nix_path).splitlines()
        if line.strip()
    }


def client_subtree_imports() -> set[str]:
    """Third-party distribution names the client closure imports.

    Walks the client tree plus every CLIENT_ALLOWED shared leaf —
    the same contract test_layering holds — so widening the
    allowlist widens this walk with it. Stdlib names and the msks
    package itself drop out; `yaml` maps to its distribution name.
    """
    files: list[Path] = sorted((PKG_ROOT / "client").rglob("*.py"))
    for leaf in sorted(CLIENT_ALLOWED):
        path = PKG_ROOT / leaf
        if path.is_dir():
            files.extend(sorted(path.rglob("*.py")))
        else:
            files.append(path.with_suffix(".py"))
    names: set[str] = set()
    for path in files:
        for node in ast.walk(ast.parse(path.read_text())):
            mods: list[str] = []
            if isinstance(node, ast.Import):
                mods = [alias.name for alias in node.names]
            elif (
                isinstance(node, ast.ImportFrom)
                and node.module
                and node.level == 0
            ):
                mods = [node.module]
            names.update(mod.split(".")[0] for mod in mods)
    return {
        IMPORT_TO_DIST.get(name, name)
        for name in names
        if name != "msks" and name not in sys.stdlib_module_names
    }


def test_every_pyproject_dependency_is_in_the_nix_package() -> None:
    """Each [project.dependencies] name appears in msks-pkg.nix's
    dependencies list — the mirror the package closure builds from."""
    have = nix_dependency_names(PKG_NIX)
    missing = dependency_names() - have
    assert not missing, (
        f"pyproject dependencies missing from nix/msks-pkg.nix: "
        f"{sorted(missing)} — the package build fails its runtime "
        f"deps check without them (#203)"
    )


def test_no_extra_nix_dependencies_beyond_pyproject() -> None:
    """The nix list carries nothing pyproject does not name, so the
    mirror stays a mirror and the two cannot drift apart silently."""
    have = nix_dependency_names(PKG_NIX)
    extra = have - dependency_names()
    assert not extra, (
        f"nix/msks-pkg.nix dependencies pyproject does not declare: "
        f"{sorted(extra)}"
    )


def test_client_nix_deps_cover_the_client_imports() -> None:
    """Every pyproject-declared package the client closure imports
    appears in msks-client-pkg.nix's dependencies — the wheel the
    client output builds and runs; a missing name is an ImportError
    at first use, not a build failure (#537). Names pyproject does
    not declare (click, via typer's own dependency tree and a
    fallback import for older typers) stay outside the mirror,
    the same rule the daemon package's mirror follows."""
    wanted = client_subtree_imports() & dependency_names()
    missing = wanted - nix_dependency_names(CLIENT_PKG_NIX)
    assert not missing, (
        f"client-closure imports missing from nix/msks-client-pkg.nix: "
        f"{sorted(missing)} — the client output's wheel cannot import "
        f"them (#537)"
    )


def test_client_nix_deps_stay_within_pyproject() -> None:
    """The client nix list carries nothing pyproject does not name:
    the client closure is a subset of the one wheel's dependency
    set, picked by name."""
    extra = nix_dependency_names(CLIENT_PKG_NIX) - dependency_names()
    assert not extra, (
        f"nix/msks-client-pkg.nix dependencies pyproject does not declare: "
        f"{sorted(extra)}"
    )


def test_client_remove_list_is_exactly_the_daemon_only_set() -> None:
    """pythonRemoveDeps names every pyproject dependency the client
    list does not carry — and nothing else. The remove list is what
    keeps the trimmed wheel's recorded requirements from
    re-expanding the closure at install time; a daemon-only
    dependency added to pyproject lands in msks-pkg.nix via the
    mirror tests above, and this holds the client half: miss the
    remove list and the client build fails only at package build
    time, the #203 failure shape, on a tree the suite never builds."""
    wanted = dependency_names() - nix_dependency_names(CLIENT_PKG_NIX)
    have = nix_list_names(CLIENT_PKG_NIX, "pythonRemoveDeps")
    missing = wanted - have
    stale = have - wanted
    assert not missing and not stale, (
        f"nix/msks-client-pkg.nix pythonRemoveDeps out of sync with "
        f"pyproject minus the client dependency list — missing: "
        f"{sorted(missing)} (the client build fails its runtime deps "
        f"check without them), stale: {sorted(stale)}"
    )


def test_both_locks_pin_the_same_nixpkgs_revision() -> None:
    """flake.nix's and README's claim that each flake attribute
    builds the same store path as its `devenv build outputs.<name>`
    twin holds while both locks pin the same nixpkgs revision
    (#537). devenv.yaml tracks the moving nixos-26.05 branch and
    the flake pins a rev, so a devenv bump that leaves flake.lock
    behind fails here — re-pin the flake to match, or soften the
    same-path claim where it is stated (README, flake.nix)."""

    def locked_rev(path: Path) -> str:
        nodes = json.loads(path.read_text())["nodes"]
        return nodes["nixpkgs"]["locked"]["rev"]

    devenv_rev = locked_rev(DEVENV_LOCK)
    flake_rev = locked_rev(FLAKE_LOCK)
    assert devenv_rev == flake_rev, (
        f"devenv.lock pins nixpkgs {devenv_rev[:12]} but flake.lock "
        f"pins {flake_rev[:12]} — the flake and devenv outputs no "
        f"longer build the same store paths; re-pin one to match "
        f"the other or update the same-path claim (#537)"
    )


def assert_hand_pinned_floor_covers_pyproject(
    pkg_nix: Path, name: str, stake: str, bump_hint: str
) -> None:
    """The hand-pinned wheel a *-pkg.nix file builds satisfies
    pyproject's >= floor for *name*: the runtime check refuses a
    build whose pin sits below the floor, and the pin is the only
    nix-side version guarantee for a dependency built by hand."""
    specifier = next(
        d
        for d in tomllib.loads(PYPROJECT.read_text())["project"][
            "dependencies"
        ]
        if re.split(r"[><=~!;\[]", d)[0].strip().lower() == name
    )
    floor = re.search(r">=\s*(\d+(?:\.\d+)*)", specifier)
    assert floor is not None, (
        f"pyproject's {name} specifier {specifier!r} declares no "
        f">= floor for {pkg_nix.name} to cover"
    )
    pin = re.search(
        r'version = "([^"]+)"', strip_comments(pkg_nix.read_text())
    )
    assert pin is not None, f"{pkg_nix.name} lost its version pin"
    pin_v = version_tuple(pin.group(1))
    floor_v = version_tuple(floor.group(1))
    assert pin_v >= floor_v, (
        f"pyproject wants {name}>={floor.group(1)}; "
        f"nix/{pkg_nix.name} pins {pin.group(1)} — {bump_hint}, or "
        f"the closure runs on a {name} older than its declared "
        f"floor, past {stake}"
    )


def test_the_hand_pinned_textual_floor_covers_pyproject() -> None:
    """pyproject's textual floor stays within what textual-pkg.nix
    pins. textual is a dependency this repo builds by hand from an
    exact wheel, so a floor bump in pyproject that the nix pin
    misses cannot fail the runtime check (8.2.9 required, 8.2.8
    shipped) and no smoke covers the TUI focus semantics the floor
    exists for — this test is that net."""
    assert_hand_pinned_floor_covers_pyproject(
        TEXTUAL_PKG,
        "textual",
        "the TUI focus semantics the floor exists for",
        "bump the pin per the file's bump-workflow header (wheel "
        "URL, hash, and propagatedBuildInputs from the new wheel's "
        "METADATA)",
    )


def test_the_hand_pinned_certifi_floor_covers_pyproject() -> None:
    """pyproject's certifi floor stays within what certifi-pkg.nix
    pins (#606). certifi is built by hand for the same reason
    textual is — the pinned channel sits below the floor — and the
    root-store refreshes the floor names (the emSign root removals)
    are exactly what a stale pin would silently drop: the nix-built
    daemon would keep verifying upstream TLS against roots the
    floor retired."""
    assert_hand_pinned_floor_covers_pyproject(
        CERTIFI_PKG,
        "certifi",
        "the root-store refreshes the floor names",
        "bump the pin (wheel URL and hash from the new uv.lock entry)",
    )
