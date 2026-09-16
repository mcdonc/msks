"""Home-volume export/import through the API (#80).

The byte-stream endpoints move a workspace's /home volume through
the daemon: GET streams the volume file out (backup, migration,
seeding), PUT replaces it atomically from the request body. The
suite pins the refusal contract (a live VM, a foreign host, the k8s
backend, a non-ext4 body, a cut-off upload) and the CLI surface on
top.
"""

import json
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from msks.app import build_app
from msks.client import cli
from msks.server.api import build_api
from msks.settings import NetSettings, ServerSettings, Settings, VmmSettings
from test_api import TOKEN, StubMicrovm, auth
from test_persist import ext4_image

from msks import persist

IMAGE = ext4_image([b"volume-bytes".ljust(persist.HOME_WINDOW_B, b"v")])


@pytest.fixture
async def home_api(tmp_path: Path):
    """The real API surface with the seam stubbed, and its parts."""
    settings = Settings(
        vmm=VmmSettings(state_dir=tmp_path / "vms"),
        net=NetSettings(enabled=False),
        server=ServerSettings(
            db_path=tmp_path / "home.db",
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


async def create_workspace(home_api, workspace_id: str) -> dict:
    created = await home_api.http.post(
        "/api/v1/workspaces",
        json={"id": workspace_id, "kernel": "/k", "rootfs": "/r"},
        headers=auth(),
    )
    assert created.status_code == 201, created.text
    return created.json()


def planted_volume(home_api, workspace_id: str, image: bytes = IMAGE) -> Path:
    """The workspace's home volume, planted with ``image`` (the seam
    stub creates no files)."""
    state_dir = home_api.app.state.settings.vmm.state_dir
    home = persist.home_volume_path(state_dir, workspace_id)
    home.parent.mkdir(parents=True, exist_ok=True)
    home.write_bytes(image)
    return home


async def test_export_unknown_workspace_is_404(home_api) -> None:
    response = await home_api.http.get("/api/v1/workspaces/ghost/home", headers=auth())
    assert response.status_code == 404
    assert "no such workspace" in response.json()["detail"]


async def test_export_streams_the_volume_bytes(home_api) -> None:
    await create_workspace(home_api, "ws-exp")
    home = planted_volume(home_api, "ws-exp")
    response = await home_api.http.get("/api/v1/workspaces/ws-exp/home", headers=auth())
    assert response.status_code == 200
    assert response.content == IMAGE
    assert response.headers["content-length"] == str(len(IMAGE))
    assert response.headers["content-type"] == "application/octet-stream"
    assert response.headers["content-disposition"] == (
        'attachment; filename="ws-exp.ext4"'
    )
    # The stream is a read: the file keeps its contents.
    assert home.read_bytes() == IMAGE


async def test_export_missing_volume_file_is_404(home_api) -> None:
    """A workspace whose volume file was lost (out-of-band removal,
    a pre-#14 row) names the gap; a start rebuilds the volume."""
    await create_workspace(home_api, "ws-novol")
    response = await home_api.http.get(
        "/api/v1/workspaces/ws-novol/home", headers=auth()
    )
    assert response.status_code == 404
    assert "does not exist under the state dir" in response.json()["detail"]


async def test_move_refused_while_the_vm_is_attached(home_api) -> None:
    """Starting/paused/running workspaces keep their volume: an
    export under a writing guest is a torn image, an import under a
    mounted device is lost work."""
    await create_workspace(home_api, "ws-live")
    planted_volume(home_api, "ws-live")
    model = home_api.app.state.model
    for status in ("starting", "running", "paused"):
        await model.set_status("ws-live", status)
        for method in ("GET", "PUT"):
            response = await home_api.http.request(
                method,
                "/api/v1/workspaces/ws-live/home",
                content=IMAGE if method == "PUT" else None,
                headers=auth(),
            )
            assert response.status_code == 409, (status, method)
            assert f"workspace ws-live is {status}" in response.json()["detail"]
    # Stopped again, the same pair answers 200.
    await model.set_status("ws-live", "stopped")
    ok = await home_api.http.get("/api/v1/workspaces/ws-live/home", headers=auth())
    assert ok.status_code == 200


async def test_move_on_foreign_host_is_409(home_api) -> None:
    await create_workspace(home_api, "ws-far")
    planted_volume(home_api, "ws-far")
    home_api.app.state.settings.vmm.host_name = "elsewhere"
    for method in ("GET", "PUT"):
        response = await home_api.http.request(
            method,
            "/api/v1/workspaces/ws-far/home",
            content=IMAGE if method == "PUT" else None,
            headers=auth(),
        )
        assert response.status_code == 409
        assert "lives on host" in response.json()["detail"]


async def test_move_refused_on_k8s(home_api, monkeypatch) -> None:
    """The k8s volume lives inside the runner pod's PVC: the byte
    streams have nothing to read or write there."""
    await create_workspace(home_api, "ws-k8s")
    monkeypatch.setattr(home_api.app.state.settings.vmm, "driver", "k8s")
    for method in ("GET", "PUT"):
        response = await home_api.http.request(
            method,
            "/api/v1/workspaces/ws-k8s/home",
            content=IMAGE if method == "PUT" else None,
            headers=auth(),
        )
        assert response.status_code == 400
        assert "not served by the k8s backend" in response.json()["detail"]


async def test_import_replaces_the_volume(home_api) -> None:
    await create_workspace(home_api, "ws-imp")
    planted_volume(home_api, "ws-imp", b"old-volume" * 8)
    response = await home_api.http.put(
        "/api/v1/workspaces/ws-imp/home", content=IMAGE, headers=auth()
    )
    assert response.status_code == 200, response.text
    assert response.json() == {"id": "ws-imp", "bytes": len(IMAGE)}
    home = planted_volume(home_api, "ws-imp")  # the path, now replaced
    assert home.read_bytes() == IMAGE
    # The scratch is swept: only the volume remains.
    state_dir = home_api.app.state.settings.vmm.state_dir
    assert not list((state_dir / "volumes").glob("*.tmp"))


async def test_import_refuses_non_ext4_and_empty(home_api) -> None:
    await create_workspace(home_api, "ws-bad")
    home = planted_volume(home_api, "ws-bad", b"preexisting" * 64)
    garbage = await home_api.http.put(
        "/api/v1/workspaces/ws-bad/home", content=b"garbage" * 1000, headers=auth()
    )
    assert garbage.status_code == 400
    assert "not an ext4 image" in garbage.json()["detail"]
    empty = await home_api.http.put(
        "/api/v1/workspaces/ws-bad/home", content=b"", headers=auth()
    )
    assert empty.status_code == 400
    assert "body is empty" in empty.json()["detail"]
    # Both refusals left the existing volume in place.
    assert home.read_bytes() == b"preexisting" * 64


async def test_import_into_missing_volume_path(home_api) -> None:
    """A workspace whose volume file never materialized (the seam
    stub creates none) gets one from the import alone."""
    await create_workspace(home_api, "ws-heal")
    response = await home_api.http.put(
        "/api/v1/workspaces/ws-heal/home", content=IMAGE, headers=auth()
    )
    assert response.status_code == 200
    assert planted_volume(home_api, "ws-heal").read_bytes() == IMAGE


async def test_moves_publish_events(home_api) -> None:
    hub = home_api.api.state.hub
    queue = hub.subscribe()
    try:
        await create_workspace(home_api, "ws-ev")
        planted_volume(home_api, "ws-ev")
        await home_api.http.get("/api/v1/workspaces/ws-ev/home", headers=auth())
        await home_api.http.put(
            "/api/v1/workspaces/ws-ev/home", content=IMAGE, headers=auth()
        )
        events = [json.loads(queue.get_nowait()) for _ in range(2)]
    finally:
        hub.unsubscribe(queue)
    assert events[0] == {
        "event": "home.exported",
        "data": {"id": "ws-ev", "bytes": len(IMAGE)},
    }
    assert events[1] == {
        "event": "home.imported",
        "data": {"id": "ws-ev", "bytes": len(IMAGE)},
    }


async def raw_put(api, path: str, messages: list[dict]) -> list[dict]:
    """Call the app over raw ASGI with a scripted receive sequence;
    the messages it sent. An emptied script answers
    ``http.disconnect`` — a client that vanished mid-upload."""
    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "PUT",
        "scheme": "https",
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"",
        "headers": [(b"authorization", f"Bearer {TOKEN}".encode())],
        "server": ("test", 443),
        "client": ("127.0.0.1", 1234),
        "root_path": "",
    }
    sent: list[dict] = []

    async def receive() -> dict:
        return messages.pop(0) if messages else {"type": "http.disconnect"}

    async def send(message: dict) -> None:
        sent.append(message)

    await api(scope, receive, send)
    return sent


async def test_upload_cut_off_mid_body_keeps_the_volume(home_api) -> None:
    """A client that dies mid-upload answers 400, not a traceback:
    the partial scratch is swept and the old volume survives."""
    await create_workspace(home_api, "ws-cut")
    home = planted_volume(home_api, "ws-cut", b"old-volume" * 8)
    sent = await raw_put(
        home_api.api,
        "/api/v1/workspaces/ws-cut/home",
        [{"type": "http.request", "body": IMAGE[:4096], "more_body": True}],
    )
    assert sent[0]["status"] == 400
    assert "ended before its body completed" in sent[1]["body"].decode()
    assert home.read_bytes() == b"old-volume" * 8
    assert not list(home.parent.glob("*.tmp"))


# --- The client layer on the same endpoints ---


async def test_client_layer_round_trips(home_api, tmp_path, monkeypatch) -> None:
    """The dogfood pair through the client's own streaming helpers:
    export one workspace's volume into another (seeding), end to
    end over the same streaming endpoints."""
    await create_workspace(home_api, "ws-a")
    planted_volume(home_api, "ws-a")
    await create_workspace(home_api, "ws-b")
    planted_volume(home_api, "ws-b", b"blank" * 100)
    monkeypatch.setenv("MSKSC_URL", "https://daemon")
    monkeypatch.setenv("MSKSC_TOKEN", TOKEN)
    url, token = cli.env_url(), cli.env_token()
    image = tmp_path / "vol.ext4"
    exported = await cli.run_home_export(
        url, token, "ws-a", str(image), home_api.transport
    )
    assert exported == len(IMAGE)
    assert image.read_bytes() == IMAGE
    imported = await cli.run_home_import(
        url, token, "ws-b", cli.volume_source(str(image)), home_api.transport
    )
    assert imported == len(IMAGE)
    assert planted_volume(home_api, "ws-b").read_bytes() == IMAGE


async def test_client_export_error_is_one_line(home_api, monkeypatch) -> None:
    monkeypatch.setenv("MSKSC_URL", "https://daemon")
    monkeypatch.setenv("MSKSC_TOKEN", TOKEN)
    with pytest.raises(SystemExit, match="msks: 404: no such workspace"):
        await cli.run_home_export(
            cli.env_url(), cli.env_token(), "ghost", "-", home_api.transport
        )


async def test_client_import_refused_volume_is_one_line(
    home_api, tmp_path, monkeypatch
) -> None:
    await create_workspace(home_api, "ws-ref")
    monkeypatch.setenv("MSKSC_URL", "https://daemon")
    monkeypatch.setenv("MSKSC_TOKEN", TOKEN)
    bad = tmp_path / "bad.ext4"
    bad.write_bytes(b"not-an-ext4" * 100)
    with pytest.raises(SystemExit, match="msks: 400: the request body is not"):
        await cli.run_home_import(
            cli.env_url(),
            cli.env_token(),
            "ws-ref",
            cli.volume_source(str(bad)),
            home_api.transport,
        )


async def test_import_disk_failure_is_503(home_api, monkeypatch) -> None:
    """An install-side failure (a full state disk) answers 503 with
    the named cause; the workspace keeps its row and its volume."""
    from msks.microvm.errors import MicrovmError

    async def boom(state_dir, workspace_id, chunks):
        raise MicrovmError("could not install the volume: no space")

    monkeypatch.setattr(persist, "import_home_volume", boom)
    await create_workspace(home_api, "ws-full")
    response = await home_api.http.put(
        "/api/v1/workspaces/ws-full/home", content=IMAGE, headers=auth()
    )
    assert response.status_code == 503
    assert "no space" in response.json()["detail"]
