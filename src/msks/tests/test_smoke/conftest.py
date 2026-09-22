"""The smoke suite runs under its own timeout ceiling (#157).

Smoke tests boot real VMs; a single run spends
minutes in legitimate waiting. The unit ceiling (30s, from
pyproject's addopts) would cut them off mid-boot, so every test
collected from THIS directory gets the marker-level override
pytest-timeout honors over the ini default: 30 minutes -- still a
ceiling (a wedged boot fails loudly, named, instead of hanging the
nightly until the job timeout kills it silently).

The path filter is load-bearing: this hook receives EVERY item in
the session (conftest hooks are session-wide regardless of where the
conftest lives), and marking unit tests would lift the ceiling from
precisely the runs it exists to protect.
"""

from pathlib import Path

import pytest

_HERE = Path(__file__).parent


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    for item in items:
        if item.path.is_relative_to(_HERE):
            if item.get_closest_marker("timeout") is not None:
                # A test that carries its own budget (the opt-in dev
                # bootstrap: downloads past the default ceiling)
                # keeps it; the marker closest to the item wins.
                continue
            item.add_marker(pytest.mark.timeout(1800))
