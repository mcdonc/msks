"""Close the integrity gaps the published pi lock leaves, and
rebase its root onto the package it installs.

The pi coding agent's published package-lock.json (the install
lock the pi.dev installer consumes, kept in sync with the
monorepo's package-lock.json by upstream's ``npm run check``)
omits ``integrity`` for the ``@earendil-works`` monorepo siblings
published in lockstep with pi itself. npm tolerates the gaps; the
nix npm-deps prefetcher refuses them. This script injects each
tarball's registry sha512 — pinning content the resolved URLs
already name, changing no resolution — strips the
``devDependencies`` the pruned lock no longer carries (the dist/
tree ships prebuilt, so the production install drops them instead
of reaching for the network), and rebases the lock's root onto
the coding agent's own manifest: the install lock's root is the
*installer wrapper* whose sole dependency is the agent, while the
build here treats the agent's tarball as the root — so the root
entry is rebuilt from the agent's package.json and the agent's
own registry self-entry is dropped (the package at the root is
the one being installed, not a dependency of itself).

Invoked by ``nix/agent-toolchain.nix`` (``patchedPiSource``) as

    python pi-shrinkwrap-patch.py \
        <package-lock> <package.json> <integrity-table>

Unit-tested by ``src/msks/tests/test_guestassets.py``; the
integrity table is the only thing that moves with a pin bump.
"""

import json
import sys

#: The install lock's root package: the installer wrapper whose
#: rebasing this script undoes.
INSTALL_ROOT_NAME = "@earendil-works/pi-coding-agent-install"

#: The agent's own registry self-entry: dropped when the root is
#: rebased onto the agent itself.
SELF_ENTRY = "node_modules/@earendil-works/pi-coding-agent"


def load_missing(path):
    """The integrity table: name → registry sha512 (base64), from
    nix/pi-shrinkwrap-integrity.json — data, not code, so the
    formatter never re-wraps a digest."""
    with open(path) as f:
        return json.load(f)


def patch_lock(lock: dict, missing: dict) -> set[str]:
    """Inject the missing integrity values in place; return the
    names actually patched. The guard the caller wants counts
    names, not events: a future lock that nests a duplicate of one
    sibling while dropping another would pass an event count while
    leaving an entry unpinned."""
    patched: set[str] = set()
    for key, entry in lock["packages"].items():
        if not isinstance(entry, dict):
            continue
        name = key.rsplit("node_modules/", 1)[-1]
        resolved = entry.get("resolved")
        if name in missing and resolved and not entry.get("integrity"):
            entry["integrity"] = "sha512-" + missing[name]
            patched.add(name)
    return patched


def rebase_lock_root(lock: dict, pkg: dict) -> bool:
    """Rebuild the lock's root entry from the agent's manifest,
    dropping the agent's registry self-entry. Returns whether the
    lock is an install lock needing the rebase — a root already
    naming the agent (a future upstream that ships its own lock
    again) is left as it comes. The self-entry's version must
    equal the manifest's: the tarball pin and the lock pin move
    together, and a bump that updates one without the other fails
    here, at patch time, instead of downstream inside npm."""
    root = lock["packages"].get("", {})
    if root.get("name") != INSTALL_ROOT_NAME:
        return False
    self_entry = lock["packages"].get(SELF_ENTRY)
    if self_entry is not None and (
        self_entry.get("version") != pkg["version"]
    ):
        raise SystemExit(
            f"the install lock pins {SELF_ENTRY} at "
            f"{self_entry.get('version')!r} but the tarball's "
            f"manifest names {pkg['version']!r} — the two pins "
            f"drifted; bump them together"
        )
    lock["name"] = pkg["name"]
    lock["version"] = pkg["version"]
    rebuilt = {
        "name": pkg["name"],
        "version": pkg["version"],
        "license": pkg.get("license"),
        "engines": pkg.get("engines"),
        "dependencies": pkg.get("dependencies", {}),
    }
    if "overrides" in pkg:
        rebuilt["overrides"] = pkg["overrides"]
    lock["packages"][""] = rebuilt
    lock["packages"].pop(SELF_ENTRY, None)
    return True


def strip_dev_dependencies(pkg: dict) -> bool:
    """Drop devDependencies when present; return whether the file
    changed."""
    if "devDependencies" not in pkg:
        return False
    del pkg["devDependencies"]
    return True


def main(argv: list[str]) -> int:
    lock_path, pkg_path, table_path = argv[1], argv[2], argv[3]
    with open(lock_path) as f:
        lock = json.load(f)
    missing = load_missing(table_path)
    patched = patch_lock(lock, missing)
    if patched != set(missing):
        unpinned = sorted(set(missing) - patched)
        raise SystemExit(
            f"expected {len(missing)} integrity gaps, "
            f"patched {sorted(patched)}; unpinned: {unpinned}"
        )

    with open(pkg_path) as f:
        pkg = json.load(f)
    strip_dev_dependencies(pkg)
    if not rebase_lock_root(lock, pkg):
        if lock["packages"][""].get("name") != pkg["name"]:
            raise SystemExit(
                f"lock root names {lock['packages'][''].get('name')!r}, "
                f"neither the install wrapper nor the agent — a new "
                f"lock shape; teach this script"
            )
    with open(lock_path, "w") as f:
        json.dump(lock, f, indent=2)
        f.write("\n")
    with open(pkg_path, "w") as f:
        json.dump(pkg, f, indent=2)
        f.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
