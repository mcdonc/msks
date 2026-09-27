#!/usr/bin/env python3
"""Import-graph gates over the daemon sources (AGENTS.md).

Six rules over the same AST walk ``scripts/check_import_cycles.py``
performs, each driven by ``import-graph.toml`` at the repo root —
a rule plus a small config file, so another codebase adopts the
gates by writing config, not checking logic:

- **No cycles at package granularity.** The module graph collapsed
  to first-level components is a DAG except for the exempt pairs.
  An exemption whose cycle no longer exists fails as stale: the
  list only shrinks.
- **Imports point down.** Every cross-package edge runs from a
  higher layer rank to a lower one. A package on an edge without a
  rank fails until it declares one; a rank for a package that no
  longer exists fails as stale.
- **Coupling matches the snapshot exactly.** Per-pair cross-package
  import-statement counts equal the file in both directions —
  growth and shrinkage both name their fix, so re-coupling is a
  deliberate edit.
- **Fan-in stays under the ceiling.** No module may be imported by
  more distinct modules than the ceiling records; the ceiling only
  goes down.
- **Shipped code is reachable.** Every module under the package
  sits in the import closure of the declared entry points; test-
  only code lives in the test tree.
- **Names come from their defining module.** Importing a name
  through a module that merely re-exports it fails unless that
  module is on the facade list; a facade that re-exports nothing
  fails as stale.

Runs at commit time (the generated pre-commit hook), inside
``msks-preflight``, and in the suite (``src/msks/tests/
test_import_graph.py``, which loads this module for the same walk
so the surfaces never drift). Exit status 1 lists every violation
grouped by gate.
"""

import ast
import importlib.util
import sys
import tomllib
from collections import Counter
from functools import lru_cache
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
PKG_ROOT = REPO_ROOT / "src" / "msks" / "msks"
CONFIG_PATH = REPO_ROOT / "import-graph.toml"

WALKER = Path(__file__).resolve().parent / "check_import_cycles.py"
walk_spec = importlib.util.spec_from_file_location(
    "check_import_cycles", WALKER
)
walk = importlib.util.module_from_spec(walk_spec)
sys.modules["check_import_cycles"] = walk
walk_spec.loader.exec_module(walk)


def load_config() -> dict:
    with CONFIG_PATH.open("rb") as fh:
        return tomllib.load(fh)


@lru_cache(maxsize=1)
def real_modules() -> frozenset[tuple[str, ...]]:
    """Every module file under the package, as dotted parts."""
    modules = set()
    for path in PKG_ROOT.rglob("*.py"):
        if "__pycache__" not in path.parts:
            parts = walk.module_of(path)
            if parts is not None:
                modules.add(parts)
    return frozenset(modules)


@lru_cache(maxsize=1)
def all_edges() -> tuple[tuple[Path, int, tuple, tuple], ...]:
    return tuple(walk.intra_package_imports())


@lru_cache(maxsize=1)
def pair_counts() -> Counter[tuple[str, str]]:
    """Cross-package import statements per ``(src_pkg, dst_pkg)``:
    one count per statement site, names folded into their module."""
    counts: Counter[tuple[str, str]] = Counter()
    for _path, _lineno, src, dst in all_edges():
        src_pkg, dst_pkg = walk.package_of(src), walk.package_of(dst)
        if src_pkg != dst_pkg:
            counts[(src_pkg, dst_pkg)] += 1
    return counts


def package_adjacency() -> dict[str, set[str]]:
    """The module graph collapsed to first-level components."""
    adjacency: dict[str, set[str]] = {}
    for src_pkg, dst_pkg in pair_counts():
        adjacency.setdefault(src_pkg, set()).add(dst_pkg)
    return adjacency


def module_graph(real: frozenset[tuple[str, ...]]):
    """``{src_module: {dst_module}}`` over real-module targets,
    self and ancestor edges excluded (the intra-package idiom)."""
    graph: dict[tuple[str, ...], set[tuple[str, ...]]] = {}
    for _path, _lineno, src, dst in all_edges():
        if dst not in real or dst == src or src[: len(dst)] == dst:
            continue
        graph.setdefault(src, set()).add(dst)
    return graph


def exempt_pairs(config: dict) -> set[frozenset[str]]:
    return {
        frozenset((src, dst))
        for src, dsts in config.get("exempt_pairs", {}).items()
        for dst in dsts
    }


def assign_target_names(node: ast.Assign) -> set[str]:
    """Plain ``Name`` targets of one assignment."""
    return {t.id for t in node.targets if isinstance(t, ast.Name)}


def import_asnames(node: ast.Import) -> set[str]:
    """The new names an aliased ``import`` binds."""
    return {a.asname for a in node.names if a.asname}


