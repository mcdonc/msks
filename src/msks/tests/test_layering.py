"""The layering contract over the msks package (#387).

The import walk lives in ``scripts/check_import_cycles.py`` (the
commit-time gate); this module loads it so the suite and the hook
share one analyzer. Two assertions:

- **The module-level import graph is acyclic** — the gate's own
  check, asserted here so the suite catches a cycle even when the
  hook was bypassed.
- **Every package-level edge sits on the whitelist below.** A
  package edge is a pair of first segments under ``msks``; a new
  one fails with the ``file:line`` that created it and the set the
  source may import today, so widening the contract is a deliberate
  whitelist edit, not an accident that slipped in.

The two granularities are both real on purpose: package-collapsed
pairs such as ``app`` ↔ ``server`` (``server/main.py`` pulls the
composer, ``app.py`` composes ``server.events``) are not runtime
cycles and live on the whitelist; a genuine module cycle fails the
first test whatever the whitelist says.

The whitelist reads bottom-up:

- ``spec`` — the shared leaf vocabulary (#387): value types,
  grammars, predicates; imports nothing from the daemon
- ``identity``, ``storage``, ``config`` — root leaves (keys, units,
  file loading)
- ``model``, ``settings``, ``persist``, ``imagestore``,
  ``secretstore``, ``llm`` — storage and configuration over the
  vocabulary
- ``consent`` — verdict policy over the model
- ``microvm``, ``net`` — the drivers and the data plane
- ``interceptor``, ``conformance``, ``conformance_args`` — tooling
  over the drivers (the conformance check composes the daemon it
  inspects; ``conformance_args`` is the surface the client shares)
- ``server`` — the HTTP surface, orchestrating everything below
- ``app`` — composition; the entry point (``server.main``) pulls it
- ``client`` — the REST client; it touches the daemon only through
  the root leaves above (#397 tracks shrinking that set)
"""

import importlib.util
import sys
from pathlib import Path

SCRIPT = (
    Path(__file__).resolve().parents[3] / "scripts" / "check_import_cycles.py"
)
spec = importlib.util.spec_from_file_location("check_import_cycles", SCRIPT)
check_import_cycles = importlib.util.module_from_spec(spec)
sys.modules["check_import_cycles"] = check_import_cycles
spec.loader.exec_module(check_import_cycles)

ALLOWED_EDGES = {
    # app composes the daemon
    ("app", "consent"),
    ("app", "interceptor"),
    ("app", "llm"),
    ("app", "microvm"),
    ("app", "model"),
    ("app", "net"),
    ("app", "secretstore"),
    ("app", "server"),
    ("app", "settings"),
    # client: the REST consumer (root leaves only; #397)
    ("client", "config"),
    ("client", "conformance"),
    ("client", "conformance_args"),
    ("client", "identity"),
    ("client", "imagestore"),
    ("client", "msks"),
    ("client", "storage"),
    # root leaves
    ("config", "settings"),
    # the local conformance check composes the daemon it inspects
    ("conformance", "app"),
    ("conformance", "conformance_args"),
    ("conformance", "imagestore"),
    ("conformance", "microvm"),
    ("conformance", "settings"),
    # verdict policy over the model, vocabulary from spec
    ("consent", "model"),
    ("consent", "spec"),
    # interceptor over the drivers
    ("interceptor", "microvm"),
    ("interceptor", "spec"),
    # the local driver: vocabulary, storage, the package root
    ("microvm", "msks"),
    ("microvm", "persist"),
    ("microvm", "spec"),
    # the ORM's vocabulary
    ("model", "spec"),
    # the data plane
    ("net", "consent"),
    ("net", "microvm"),
    ("net", "spec"),
    # seeding and volume bookkeeping
    ("persist", "identity"),
    ("persist", "microvm"),
    ("persist", "spec"),
    # the secret store over the model
    ("secretstore", "model"),
    # the HTTP surface orchestrates; main.py pulls the composer
    ("server", "app"),
    ("server", "config"),
    ("server", "identity"),
    ("server", "imagestore"),
    ("server", "llm"),
    ("server", "microvm"),
    ("server", "model"),
    ("server", "msks"),
    ("server", "persist"),
    ("server", "secretstore"),
    ("server", "settings"),
    ("server", "spec"),
    ("server", "storage"),
    # settings parse against the vocabulary and key types
    ("settings", "identity"),
    ("settings", "spec"),
}


def test_module_import_graph_is_acyclic():
    cycle = check_import_cycles.find_cycle()
    assert cycle is None, (
        "module import cycle in the msks package "
        f"(#387 closed these): {' -> '.join(cycle)} — reproduce with"
        " scripts/check_import_cycles.py"
    )


def test_package_edges_are_whitelisted():
    found: dict[tuple[str, str], list[str]] = {}
    root = check_import_cycles.PKG_ROOT
    for path, lineno, src, dst in check_import_cycles.intra_package_imports():
        src_pkg = check_import_cycles.package_of(src)
        dst_pkg = check_import_cycles.package_of(dst)
        if src_pkg != dst_pkg:
            site = f"{path.relative_to(root)}:{lineno}"
            found.setdefault((src_pkg, dst_pkg), []).append(site)

    unknown = {
        pair: sites
        for pair, sites in found.items()
        if pair not in ALLOWED_EDGES
    }
    if unknown:
        lines = ["package edges outside the layering whitelist (#387):"]
        for (src, dst), sites in sorted(unknown.items()):
            allowed = sorted(d for s, d in ALLOWED_EDGES if s == src)
            lines.append(
                f"  {src} -> {dst} at {', '.join(sorted(sites))}; {src} may"
                f" import: {', '.join(allowed) or '(nothing)'} — import from"
                " one of those, or widen the whitelist in test_layering.py"
                " deliberately"
            )
        assert False, "\n".join(lines)
