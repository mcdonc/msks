import os
import shutil
from pathlib import Path

import pytest
from msks.app import App
from msks.model.db import tighten_db_mode
from msks.model.model import Model
from msks.settings import ServerSettings, Settings

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


@pytest.fixture(scope="session", autouse=True)
def preseeded_migrations(tmp_path_factory):
    """Give every fresh test database a pre-migrated copy.

    ``Model.migrate`` walks the full Alembic chain to head on every
    app startup, and this suite builds a fresh app (so a fresh
    database) per test — the walk dominated the suite's setup time
    while proving nothing per-test that the migration tests do not
    already pin. The session builds one template at head, and a
    migrate onto an absent path copies it instead of re-walking the
    chain. A path that already has a file (the torn-migration and
    legacy-schema tests prepare their own databases first) keeps
    the real walk, so the heal and upgrade paths stay exercised.
    The template is rebuilt from this tree at session start, one
    per worker, so it always matches the code under test.
    """
    template = tmp_path_factory.mktemp("migrated-template") / "head.db"
    settings = Settings(server=ServerSettings(db_path=template))
    Model(App(settings)).migrate()
    real_migrate = Model.migrate

    def migrate(self) -> None:
        db_path = self._db_path()
        if db_path.exists():
            real_migrate(self)
            return
        shutil.copyfile(template, db_path)
        tighten_db_mode(db_path)

    Model.migrate = migrate
    yield


@pytest.fixture(autouse=True)
def isolated_client_config(
    client_config_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Keep the client's first-run template out of the home tree.

    Every ``msks`` invocation reads (and on first run generates)
    ``msks.yaml`` under ``$MSKSC_CONFIG_DIR`` (#314); the suite
    points that root at a throwaway directory so a bare
    ``cli.main`` run never writes into the operator's
    ``~/.config``. Tests that exercise the documented default-path
    resolution relocate it themselves, the way the msksd tests
    do.
    """
    monkeypatch.setenv("MSKSC_CONFIG_DIR", str(client_config_root))


@pytest.fixture(scope="session")
def client_config_root(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """The throwaway config-tree root for one test session."""
    return tmp_path_factory.mktemp("msks-client-config")


@pytest.fixture(autouse=True)
def devenv_shell_presets(monkeypatch: pytest.MonkeyPatch) -> None:
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
