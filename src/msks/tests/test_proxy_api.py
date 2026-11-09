"""The proxy routes (#807, #808): create, list, remove, and the
live-listener path — against the API fixture with a stubbed net
seam."""

import asyncio
import contextlib
from pathlib import Path

import httpx
import pytest
from msks.app import build_app
from msks.net.manager import NetAttachment
from msks.server.api import build_api
from msks.settings import NetSettings, ServerSettings, Settings, VmmSettings
from test_api import TOKEN, StubMicrovm, auth


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


def attachment(tap_ip: str) -> NetAttachment:
    return NetAttachment(
        workspace_id="ws-live",
        tap="msks-tap",
        mac="02:00:00:00:00:02",
        guest_ip="127.0.0.1",
        tap_ip=tap_ip,
        slice=0,
    )


async def free_port() -> int:
    """A port the kernel confirms free (bound then released).

    The suite runs under xdist: a fixed port two workers bind at
    once is a failed bind, and a proxy whose bind fails reports a
    status the test did not ask for."""
    server = await asyncio.start_server(lambda r, w: None, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    server.close()
    await server.wait_closed()
    return port


async def make_workspace(http, name: str, egress: bool = True) -> str:
    created = await http.post(
        "/api/v1/workspaces",
        json={
            "id": name,
            "kernel": "/k",
            "rootfs": "/r",
            "egress": egress,
        },
        headers=auth(),
    )
    assert created.status_code == 201
    return created.json()["id"]


async def test_create_forward_on_stopped_workspace(client) -> None:
    http, _app, _stub = client
    wid = await make_workspace(http, "ws-a")
    listen_port = await free_port()
    created = await http.post(
        f"/api/v1/workspaces/{wid}/proxies",
        json={
            "direction": "forward",
            "listen_port": listen_port,
            "target_port": 22,
        },
        headers=auth(),
    )
    assert created.status_code == 200
    row = created.json()
    # A forward bind needs no attachment: the listener is up at
    # once, and each connection refuses individually until the
    # workspace runs (the daemon-side dial has no guest to reach).
    assert row["status"] == "listening"
    assert row["listen_host"] == "127.0.0.1"
    assert row["target_host"] is None
    listed = await http.get(
        f"/api/v1/workspaces/{wid}/proxies", headers=auth()
    )
    assert listed.json()[0]["status"] == "listening"


async def test_create_refuses_workspace_without_egress(client) -> None:
    http, _app, _stub = client
    wid = await make_workspace(http, "ws-plain", egress=False)
    created = await http.post(
        f"/api/v1/workspaces/{wid}/proxies",
        json={"direction": "forward", "listen_port": 2201, "target_port": 22},
        headers=auth(),
    )
    assert created.status_code == 400
    assert "no NIC" in created.json()["detail"]


async def test_create_refuses_direction_mismatched_hosts(client) -> None:
    http, _app, _stub = client
    wid = await make_workspace(http, "ws-shape")
    forward_with_target = await http.post(
        f"/api/v1/workspaces/{wid}/proxies",
        json={
            "direction": "forward",
            "listen_port": 2201,
            "target_port": 22,
            "target_host": "127.0.0.1",
        },
        headers=auth(),
    )
    assert forward_with_target.status_code == 400
    assert "target_host" in forward_with_target.json()["detail"]
    remote_with_listen = await http.post(
        f"/api/v1/workspaces/{wid}/proxies",
        json={
            "direction": "remote",
            "listen_port": 9200,
            "target_port": 9200,
            "listen_host": "127.0.0.1",
        },
        headers=auth(),
    )
    assert remote_with_listen.status_code == 400
    assert "listen_host" in remote_with_listen.json()["detail"]
    bad_direction = await http.post(
        f"/api/v1/workspaces/{wid}/proxies",
        json={"direction": "sideways", "listen_port": 1, "target_port": 1},
        headers=auth(),
    )
    assert bad_direction.status_code == 422
    bad_port = await http.post(
        f"/api/v1/workspaces/{wid}/proxies",
        json={"direction": "remote", "listen_port": 0, "target_port": 1},
        headers=auth(),
    )
    assert bad_port.status_code == 422


async def test_create_refuses_duplicate_binds(client) -> None:
    http, _app, _stub = client
    first = await make_workspace(http, "ws-one")
    second = await make_workspace(http, "ws-two")
    body = {
        "direction": "forward",
        "listen_port": await free_port(),
        "target_port": 22,
    }
    created = await http.post(
        f"/api/v1/workspaces/{first}/proxies", json=body, headers=auth()
    )
    assert created.status_code == 200
    # A forward bind is daemon-wide: another workspace asking the
    # identical spec gets the standing row (the declarative ask),
    # and a different spec on the same bind is the refusal.
    dup = await http.post(
        f"/api/v1/workspaces/{second}/proxies", json=body, headers=auth()
    )
    assert dup.status_code == 200
    assert dup.json()["id"] == created.json()["id"]
    other_spec = await http.post(
        f"/api/v1/workspaces/{second}/proxies",
        json={**body, "target_port": 80},
        headers=auth(),
    )
    assert other_spec.status_code == 409
    # A remote bind is its own tap: the same port on another
    # workspace is a different listener.
    remote = await http.post(
        f"/api/v1/workspaces/{second}/proxies",
        json={
            "direction": "remote",
            "listen_port": body["listen_port"],
            "target_port": 22,
        },
        headers=auth(),
    )
    assert remote.status_code == 200
    same_tap = await http.post(
        f"/api/v1/workspaces/{second}/proxies",
        json={
            "direction": "remote",
            "listen_port": body["listen_port"],
            "target_port": 80,
        },
        headers=auth(),
    )
    assert same_tap.status_code == 409
    # A different port on the same address is a different bind.
    other_port = await http.post(
        f"/api/v1/workspaces/{first}/proxies",
        json={
            "direction": "forward",
            "listen_port": await free_port(),
            "target_port": 22,
        },
        headers=auth(),
    )
    assert other_port.status_code == 200


async def test_daemon_wide_listing_and_delete(client) -> None:
    http, _app, _stub = client
    wid = await make_workspace(http, "ws-rm")
    created = await http.post(
        f"/api/v1/workspaces/{wid}/proxies",
        json={
            "direction": "remote",
            "listen_port": 9200,
            "target_port": 9200,
            "label": "es",
        },
        headers=auth(),
    )
    proxy_id = created.json()["id"]
    all_rows = await http.get("/api/v1/proxies", headers=auth())
    assert [row["id"] for row in all_rows.json()] == [proxy_id]
    assert all_rows.json()[0]["label"] == "es"
    missing = await http.delete(
        f"/api/v1/workspaces/{wid}/proxies/999", headers=auth()
    )
    assert missing.status_code == 404
    removed = await http.delete(
        f"/api/v1/workspaces/{wid}/proxies/{proxy_id}", headers=auth()
    )
    assert removed.status_code == 200
    assert removed.json()["removed"] is True
    again = await http.delete(
        f"/api/v1/workspaces/{wid}/proxies/{proxy_id}", headers=auth()
    )
    assert again.status_code == 404
    empty = await http.get("/api/v1/proxies", headers=auth())
    assert empty.json() == []


async def test_create_on_attached_workspace_listens_at_once(client) -> None:
    http, app, _stub = client
    wid = await make_workspace(http, "ws-live")
    net = app.state.net
    net._attachments[wid] = attachment("127.0.0.1")

    async def echo(reader, writer):
        while True:
            data = await reader.read(4096)
            if not data:
                break
            writer.write(data.upper())
            await writer.drain()
        writer.close()

    guest = await asyncio.start_server(echo, "127.0.0.1", 0)
    async with guest:
        guest_port = guest.sockets[0].getsockname()[1]
        net.dialer = lambda host, port: asyncio.open_connection(
            "127.0.0.1", guest_port
        )
        listener = await asyncio.start_server(
            lambda r, w: None, "127.0.0.1", 0
        )
        listen_port = listener.sockets[0].getsockname()[1]
        listener.close()
        await listener.wait_closed()
        created = await http.post(
            f"/api/v1/workspaces/{wid}/proxies",
            json={
                "direction": "forward",
                "listen_port": listen_port,
                "target_port": guest_port,
            },
            headers=auth(),
        )
        assert created.status_code == 200
        assert created.json()["status"] == "listening"
        # The daemon's listener bridges to the guest dial.
        reader, writer = await asyncio.open_connection(
            "127.0.0.1", listen_port
        )
        writer.write(b"ping\n")
        await writer.drain()
        assert await reader.read(4096) == b"PING\n"
        writer.close()
        with contextlib.suppress(Exception):
            await writer.wait_closed()
        removed = await http.delete(
            f"/api/v1/workspaces/{wid}/proxies/{created.json()['id']}",
            headers=auth(),
        )
        assert removed.status_code == 200
    net._attachments.pop(wid, None)


async def test_remote_lifecycle_on_an_attached_workspace(client) -> None:
    """A remote proxy on a running workspace binds its tap port at
    once, the admission swap rides the create and the delete, and
    the workspace-scoped delete closes the listener."""
    http, app, _stub = client
    wid = await make_workspace(http, "ws-remote")
    net = app.state.net
    net._attachments[wid] = attachment("127.0.0.1")
    created = await http.post(
        f"/api/v1/workspaces/{wid}/proxies",
        json={
            "direction": "remote",
            "listen_port": 9200,
            "target_port": 9200,
        },
        headers=auth(),
    )
    assert created.status_code == 200
    assert created.json()["status"] == "listening"
    proxy_id = created.json()["id"]
    # A same-port forward on another workspace is a different bind.
    other = await make_workspace(http, "ws-other")
    port_ok = await http.post(
        f"/api/v1/workspaces/{other}/proxies",
        json={
            "direction": "forward",
            "listen_port": await free_port(),
            "target_port": 22,
        },
        headers=auth(),
    )
    assert port_ok.status_code == 200
    removed = await http.delete(
        f"/api/v1/workspaces/{wid}/proxies/{proxy_id}", headers=auth()
    )
    assert removed.status_code == 200
    net._attachments.pop(wid, None)


async def test_remote_create_annotates_a_failed_admission_refresh(
    client, monkeypatch
) -> None:
    """A remote create whose nft swap fails still answers with its
    row and listener (#807 review): the reason names the missing
    admission, and a later bind that fails refreshes nothing."""
    http, app, _stub = client
    wid = await make_workspace(http, "ws-adj")
    net = app.state.net
    net._attachments[wid] = attachment("127.0.0.1")
    calls = []

    async def failing(workspace_id):
        calls.append(workspace_id)
        raise RuntimeError("nft refused")

    monkeypatch.setattr(net, "refresh_proxies", failing)
    try:
        port = await free_port()
        created = await http.post(
            f"/api/v1/workspaces/{wid}/proxies",
            json={
                "direction": "remote",
                "listen_port": port,
                "target_port": port,
            },
            headers=auth(),
        )
        assert created.status_code == 200
        row = created.json()
        assert row["status"] == "listening"
        assert "admission refresh failed" in row["reason"]
        assert calls == [wid]
        # The note survives reads after the create (#807 review
        # round 2): the listing keeps showing it.
        listed = await http.get("/api/v1/proxies", headers=auth())
        assert listed.json()[0]["reason"] == row["reason"]
        # A bind that fails gains no admission, so it triggers no
        # swap.
        squatted = await asyncio.start_server(
            lambda r, w: None, "127.0.0.1", 0
        )
        taken = squatted.sockets[0].getsockname()[1]
        failed = await http.post(
            f"/api/v1/workspaces/{wid}/proxies",
            json={
                "direction": "remote",
                "listen_port": taken,
                "target_port": taken,
            },
            headers=auth(),
        )
        assert failed.json()["status"] == "failed"
        assert calls == [wid]
        squatted.close()
    finally:
        net._attachments.pop(wid, None)


async def test_add_is_idempotent_for_an_identical_spec(client) -> None:
    """``msks proxy add`` is declarative (#807): the same spec
    returns the standing row; a different spec on the same bind
    is the refusal."""
    http, _app, _stub = client
    wid = await make_workspace(http, "ws-idem")
    body = {"direction": "forward", "listen_port": 2201, "target_port": 22}
    first = await http.post(
        f"/api/v1/workspaces/{wid}/proxies", json=body, headers=auth()
    )
    assert first.status_code == 200
    again = await http.post(
        f"/api/v1/workspaces/{wid}/proxies", json=body, headers=auth()
    )
    assert again.status_code == 200
    assert again.json()["id"] == first.json()["id"]
    changed = await http.post(
        f"/api/v1/workspaces/{wid}/proxies",
        json={**body, "target_port": 80},
        headers=auth(),
    )
    assert changed.status_code == 409


async def test_remote_same_port_across_workspaces_is_no_conflict(
    client,
) -> None:
    """A remote bind is its own tap: the same port on another
    workspace is a different listener."""
    http, _app, _stub = client
    one = await make_workspace(http, "tap-one")
    two = await make_workspace(http, "tap-two")
    body = {
        "direction": "remote",
        "listen_port": 9200,
        "target_port": 9200,
    }
    first = await http.post(
        f"/api/v1/workspaces/{one}/proxies", json=body, headers=auth()
    )
    assert first.status_code == 200
    second = await http.post(
        f"/api/v1/workspaces/{two}/proxies", json=body, headers=auth()
    )
    assert second.status_code == 200
    assert second.json()["id"] != first.json()["id"]
    # Re-asking the identical remote spec returns the standing row.
    repeat = await http.post(
        f"/api/v1/workspaces/{two}/proxies",
        json={
            **body,
            "target_host": body.get("target_host") or "127.0.0.1",
        },
        headers=auth(),
    )
    assert repeat.status_code == 200
    assert repeat.json()["id"] == second.json()["id"]
