"""Model-layer tests: CRUD, tokens, bootstrap, status validation."""

from contextlib import closing
from pathlib import Path

import pytest
from msks.app import App, build_app
from msks.microvm import VmSpec
from msks.model import hash_token, new_token
from msks.settings import ServerSettings, Settings
from sqlalchemy.exc import OperationalError


@pytest.fixture
async def app_for(tmp_path: Path):
    """Build apps over a per-test database; every engine disposed on
    teardown so pooled sqlite connections close deterministically
    (no unclosed-database ResourceWarnings from the GC)."""
    made: list[App] = []

    def factory(bootstrap: str | None = None) -> App:
        settings = Settings(
            server=ServerSettings(db_path=tmp_path / "t.db", bootstrap_token=bootstrap)
        )
        app = build_app(settings)
        made.append(app)
        return app

    yield factory
    for app in made:
        await app.state.model.close()


def spec(wid: str = "ws1", **kw) -> VmSpec:
    fields = {
        "workspace_id": wid,
        "kernel": "/k",
        "rootfs": "/r",
        "initrd": None,
        "cpus": 2,
        "mem_mib": 512,
    }
    fields.update(kw)
    return VmSpec(**fields)


async def test_create_and_get_workspace(app_for) -> None:
    app = app_for()
    await app.state.model.create_all()
    row = await app.state.model.create_workspace(spec(initrd="/i"))
    assert row["id"] == "ws1"
    assert row["status"] == "created"
    assert row["initrd"] == "/i"
    fetched = await app.state.model.get_workspace("ws1")
    assert fetched["cpus"] == 2


async def test_get_absent_workspace(app_for) -> None:
    app = app_for()
    await app.state.model.create_all()
    assert await app.state.model.get_workspace("nope") is None


async def test_list_workspaces(app_for) -> None:
    app = app_for()
    await app.state.model.create_all()
    await app.state.model.create_workspace(spec("a"))
    await app.state.model.create_workspace(spec("b"))
    ids = [row["id"] for row in await app.state.model.list_workspaces()]
    assert ids == ["a", "b"]


async def test_set_status_roundtrip_and_reject(app_for) -> None:
    app = app_for()
    await app.state.model.create_all()
    await app.state.model.create_workspace(spec())
    assert await app.state.model.set_status("ws1", "running") is True
    assert (await app.state.model.get_workspace("ws1"))["status"] == "running"
    assert await app.state.model.set_status("ghost", "running") is False
    with pytest.raises(ValueError, match="unknown workspace status"):
        await app.state.model.set_status("ws1", "teleported")


async def test_delete_workspace(app_for) -> None:
    app = app_for()
    await app.state.model.create_all()
    await app.state.model.create_workspace(spec())
    assert await app.state.model.delete_workspace("ws1") is True
    assert await app.state.model.delete_workspace("ws1") is False


async def test_token_lifecycle(app_for) -> None:
    app = app_for()
    await app.state.model.create_all()
    token_id, plaintext = await app.state.model.create_token("cli")
    assert await app.state.model.token_valid(plaintext) is True
    assert await app.state.model.token_valid("wrong") is False
    rows = await app.state.model.list_tokens()
    assert rows[0]["name"] == "cli"
    assert await app.state.model.revoke_token(token_id) is True
    assert await app.state.model.token_valid(plaintext) is False
    assert await app.state.model.revoke_token(999) is False


async def test_bootstrap_token_inserts_once(app_for) -> None:
    app = app_for(bootstrap="boot-secret")
    await app.state.model.create_all()
    assert await app.state.model.bootstrap_token() == "boot-secret"
    assert await app.state.model.bootstrap_token() is None
    assert await app.state.model.token_valid("boot-secret") is True


async def test_bootstrap_absent_without_config(app_for) -> None:
    app = app_for()
    await app.state.model.create_all()
    assert await app.state.model.bootstrap_token() is None


async def test_engine_is_cached(app_for) -> None:
    app = app_for()
    assert app.state.model.engine() is app.state.model.engine()


async def test_engine_recreates_after_close(app_for) -> None:
    app = app_for()
    await app.state.model.create_all()
    await app.state.model.close()
    await app.state.model.create_all()
    assert await app.state.model.get_workspace("x") is None


def test_hash_and_new_tokens() -> None:
    first = new_token()
    assert first != new_token()
    assert hash_token(first) == hash_token(first)
    assert len(hash_token(first)) == 64


async def test_close_without_engine(app_for) -> None:
    app = app_for()
    await app.state.model.close()
    assert app.state.model._engine is None


async def test_migrate_recovers_from_torn_migration(tmp_path: Path, app_for) -> None:
    # A power cut between 0001's committed DDL and its version stamp
    # leaves tables present with no alembic_version row; migrate()
    # stamps head instead of wedging on "table already exists".
    import sqlite3

    from msks.model.db import Base, engine_for

    settings = Settings(server=ServerSettings(db_path=tmp_path / "t.db"))
    engine = engine_for(settings.server.db_path)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    await engine.dispose()
    with closing(sqlite3.connect(settings.server.db_path)) as connection:
        names = {
            name
            for (name,) in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
    assert "tokens" in names  # DDL present, no alembic_version row
    app = app_for()  # Model takes the app (house rule)
    app.state.settings = settings
    app.state.model.migrate()  # must not raise
    with closing(sqlite3.connect(settings.server.db_path)) as connection:
        stamped = connection.execute(
            "SELECT version_num FROM alembic_version"
        ).fetchall()
    assert stamped


def test_migrate_reraises_unrelated_operational_errors(
    tmp_path: Path, monkeypatch
) -> None:
    # Only the torn-migration "already exists" heals; any other
    # OperationalError (locked db, io error) surfaces unchanged.
    from alembic import command as alembic_command
    from msks.model import model as model_mod

    def boom(config, revision):
        raise OperationalError("statement", {}, Exception("database is locked"))

    monkeypatch.setattr(alembic_command, "upgrade", boom)
    settings = Settings(server=ServerSettings(db_path=tmp_path / "locked.db"))
    with pytest.raises(OperationalError, match="locked"):
        model_mod.Model(App(settings)).migrate()
