"""Model-layer tests: CRUD, tokens, bootstrap, status validation."""

from pathlib import Path

import pytest
from msks.app import build_app
from msks.microvm import VmSpec
from msks.model import hash_token, new_token
from msks.settings import ServerSettings, Settings


def app_for(tmp_path: Path, bootstrap: str | None = None):
    settings = Settings(
        server=ServerSettings(db_path=tmp_path / "t.db", bootstrap_token=bootstrap)
    )
    return build_app(settings)


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


async def test_create_and_get_workspace(tmp_path: Path) -> None:
    app = app_for(tmp_path)
    await app.state.model.create_all()
    row = await app.state.model.create_workspace(spec(initrd="/i"))
    assert row["id"] == "ws1"
    assert row["status"] == "created"
    assert row["initrd"] == "/i"
    fetched = await app.state.model.get_workspace("ws1")
    assert fetched["cpus"] == 2


async def test_get_absent_workspace(tmp_path: Path) -> None:
    app = app_for(tmp_path)
    await app.state.model.create_all()
    assert await app.state.model.get_workspace("nope") is None


async def test_list_workspaces(tmp_path: Path) -> None:
    app = app_for(tmp_path)
    await app.state.model.create_all()
    await app.state.model.create_workspace(spec("a"))
    await app.state.model.create_workspace(spec("b"))
    ids = [row["id"] for row in await app.state.model.list_workspaces()]
    assert ids == ["a", "b"]


async def test_set_status_roundtrip_and_reject(tmp_path: Path) -> None:
    app = app_for(tmp_path)
    await app.state.model.create_all()
    await app.state.model.create_workspace(spec())
    assert await app.state.model.set_status("ws1", "running") is True
    assert (await app.state.model.get_workspace("ws1"))["status"] == "running"
    assert await app.state.model.set_status("ghost", "running") is False
    with pytest.raises(ValueError, match="unknown workspace status"):
        await app.state.model.set_status("ws1", "teleported")


async def test_delete_workspace(tmp_path: Path) -> None:
    app = app_for(tmp_path)
    await app.state.model.create_all()
    await app.state.model.create_workspace(spec())
    assert await app.state.model.delete_workspace("ws1") is True
    assert await app.state.model.delete_workspace("ws1") is False


async def test_token_lifecycle(tmp_path: Path) -> None:
    app = app_for(tmp_path)
    await app.state.model.create_all()
    token_id, plaintext = await app.state.model.create_token("cli")
    assert await app.state.model.token_valid(plaintext) is True
    assert await app.state.model.token_valid("wrong") is False
    rows = await app.state.model.list_tokens()
    assert rows[0]["name"] == "cli"
    assert await app.state.model.revoke_token(token_id) is True
    assert await app.state.model.token_valid(plaintext) is False
    assert await app.state.model.revoke_token(999) is False


async def test_bootstrap_token_inserts_once(tmp_path: Path) -> None:
    app = app_for(tmp_path, bootstrap="boot-secret")
    await app.state.model.create_all()
    assert await app.state.model.bootstrap_token() == "boot-secret"
    assert await app.state.model.bootstrap_token() is None
    assert await app.state.model.token_valid("boot-secret") is True


async def test_bootstrap_absent_without_config(tmp_path: Path) -> None:
    app = app_for(tmp_path)
    await app.state.model.create_all()
    assert await app.state.model.bootstrap_token() is None


async def test_engine_is_cached(tmp_path: Path) -> None:
    app = app_for(tmp_path)
    assert app.state.model.engine() is app.state.model.engine()


async def test_engine_recreates_after_close(tmp_path: Path) -> None:
    app = app_for(tmp_path)
    await app.state.model.create_all()
    await app.state.model.close()
    await app.state.model.create_all()
    assert await app.state.model.get_workspace("x") is None


def test_hash_and_new_tokens() -> None:
    first = new_token()
    assert first != new_token()
    assert hash_token(first) == hash_token(first)
    assert len(hash_token(first)) == 64


async def test_close_without_engine(tmp_path: Path) -> None:
    app = app_for(tmp_path)
    await app.state.model.close()
    assert app.state.model._engine is None
