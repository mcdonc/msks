"""Client CLI tests: list/create against mocks and the real API.

The mock-transport tests pin the client contract (auth header, POST
body, output shape, one-line errors); the ASGI tests run the same
``api_call`` seam against the real daemon surface.
"""

import asyncio
import json
from pathlib import Path

import httpx
import pytest
from msks.app import build_app
from msks.client import cli
from msks.server.api import build_api
from msks.settings import ServerSettings, Settings, VmmSettings
from test_api import TOKEN, StubMicrovm

ROWS = [
    {"id": "alpha", "status": "running", "image_hash": "a" * 64, "host": "hv1"},
    {"id": "beta", "status": "created", "image_hash": None, "host": None},
]


def mock(handler) -> httpx.MockTransport:
    return httpx.MockTransport(handler)


def client_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MSKSC_URL", "https://daemon")
    monkeypatch.setenv("MSKSC_TOKEN", "tok")


def test_cmd_list_formats_rows(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    client_env(monkeypatch)
    rc = cli.cmd_list(transport=mock(lambda req: httpx.Response(200, json=ROWS)))
    assert rc == 0
    out = capsys.readouterr().out
    assert "alpha" in out and "running" in out and "hv1" in out
    assert "a" * 12 in out  # the image hash is shortened to 12 chars
    assert "beta" in out and "created" in out and "-" in out


def test_cmd_list_json(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    client_env(monkeypatch)
    cli.cmd_list(
        as_json=True, transport=mock(lambda req: httpx.Response(200, json=ROWS))
    )
    assert json.loads(capsys.readouterr().out) == ROWS


def test_cmd_list_empty_prints_nothing(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    client_env(monkeypatch)
    rc = cli.cmd_list(transport=mock(lambda req: httpx.Response(200, json=[])))
    assert rc == 0
    assert capsys.readouterr().out == ""


def test_cmd_create_posts_body_and_prints(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    client_env(monkeypatch)
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        seen["auth"] = request.headers.get("authorization")
        seen["body"] = json.loads(request.content)
        return httpx.Response(201, json={"id": "ws1", "status": "created"})

    rc = cli.cmd_create({"id": "ws1", "cpus": 4}, transport=mock(handler))
    assert rc == 0
    assert seen["path"] == "/api/v1/workspaces"
    assert seen["auth"] == "Bearer tok"
    assert seen["body"] == {"id": "ws1", "cpus": 4}
    out = capsys.readouterr().out
    assert "created ws1" in out
    assert "shell" not in out  # no attach hint without --start


def test_cmd_create_start_boots_and_hints(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    client_env(monkeypatch)
    paths = []

    def handler(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        if request.url.path.endswith("/start"):
            return httpx.Response(200, json={"id": "ws1", "status": "running"})
        return httpx.Response(201, json={"id": "ws1", "status": "created"})

    rc = cli.cmd_create({"id": "ws1"}, start=True, transport=mock(handler))
    assert rc == 0
    assert paths == ["/api/v1/workspaces", "/api/v1/workspaces/ws1/start"]
    out = capsys.readouterr().out
    assert "created ws1 (running)" in out
    assert "msks shell ws1" in out


def test_api_call_status_error_uses_detail() -> None:
    transport = mock(
        lambda req: httpx.Response(409, json={"detail": "workspace exists"})
    )
    with pytest.raises(SystemExit, match="409: workspace exists"):
        asyncio.run(cli.api_call("POST", "https://d", "t", "/x", transport=transport))


def test_api_call_non_detail_json_falls_back_to_body() -> None:
    transport = mock(lambda req: httpx.Response(500, json={"nope": 1}))
    with pytest.raises(SystemExit, match="nope"):
        asyncio.run(cli.api_call("GET", "https://d", "t", "/x", transport=transport))


def test_api_call_non_json_body_falls_back_to_text() -> None:
    transport = mock(lambda req: httpx.Response(503, text="boom"))
    with pytest.raises(SystemExit, match="boom"):
        asyncio.run(cli.api_call("GET", "https://d", "t", "/x", transport=transport))


def test_cmd_list_unreachable_daemon(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MSKSC_URL", "https://127.0.0.1:1")
    monkeypatch.setenv("MSKSC_TOKEN", "tok")
    monkeypatch.delenv("MSKSC_CAFILE", raising=False)
    with pytest.raises(SystemExit, match="cannot reach"):
        cli.cmd_list()


def test_main_list_dispatch(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    client_env(monkeypatch)
    rc = cli.main(
        ["list", "--json"], transport=mock(lambda req: httpx.Response(200, json=ROWS))
    )
    assert rc == 0
    assert json.loads(capsys.readouterr().out) == ROWS


def test_main_create_dispatch(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    client_env(monkeypatch)
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(request.content)
        return httpx.Response(201, json={"id": "ws1", "status": "created"})

    rc = cli.main(
        ["create", "ws1", "--image", "debian:13", "--cpus", "4"],
        transport=mock(handler),
    )
    assert rc == 0
    assert seen["body"] == {"id": "ws1", "image": "debian:13", "cpus": 4}
    assert "created ws1" in capsys.readouterr().out


@pytest.fixture
async def api_transport(tmp_path: Path):
    """The real API surface, in-process, with the seam stubbed."""
    settings = Settings(
        vmm=VmmSettings(state_dir=tmp_path / "vms"),
        server=ServerSettings(
            db_path=tmp_path / "cli.db",
            bootstrap_token=TOKEN,
            event_poll_s=10.0,
        ),
    )
    app = build_app(settings)
    app.state.microvm = StubMicrovm()
    api = build_api(app)
    async with api.router.lifespan_context(api):
        yield httpx.ASGITransport(app=api)


async def test_api_call_creates_and_lists_workspaces(api_transport) -> None:
    row = await cli.api_call(
        "POST",
        "https://test",
        TOKEN,
        "/api/v1/workspaces",
        json_body={"id": "cli-a", "kernel": "/k", "rootfs": "/r"},
        transport=api_transport,
    )
    assert row["id"] == "cli-a"
    assert row["status"] == "created"
    rows = await cli.api_call(
        "GET", "https://test", TOKEN, "/api/v1/workspaces", transport=api_transport
    )
    assert [item["id"] for item in rows] == ["cli-a"]


async def test_api_call_maps_bad_token(api_transport) -> None:
    with pytest.raises(SystemExit, match="invalid or revoked token"):
        await cli.api_call(
            "GET",
            "https://test",
            "wrong-token",
            "/api/v1/workspaces",
            transport=api_transport,
        )
