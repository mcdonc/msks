"""The import-graph gates over ``import-graph.toml`` (AGENTS.md).

Loads ``scripts/check_import_graph.py`` — the same analyzer the
commit-time hook and ``msks-preflight`` run — and asserts each gate
holds on the tree, so a gate bypassed at commit time still fails
the suite. The config file is the contract: every entry must match
the tree in both directions (stale entries fail like new edges),
and widening it is a deliberate edit whose diff states why.
"""

import importlib.util
import sys
from pathlib import Path

SCRIPT = (
    Path(__file__).resolve().parents[3] / "scripts" / "check_import_graph.py"
)
spec = importlib.util.spec_from_file_location("check_import_graph", SCRIPT)
check_import_graph = importlib.util.module_from_spec(spec)
sys.modules["check_import_graph"] = check_import_graph
spec.loader.exec_module(check_import_graph)

config = check_import_graph.load_config()


def test_package_graph_is_acyclic_except_exemptions():
    violations = check_import_graph.check_cycles(config)
    assert not violations, "\n".join(violations)


def test_imports_point_down_through_the_rank_map():
    violations = check_import_graph.check_ranks(config)
    assert not violations, "\n".join(violations)


def test_coupling_matches_the_snapshot_exactly():
    violations = check_import_graph.check_snapshot(config)
    assert not violations, "\n".join(violations)


def test_module_fan_in_stays_under_the_ceiling():
    violations = check_import_graph.check_fan_in(config)
    assert not violations, "\n".join(violations)


def test_shipped_modules_are_reachable_from_entry_points():
    violations = check_import_graph.check_reachable(config)
    assert not violations, "\n".join(violations)


def test_names_come_from_defining_modules_or_facades():
    violations = check_import_graph.check_facades(config)
    assert not violations, "\n".join(violations)
