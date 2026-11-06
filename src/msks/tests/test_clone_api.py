"""Workspace cloning through the API (#773).

A clone is a full fork of a stopped workspace: both disks copy,
the create-time config rides the new row, per-instance state mints
fresh, verdict rows stay with the source, and the clone starts
outside every source-scoped placeholder. The suite pins the copy
contract, the refusal contract (a live source, a taken name, a
foreign host, missing artifacts, the floor), and the CLI surface.
"""

from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from msks.app import build_app
from msks.server.api import build_api
from msks.server.api.volumes import home_volume_lock
from msks.settings import NetSettings, ServerSettings, Settings, VmmSettings
from msks.spec.vm import VmStatus
from test_api import TOKEN, StubMicrovm, auth

from msks import persist

OVERLAY_BYTES = b"qcow2-overlay-bytes".ljust(4096, b"o")
VOLUME_BYTES = b"ext4-volume-bytes".ljust(4096, b"v")


@pytest.fixture
async def clone_api(tmp_path: Path):
    """The real API surface with the seam stubbed, and its parts."""
    settings = Settings(
        vmm=VmmSettings(state_dir=tmp_path / "vms"),
        net=NetSettings(enabled=False),
        server=ServerSettings(
            db_path=tmp_path / "clone.db",
            bootstrap_token=TOKEN,
            event_poll_s=10.0,
        ),
    )
    app = build_app(settings)
    stub = StubMicrovm()
    app.state.microvm = stub
    api = build_api(app)
    async with api.router.lifespan_context(api):
        transport = httpx.ASGITransport(app=api)
        async with httpx.AsyncClient(
            transport=transport, base_url="https://test"
        ) as http:
            yield SimpleNamespace(
                http=http, transport=transport, api=api, app=app, stub=stub
            )


async def make_source(clone_api, name: str = "seed", **fields) -> dict:
    """One stopped workspace with both artifacts planted: the
    stub creates no files, so the tests plant the bytes the clone
    copies."""
    from testkeys import mint

    _private, public = mint("ed25519")
    body = {
        "name": name,
        "kernel": "/k",
        "rootfs": "/r",
        "ssh_pubkey": f"{public} operator@laptop",
        **fields,
    }
    created = await clone_api.http.post(
        "/api/v1/workspaces", json=body, headers=auth()
    )
    assert created.status_code == 201, created.text
    row = created.json()
    state_dir = clone_api.app.state.settings.vmm.state_dir
    overlay = persist.overlay_path(state_dir, row["id"])
    overlay.parent.mkdir(parents=True, exist_ok=True)
    overlay.write_bytes(OVERLAY_BYTES)
    home = persist.home_volume_path(state_dir, row["id"])
    home.parent.mkdir(parents=True, exist_ok=True)
    home.write_bytes(VOLUME_BYTES)
    return row


def artifacts(clone_api, workspace_id: str) -> tuple[Path, Path]:
    state_dir = clone_api.app.state.settings.vmm.state_dir
    return (
        persist.overlay_path(state_dir, workspace_id),
        persist.home_volume_path(state_dir, workspace_id),
    )


async def clone(clone_api, source: str, name: str):
    return await clone_api.http.post(
        f"/api/v1/workspaces/{source}/clone",
        json={"name": name},
        headers=auth(),
    )


async def test_clone_copies_both_disks_and_row(clone_api):
    """The happy path: the clone's row carries the source's
    create-time config under a fresh id, both disks copy
    byte-for-byte, the source keeps everything, and no seed disk
    forks (the payload rides the row; the clone's first start
    builds its own)."""
    row = await make_source(
        clone_api,
        egress_mode="static",
        allow=[".debian.org"],
        user_data="#!/bin/sh\necho seeded",
        user="alice",
        cpus=4,
        mem_mib=4096,
    )
    made = await clone(clone_api, row["id"], "task-42")
    assert made.status_code == 201, made.text
    fresh = made.json()
    assert fresh["id"] != row["id"]
    assert fresh["name"] == "task-42"
    assert fresh["status"] == "created"
    for field in (
        "kernel",
        "initrd",
        "rootfs",
        "cmdline",
        "cpus",
        "mem_mib",
        "root_mib",
        "home_mib",
        "image_hash",
        "egress",
        "egress_mode",
        "egress_allowlist",
        "user_data",
        "login_user",
        "timezone",
        "mounts",
    ):
        assert fresh[field] == row[field], field
    # The identity's comment names the clone, not the source.
    assert fresh["ssh_pubkey"].split()[:2] == row["ssh_pubkey"].split()[:2]
    assert fresh["ssh_pubkey"].endswith("msks-client:task-42")
    # Both disks copied; the source's untouched.
    src_overlay, src_home = artifacts(clone_api, row["id"])
    dst_overlay, dst_home = artifacts(clone_api, fresh["id"])
    assert dst_overlay.read_bytes() == OVERLAY_BYTES
    assert dst_home.read_bytes() == VOLUME_BYTES
    assert src_overlay.read_bytes() == OVERLAY_BYTES
    assert src_home.read_bytes() == VOLUME_BYTES
    # The seed never forks: the clone's vm directory holds no
    # seed, while the payload rides its row.
    state_dir = clone_api.app.state.settings.vmm.state_dir
    assert not persist.seed_path(state_dir, fresh["id"]).exists()
    assert fresh["user_data"] == "#!/bin/sh\necho seeded"


