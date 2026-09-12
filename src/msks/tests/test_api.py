"""API-level tests over ASGITransport with a stubbed microvm seam."""

from pathlib import Path

import httpx
import pytest
from msks.app import build_app
from msks.microvm.spec import VmInfo, VmSpec, VmStatus
from msks.server.api import build_api
from msks.settings import ServerSettings, Settings

TOKEN = "test-token"


def auth(token: str = TOKEN) -> dict:
    return {"Authorization": f"Bearer {token}"}


class StubMicrovm:
    """Records seam calls; reports a controllable status per workspace."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []
        self.statuses: dict[str, VmStatus] = {}

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


@pytest.fixture
async def client(tmp_path: Path):
    settings = Settings(
        server=ServerSettings(
            db_path=tmp_path / "api.db",
            bootstrap_token=TOKEN,
            event_poll_s=10.0,
        )
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
    assert created.status_code == 200
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
    assert created.status_code == 200
    assert created.json()["status"] == "created"
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
    assert bad.status_code == 422
