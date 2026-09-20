"""Model-layer tests: CRUD, tokens, bootstrap, status validation."""

import sqlite3
from contextlib import closing
from datetime import datetime
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic import command
from msks.app import App, build_app
from msks.microvm import VmSpec
from msks.model import hash_token, new_token
from msks.model import model as model_mod
from msks.model.db import Base, engine_for
from msks.model.model import alembic_config
from msks.settings import ServerSettings, Settings
from sqlalchemy.exc import OperationalError


@pytest.fixture
async def app_for(tmp_path: Path):
    """Build apps over a per-test database; every engine disposed on
    teardown so pooled sqlite connections close deterministically
    (no unclosed-database ResourceWarnings from the GC)."""
    made: list[App] = []

    def factory(
        bootstrap: str | None = None, db_path: Path | None = None
    ) -> App:
        settings = Settings(
            server=ServerSettings(
                db_path=db_path or tmp_path / "t.db", bootstrap_token=bootstrap
            )
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


async def test_create_and_fetch_minted_identity(app_for) -> None:
    """The #111 columns round-trip: the spec's public half plus the
    mint-to-row private half, which never rides the API-facing dict."""
    app = app_for()
    await app.state.model.create_all()
    private_pem = "-----BEGIN OPENSSH PRIVATE KEY-----\n...\n"
    pub = "ecdsa-sha2-nistp256 AAAA msksd:ws1"
    row = await app.state.model.create_workspace(
        spec(ssh_pubkey=pub), ssh_privkey=private_pem
    )
    assert row["ssh_pubkey"] == pub
    assert "ssh_privkey" not in row
    key = await app.state.model.get_ssh_key("ws1")
    assert key == {"public_key": pub, "private_key": private_pem}


async def test_get_ssh_key_without_identity_and_absent(app_for) -> None:
    """A pre-#111 row carries NULL halves; a missing workspace is
    None — the caller distinguishes row-missing from
    identity-missing."""
    app = app_for()
    await app.state.model.create_all()
    await app.state.model.create_workspace(spec())
    key = await app.state.model.get_ssh_key("ws1")
    assert key == {"public_key": None, "private_key": None}
    assert await app.state.model.get_ssh_key("nope") is None


async def test_workspace_carries_artifact_facts(app_for) -> None:
    """The #14 columns round-trip: image binding, owning host, sizes."""
    app = app_for()
    await app.state.model.create_all()
    row = await app.state.model.create_workspace(
        spec(root_mib=512, home_mib=128),
        image_hash="ab" * 32,
        host="metal-2",
    )
    assert (row["image_hash"], row["host"]) == ("ab" * 32, "metal-2")
    assert (row["root_mib"], row["home_mib"]) == (512, 128)
    fetched = await app.state.model.get_workspace("ws1")
    assert fetched["image_hash"] == "ab" * 32
    assert fetched["root_mib"] == 512


async def test_migration_backfills_pre14_rows(tmp_path: Path, app_for) -> None:
    """A database stamped at 0001 upgrades in place: existing rows
    gain the #14 columns with usable defaults, data intact."""
    app = app_for()
    db_path = tmp_path / "t.db"
    config = alembic_config(db_path)
    command.upgrade(config, "0001")
    engine = sa.create_engine(f"sqlite:///{db_path}")
    try:
        with engine.begin() as conn:
            # Reflected off the migrated database, so the insert sees
            # exactly the 0001 shape — a row written before #14.
            workspaces = sa.Table(
                "workspaces", sa.MetaData(), autoload_with=conn
            )
            assert "image_hash" not in workspaces.columns
            conn.execute(
                workspaces.insert().values(
                    id="old",
                    kernel="/k",
                    initrd=None,
                    rootfs="/r",
                    cmdline="c",
                    cpus=1,
                    mem_mib=256,
                    status="stopped",
                    created_at=datetime(2026, 1, 1),
                    updated_at=datetime(2026, 1, 1),
                )
            )
    finally:
        engine.dispose()
    app.state.model.migrate()
    row = await app.state.model.get_workspace("old")
    assert row is not None
    assert row["image_hash"] is None  # explicit-boot rows bind no image
    assert row["host"] is None  # pre-#14 rows adopt the starting host
    assert row["root_mib"] == 10240
    assert row["home_mib"] == 2048
    assert row["egress"] is False  # pre-egress rows keep the no-NIC posture
    assert row["status"] == "stopped"


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


async def test_migrate_recovers_from_torn_migration(
    tmp_path: Path, app_for
) -> None:
    # A power cut between 0001's committed DDL and its version stamp
    # leaves tables present with no alembic_version row; migrate()
    # stamps head instead of wedging on "table already exists".
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
    def boom(config, revision):
        raise OperationalError(
            "statement", {}, Exception("database is locked")
        )

    monkeypatch.setattr(command, "upgrade", boom)
    settings = Settings(server=ServerSettings(db_path=tmp_path / "locked.db"))
    with pytest.raises(OperationalError, match="locked"):
        model_mod.Model(App(settings)).migrate()


def test_migrate_refuses_to_stamp_past_an_older_gap(
    tmp_path, monkeypatch
) -> None:
    """A torn-shaped error on a database already at head must not be
    stamped past: with a longer chain, that gap would skip pending
    DDL — it needs an operator, not a guess (#14 round two)."""
    settings = Settings(server=ServerSettings(db_path=tmp_path / "at-head.db"))
    model_mod.Model(App(settings)).migrate()  # DB now stamped at head

    def boom(config, revision):
        raise OperationalError(
            "statement", {}, Exception("duplicate column name")
        )

    monkeypatch.setattr(command, "upgrade", boom)
    with pytest.raises(OperationalError, match="duplicate column name"):
        model_mod.Model(App(settings)).migrate()


async def test_workspace_egress_default_and_opt_out(app_for) -> None:
    """Egress is the workspace default (#52); egress=False opts out."""
    app = app_for()
    await app.state.model.create_all()
    row = await app.state.model.create_workspace(spec())
    assert row["egress"] is True
    quiet = await app.state.model.create_workspace(spec("ws2", egress=False))
    assert quiet["egress"] is False
    fetched = await app.state.model.get_workspace("ws1")
    assert fetched["egress"] is True


async def test_workspace_carries_user_data(app_for) -> None:
    """The #41 payload round-trips through the row (None when the
    workspace was created without one)."""
    app = app_for()
    await app.state.model.create_all()
    payload = "#!/bin/sh\necho first boot > /root/stamp\n"
    row = await app.state.model.create_workspace(spec(user_data=payload))
    assert row["user_data"] == payload
    fetched = await app.state.model.get_workspace("ws1")
    assert fetched["user_data"] == payload
    plain = await app.state.model.create_workspace(spec("ws2"))
    assert plain["user_data"] is None


async def test_database_file_is_private(app_for, tmp_path: Path) -> None:
    """The database records user_data payloads (#41), which can embed
    tokens: the file is created 0600, whichever path makes it first
    (migrate or the engine) — and a looser mode carried over from a
    pre-#41 database is tightened, not just avoided."""
    db_path = tmp_path / "private" / "msks.db"
    db_path.parent.mkdir(parents=True)
    db_path.write_bytes(b"")  # a pre-#41 file at its old mode
    db_path.chmod(0o644)
    app = app_for(db_path=db_path)
    app.state.model.migrate()
    assert db_path.stat().st_mode & 0o777 == 0o600


async def test_set_sizes_updates_named_columns(app_for) -> None:
    app = app_for()
    await app.state.model.create_all()
    await app.state.model.create_workspace(
        VmSpec(
            workspace_id="ws-size",
            kernel=Path("/k"),
            rootfs=Path("/r"),
            root_mib=10240,
            home_mib=2048,
        )
    )
    assert await app.state.model.set_sizes(
        "ws-size", root_mib=20480, home_mib=4096
    )
    row = await app.state.model.get_workspace("ws-size")
    assert row["root_mib"] == 20480
    assert row["home_mib"] == 4096
    # One side alone keeps the other.
    assert await app.state.model.set_sizes("ws-size", None, 8192)
    row = await app.state.model.get_workspace("ws-size")
    assert row["root_mib"] == 20480
    assert row["home_mib"] == 8192
    assert not await app.state.model.set_sizes("ghost", None, 64)
