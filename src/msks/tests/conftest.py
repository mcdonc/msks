import os

import pytest

from msks import guestassets

# Use the sysmon coverage engine — on Python 3.14 sys.monitoring measures
# branches and tracks greenlet-executed code natively, which is why CI
# runs with `-n auto` (a single-process run under-counts and shows a
# false low total; see AGENTS.md). No `concurrency` option anywhere:
# sysmon does not support it.
os.environ.setdefault("COVERAGE_CORE", "sysmon")

# Self-provisioned smoke-test assets (#5): when the msks-build-guest
# script has built the guest assets (.devenv/state/guest by default;
# MSKS_GUEST_DIR relocates it) and /dev/kvm is usable, point the
# MSKSD_TEST_* variables at the built artifacts. Explicitly exported
# variables win; when nothing was built the smoke tests keep
# skipping themselves.
for _name, _value in guestassets.smoke_env_defaults(
    guestassets.load_guest_assets(),
).items():
    os.environ.setdefault(_name, _value)


@pytest.fixture(autouse=True)
def msksc_client_dirs(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep the devenv shell's presets out of the suite.

    The shell presets MSKSC_CACHE_DIR and MSKSC_DATA_DIR at the
    worktree's own state (#251) and MSKSD_CONFIG_DIR at the worktree
    root (#262); tests exercise the documented defaults (the XDG
    roots, the home config tree) and their own explicit overrides,
    so the ambient presets never pick the root for them.
    """
    monkeypatch.delenv("MSKSC_CACHE_DIR", raising=False)
    monkeypatch.delenv("MSKSC_DATA_DIR", raising=False)
    monkeypatch.delenv("MSKSD_CONFIG_DIR", raising=False)
