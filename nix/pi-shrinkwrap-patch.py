"""Close the integrity gaps the published pi shrinkwrap leaves.

The pi coding agent's published npm-shrinkwrap.json omits
``integrity`` for the five ``@earendil-works`` monorepo siblings
(published in lockstep with pi itself). npm tolerates the gaps; the
nix npm-deps prefetcher refuses them. This script injects each
tarball's registry sha512 — pinning content the resolved URLs
already name, changing no resolution — and strips the
``devDependencies`` the pruned lock no longer carries (the dist/
tree ships prebuilt, so the production install drops them instead
of reaching for the network).

Invoked by ``nix/guest-debian.nix`` (``patchedPiSource``) as

    python pi-shrinkwrap-patch.py <shrinkwrap> <package.json>

Unit-tested by ``src/msks/tests/test_guestassets.py``; the MISSING
table is the only thing that moves with a pin bump.
"""

import json
import sys

#: The sha512 integrity (registry tarball digests, base64) for the
#: five lockstep siblings the published lock leaves unpinned.
MISSING = {
    "@earendil-works/chord": "bg7IkJGFcEaMqqYgOGUiq5Ky"
    "9RghpRfrlZ8I/v/1b4bBZ02A7",
    "@earendil-works/pi-agent-core": "Zev3B0HK7YS5A4EZQ2XnEqiJu"
    "irx6QBiltJ+LpmjV5a/+2IU0cf",
    "@earendil-works/pi-ai": "X/3PfQBnnoeVdO9Cv8zHghUMg"
    "lzlgNZYGNzoPnbRoGnHl3Rw3T",
    "@earendil-works/pi-telemetry": "MC6TRQH5lgMXpcN+Vku2WMI2T"
    "8BsiUPzMQHGo81uqFZD3/9O79",
    "@earendil-works/pi-tui": "YEH2vRyOeiO7hhN6j6AE6YwKS"
    "q2Kz2f3XR8bj1TbR+aGE/JsnY",
}


def patch_lock(lock: dict) -> set[str]:
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
        if name in MISSING and resolved and not entry.get("integrity"):
            entry["integrity"] = "sha512-" + MISSING[name]
            patched.add(name)
    return patched


def strip_dev_dependencies(pkg: dict) -> bool:
    """Drop devDependencies when present; return whether the file
    changed."""
    if "devDependencies" not in pkg:
        return False
    del pkg["devDependencies"]
    return True


def main(argv: list[str]) -> int:
    lock_path, pkg_path = argv[1], argv[2]
    with open(lock_path) as f:
        lock = json.load(f)
    patched = patch_lock(lock)
    if patched != set(MISSING):
        missing = sorted(set(MISSING) - patched)
        raise SystemExit(
            f"expected {len(MISSING)} integrity gaps, "
            f"patched {sorted(patched)}; unpinned: {missing}"
        )
    with open(lock_path, "w") as f:
        json.dump(lock, f, indent=2)
        f.write("\n")

    with open(pkg_path) as f:
        pkg = json.load(f)
    if strip_dev_dependencies(pkg):
        with open(pkg_path, "w") as f:
            json.dump(pkg, f, indent=2)
            f.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