def def_names(node: ast.AST) -> set[str]:
    """Names one def/class/assignment node binds, empty for the
    rest."""
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
        return {node.name}
    if isinstance(node, ast.Assign):
        return assign_target_names(node)
    if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
        return {node.target.id}
    return set()


def defined_names(tree: ast.AST) -> set[str]:
    """Names a module binds itself: definitions, assignments, and
    aliased imports rebinding a new name."""
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(import_asnames(node))
        else:
            names.update(def_names(node))
    return names


def own_name_aliases(node: ast.ImportFrom) -> list[str]:
    """The statement's aliases that bind an imported name under its
    own name (star imports and aliased renames excluded — a rename
    binds a new name and counts as a definition)."""
    return [
        a.name
        for a in node.names
        if a.name != "*" and a.asname in (None, a.name)
    ]


def reexport_sources(
    pkg_parts: tuple[str, ...], node: ast.ImportFrom
) -> dict[str, tuple[str, ...]]:
    """The msks module each own-name alias of one ImportFrom pulls a
    name from."""
    base = walk.relative_base(pkg_parts, node)
    if base is None:
        return {}
    source = walk.resolved_module(node, base)
    if not source or source[0] != "msks":
        return {}
    return {name: source for name in own_name_aliases(node)}


@lru_cache(maxsize=1)
def bindings() -> tuple[dict, dict]:
    """Per module: the names it defines, and the msks modules its
    own-name imports pull each re-exported name from."""
    defines: dict[tuple[str, ...], set[str]] = {}
    reexports: dict[tuple[str, ...], dict[str, tuple[str, ...]]] = {}
    for path in sorted(PKG_ROOT.rglob("*.py")):
        parts = walk.module_of(path)
        if parts is None:
            continue
        tree = ast.parse(path.read_text(), filename=str(path))
        defines[parts] = defined_names(tree)
        reexports[parts] = name_sources(path, parts, tree)
    return defines, reexports


def name_sources(
    path: Path, parts: tuple[str, ...], tree: ast.AST
) -> dict[str, tuple[str, ...]]:
    """``{name: source_module}`` for the file's own-name ImportFrom
    aliases that pull from inside the msks package."""
    sources: dict[str, tuple[str, ...]] = {}
    pkg_parts = parts if path.name == "__init__.py" else parts[:-1]
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            sources.update(reexport_sources(pkg_parts, node))
    return sources


def package_sccs(adjacency: dict[str, set[str]]) -> list[list[str]]:
    """Strongly connected components of the collapsed graph."""
    index: dict[str, int] = {}
    low: dict[str, int] = {}
    stack: list[str] = []
    on_stack: set[str] = set()
    order = [0]
    comps: list[list[str]] = []

    def visit(module: str) -> None:
        index[module] = low[module] = order[0]
        order[0] += 1
        stack.append(module)
        on_stack.add(module)
        for dep in adjacency.get(module, ()):
            if dep not in index:
                visit(dep)
                low[module] = min(low[module], low[dep])
            elif dep in on_stack:
                low[module] = min(low[module], index[dep])
        if low[module] == index[module]:
            comp = []
            while True:
                item = stack.pop()
                on_stack.discard(item)
                comp.append(item)
                if item == module:
                    break
            comps.append(comp)

    for module in list(adjacency):
        if module not in index:
            visit(module)
    return comps


def unexempted_pairs(
    ordered: list[str], exempt: set[frozenset[str]]
) -> list[str]:
    """The pairs inside one cyclic component carrying no
    exemption."""
    return [
        f"{a} <-> {b}"
        for i, a in enumerate(ordered)
        for b in ordered[i + 1 :]
        if frozenset((a, b)) not in exempt
    ]


def unexempted_cycle_pairs(
    comps: list[list[str]], exempt: set[frozenset[str]]
) -> list[str]:
    """One violation per cyclic component holding an unexempted
    pair, naming the pairs."""
    violations = []
    for comp in comps:
        if len(comp) < 2:
            continue
        ordered = sorted(comp)
        unexempt = unexempted_pairs(ordered, exempt)
        if unexempt:
            violations.append(
                "package cycle in "
                + "/".join(ordered)
                + " without an exemption: "
                + ", ".join(unexempt)
                + " — extract a shared leaf, or widen exempt_pairs"
                " deliberately (exemptions only shrink)"
            )
    return violations


def stale_exemptions(
    adjacency: dict[str, set[str]], exempt: set[frozenset[str]]
) -> list[str]:
    """Exemptions whose pair no longer cycles both ways."""
    violations = []
    for pair in sorted(exempt, key=sorted):
        a, b = sorted(pair)
        both = b in adjacency.get(a, ()) and a in adjacency.get(b, ())
        if not both:
            violations.append(
                f"exemption {a} <-> {b} is stale — the pair no longer"
                " cycles both ways; prune it from exempt_pairs"
            )
    return violations


