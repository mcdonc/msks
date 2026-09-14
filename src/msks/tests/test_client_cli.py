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
from msks.client import cli, rest
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
    auths = []

    def handler(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        auths.append(request.headers.get("authorization"))
        if request.url.path.endswith("/start"):
            return httpx.Response(200, json={"id": "ws1", "status": "running"})
        return httpx.Response(201, json={"id": "ws1", "status": "created"})

    rc = cli.cmd_create({"id": "ws1"}, start=True, transport=mock(handler))
    assert rc == 0
    assert paths == ["/api/v1/workspaces", "/api/v1/workspaces/ws1/start"]
    # The bearer token rides every request, the boot call included.
    assert auths == ["Bearer tok", "Bearer tok"]
    out = capsys.readouterr().out
    assert "created ws1" in out
    assert "msks shell ws1" in out


def test_cmd_create_start_failure_keeps_the_workspace(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    client_env(monkeypatch)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/start"):
            return httpx.Response(503, json={"detail": "vmm launch failed"})
        return httpx.Response(201, json={"id": "ws1", "status": "created"})

    with pytest.raises(SystemExit, match="msks start ws1"):
        cli.cmd_create({"id": "ws1"}, start=True, transport=mock(handler))
    # The id printed before the boot attempt: the workspace exists.
    assert "created ws1" in capsys.readouterr().out


def test_cmd_start_boots_and_prints(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    client_env(monkeypatch)
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        seen["auth"] = request.headers.get("authorization")
        return httpx.Response(200, json={"id": "ws1", "status": "running"})

    rc = cli.cmd_start("ws1", transport=mock(handler))
    assert rc == 0
    assert seen["path"] == "/api/v1/workspaces/ws1/start"
    assert seen["auth"] == "Bearer tok"
    assert "ws1 running" in capsys.readouterr().out


def test_api_call_status_error_uses_detail() -> None:
    transport = mock(
        lambda req: httpx.Response(409, json={"detail": "workspace exists"})
    )
    with pytest.raises(SystemExit, match="409: workspace exists"):
        asyncio.run(rest.api_call("POST", "https://d", "t", "/x", transport=transport))


def test_api_call_validation_errors_join_to_one_line() -> None:
    # FastAPI's 422 detail is a list of error objects, not a string;
    # the CLI must not dump a Python repr at the operator.
    detail = [
        {"loc": ["body", "id"], "msg": "String should match pattern"},
        {"msg": "Input should be greater than 0"},
    ]
    transport = mock(lambda req: httpx.Response(422, json={"detail": detail}))
    with pytest.raises(
        SystemExit,
        match="body.id: String should match pattern; Input should be greater than 0",
    ):
        asyncio.run(rest.api_call("POST", "https://d", "t", "/x", transport=transport))


def test_error_detail_empty_validation_list() -> None:
    response = httpx.Response(422, json={"detail": []})
    assert rest.error_detail(response) == "invalid request"


def test_api_call_timeout_names_the_daemon() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("read timed out")

    with pytest.raises(SystemExit, match="timed out talking to"):
        asyncio.run(
            rest.api_call("POST", "https://d", "t", "/x", transport=mock(handler))
        )


def test_api_client_reuses_a_passed_ssl_context() -> None:
    # The shell passes its already-built context so the unverified
    # TLS warning prints once per invocation, not once per REST call.
    # Pinned against httpx internals: verify lands on the default
    # transport's ssl context.
    import ssl

    ctx = ssl.create_default_context()
    client = rest.api_client("https://d", "t", ssl_ctx=ctx)
    try:
        assert client._transport._pool._ssl_context is ctx  # type: ignore[attr-defined]
    finally:
        asyncio.run(client.aclose())


def test_api_call_non_detail_json_falls_back_to_body() -> None:
    transport = mock(lambda req: httpx.Response(500, json={"nope": 1}))
    with pytest.raises(SystemExit, match="nope"):
        asyncio.run(cli.api_call("GET", "https://d", "t", "/x", transport=transport))


def test_api_call_non_json_body_falls_back_to_text() -> None:
    transport = mock(lambda req: httpx.Response(503, text="boom"))
    with pytest.raises(SystemExit, match="boom"):
        asyncio.run(cli.api_call("GET", "https://d", "t", "/x", transport=transport))


def test_cmd_list_unreachable_daemon(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("MSKSC_URL", "https://127.0.0.1:1")
    monkeypatch.setenv("MSKSC_TOKEN", "tok")
    monkeypatch.delenv("MSKSC_CAFILE", raising=False)
    with pytest.raises(SystemExit, match="cannot reach"):
        cli.cmd_list()
    capsys.readouterr()  # swallow the unverified-TLS warning


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


def test_main_start_dispatch(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    client_env(monkeypatch)
    rc = cli.main(
        ["start", "ws1"],
        transport=mock(lambda req: httpx.Response(200, json={"status": "running"})),
    )
    assert rc == 0
    assert "ws1 running" in capsys.readouterr().out


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
    row = await rest.api_call(
        "POST",
        "https://test",
        TOKEN,
        "/api/v1/workspaces",
        json_body={"id": "cli-a", "kernel": "/k", "rootfs": "/r"},
        transport=api_transport,
    )
    assert row["id"] == "cli-a"
    assert row["status"] == "created"
    rows = await rest.api_call(
        "GET", "https://test", TOKEN, "/api/v1/workspaces", transport=api_transport
    )
    assert [item["id"] for item in rows] == ["cli-a"]


async def test_api_call_maps_bad_token(api_transport) -> None:
    with pytest.raises(SystemExit, match="invalid or revoked token"):
        await rest.api_call(
            "GET",
            "https://test",
            "wrong-token",
            "/api/v1/workspaces",
            transport=api_transport,
        )


async def test_ensure_running_boots_a_created_workspace(api_transport) -> None:
    transport = api_transport
    await rest.api_call(
        "POST",
        "https://test",
        TOKEN,
        "/api/v1/workspaces",
        json_body={"id": "cli-b", "kernel": "/k", "rootfs": "/r"},
        transport=transport,
    )
    await rest.ensure_running("cli-b", "https://test", TOKEN, transport=transport)
    row = await rest.api_call(
        "GET", "https://test", TOKEN, "/api/v1/workspaces/cli-b", transport=transport
    )
    assert row["status"] == "running"


async def test_ensure_running_skips_a_running_workspace(api_transport) -> None:
    app_transport = api_transport
    await rest.api_call(
        "POST",
        "https://test",
        TOKEN,
        "/api/v1/workspaces",
        json_body={"id": "cli-c", "kernel": "/k", "rootfs": "/r"},
        transport=app_transport,
    )
    await rest.api_call(
        "POST",
        "https://test",
        TOKEN,
        "/api/v1/workspaces/cli-c/start",
        transport=app_transport,
    )
    await rest.ensure_running("cli-c", "https://test", TOKEN, transport=app_transport)
    row = await rest.api_call(
        "GET",
        "https://test",
        TOKEN,
        "/api/v1/workspaces/cli-c",
        transport=app_transport,
    )
    assert row["status"] == "running"


async def test_ensure_running_refuses_paused(
    capsys: pytest.CaptureFixture[str],
) -> None:
    transport = mock(
        lambda req: httpx.Response(200, json={"id": "ws1", "status": "paused"})
    )
    with pytest.raises(SystemExit, match="paused.*no resume"):
        await rest.ensure_running("ws1", "https://d", "t", transport=transport)


async def test_ensure_running_waits_out_a_concurrent_boot(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    statuses = ["starting", "starting", "running"]
    posts: list[str] = []

    async def fast_sleep(seconds: float) -> None:
        pass

    monkeypatch.setattr(rest.asyncio, "sleep", fast_sleep)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            posts.append(request.url.path)
            raise AssertionError("no start while another boot runs")
        return httpx.Response(200, json={"id": "ws1", "status": statuses.pop(0)})

    await rest.ensure_running("ws1", "https://d", "t", transport=mock(handler))
    assert posts == []
    assert "waiting for the boot" in capsys.readouterr().err


async def test_ensure_running_boot_wait_times_out(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fast_sleep(seconds: float) -> None:
        pass

    monkeypatch.setattr(rest.asyncio, "sleep", fast_sleep)
    monkeypatch.setattr(rest, "BOOT_WAIT_S", 0.0)
    transport = mock(
        lambda req: httpx.Response(200, json={"id": "ws1", "status": "starting"})
    )
    with pytest.raises(SystemExit, match="still starting"):
        await rest.ensure_running("ws1", "https://d", "t", transport=transport)


async def test_ensure_running_attaches_to_a_won_race() -> None:
    # GET says stopped, another client's start wins the race: the
    # 503 is swallowed because the re-check says running.
    calls: list[str] = []
    statuses = iter(["stopped", "running"])

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(f"{request.method} {request.url.path}")
        if request.method == "POST":
            return httpx.Response(503, json={"detail": "VM ws1 already exists"})
        return httpx.Response(200, json={"id": "ws1", "status": next(statuses)})

    await rest.ensure_running("ws1", "https://d", "t", transport=mock(handler))
    assert calls == [
        "GET /api/v1/workspaces/ws1",
        "POST /api/v1/workspaces/ws1/start",
        "GET /api/v1/workspaces/ws1",
    ]


async def test_ensure_running_lost_race_reports_the_state() -> None:
    statuses = iter(["stopped", "stopped"])

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            return httpx.Response(503, json={"detail": "VM ws1 already exists"})
        return httpx.Response(200, json={"id": "ws1", "status": next(statuses)})

    with pytest.raises(SystemExit, match="ws1 is stopped"):
        await rest.ensure_running("ws1", "https://d", "t", transport=mock(handler))


def test_main_interrupt_is_one_line(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def interrupted(*args, **kwargs) -> int:
        raise KeyboardInterrupt

    monkeypatch.setattr(cli, "cmd_list", interrupted)
    with pytest.raises(SystemExit) as excinfo:
        cli.main(["list"])
    assert excinfo.value.code == 130
    assert "interrupted" in capsys.readouterr().err
