"""Client CLI tests: the command surface against mocks and the real API.

The mock-transport tests pin the client contract (auth header, method,
path, POST body, output shape, one-line errors); the ASGI tests run
the same ``api_call`` seam against the real daemon surface.
"""

import argparse
import asyncio
import io
import json
import ssl
from pathlib import Path

import httpx
import pytest
from msks.app import build_app
from msks.client import cli, rest
from msks.server.api import build_api
from msks.settings import NetSettings, ServerSettings, Settings, VmmSettings
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


KEY_BODY = {
    "workspace": "alpha",
    "type": "ecdsa-sha2-nistp256",
    "public_key": "ecdsa-sha2-nistp256 AAAA msksd:alpha",
    "private_key": (
        "-----BEGIN OPENSSH PRIVATE KEY-----\nbytebyte\n"
        "-----END OPENSSH PRIVATE KEY-----\n"
    ),
}


def test_cmd_key_prints_public(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """``msks key`` prints the safe half by default: the public line
    a token holder can paste anywhere."""
    client_env(monkeypatch)
    seen = {}

    def handler(req: httpx.Request) -> httpx.Response:
        seen["path"] = req.url.path
        seen["auth"] = req.headers.get("authorization")
        return httpx.Response(200, json=KEY_BODY)

    rc = cli.cmd_key("alpha", transport=mock(handler))
    assert rc == 0
    assert seen["path"] == "/api/v1/workspaces/alpha/ssh-key"
    assert seen["auth"] == "Bearer tok"
    assert capsys.readouterr().out.strip() == KEY_BODY["public_key"]


def test_cmd_key_private_and_out(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    """--private prints the private half; --out materializes it 0600
    at the operator-named path and prints only the path."""
    client_env(monkeypatch)
    transport = mock(lambda req: httpx.Response(200, json=KEY_BODY))
    rc = cli.cmd_key("alpha", as_private=True, transport=transport)
    assert rc == 0
    assert capsys.readouterr().out == KEY_BODY["private_key"]
    out = tmp_path / "id"
    rc = cli.cmd_key("alpha", out=str(out), transport=transport)
    assert rc == 0
    assert capsys.readouterr().out.strip() == str(out)
    assert out.read_text() == KEY_BODY["private_key"]
    assert out.stat().st_mode & 0o777 == 0o600


def test_cmd_key_out_forces_mode_on_existing_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """``--out`` forces 0600 even on a pre-existing world-readable
    file: open()'s mode argument only applies at creation, the
    write must not leave the old mode under new contents."""
    client_env(monkeypatch)
    out = tmp_path / "leaky"
    out.write_text("stale\n")
    out.chmod(0o644)
    rc = cli.cmd_key(
        "alpha",
        out=str(out),
        transport=mock(lambda req: httpx.Response(200, json=KEY_BODY)),
    )
    assert rc == 0
    assert out.read_text() == KEY_BODY["private_key"]
    assert out.stat().st_mode & 0o777 == 0o600


def test_cmd_key_out_error_is_one_line(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """An unwritable --out path fails with one readable line (exit 1),
    the CLI's error contract — not a raw traceback."""
    client_env(monkeypatch)
    with pytest.raises(SystemExit, match="msks: cannot write key file"):
        cli.cmd_key(
            "alpha",
            out=str(tmp_path / "no-such-dir" / "k"),
            transport=mock(lambda req: httpx.Response(200, json=KEY_BODY)),
        )


def test_cmd_key_dispatch(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The parser route: `msks key <ws>` through main()."""
    client_env(monkeypatch)
    rc = cli.main(
        ["key", "alpha"], transport=mock(lambda req: httpx.Response(200, json=KEY_BODY))
    )
    assert rc == 0
    assert "ecdsa-sha2-nistp256" in capsys.readouterr().out


def test_cmd_ls_formats_rows(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    client_env(monkeypatch)
    rc = cli.cmd_ls(transport=mock(lambda req: httpx.Response(200, json=ROWS)))
    assert rc == 0
    out = capsys.readouterr().out
    assert "alpha" in out and "running" in out and "hv1" in out
    assert "a" * 12 in out  # the image hash is shortened to 12 chars
    assert "beta" in out and "created" in out and "-" in out


def test_cmd_ls_json(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    client_env(monkeypatch)
    cli.cmd_ls(as_json=True, transport=mock(lambda req: httpx.Response(200, json=ROWS)))
    assert json.loads(capsys.readouterr().out) == ROWS


def test_cmd_ls_empty_prints_nothing(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    client_env(monkeypatch)
    rc = cli.cmd_ls(transport=mock(lambda req: httpx.Response(200, json=[])))
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
    assert "attach with" not in out  # no attach hint without --start


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
    assert "msks console ws1" in out


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


def test_cmd_stop_posts_and_prints(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    client_env(monkeypatch)
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["method"] = request.method
        seen["path"] = request.url.path
        seen["auth"] = request.headers.get("authorization")
        return httpx.Response(200, json={"id": "ws1", "status": "stopped"})

    rc = cli.cmd_stop("ws1", transport=mock(handler))
    assert rc == 0
    assert seen["method"] == "POST"
    assert seen["path"] == "/api/v1/workspaces/ws1/stop"
    assert seen["auth"] == "Bearer tok"
    assert "ws1 stopped" in capsys.readouterr().out


def test_cmd_stop_missing_workspace_is_one_line(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client_env(monkeypatch)
    transport = mock(
        lambda req: httpx.Response(404, json={"detail": "no such workspace"})
    )
    with pytest.raises(SystemExit, match="msks: 404: no such workspace"):
        cli.cmd_stop("ghost", transport=transport)


def test_cmd_stop_shutdown_deadline_is_one_line(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A stop that misses the daemon's shutdown deadline answers
    # 503 with the endpoint's detail; the client passes it through.
    client_env(monkeypatch)
    transport = mock(
        lambda req: httpx.Response(503, json={"detail": "workspace ws1 wedged"})
    )
    with pytest.raises(SystemExit, match="msks: 503: workspace ws1 wedged"):
        cli.cmd_stop("ws1", transport=transport)


def test_cmd_rm_deletes_and_prints(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    client_env(monkeypatch)
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["method"] = request.method
        seen["path"] = request.url.path
        seen["auth"] = request.headers.get("authorization")
        return httpx.Response(200, json={"deleted": "ws1"})

    rc = cli.cmd_rm(["ws1"], transport=mock(handler))
    assert rc == 0
    assert seen["method"] == "DELETE"
    assert seen["path"] == "/api/v1/workspaces/ws1"
    assert seen["auth"] == "Bearer tok"
    assert "ws1 deleted" in capsys.readouterr().out


def test_cmd_rm_accepts_multiple_ids(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    client_env(monkeypatch)
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        workspace_id = request.url.path.rsplit("/", 1)[1]
        calls.append(f"{request.method} {workspace_id}")
        return httpx.Response(200, json={"deleted": workspace_id})

    rc = cli.cmd_rm(["a", "b", "c"], transport=mock(handler))
    assert rc == 0
    assert calls == ["DELETE a", "DELETE b", "DELETE c"]
    assert capsys.readouterr().out == "a deleted\nb deleted\nc deleted\n"


def test_cmd_rm_multiple_stops_at_first_failure(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Ids already removed stay removed and confirmed; the run
    stops at the first refusal with the API's one line."""
    client_env(monkeypatch)
    paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        if request.url.path.endswith("/b"):
            return httpx.Response(404, json={"detail": "no such workspace"})
        return httpx.Response(200, json={"deleted": "x"})

    with pytest.raises(SystemExit, match="msks: 404: no such workspace"):
        cli.cmd_rm(["a", "b", "c"], transport=mock(handler))
    assert paths == ["/api/v1/workspaces/a", "/api/v1/workspaces/b"]
    out = capsys.readouterr().out
    assert "a deleted" in out
    assert "c deleted" not in out


def test_cmd_rm_missing_workspace_is_one_line(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client_env(monkeypatch)
    transport = mock(
        lambda req: httpx.Response(404, json={"detail": "no such workspace"})
    )
    with pytest.raises(SystemExit, match="msks: 404: no such workspace"):
        cli.cmd_rm(["ghost"], transport=transport)


def test_cmd_rm_foreign_host_is_one_line(monkeypatch: pytest.MonkeyPatch) -> None:
    # The 409 host-mismatch detail surfaces verbatim, like shell's
    # close codes — the operator learns where the artifacts live.
    client_env(monkeypatch)
    detail = "home volume for workspace ws1 lives on host hv1; this host is hv2"
    transport = mock(lambda req: httpx.Response(409, json={"detail": detail}))
    with pytest.raises(SystemExit, match="msks: 409: home volume.*lives on host hv1"):
        cli.cmd_rm(["ws1"], transport=transport)


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


def test_cmd_ls_unreachable_daemon(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("MSKSC_URL", "https://127.0.0.1:1")
    monkeypatch.setenv("MSKSC_TOKEN", "tok")
    monkeypatch.delenv("MSKSC_CAFILE", raising=False)
    with pytest.raises(SystemExit, match="cannot reach"):
        cli.cmd_ls()
    capsys.readouterr()  # swallow the unverified-TLS warning


def test_main_ls_dispatch(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    client_env(monkeypatch)
    rc = cli.main(
        ["ls", "--json"], transport=mock(lambda req: httpx.Response(200, json=ROWS))
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


def test_main_stop_dispatch(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    client_env(monkeypatch)
    rc = cli.main(
        ["stop", "ws1"],
        transport=mock(lambda req: httpx.Response(200, json={"status": "stopped"})),
    )
    assert rc == 0
    assert "ws1 stopped" in capsys.readouterr().out


def test_main_rm_dispatch(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    client_env(monkeypatch)
    rc = cli.main(
        ["rm", "ws1", "ws2"],
        transport=mock(lambda req: httpx.Response(200, json={"deleted": "x"})),
    )
    assert rc == 0
    assert capsys.readouterr().out == "ws1 deleted\nws2 deleted\n"


@pytest.fixture
async def api_transport(tmp_path: Path):
    """The real API surface, in-process, with the seam stubbed."""
    settings = Settings(
        vmm=VmmSettings(state_dir=tmp_path / "vms"),
        net=NetSettings(enabled=False),
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


async def test_api_call_stops_and_deletes_a_workspace(api_transport) -> None:
    """The ls/stop/rm endpoints through the client's own plumbing,
    against the real daemon surface with the seam stubbed."""
    transport = api_transport
    await rest.api_call(
        "POST",
        "https://test",
        TOKEN,
        "/api/v1/workspaces",
        json_body={"id": "cli-l", "kernel": "/k", "rootfs": "/r"},
        transport=transport,
    )
    await rest.api_call(
        "POST",
        "https://test",
        TOKEN,
        "/api/v1/workspaces/cli-l/start",
        transport=transport,
    )
    stopped = await rest.api_call(
        "POST",
        "https://test",
        TOKEN,
        "/api/v1/workspaces/cli-l/stop",
        transport=transport,
    )
    assert stopped == {"id": "cli-l", "status": "stopped"}
    rows = await rest.api_call(
        "GET", "https://test", TOKEN, "/api/v1/workspaces", transport=transport
    )
    assert [row["status"] for row in rows] == ["stopped"]
    deleted = await rest.api_call(
        "DELETE", "https://test", TOKEN, "/api/v1/workspaces/cli-l", transport=transport
    )
    assert deleted == {"deleted": "cli-l"}
    with pytest.raises(SystemExit, match="404: no such workspace"):
        await rest.api_call(
            "GET",
            "https://test",
            TOKEN,
            "/api/v1/workspaces/cli-l",
            transport=transport,
        )


async def test_api_call_rm_deletes_a_running_workspace(api_transport) -> None:
    """rm on a running workspace: the daemon stops the VMM (kill as
    the wedged fallback) before removing the artifacts."""
    transport = api_transport
    stub = transport.app.state.msks_app.state.microvm
    await rest.api_call(
        "POST",
        "https://test",
        TOKEN,
        "/api/v1/workspaces",
        json_body={"id": "cli-r", "kernel": "/k", "rootfs": "/r"},
        transport=transport,
    )
    await rest.api_call(
        "POST",
        "https://test",
        TOKEN,
        "/api/v1/workspaces/cli-r/start",
        transport=transport,
    )
    deleted = await rest.api_call(
        "DELETE", "https://test", TOKEN, "/api/v1/workspaces/cli-r", transport=transport
    )
    assert deleted == {"deleted": "cli-r"}
    assert stub.calls.index(("shutdown", "cli-r")) < stub.calls.index(
        ("cleanup", "cli-r")
    )


async def test_api_call_stop_and_rm_missing_are_one_line(api_transport) -> None:
    for path in (
        "/api/v1/workspaces/ghost/stop",
        "/api/v1/workspaces/ghost",
    ):
        with pytest.raises(SystemExit, match="msks: 404: no such workspace"):
            await rest.api_call(
                "POST" if path.endswith("/stop") else "DELETE",
                "https://test",
                TOKEN,
                path,
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
    with pytest.raises(
        SystemExit, match=r"paused and the daemon has no resume.*msks stop ws1"
    ):
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

    monkeypatch.setattr(cli, "cmd_ls", interrupted)
    with pytest.raises(SystemExit) as excinfo:
        cli.main(["ls"])
    assert excinfo.value.code == 130
    assert "interrupted" in capsys.readouterr().err


def test_create_body_egress_flags() -> None:
    """--egress/--no-egress override the create body; unset sends the
    daemon's default (egress on, #52)."""
    parser = cli.build_parser()

    def body(argv: list[str]) -> dict:
        return cli.create_body(parser.parse_args(argv))

    assert "egress" not in body(["create", "ws"])
    assert body(["create", "ws", "--egress"])["egress"] is True
    assert body(["create", "ws", "--no-egress"])["egress"] is False


# --- The image catalog commands (#65) ---


def image_row(name: str, version: str, digest: str, default: bool = False) -> dict:
    return {
        "hash": digest,
        "name": name,
        "version": version,
        "cmdline": "console=hvc0 root=/dev/vda rw",
        "vsock_shell_port": 1073741826,
        "kernel_version": "6.12.107+deb13",
        "kernel_format": "raw",
        "default": default,
    }


IMAGES = [
    image_row("debian", "13", "a" * 64, default=True),
    image_row("debian", "12", "b" * 64),
    image_row("alpine", "3.20", "c" * 64),
]


def listing_transport(handler=None) -> httpx.MockTransport:
    """A GET /api/v1/images catalog plus per-request extras."""

    def default(request: httpx.Request) -> httpx.Response:
        if handler is not None and request.method != "GET":
            return handler(request)
        return httpx.Response(200, json=IMAGES)

    return mock(default)


def test_image_ls_formats_rows(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    client_env(monkeypatch)
    rc = cli.cmd_image_ls(transport=listing_transport())
    assert rc == 0
    out = capsys.readouterr().out
    assert "debian:13" in out and "a" * 12 in out and "default" in out
    assert "debian:12" in out and "alpine:3.20" in out
    # The non-default rows carry the dash flag, and kernel facts show.
    assert "6.12.107+deb13 (raw)" in out


def test_image_ls_marks_only_the_default(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    client_env(monkeypatch)
    cli.cmd_image_ls(transport=listing_transport())
    lines = capsys.readouterr().out.splitlines()
    flags = [line.split()[2] for line in lines]
    assert flags == ["default", "-", "-"]


def test_image_ls_json_is_the_api_document(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    client_env(monkeypatch)
    cli.cmd_image_ls(as_json=True, transport=listing_transport())
    assert json.loads(capsys.readouterr().out) == IMAGES


def test_image_ls_empty_prints_nothing(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    client_env(monkeypatch)
    rc = cli.cmd_image_ls(transport=mock(lambda req: httpx.Response(200, json=[])))
    assert rc == 0
    assert capsys.readouterr().out == ""


def test_image_import_posts_source_and_prints_ref(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    client_env(monkeypatch)
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        seen["auth"] = request.headers.get("authorization")
        seen["body"] = json.loads(request.content)
        return httpx.Response(
            201,
            json={
                "hash": "a" * 64,
                "name": "debian",
                "version": "13",
                "ref": "debian:13",
            },
        )

    rc = cli.cmd_image_import("/srv/images/debian.tar", transport=mock(handler))
    assert rc == 0
    assert seen["path"] == "/api/v1/images"
    assert seen["auth"] == "Bearer tok"
    assert seen["body"] == {"source": "/srv/images/debian.tar"}
    out = capsys.readouterr().out
    assert "imported debian:13" in out and "a" * 12 in out


def test_image_import_help_states_the_daemon_reads_the_path(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The daemon-side-path fact is pinned in the real --help text,
    not just implied (an issue #65 acceptance criterion)."""
    with pytest.raises(SystemExit) as excinfo:
        cli.main(["image", "import", "--help"])
    assert excinfo.value.code == 0
    out = capsys.readouterr().out
    assert "read by the daemon" in out
    assert "not uploaded" in out


@pytest.mark.parametrize(
    "ref",
    [
        "debian:13",
        "debian",
        "debian@" + "a" * 64,
        "a" * 64,
        "a" * 12,  # a unique hash prefix, as ls prints it
    ],
)
def test_image_rm_resolves_every_reference_form(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    ref: str,
) -> None:
    """All four daemon-side forms (plus a unique prefix) DELETE the
    same digest; bare name picks the newest version (13 over 12)."""
    client_env(monkeypatch)
    deleted = []

    def handler(request: httpx.Request) -> httpx.Response:
        deleted.append(request.url.path)
        return httpx.Response(200, json={"removed": "a" * 64})

    rc = cli.cmd_image_rm(ref, transport=listing_transport(handler))
    assert rc == 0
    assert deleted == [f"/api/v1/images/{'a' * 64}"]
    assert "debian:13 deleted" in capsys.readouterr().out


def test_image_rm_refusal_names_the_workspace(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client_env(monkeypatch)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(409, json={"detail": "workspace ws1 boots this image"})

    with pytest.raises(SystemExit, match=r"msks: 409: workspace ws1 boots this image"):
        cli.cmd_image_rm("debian:13", transport=listing_transport(handler))


def test_image_rm_miss_lists_the_catalog(monkeypatch: pytest.MonkeyPatch) -> None:
    client_env(monkeypatch)
    with pytest.raises(SystemExit) as excinfo:
        cli.cmd_image_rm("fedora:40", transport=listing_transport())
    message = str(excinfo.value)
    assert "no image matches 'fedora:40'" in message
    assert "debian:13" in message and "alpine:3.20" in message
    # A bare name that matches nothing takes the same exit line.
    with pytest.raises(SystemExit, match="no image matches 'fedora'"):
        cli.cmd_image_rm("fedora", transport=listing_transport())


def test_image_rm_ambiguous_prefix_is_named(monkeypatch: pytest.MonkeyPatch) -> None:
    client_env(monkeypatch)
    rows = IMAGES + [image_row("debian", "13.1", "a" * 63 + "e")]

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=rows)

    with pytest.raises(SystemExit) as excinfo:
        cli.cmd_image_rm("a" * 63, transport=mock(handler))
    message = str(excinfo.value)
    assert "matches 2 images" in message
    assert "debian:13" in message and "debian:13.1" in message
    assert "use the full hash or name@hash" in message


def test_image_rm_malformed_pin_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    client_env(monkeypatch)
    for ref in ("debian@zzz", "debian@" + "a" * 12):
        with pytest.raises(SystemExit, match=f"malformed image hash in '{ref}'"):
            cli.cmd_image_rm(ref, transport=listing_transport())


def test_image_rm_empty_ref_is_a_miss(monkeypatch: pytest.MonkeyPatch) -> None:
    """An empty reference (unset $REF) matches nothing — it must not
    select every hash via the empty-prefix path."""
    client_env(monkeypatch)
    sole = mock(lambda req: httpx.Response(200, json=[IMAGES[0]]))
    with pytest.raises(SystemExit, match="no image matches ''"):
        cli.cmd_image_rm("", transport=sole)


def test_image_info_prints_the_record(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    client_env(monkeypatch)
    rc = cli.cmd_image_info("alpine", transport=listing_transport())
    assert rc == 0
    out = capsys.readouterr().out
    assert "ref      alpine:3.20" in out
    assert "hash     " + "c" * 64 in out
    assert "kernel   6.12.107+deb13 (raw)" in out
    assert "cmdline  console=hvc0 root=/dev/vda rw" in out
    assert "console  vsock port 1073741826" in out
    assert "seed     provisioner - (none declared)" in out
    assert "default  no" in out


def test_main_dispatches_image_subcommands(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    client_env(monkeypatch)
    rc = cli.main(
        ["image", "ls"],
        transport=listing_transport(),
    )
    assert rc == 0
    assert "debian:13" in capsys.readouterr().out


async def test_image_commands_against_the_real_api(api_transport) -> None:
    """The image command cores against the real daemon surface: a
    real containerDisk import, the listing (first import becomes the
    default), ref-form removal, and the 404 after it is gone."""
    from test_imagestore import build_containerdisk

    transport = api_transport
    app = transport.app.state.msks_app
    archive = app.state.settings.vmm.state_dir / "debian-13.tar"
    archive.parent.mkdir(parents=True, exist_ok=True)
    build_containerdisk(archive)

    record = await cli.import_image("https://test", TOKEN, str(archive), transport)
    assert record["ref"] == "debian:13.6"

    rows = await cli.fetch_images("https://test", TOKEN, transport)
    assert len(rows) == 1
    assert rows[0]["hash"] == record["hash"]
    assert rows[0]["default"] is True  # the sole import is designated

    described = await cli.describe_image(
        "https://test", TOKEN, record["hash"], transport
    )
    assert described["hash"] == record["hash"]

    removed = await cli.remove_image("https://test", TOKEN, "debian:13.6", transport)
    assert removed == {"removed": record["hash"]}
    with pytest.raises(SystemExit, match="no image matches"):
        await cli.remove_image("https://test", TOKEN, "debian:13.6", transport)


def test_image_rm_miss_caps_a_large_catalog(monkeypatch: pytest.MonkeyPatch) -> None:
    """The miss line spells out at most CATALOG_REF_CAP refs."""
    client_env(monkeypatch)
    rows = [image_row("distro", str(n), f"{n:064x}") for n in range(12)]
    with pytest.raises(SystemExit) as excinfo:
        cli.cmd_image_rm(
            "missing", transport=mock(lambda req: httpx.Response(200, json=rows))
        )
    message = str(excinfo.value)
    assert "distro:0" in message and "distro:7" in message
    assert "distro:8" not in message
    assert "(+4 more)" in message


def test_image_rm_hex_name_wins_over_hash_prefix(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A catalog name that looks like hex resolves by name — the
    daemon's precedence — never as some other image's hash prefix."""
    client_env(monkeypatch)
    rows = [
        image_row("cafe", "1", "9" * 64),
        image_row("other", "9", "cafeaaa" + "0" * 57),
    ]
    deleted = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, json=rows)
        deleted.append(request.url.path)
        return httpx.Response(200, json={"removed": "9" * 64})

    rc = cli.cmd_image_rm("cafe", transport=mock(handler))
    assert rc == 0
    assert deleted == [f"/api/v1/images/{'9' * 64}"]
    assert "cafe:1 deleted" in capsys.readouterr().out


def test_image_rm_hash_shaped_miss_does_not_fall_through_to_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A full 64-hex ref is only ever a hash (the daemon's
    is_hash_shape short-circuit): a miss is a miss even when an image
    is named that same string."""
    client_env(monkeypatch)
    rows = [image_row("f" * 64, "1", "9" * 64)]
    with pytest.raises(SystemExit, match="no image matches"):
        cli.cmd_image_rm(
            "f" * 63 + "e", transport=mock(lambda req: httpx.Response(200, json=rows))
        )


def test_image_rm_bare_name_with_duplicate_refs_is_ambiguous(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two imports of the same name:version: the bare name surfaces
    the ambiguity (with hashes to tell them apart), not a silent
    arbitrary pick."""
    client_env(monkeypatch)
    rows = [
        image_row("debian", "13", "a" * 64),
        image_row("debian", "13", "b" * 64),
    ]
    with pytest.raises(SystemExit) as excinfo:
        cli.cmd_image_rm(
            "debian", transport=mock(lambda req: httpx.Response(200, json=rows))
        )
    message = str(excinfo.value)
    assert "matches 2 images" in message
    assert f"debian:13 ({'a' * 12})" in message and f"debian:13 ({'b' * 12})" in message


def test_image_rm_on_an_empty_catalog_names_it(monkeypatch: pytest.MonkeyPatch) -> None:
    client_env(monkeypatch)
    with pytest.raises(SystemExit, match=r"\(the catalog is empty\)"):
        cli.cmd_image_rm(
            "debian", transport=mock(lambda req: httpx.Response(200, json=[]))
        )


def test_create_user_data_reads_the_file(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    """--user-data FILE (#41) carries the file's bytes verbatim as the
    create body's user_data."""
    client_env(monkeypatch)
    payload = "#!/bin/sh\necho seeded > /root/stamp\n"
    source = tmp_path / "seed.sh"
    source.write_text(payload)
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(request.content)
        return httpx.Response(201, json={"id": "ws1", "status": "created"})

    rc = cli.main(
        ["create", "ws1", "--user-data", str(source)],
        transport=mock(handler),
    )
    assert rc == 0
    assert seen["body"] == {"id": "ws1", "user_data": payload}
    assert "created ws1" in capsys.readouterr().out


def test_create_user_data_reads_stdin(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    """--user-data - reads the payload from stdin, script-style."""
    client_env(monkeypatch)
    payload = "#!/bin/sh\ntrue\n"
    monkeypatch.setattr("sys.stdin", io.StringIO(payload))
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(request.content)
        return httpx.Response(201, json={"id": "ws1", "status": "created"})

    rc = cli.main(["create", "ws1", "--user-data", "-"], transport=mock(handler))
    assert rc == 0
    assert seen["body"]["user_data"] == payload


def test_create_user_data_missing_file_is_one_line(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    """An unreadable payload file fails before any network activity,
    one readable line."""
    client_env(monkeypatch)
    with pytest.raises(SystemExit, match="cannot read user-data"):
        cli.create_body(
            argparse.Namespace(
                workspace_id="ws1",
                image=None,
                kernel=None,
                initrd=None,
                rootfs=None,
                cmdline=None,
                cpus=None,
                mem_mib=None,
                root_mib=None,
                home_mib=None,
                egress=None,
                user_data="/nonexistent/seed.sh",
                start=False,
            )
        )
    assert capsys.readouterr().err == ""


def test_create_user_data_non_utf8_is_one_line(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture, tmp_path: Path
) -> None:
    """A binary payload file fails like an unreadable one: one line,
    before any network activity."""
    client_env(monkeypatch)
    source = tmp_path / "seed.bin"
    source.write_bytes(b"\xff\xfe#\x00")
    with pytest.raises(SystemExit, match="cannot read user-data"):
        cli.read_user_data(str(source))