def check_cycles(config: dict) -> list[str]:
    adjacency = package_adjacency()
    exempt = exempt_pairs(config)
    return unexempted_cycle_pairs(package_sccs(adjacency), exempt) + (
        stale_exemptions(adjacency, exempt)
    )


def missing_rank(
    ranks: dict[str, int], src_pkg: str, dst_pkg: str
) -> str | None:
    """The undeclared-rank violation for an edge, when one of its
    packages carries no rank."""
    if src_pkg in ranks and dst_pkg in ranks:
        return None
    missing = src_pkg if src_pkg not in ranks else dst_pkg
    return (
        f"{src_pkg} -> {dst_pkg}: {missing} has no rank —"
        " declare one in import-graph.toml [ranks]"
    )


def rank_violation(
    ranks: dict[str, int],
    exempt: set[frozenset[str]],
    src_pkg: str,
    dst_pkg: str,
) -> str | None:
    if frozenset((src_pkg, dst_pkg)) in exempt:
        return None
    missing = missing_rank(ranks, src_pkg, dst_pkg)
    if missing:
        return missing
    if ranks[src_pkg] <= ranks[dst_pkg]:
        return (
            f"{src_pkg} -> {dst_pkg} runs rank {ranks[src_pkg]} ->"
            f" {ranks[dst_pkg]} — imports point down; move the"
            " shared name to a lower-ranked module or widen the"
            " ranks deliberately"
        )
    return None


def check_ranks(config: dict) -> list[str]:
    violations = []
    ranks: dict[str, int] = config.get("ranks", {})
    exempt = exempt_pairs(config)
    for src_pkg, dst_pkg in pair_counts():
        violation = rank_violation(ranks, exempt, src_pkg, dst_pkg)
        if violation:
            violations.append(violation)
    existing = {walk.package_of(m) for m in real_modules()}
    for pkg in sorted(set(ranks) - existing):
        violations.append(
            f"rank declared for {pkg} but no such package exists —"
            " prune it from [ranks]"
        )
    return violations


def snapshot_violation(
    recorded: dict[tuple[str, str], int],
    src_pkg: str,
    dst_pkg: str,
    count: int,
) -> str | None:
    known = recorded.get((src_pkg, dst_pkg))
    if known is None:
        return (
            f"{src_pkg} -> {dst_pkg} carries {count} import(s)"
            " but no snapshot entry — add the pair deliberately"
            " (import-graph.toml [snapshot] and test_layering's"
            " ALLOWED_EDGES)"
        )
    if count > known:
        return (
            f"{src_pkg} -> {dst_pkg} grew {known} -> {count} —"
            " coupling only shrinks; update [snapshot] in the same"
            " commit with the reason, or import from a module"
            " already coupled"
        )
    if count < known:
        return (
            f"{src_pkg} -> {dst_pkg} shrank {known} -> {count} —"
            " update [snapshot] in the same commit"
        )
    return None


def recorded_counts(
    snapshot: dict[str, dict[str, int]],
) -> dict[tuple[str, str], int]:
    """The snapshot tables flattened to ``{(src, dst): count}``."""
    recorded: dict[tuple[str, str], int] = {}
    for src, dsts in snapshot.items():
        for dst, count in dsts.items():
            recorded[(src, dst)] = count
    return recorded


def check_snapshot(config: dict) -> list[str]:
    violations = []
    snapshot: dict[str, dict[str, int]] = config.get("snapshot", {})
    recorded = recorded_counts(snapshot)
    actual = pair_counts()
    for (src_pkg, dst_pkg), count in sorted(actual.items()):
        violation = snapshot_violation(recorded, src_pkg, dst_pkg, count)
        if violation:
            violations.append(violation)
    for pair in sorted(set(recorded) - set(actual)):
        violations.append(
            f"{pair[0]} -> {pair[1]} is snapshotted but no such"
            " import exists — prune the entry"
        )
    return violations


def importer_counts(
    real: frozenset[tuple[str, ...]],
) -> dict[tuple[str, ...], set[tuple[str, ...]]]:
    """Distinct importers per real-module target."""
    importers: dict[tuple[str, ...], set[tuple[str, ...]]] = {}
    for _path, _lineno, src, dst in all_edges():
        if dst in real and dst != src and src[: len(dst)] != dst:
            importers.setdefault(dst, set()).add(src)
    return importers


def check_fan_in(config: dict) -> list[str]:
    ceiling: int = config.get("fan_in_ceiling", 0)
    importers = importer_counts(real_modules())
    violations = []
    for dst, sources in sorted(importers.items(), key=lambda kv: -len(kv[1])):
        if len(sources) > ceiling:
            violations.append(
                f"{'.'.join(dst)} has {len(sources)} importers"
                f" (ceiling {ceiling}) — split what everyone"
                " imports out of it; the ceiling only goes down"
            )
    return violations