async def test_clone_mints_its_own_address_slice(clone_api):
    """The egress slice is per-instance pool state: the clone's is
    empty until its own first attach, never the source's."""
    row = await make_source(clone_api)
    made = await clone(clone_api, row["name"], "fork")
    assert made.status_code == 201, made.text
    assert made.json()["egress_slice"] is None


async def test_clone_leaves_verdicts_with_the_source(clone_api):
    """In-effect verdict rows never copy: the clone's posture is
    the mode and allowlist it inherited, and each ``allow
    forever`` is re-earned."""
    row = await make_source(clone_api, egress_mode="interactive")
    made = await clone(clone_api, row["id"], "fork")
    assert made.status_code == 201, made.text
    model = clone_api.app.state.model
    consent = await model.egress_consent.list_active(row["id"])
    assert consent == []
    assert await model.egress_consent.list_active(made.json()["id"]) == []


async def test_clone_refuses_a_running_source(clone_api):
    """The row's status refuses first; the seam's truth-guard
    refuses a row the watcher has not caught up with."""
    row = await make_source(clone_api)
    await clone_api.app.state.model.set_status(row["id"], "running")
    refused = await clone(clone_api, row["id"], "fork")
    assert refused.status_code == 409
    assert "stop it before cloning it" in refused.json()["detail"]
    # The row says stopped, the seam says live (#80's spawn
    # window): the seam wins.
    await clone_api.app.state.model.set_status(row["id"], "stopped")
    clone_api.stub.statuses[row["id"]] = VmStatus.RUNNING
    truth = await clone(clone_api, row["id"], "fork")
    assert truth.status_code == 409
    assert "stop it before cloning it" in truth.json()["detail"]


async def test_clone_refuses_a_taken_name(clone_api):
    row = await make_source(clone_api)
    first = await clone(clone_api, row["id"], "fork")
    assert first.status_code == 201, first.text
    second = await clone(clone_api, row["id"], "fork")
    assert second.status_code == 409
    assert second.json()["detail"] == "workspace exists"


async def test_clone_answers_404_for_an_unknown_source(clone_api):
    refused = await clone(clone_api, "nobody", "fork")
    assert refused.status_code == 404


async def test_clone_validates_the_name(clone_api):
    """The name rides the create grammar: the charset, the length,
    and the required-ness all answer the schema's own refusal."""
    row = await make_source(clone_api)
    for bad in ("", "-lead", "Upper", "a" * 65, "under_score"):
        refused = await clone_api.http.post(
            f"/api/v1/workspaces/{row['id']}/clone",
            json={"name": bad},
            headers=auth(),
        )
        assert refused.status_code == 422, bad
    nameless = await clone_api.http.post(
        f"/api/v1/workspaces/{row['id']}/clone", json={}, headers=auth()
    )
    assert nameless.status_code == 422


async def test_clone_names_a_missing_artifact(clone_api):
    """A source without its volume file is a named 507-side
    refusal (the daemon's), never a silent blank fork — and the
    overlay this clone already installed goes with it."""
    row = await make_source(clone_api)
    state_dir = clone_api.app.state.settings.vmm.state_dir
    persist.home_volume_path(state_dir, row["id"]).unlink()
    refused = await clone(clone_api, row["id"], "fork")
    assert refused.status_code == 503
    assert "missing" in refused.json()["detail"]
    # The first copy's target did not survive the second's
    # refusal: nothing half-copied outlives the clone.
    leftovers = list((state_dir / "vms").glob("*/root.qcow2"))
    assert {p.read_bytes() for p in leftovers} == {OVERLAY_BYTES}


async def test_clone_refuses_leftovers_at_its_target(clone_api, monkeypatch):
    """A file at the fresh id's target path is debris, never data
    to adopt — and the refusal leaves the debris alone (the
    create route's contract: the operator clears it)."""
    import msks.server.api.clone as clone_module

    async def fixed_mint(model):
        return "0123456789"

    row = await make_source(clone_api)
    monkeypatch.setattr(clone_module, "mint_workspace_id", fixed_mint)
    state_dir = clone_api.app.state.settings.vmm.state_dir
    debris = persist.overlay_path(state_dir, "0123456789")
    debris.parent.mkdir(parents=True, exist_ok=True)
    debris.write_bytes(b"predecessor debris")
    refused = await clone(clone_api, row["id"], "fork")
    assert refused.status_code == 503
    assert "already exists" in refused.json()["detail"]
    assert debris.read_bytes() == b"predecessor debris"


