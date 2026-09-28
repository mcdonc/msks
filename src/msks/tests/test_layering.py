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

The two granularities are both real on purpose: a package-collapsed
pair can hide runtime composition behind an entry-point edge — the
``app`` ↔ ``server`` pair #408 dissolved by relocating the event
hub (``server.events``) to the root leaf ``events.py``; today
``server/main.py → app`` is the one edge between the components.
The table below reads bottom-up, with two sanctioned upward edges:
``server.main`` and ``conformance`` each pull the composer (the
process entry point, and the local check composing the daemon it
inspects). A genuine module cycle fails the first test whatever
the whitelist says.

The whitelist reads bottom-up:

- ``spec`` — the shared leaf vocabulary (#387): value types,
  grammars, predicates; imports nothing from the daemon
- ``identity``, ``storage``, ``configio``, ``events`` — root leaves
  (keys, units, config-file reading and first-run writing; the WSS
  event hub #408, shared by the composition layer and the HTTP
  surface)
- ``model``, ``settings``, ``persist``, ``imagestore``,
  ``secretstore``, ``llm`` — storage and configuration over the
  vocabulary
- ``consent`` — verdict policy over the model
- ``microvm``, ``net`` — the drivers and the data plane; their
  vocabulary (spec types, decided pins, the shared failure class)
  comes from ``spec`` alone (#401: net hands policy translation
  to consent and takes decided values back); microvm reads its
  storage through ``persist`` directly (#407) — routing through
  the package root was the last root edge
- ``interceptor`` — tooling over the drivers' vocabulary: spec
  alone since #407 (the failure class lives in ``spec.failures``,
  so the interceptor's driver edge went with it)
- ``conformance``, ``conformance_args`` — the conformance check
  composes the daemon it inspects; ``conformance_args`` is the
  surface the client shares
- ``server`` — the HTTP surface, orchestrating everything below;
  it reads its version from ``spec.version``, not the package
  root (#407)
- ``app`` — composition; the entry point (``server.main``) pulls
  it, and that is the only edge between the two components since
  #408 (the hub the composer builds is a root leaf)
- ``client`` — the REST client (#397): it imports its own
  siblings, stdlib, third-party packages, and the allowlist
  ``identity``, ``conformance_args``, ``spec``, ``configio`` — the
  shared leaves. The local ``image check`` runs the standalone
  conformance entry as a child process, so the daemon composition
  stays out of the client process. ``test_client_imports_stay_within
  _the_allowlist`` holds the rule; widening it is a deliberate edit
  there and in ``ALLOWED_EDGES``.
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
    # app composes the daemon; the hub is a root leaf (#408)
    ("app", "consent"),
    ("app", "events"),
    ("app", "interceptor"),
    ("app", "llm"),
    ("app", "microvm"),
    ("app", "model"),
    ("app", "net"),
    ("app", "secretstore"),
    ("app", "settings"),
    # client: the REST consumer — only the shared-leaf allowlist
    # (#397; enforced separately by the client test below)
    ("client", "configio"),
    ("client", "conformance_args"),
    ("client", "identity"),
    ("client", "spec"),
    # root leaves
    ("config", "settings"),
    ("config", "configio"),
    # the local conformance check composes the daemon it inspects
    ("conformance", "app"),
    ("conformance", "conformance_args"),
    ("conformance", "imagestore"),
    ("conformance", "microvm"),
    ("conformance", "settings"),
    # verdict policy over the model, vocabulary from spec
    ("consent", "model"),
    ("consent", "spec"),
    # interceptor: vocabulary from spec (#407 pruned the driver edge)
    ("interceptor", "spec"),
    # the local driver: vocabulary and storage read directly (#407)
    ("microvm", "persist"),
    ("microvm", "spec"),
    # the ORM's vocabulary
    ("model", "spec"),
    # the data plane: vocabulary and decided pins from spec alone
    # (#401 — the consent side decides, net applies)
    ("net", "spec"),
    # seeding and volume bookkeeping; the failure class is
    # spec vocabulary since #401, imported from there since #407
    ("persist", "identity"),
    ("persist", "spec"),
    # the secret store over the model
    ("secretstore", "model"),
    # the image catalog's ref grammar comes from the leaf vocabulary
    ("imagestore", "spec"),
    # the HTTP surface orchestrates; main.py pulls the composer,
    # the hub relay comes from the events root leaf (#408)
    ("server", "app"),
    ("server", "config"),
    ("server", "events"),
    ("server", "identity"),
    ("server", "imagestore"),
    ("server", "llm"),
    ("server", "microvm"),
    ("server", "model"),
    ("server", "persist"),
    ("server", "secretstore"),
    ("server", "settings"),
    ("server", "spec"),
    ("server", "storage"),
    # settings parse against the vocabulary and key types
    ("settings", "identity"),
    ("settings", "spec"),
}


# The client-isolation allowlist (#397): the packages outside
# ``client`` its modules may import. Widening this is a deliberate
# contract edit — a new entry needs a shared-leaf reason, not a
# convenience import.
CLIENT_ALLOWED = {"configio", "conformance_args", "identity", "spec"}


def test_client_imports_stay_within_the_allowlist():
    found: dict[str, list[str]] = {}
    for path, lineno, src, dst in check_import_cycles.intra_package_imports():
        if check_import_cycles.package_of(src) != "client":
            continue
        dst_pkg = check_import_cycles.package_of(dst)
        if dst_pkg != "client":
            site = f"{path.relative_to(check_import_cycles.PKG_ROOT)}:{lineno}"
            found.setdefault(dst_pkg, []).append(site)

    extra = set(found) - CLIENT_ALLOWED
    unused = CLIENT_ALLOWED - set(found)
    if extra or unused:
        lines = ["client imports outside the isolation allowlist (#397):"]
        for pkg in sorted(extra):
            sites = ", ".join(sorted(set(found[pkg])))
            lines.append(
                f"  client -> {pkg} at {sites} — the"
                " client is a standalone REST consumer; import the API's"
                " data, or extend CLIENT_ALLOWED in test_layering.py with a"
                " shared-leaf reason"
            )
        for pkg in sorted(unused):
            lines.append(
                f"  {pkg} is allowlisted but no client import uses it —"
                " prune it so the contract matches the tree"
            )
        assert False, "\n".join(lines)


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
    stale = set(ALLOWED_EDGES) - set(found)
    if unknown or stale:
        lines = ["package edges outside the layering whitelist (#387):"]
        for (src, dst), sites in sorted(unknown.items()):
            allowed = sorted(d for s, d in ALLOWED_EDGES if s == src)
            site_list = ", ".join(sorted(set(sites)))
            lines.append(
                f"  {src} -> {dst} at {site_list}; {src} may"
                f" import: {', '.join(allowed) or '(nothing)'} — import from"
                " one of those, or widen the whitelist in test_layering.py"
                " deliberately"
            )
        for src, dst in sorted(stale):
            lines.append(
                f"  {src} -> {dst} is whitelisted but no such edge exists —"
                " prune it so the contract matches the tree"
            )
        assert False, "\n".join(lines)
