"""API-level tests over ASGITransport with a stubbed microvm seam."""

from pathlib import Path

import httpx
import pytest
from msks.app import build_app
from msks.microvm.errors import MicrovmError, MicrovmTimeoutError
from msks.microvm.spec import VmInfo, VmSpec, VmStatus
from msks.server.api import build_api
from msks.settings import ServerSettings, Settings, VmmSettings
from sqlalchemy.exc import OperationalError

TOKEN = "test-token"


def auth(token: str = TOKEN) -> dict:
    return {"Authorization": f"Bearer {token}"}


class StubMicrovm:
    """Records seam calls; reports a controllable status per workspace."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []
        self.statuses: dict[str, VmStatus] = {}
        self.fail_prepare = False

    async def prepare(self, spec: VmSpec) -> None:
        if self.fail_prepare:
            raise MicrovmError("prepare boom")
        self.calls.append(("prepare", spec.workspace_id))

    async def launch(self, spec: VmSpec) -> None:
        self.calls.append(("launch", spec.workspace_id))
        self.statuses[spec.workspace_id] = VmStatus.RUNNING

    async def info(self, workspace_id: str) -> VmInfo:
        status = self.statuses.get(workspace_id, VmStatus.ABSENT)
        return VmInfo(workspace_id, status)

    async def shutdown(self, workspace_id: str, timeout_s: float | None = None) -> None:
        self.calls.append(("shutdown", workspace_id))
        self.statuses[workspace_id] = VmStatus.STOPPED

    async def kill(self, workspace_id: str) -> None:
        self.calls.append(("kill", workspace_id))
        self.statuses[workspace_id] = VmStatus.STOPPED

    async def cleanup(self, workspace_id: str) -> None:
        self.calls.append(("cleanup", workspace_id))

    async def reset(self, workspace_id: str) -> None:
        self.calls.append(("reset", workspace_id))


@pytest.fixture
async def client(tmp_path: Path):
    settings = Settings(
        vmm=VmmSettings(state_dir=tmp_path / "vms"),
        server=ServerSettings(
            db_path=tmp_path / "api.db",
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
            yield http, app, stub


async def test_lifespan_startup_failure_closes_engine(tmp_path: Path) -> None:
    # A startup step can raise after the engine exists (locked db,
    # full disk): the lifespan must still dispose it, or the GC emits
    # the unclosed-database warning this suite keeps at zero.
    settings = Settings(
        vmm=VmmSettings(state_dir=tmp_path / "vms"),
        server=ServerSettings(db_path=tmp_path / "f.db", bootstrap_token=TOKEN),
    )
    app = build_app(settings)
    model = app.state.model

    async def bootstrap_boom() -> None:
        model.engine()  # the real bootstrap creates the engine first
        raise OperationalError("statement", {}, Exception("database is locked"))

    model.bootstrap_token = bootstrap_boom
    api = build_api(app)
    with pytest.raises(OperationalError, match="locked"):
        async with api.router.lifespan_context(api):
            pass  # pragma: no cover - startup fails before the yield
    assert model._engine is None


async def test_health_is_public(client) -> None:
    http, _app, _stub = client
    response = await http.get("/api/v1/health")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


async def test_auth_required(client) -> None:
    http, _app, _stub = client
    response = await http.get("/api/v1/tokens")
    assert response.status_code == 401
    response = await http.get("/api/v1/tokens", headers=auth("nope"))
    assert response.status_code == 401
    response = await http.get("/api/v1/tokens", headers={"Authorization": "weird"})
    assert response.status_code == 401


async def test_token_admin(client) -> None:
    http, _app, _stub = client
    created = await http.post("/api/v1/tokens", json={"name": "cli"}, headers=auth())
    assert created.status_code == 201
    plaintext = created.json()["token"]
    listed = await http.get("/api/v1/tokens", headers=auth())
    assert listed.status_code == 200
    revoked = await http.delete(
        f"/api/v1/tokens/{created.json()['id']}", headers=auth()
    )
    assert revoked.status_code == 200
    gone = await http.delete("/api/v1/tokens/999", headers=auth())
    assert gone.status_code == 404
    response = await http.get("/api/v1/workspaces", headers=auth(plaintext))
    assert response.status_code == 401
    response = await http.get("/api/v1/workspaces", headers=auth())
    assert response.status_code == 200


async def test_workspace_lifecycle(client) -> None:
    http, _app, stub = client
    created = await http.post(
        "/api/v1/workspaces",
        json={"id": "ws-a", "kernel": "/k", "rootfs": "/r", "mem_mib": 256},
        headers=auth(),
    )
    assert created.status_code == 201
    assert created.json()["status"] == "created"
    # Created with the workspace (#14): the artifacts are prepared at
    # create, and the row records its host and artifact sizes.
    assert ("prepare", "ws-a") in stub.calls
    assert created.json()["host"]
    assert created.json()["root_mib"] == 10240
    assert created.json()["home_mib"] == 2048
    dup = await http.post(
        "/api/v1/workspaces",
        json={"id": "ws-a", "kernel": "/k", "rootfs": "/r"},
        headers=auth(),
    )
    assert dup.status_code == 409
    started = await http.post("/api/v1/workspaces/ws-a/start", headers=auth())
    assert started.json()["status"] == "running"
    assert ("launch", "ws-a") in stub.calls
    listed = await http.get("/api/v1/workspaces", headers=auth())
    assert [row["id"] for row in listed.json()] == ["ws-a"]
    one = await http.get("/api/v1/workspaces/ws-a", headers=auth())
    assert one.json()["cpus"] == 2
    stopped = await http.post("/api/v1/workspaces/ws-a/stop", headers=auth())
    assert stopped.json()["status"] == "stopped"
    # Stop keeps the data (#14): no cleanup, no reset.
    assert ("cleanup", "ws-a") not in stub.calls
    deleted = await http.delete("/api/v1/workspaces/ws-a", headers=auth())
    assert deleted.status_code == 200
    missing = await http.get("/api/v1/workspaces/ws-a", headers=auth())
    assert missing.status_code == 404
    start_missing = await http.post("/api/v1/workspaces/ghost/start", headers=auth())
    assert start_missing.status_code == 404


async def test_workspace_validation(client) -> None:
    http, _app, _stub = client
    bad = await http.post(
        "/api/v1/workspaces", json={"id": "x", "kernel": "/k"}, headers=auth()
    )
    # No rootfs and no catalog to resolve from: a semantic 400, not a
    # schema 422 (kernel/rootfs became optional with the image
    # catalog, #40).
    assert bad.status_code == 400
    assert "required" in bad.json()["detail"]


async def test_microvm_error_maps_to_503(client) -> None:
    http, _app, stub = client
    await http.post(
        "/api/v1/workspaces",
        json={"id": "ws-e", "kernel": "/k", "rootfs": "/r"},
        headers=auth(),
    )

    async def explode(spec):
        raise MicrovmError("boom")

    stub.launch = explode
    response = await http.post("/api/v1/workspaces/ws-e/start", headers=auth())
    assert response.status_code == 503
    assert response.json()["detail"] == "boom"


async def test_delete_falls_back_to_kill(client) -> None:
    http, _app, stub = client
    await http.post(
        "/api/v1/workspaces",
        json={"id": "ws-k", "kernel": "/k", "rootfs": "/r"},
        headers=auth(),
    )

    async def wedged(workspace_id, timeout_s=None):
        raise MicrovmTimeoutError("wedged")

    stub.shutdown = wedged
    response = await http.delete("/api/v1/workspaces/ws-k", headers=auth())
    assert response.status_code == 200
    assert ("kill", "ws-k") in stub.calls


async def test_create_race_maps_to_409(client, monkeypatch) -> None:
    http, app, _stub = client
    from sqlalchemy.exc import IntegrityError

    async def lose(spec, image_hash=None, host=None):
        raise IntegrityError("stmt", {}, Exception("unique"))

    monkeypatch.setattr(app.state.model, "create_workspace", lose)
    response = await http.post(
        "/api/v1/workspaces",
        json={"id": "ws-race", "kernel": "/k", "rootfs": "/r"},
        headers=auth(),
    )
    assert response.status_code == 409


async def test_create_rolls_back_when_prepare_fails(client) -> None:
    """A workspace whose artifacts could not be created leaves no row
    and no half-made artifacts behind (#14)."""
    http, _app, stub = client
    stub.fail_prepare = True
    response = await http.post(
        "/api/v1/workspaces",
        json={"id": "ws-f", "kernel": "/k", "rootfs": "/r"},
        headers=auth(),
    )
    assert response.status_code == 503
    assert "prepare boom" in response.json()["detail"]
    assert ("cleanup", "ws-f") in stub.calls
    gone = await http.get("/api/v1/workspaces/ws-f", headers=auth())
    assert gone.status_code == 404


async def test_create_records_requested_sizes(client) -> None:
    http, _app, _stub = client
    created = await http.post(
        "/api/v1/workspaces",
        json={
            "id": "ws-s",
            "kernel": "/k",
            "rootfs": "/r",
            "root_mib": 512,
            "home_mib": 128,
        },
        headers=auth(),
    )
    assert created.status_code == 201
    row = created.json()
    assert (row["root_mib"], row["home_mib"]) == (512, 128)


async def test_start_on_foreign_host_is_rejected(client) -> None:
    """Placement is a fact about the artifacts (#14): a start on the
    wrong host names where they live instead of booting empties."""
    http, app, _stub = client
    created = await http.post(
        "/api/v1/workspaces",
        json={"id": "ws-x", "kernel": "/k", "rootfs": "/r"},
        headers=auth(),
    )
    assert created.status_code == 201
    recorded_host = created.json()["host"]
    app.state.settings.vmm.host_name = "some-other-host"
    response = await http.post("/api/v1/workspaces/ws-x/start", headers=auth())
    assert response.status_code == 409
    assert (
        f"home volume for workspace ws-x lives on host {recorded_host}"
        in response.json()["detail"]
    )
    # A pre-#14 row without a host is adopted: the artifacts are
    # wherever this daemon finds them.
    await app.state.model.delete_workspace("ws-x")
    await app.state.model.create_workspace(
        VmSpec(workspace_id="ws-x", kernel=Path("/k"), rootfs=Path("/r")),
        image_hash=None,
        host=None,
    )
    response = await http.post("/api/v1/workspaces/ws-x/start", headers=auth())
    assert response.status_code == 200


async def test_reset_stops_then_drops_overlay_only(client) -> None:
    http, _app, stub = client
    await http.post(
        "/api/v1/workspaces",
        json={"id": "ws-r", "kernel": "/k", "rootfs": "/r"},
        headers=auth(),
    )
    await http.post("/api/v1/workspaces/ws-r/start", headers=auth())
    response = await http.post("/api/v1/workspaces/ws-r/reset", headers=auth())
    assert response.status_code == 200
    assert response.json() == {"id": "ws-r", "status": "created"}
    calls = stub.calls
    assert ("reset", "ws-r") in calls
    # Reset stops the VM (the overlay is the running root device) and
    # removes none of the persistent data itself.
    assert calls.index(("shutdown", "ws-r")) < calls.index(("reset", "ws-r"))
    assert ("cleanup", "ws-r") not in calls


async def test_reset_falls_back_to_kill(client) -> None:
    http, _app, stub = client
    await http.post(
        "/api/v1/workspaces",
        json={"id": "ws-w", "kernel": "/k", "rootfs": "/r"},
        headers=auth(),
    )

    async def wedged(workspace_id, timeout_s=None):
        raise MicrovmTimeoutError("wedged")

    stub.shutdown = wedged
    response = await http.post("/api/v1/workspaces/ws-w/reset", headers=auth())
    assert response.status_code == 200
    assert ("kill", "ws-w") in stub.calls


async def test_reset_missing_workspace_is_404(client) -> None:
    http, _app, _stub = client
    response = await http.post("/api/v1/workspaces/ghost/reset", headers=auth())
    assert response.status_code == 404


async def test_image_pinned_by_workspace_artifacts(client) -> None:
    """An image with live workspaces cannot be removed (#14): the
    overlay backs it; deleting the workspace releases the pin."""
    from test_imagestore import build_containerdisk

    http, app, _stub = client
    state_dir = app.state.settings.vmm.state_dir
    state_dir.mkdir(parents=True, exist_ok=True)
    archive = state_dir / "ws-image.tar"
    build_containerdisk(archive)
    imported = await http.post(
        "/api/v1/images", json={"source": str(archive)}, headers=auth()
    )
    assert imported.status_code == 201, imported.text
    digest = imported.json()["hash"]
    created = await http.post(
        "/api/v1/workspaces", json={"id": "ws-img"}, headers=auth()
    )
    assert created.status_code == 201, created.text
    assert created.json()["image_hash"] == digest
    blocked = await http.delete(f"/api/v1/images/{digest}", headers=auth())
    assert blocked.status_code == 409
    assert "ws-img" in blocked.json()["detail"]
    deleted = await http.delete("/api/v1/workspaces/ws-img", headers=auth())
    assert deleted.status_code == 200
    released = await http.delete(f"/api/v1/images/{digest}", headers=auth())
    assert released.status_code == 200


async def test_delete_never_started_workspace(client) -> None:
    http, _app, _stub = client
    await http.post(
        "/api/v1/workspaces",
        json={"id": "never-started", "kernel": "/k", "rootfs": "/r"},
        headers=auth(),
    )
    response = await http.delete("/api/v1/workspaces/never-started", headers=auth())
    assert response.status_code == 200