async def test_clone_answers_the_floor(clone_api, monkeypatch):
    """A state disk under the floor refuses before any byte
    moves."""
    from msks import storage

    row = await make_source(clone_api)
    tight = {"free": 100 << 20, "total": 1 << 40, "used": 0}
    monkeypatch.setattr(storage, "state_usage", lambda d: tight)
    refused = await clone(clone_api, row["id"], "fork")
    assert refused.status_code == 507
    assert "floor" in refused.json()["detail"]


async def set_host(app, workspace_id: str, host: str) -> None:
    """Move a row's placement column directly: placement is a
    create-time fact with no route of its own."""
    from msks.model.db import sessionmaker_for
    from msks.model.workspaces import Workspace
    from sqlalchemy import update

    maker = sessionmaker_for(clone_engine(app))
    async with maker() as session:
        await session.execute(
            update(Workspace)
            .where(Workspace.id == workspace_id)
            .values(host=host)
        )
        await session.commit()


def clone_engine(app):
    return app.state.model.engine()


async def test_clone_refuses_a_foreign_host(clone_api):
    """The source's artifacts live on one host: a clone from
    another answers the placement refusal, never an empty
    fork."""
    row = await make_source(clone_api)
    await set_host(clone_api.app, row["id"], "elsewhere")
    refused = await clone(clone_api, row["id"], "fork")
    assert refused.status_code == 409
    assert "elsewhere" in refused.json()["detail"]


async def test_clone_answers_404_when_the_source_dies_mid_route(
    clone_api, monkeypatch
):
    """A source deleted between the outer fetch and the locked
    re-read answers 404: the clone never copies a vanished
    workspace's paths."""
    import msks.server.api.clone as clone_module

    row = await make_source(clone_api)

    async def mint_and_delete(model):
        await model.delete_workspace(row["id"])
        return "0123456789"

    monkeypatch.setattr(clone_module, "mint_workspace_id", mint_and_delete)
    refused = await clone(clone_api, row["id"], "fork")
    assert refused.status_code == 404


async def test_clone_loses_a_name_race_cleanly(clone_api, monkeypatch):
    """A lost insert race answers the 409 with the copied pair
    removed: the fresh random id's artifacts are unambiguously
    this clone's, so no orphan survives the refusal."""
    from sqlalchemy.exc import IntegrityError

    row = await make_source(clone_api)
    model = clone_api.app.state.model
    real_create = model.create_workspace

    async def racing_create(*args, **kwargs):
        await real_create(*args, **kwargs)
        raise IntegrityError("stmt", {}, Exception("unique name"))

    monkeypatch.setattr(model, "create_workspace", racing_create)
    refused = await clone(clone_api, row["id"], "fork")
    assert refused.status_code == 409
    assert refused.json()["detail"] == "workspace exists"
    state_dir = clone_api.app.state.settings.vmm.state_dir
    # The pair this clone installed is gone: only the source's
    # artifacts remain.
    leftovers = sorted(
        p.name for p in (state_dir / "vms").glob("*/root.qcow2")
    )
    assert leftovers == ["root.qcow2"]


def test_reannotated_pubkey_covers_the_keyless_and_unusual() -> None:
    """The comment rewrite leaves a keyless row keyless and copies
    a line the validator refuses as it stands (a row written
    before the annotation settled)."""
    from msks.server.api.clone import reannotated_pubkey

    assert reannotated_pubkey({"ssh_pubkey": None}, "fork") is None
    row = {"ssh_pubkey": "not-a-key-line", "id": "1a"}
    assert reannotated_pubkey(row, "fork") == "not-a-key-line"


async def test_clone_serializes_against_the_source_move_lock(clone_api):
    """A clone holds the source's move-lock: a boot arriving mid-
    copy waits it out (the lock the start route shares), so the
    copied pair is consistent without a refused start."""
    row = await make_source(clone_api)
    lock = await home_volume_lock(clone_api.app, row["id"])
    assert await lock.acquire() is True
    try:
        await clone_api.app.state.model.set_status(row["id"], "running")
        refused = await clone(clone_api, row["id"], "fork")
        assert refused.status_code == 409
    finally:
        lock.release()
    await clone_api.app.state.model.set_status(row["id"], "created")
    made = await clone(clone_api, row["id"], "fork")
    assert made.status_code == 201, made.text
