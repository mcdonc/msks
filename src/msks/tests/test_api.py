"""API-level tests over ASGITransport with a stubbed microvm seam."""

from pathlib import Path

import httpx
import pytest
from msks.app import build_app
from msks.microvm.errors import MicrovmError, MicrovmTimeoutError
from msks.microvm.spec import VmInfo, VmSpec, VmStatus
from msks.server.api import build_api
from msks.settings import NetSettings, ServerSettings, Settings, VmmSettings
from sqlalchemy.exc import IntegrityError, OperationalError

TOKEN = "test-token"


def auth(token: str = TOKEN) -> dict:
    return {"Authorization": f"Bearer {token}"}


class StubMicrovm:
    """Records seam calls; reports a controllable status per workspace."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []
        self.statuses: dict[str, VmStatus] = {}
        self.fail_prepare = False
        self.seen_specs: dict[str, VmSpec] = {}

    async def prepare(self, spec: VmSpec) -> None:
        if self.fail_prepare:
            raise MicrovmError("prepare boom")
        self.calls.append(("prepare", spec.workspace_id))
        self.seen_specs[spec.workspace_id] = spec

    async def launch(self, spec: VmSpec) -> None:
        self.calls.append(("launch", spec.workspace_id))
        self.seen_specs[spec.workspace_id] = spec
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
        net=NetSettings(enabled=False),
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
        net=NetSettings(enabled=False),
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
    http, app, stub = client

    async def lose(spec, image_hash=None, host=None):
        raise IntegrityError("stmt", {}, Exception("unique"))

    monkeypatch.setattr(app.state.model, "create_workspace", lose)
    response = await http.post(
        "/api/v1/workspaces",
        json={"id": "ws-race", "kernel": "/k", "rootfs": "/r"},
        headers=auth(),
    )
    assert response.status_code == 409
    # The winner's row now owns whatever blank artifacts sit at the
    # id's paths — the loser cleans nothing (that would break the
    # row-exists-⇒-artifacts-exist invariant).
    assert ("cleanup", "ws-race") not in stub.calls


async def test_create_race_after_prepare_answers_409(client, monkeypatch) -> None:
    """A racer that won between the 404 check and a strict-prepare
    refusal turns the 503 into the honest 409."""
    http, app, _stub = client
    real_get = app.state.model.get_workspace
    checks = 0

    async def first_look_then_real(workspace_id):
        nonlocal checks
        checks += 1
        if checks == 1:
            return None  # the 404 pre-check: no workspace yet
        return await real_get(workspace_id)  # the racer has since won

    monkeypatch.setattr(app.state.model, "get_workspace", first_look_then_real)
    await app.state.model.create_workspace(
        VmSpec(workspace_id="ws-lost", kernel=Path("/k"), rootfs=Path("/r"))
    )
    _stub.fail_prepare = True
    response = await http.post(
        "/api/v1/workspaces",
        json={"id": "ws-lost", "kernel": "/k", "rootfs": "/r"},
        headers=auth(),
    )
    assert response.status_code == 409
    assert response.json()["detail"] == "workspace exists"


async def test_prepare_failure_leaves_no_trace(client) -> None:
    """A refused create writes no row — and removes nothing (#14):
    a leftover artifact from a previous workspace of the id stays
    for the operator to clear by hand."""
    http, _app, stub = client
    stub.fail_prepare = True
    response = await http.post(
        "/api/v1/workspaces",
        json={"id": "ws-f", "kernel": "/k", "rootfs": "/r"},
        headers=auth(),
    )
    assert response.status_code == 503
    assert "prepare boom" in response.json()["detail"]
    assert ("cleanup", "ws-f") not in stub.calls
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


async def test_reset_on_foreign_host_is_409(client) -> None:
    """The overlay lives on its owning host; resetting from another
    host must refuse instead of no-op'ing on this host's file and
    reporting a pristine root (#14)."""
    http, app, stub = client
    await http.post(
        "/api/v1/workspaces",
        json={"id": "ws-foreign", "kernel": "/k", "rootfs": "/r"},
        headers=auth(),
    )
    app.state.settings.vmm.host_name = "elsewhere"
    response = await http.post("/api/v1/workspaces/ws-foreign/reset", headers=auth())
    assert response.status_code == 409
    assert "lives on host" in response.json()["detail"]
    assert ("reset", "ws-foreign") not in stub.calls


async def test_stop_on_foreign_host_is_409(client) -> None:
    """A stop that cannot reach the VMM must not mark it stopped."""
    http, app, stub = client
    await http.post(
        "/api/v1/workspaces",
        json={"id": "ws-s", "kernel": "/k", "rootfs": "/r"},
        headers=auth(),
    )
    app.state.settings.vmm.host_name = "elsewhere"
    response = await http.post("/api/v1/workspaces/ws-s/stop", headers=auth())
    assert response.status_code == 409
    assert ("shutdown", "ws-s") not in stub.calls
    status = await http.get("/api/v1/workspaces/ws-s", headers=auth())
    assert status.json()["status"] != "stopped"


async def test_delete_on_foreign_host_is_409(client) -> None:
    """Deleting the row from a non-owning host would orphan a running
    VM — every route 404s without the row."""
    http, app, stub = client
    await http.post(
        "/api/v1/workspaces",
        json={"id": "ws-d", "kernel": "/k", "rootfs": "/r"},
        headers=auth(),
    )
    app.state.settings.vmm.host_name = "elsewhere"
    response = await http.delete("/api/v1/workspaces/ws-d", headers=auth())
    assert response.status_code == 409
    assert ("cleanup", "ws-d") not in stub.calls
    still = await http.get("/api/v1/workspaces/ws-d", headers=auth())
    assert still.status_code == 200


async def test_k8s_create_records_no_host(client, monkeypatch) -> None:
    """Placement is a local-backend fact: on the k8s driver the row
    records no host, so any daemon in the cluster may start it."""
    http, app, _stub = client
    app.state.settings.vmm.driver = "k8s"
    response = await http.post(
        "/api/v1/workspaces",
        json={"id": "ws-k8s", "kernel": "/k", "rootfs": "/r", "egress": False},
        headers=auth(),
    )
    assert response.status_code == 201
    assert response.json()["host"] is None
    app.state.settings.vmm.driver = "local"


async def test_image_pinned_by_workspace_artifacts(client) -> None:
    """An image with live workspaces cannot be removed (#14): the
    overlay backs it; deleting the workspace releases the pin."""
    # allow-deferred-import: module-scope would be circular
    # (test_imagestore imports TOKEN/StubMicrovm/auth from here).
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


async def test_create_records_egress(client) -> None:
    """Workspaces get egress by default (#52); "egress": false opts
    into the no-NIC posture."""
    http, _app, _stub = client
    created = await http.post(
        "/api/v1/workspaces",
        json={"id": "ws-eg", "kernel": "/k", "rootfs": "/r", "egress": True},
        headers=auth(),
    )
    assert created.status_code == 201
    assert created.json()["egress"] is True
    fetched = await http.get("/api/v1/workspaces/ws-eg", headers=auth())
    assert fetched.json()["egress"] is True
    plain = await http.post(
        "/api/v1/workspaces",
        json={"id": "ws-plain", "kernel": "/k", "rootfs": "/r"},
        headers=auth(),
    )
    assert plain.json()["egress"] is True  # the default
    quiet = await http.post(
        "/api/v1/workspaces",
        json={"id": "ws-quiet", "kernel": "/k", "rootfs": "/r", "egress": False},
        headers=auth(),
    )
    assert quiet.json()["egress"] is False


async def test_create_refuses_egress_on_k8s(client, monkeypatch) -> None:
    """Egress is the create default, so the k8s backend refuses at
    CREATE (#70 review) — not at first boot, which would trap the id
    until delete+recreate."""
    http, app, _stub = client
    monkeypatch.setattr(app.state.settings.vmm, "driver", "k8s")
    refused = await http.post(
        "/api/v1/workspaces",
        json={"id": "ws-k8s", "kernel": "/k", "rootfs": "/r", "egress": True},
        headers=auth(),
    )
    assert refused.status_code == 400
    assert 'egress": false' in refused.json()["detail"]
    quiet = await http.post(
        "/api/v1/workspaces",
        json={"id": "ws-k8s", "kernel": "/k", "rootfs": "/r", "egress": False},
        headers=auth(),
    )
    assert quiet.status_code == 201


async def test_create_with_user_data_reaches_the_row(client) -> None:
    """user_data (#41) rides the create into the row and the seam's
    spec: the payload is echoed verbatim and prepared for boot."""
    http, _app, stub = client
    payload = "#!/bin/sh\necho seeded > /root/stamp\n"
    created = await http.post(
        "/api/v1/workspaces",
        json={"id": "ws-ud", "kernel": "/k", "rootfs": "/r", "user_data": payload},
        headers=auth(),
    )
    assert created.status_code == 201, created.text
    assert created.json()["user_data"] == payload
    assert stub.seen_specs["ws-ud"].user_data == payload
    fetched = await http.get("/api/v1/workspaces/ws-ud", headers=auth())
    assert fetched.json()["user_data"] == payload


async def test_create_rejects_empty_user_data(client) -> None:
    http, _app, _stub = client
    empty = await http.post(
        "/api/v1/workspaces",
        json={"id": "ws-ud", "kernel": "/k", "rootfs": "/r", "user_data": "  \n"},
        headers=auth(),
    )
    assert empty.status_code == 400
    assert "user_data is empty" in empty.json()["detail"]
    oversized = await http.post(
        "/api/v1/workspaces",
        json={
            "id": "ws-ud",
            "kernel": "/k",
            "rootfs": "/r",
            "user_data": "x" * 65537,
        },
        headers=auth(),
    )
    assert oversized.status_code == 422


async def test_create_accepts_both_payload_forms(client) -> None:
    """cloud-init runs #! scripts and cloud-config documents alike
    (#41); an image declares its provisioner for the operator, and
    create accepts both forms for a declared image and an undeclared
    one alike (explicit-artifact boots have no manifest at all)."""
    # allow-deferred-import: module-scope would be circular
    # (test_imagestore imports TOKEN/StubMicrovm/auth from here).
    import json

    from test_imagestore import build_containerdisk

    http, app, _stub = client
    state_dir = app.state.settings.vmm.state_dir
    state_dir.mkdir(parents=True, exist_ok=True)
    manifest = {
        "schema": 2,
        "name": "img-cloud",
        "version": "1.0",
        "cmdline": "console=ttyS0 root=/dev/vda ro",
        "vsock_shell_port": 1023,
        "capabilities": {"provisioner": "cloud-init"},
    }
    archive = state_dir / "img-cloud.tar"
    build_containerdisk(
        archive,
        schema=False,
        members={
            "boot/vmlinuz": b"kernel-bytes",
            "boot/initrd.img": b"initrd-bytes",
            "disk/rootfs.ext4": b"rootfs-bytes",
            "disk/image.json": json.dumps(manifest).encode(),
        },
    )
    imported = await http.post(
        "/api/v1/images", json={"source": str(archive)}, headers=auth()
    )
    assert imported.status_code == 201, imported.text
    listed = await http.get("/api/v1/images", headers=auth())
    assert listed.json()[0]["provisioner"] == "cloud-init"

    cloud_config = "#cloud-config\npackages: []\n"
    for wid, body_extra in (
        ("ws-cc", {"image": "img-cloud", "user_data": cloud_config}),
        ("ws-sh", {"image": "img-cloud", "user_data": "#!/bin/sh\n"}),
        # No manifest, no declaration: the same acceptance (msksd
        # cannot police a foreign guest's consumer).
        ("ws-bare", {"kernel": "/k", "rootfs": "/r", "user_data": cloud_config}),
    ):
        created = await http.post(
            "/api/v1/workspaces", json={"id": wid, **body_extra}, headers=auth()
        )
        assert created.status_code == 201, created.text
        assert created.json()["user_data"] == body_extra["user_data"]


async def test_workspace_mutation_is_refused_with_a_named_error(client) -> None:
    """user_data is create-time (#41): PUT/PATCH answer a 405 that
    says what to do instead, and an unknown id still 404s."""
    http, _app, _stub = client
    await http.post(
        "/api/v1/workspaces",
        json={"id": "ws-fix", "kernel": "/k", "rootfs": "/r"},
        headers=auth(),
    )
    for method in ("put", "patch"):
        response = await getattr(http, method)(
            "/api/v1/workspaces/ws-fix",
            json={"user_data": "#!/bin/sh\ntrue\n"},
            headers=auth(),
        )
        assert response.status_code == 405
        assert "delete the workspace and recreate" in response.json()["detail"]
    missing = await http.patch(
        "/api/v1/workspaces/ghost", json={"cpus": 4}, headers=auth()
    )
    assert missing.status_code == 404


async def test_create_refuses_user_data_on_k8s(client, monkeypatch) -> None:
    """The k8s runner does not build seed disks yet: refuse at create
    (the egress shape) instead of storing a payload nothing runs."""
    http, app, _stub = client
    monkeypatch.setattr(app.state.settings.vmm, "driver", "k8s")
    refused = await http.post(
        "/api/v1/workspaces",
        json={
            "id": "ws-k8s-ud",
            "kernel": "/k",
            "rootfs": "/r",
            "egress": False,
            "user_data": "#!/bin/sh\ntrue\n",
        },
        headers=auth(),
    )
    assert refused.status_code == 400
    assert "without user_data" in refused.json()["detail"]