def unvisited_ancestors(
    module: tuple[str, ...], reached: set[tuple[str, ...]]
) -> list[tuple[str, ...]]:
    """The module's ancestor packages not reached yet."""
    return [
        anc
        for anc in (module[:i] for i in range(1, len(module)))
        if anc not in reached
    ]


def import_closure(
    real: frozenset[tuple[str, ...]],
    graph: dict[tuple[str, ...], set[tuple[str, ...]]],
    entries: list[tuple[str, ...]],
) -> set[tuple[str, ...]]:
    """Modules the entry points load, directly or through ancestor
    package ``__init__`` chains."""
    reached: set[tuple[str, ...]] = set()
    queue = [entry for entry in entries if entry in real]
    while queue:
        module = queue.pop()
        if module in reached:
            continue
        reached.add(module)
        queue.extend(unvisited_ancestors(module, reached))
        queue.extend(graph.get(module, ()))
    return reached


def check_reachable(config: dict) -> list[str]:
    real = real_modules()
    graph = module_graph(real)
    entries = [
        tuple(entry.split(".")) for entry in config.get("entry_points", [])
    ]
    violations = [
        f"entry point {'.'.join(entry)} is not a module under the package"
        for entry in entries
        if entry not in real
    ]
    reached = import_closure(real, graph, entries)
    for module in sorted(real - reached):
        violations.append(
            f"{'.'.join(module)} is unreachable from the entry points"
            " — wire it in, add an entry point, or move it to the"
            " test tree"
        )
    return violations


def name_target_holder(
    dst: tuple[str, ...], real: frozenset[tuple[str, ...]]
) -> tuple[str, ...] | None:
    """``dst``'s holder module, when ``dst`` is an ImportFrom name
    leaf under the msks package and not itself a module import."""
    holder = dst[:-1]
    if len(holder) < 2 or holder[0] != "msks" or dst in real:
        return None
    return holder


def holder_reexports_name(
    holder: tuple[str, ...], name: str, reexports: dict
) -> tuple[str, ...] | None:
    """The msks module ``holder`` re-exports ``name`` from, when it
    does and that module is a different one."""
    source = reexports.get(holder, {}).get(name)
    if source is None or tuple(source) == holder:
        return None
    return tuple(source)


def reexport_holder(
    dst: tuple[str, ...],
    real: frozenset[tuple[str, ...]],
    defines: dict,
    reexports: dict,
) -> tuple[str, ...] | None:
    """The module ``dst``'s name is re-exported from, when ``dst``
    is a name-level target whose holder re-exports it."""
    holder = name_target_holder(dst, real)
    if holder is None:
        return None
    if dst[-1] in defines.get(holder, ()):
        return None
    if holder_reexports_name(holder, dst[-1], reexports) is None:
        return None
    return holder


def check_facades(config: dict) -> list[str]:
    violations = []
    real = real_modules()
    facades: list[str] = config.get("facades", [])
    defines, reexports = bindings()
    for path, lineno, src, dst in all_edges():
        holder = reexport_holder(dst, real, defines, reexports)
        if holder is None or ".".join(holder) in facades:
            continue
        source = holder_reexports_name(holder, dst[-1], reexports)
        site = f"{path.relative_to(PKG_ROOT)}:{lineno}"
        violations.append(
            f"{'.'.join(src)} imports {dst[-1]} from"
            f" {'.'.join(holder)} ({site}), which re-exports it from"
            f" {'.'.join(source)} — import from the defining module"
            " or declare the facade in import-graph.toml"
        )
    return violations + stale_facades(facades, real, reexports)


def stale_facades(facades: list[str], real: set, reexports: dict) -> list[str]:
    """Facade entries that match nothing on the tree."""
    violations = []
    for facade in facades:
        module = tuple(facade.split("."))
        if module not in real:
            violations.append(
                f"facade {facade} is not a module under the package"
                " — prune it from [facades]"
            )
        elif not reexports.get(module):
            violations.append(
                f"facade {facade} re-exports nothing — prune it from [facades]"
            )
    return violations


GATES = [
    ("cycles", check_cycles),
    ("ranks", check_ranks),
    ("snapshot", check_snapshot),
    ("fan-in", check_fan_in),
    ("reachable", check_reachable),
    ("facades", check_facades),
]


def main() -> int:
    config = load_config()
    failed = False
    for name, gate in GATES:
        violations = gate(config)
        if violations:
            failed = True
            print(f"check_import_graph [{name}]:", file=sys.stderr)
            for violation in violations:
                print(f"  {violation}", file=sys.stderr)
    if failed:
        return 1
    print(
        f"check_import_graph: green — {len(real_modules())} modules,"
        f" {len(pair_counts())} cross-package pairs"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
