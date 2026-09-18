"""The smoke suite runs under its own timeout ceiling (#157).

Smoke tests boot real appliances and workspaces; a single run spends
minutes in legitimate waiting. The unit ceiling (30s, from
pyproject's addopts) would cut them off mid-boot, so every test
collected here gets the marker-level override pytest-timeout honors
over the ini default: 30 minutes -- still a ceiling (a wedged boot
fails loudly, named, instead of hanging the nightly until the job
timeout kills it silently).
"""

import pytest


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    for item in items:
        item.add_marker(pytest.mark.timeout(1800))
