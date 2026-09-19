"""The suite's own timeout ceiling is wired as claimed (#157).

#151's incident: a stall cost 10-15 minutes per run while "passing".
These pin the guardrails' wiring -- the ceiling in addopts, the
durations reporting, the registered marker the smoke override uses,
and the smoke conftest's hook that lifts the ceiling for real boots.
"""

import importlib.util
from pathlib import Path

import pytest

SMOKE_CONFTEST = Path(__file__).parent / "test_smoke" / "conftest.py"


def test_ceiling_is_30s_in_addopts(pytestconfig) -> None:
    assert "--timeout=30" in pytestconfig.getini("addopts")


def test_addopts_report_durations(pytestconfig) -> None:
    assert "--durations=10" in pytestconfig.getini("addopts")


def test_timeout_marker_is_registered() -> None:
    """pytest-timeout's marker exists -- the smoke override and any
    per-test ceiling depend on it being registered by the plugin."""
    assert hasattr(pytest.mark, "timeout")


def test_smoke_conftest_lifts_the_ceiling() -> None:
    """The smoke conftest owns the marker-level override hook that
    pytest-timeout honors above the ini/addopts default -- and the
    hook marks ONLY items from its own directory (#154 r3: the
    filter is load-bearing; without it every unit test in a
    full-tree run would run at the smoke ceiling)."""
    spec = importlib.util.spec_from_file_location(
        "smoke_conftest", SMOKE_CONFTEST
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert callable(module.pytest_collection_modifyitems)

    class Item:
        def __init__(self, path: Path):
            self.path = path
            self.marks: list = []

        def add_marker(self, mark) -> None:
            self.marks.append(mark)

    here = SMOKE_CONFTEST.parent
    smoke_item = Item(here / "test_appliance.py")
    unit_item = Item(here.parent / "test_local_driver.py")
    lookalike = Item(here.parent / "test_smoke_harness.py")
    module.pytest_collection_modifyitems([smoke_item, unit_item, lookalike])
    assert smoke_item.marks, "smoke items must get the override"
    assert not unit_item.marks, "unit items must keep the 30s ceiling"
    assert not lookalike.marks, "a test_smoke_* FILE is not the dir"
