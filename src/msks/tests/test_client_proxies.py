"""The proxy commands (#807, #808): ``msks proxy add`` / ``ls`` /
``rm`` — the spec grammar, the wire bodies, and the output shapes,
against the mock-transport seam."""

import json

import httpx
import pytest
from msks.client import proxies as px

ROW = {
    "id": 3,
    "workspace_id": "1a2b3c4d",
    "direction": "forward",
    "listen_host": "127.0.0.1",
    "listen_port": 2201,
    "target_host": None,
    "target_port": 22,
    "label": "ssh",
    "status": "listening",
    "reason": None,
}


def client_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MSKSC_URL", "https://daemon")
    monkeypatch.setenv("MSKSC_TOKEN", "tok")


def test_local_spec_forms() -> None:
    # ssh's own literal shape: PORT:HOST:PORT with HOST naming the
    # workspace itself, either spelling; a leading bind address,
    # IPv6 bracketed.
    assert px.parse_local_spec("2201:localhost:22") == {
        "direction": "forward",
        "listen_port": 2201,
        "target_port": 22,
    }
    assert px.parse_local_spec("2201:127.0.0.1:22") == {
        "direction": "forward",
        "listen_port": 2201,
        "target_port": 22,
    }
    assert px.parse_local_spec("0.0.0.0:2201:localhost:22") == {
        "direction": "forward",
        "listen_host": "0.0.0.0",
        "listen_port": 2201,
        "target_port": 22,
    }
    assert px.parse_local_spec("[::1]:2201:localhost:22") == {
        "direction": "forward",
        "listen_host": "::1",
        "listen_port": 2201,
        "target_port": 22,
    }
    # A host the workspace cannot name refuses with the rule and
    # the direction that serves it.
    with pytest.raises(SystemExit, match="use -R"):
        px.parse_local_spec("2201:db.internal:22")
    # The shorthand is gone: the host field is part of the shape.
    with pytest.raises(SystemExit):
        px.parse_local_spec("2201:22")
    with pytest.raises(SystemExit):
        px.parse_local_spec("2201")
    with pytest.raises(SystemExit):
        px.parse_local_spec("[::1]:2201:localhost")
    with pytest.raises(SystemExit):
        px.parse_local_spec("[::1:2201:localhost:22")
    with pytest.raises(SystemExit):
        px.parse_local_spec("::1:2201:localhost:22")
    with pytest.raises(SystemExit):
        px.parse_local_spec("abc:localhost:22")
    with pytest.raises(SystemExit):
        px.parse_local_spec("2201:localhost:0")


def test_remote_spec_forms() -> None:
    assert px.parse_remote_spec("9200:localhost:9200") == {
        "direction": "remote",
        "listen_port": 9200,
        "target_host": "localhost",
        "target_port": 9200,
    }
    assert px.parse_remote_spec("5432:db.internal:5432")["target_host"] == (
        "db.internal"
    )
    assert px.parse_remote_spec("80:[::1]:8080")["target_host"] == "::1"
    with pytest.raises(SystemExit):
        px.parse_remote_spec("9200")
    with pytest.raises(SystemExit):
        px.parse_remote_spec("9200:db.internal")
    with pytest.raises(SystemExit):
        px.parse_remote_spec("9200:db::5432")


def test_proxy_endpoint_shapes_both_directions() -> None:
    listen, target = px.proxy_endpoint(ROW)
    assert (listen, target) == ("127.0.0.1:2201", "guest:22")
    remote = {**ROW, "direction": "remote", "listen_host": None}
    listen, target = px.proxy_endpoint(remote)
    assert (listen, target) == ("gateway:2201", "127.0.0.1:22")


def test_row_status_carries_any_reason() -> None:
    # A listening row the guest cannot reach still says so — the
    # admission note rides beside the status (#807 review round 2).
    assert px.row_status(ROW) == "listening"
    unreachable = {**ROW, "reason": "admission refresh failed: nft"}
    assert px.row_status(unreachable) == (
        "listening (admission refresh failed: nft)"
    )
    failed = {**ROW, "status": "failed", "reason": "address in use"}
    assert px.row_status(failed) == "failed (address in use)"


def test_add_posts_the_spec_on_the_workspace_route(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from msks.client import cli

    client_env(monkeypatch)
    seen = {}

    def handler(req: httpx.Request) -> httpx.Response:
        seen["method"] = req.method
        seen["path"] = req.url.path
        seen["body"] = json.loads(req.content)
        return httpx.Response(200, json=ROW)

    rc = cli.main(
        ["proxy", "add", "alpha", "-L", "2201:localhost:22", "--label", "ssh"],
        transport=httpx.MockTransport(handler),
    )
    assert rc == 0
    assert seen["method"] == "POST"
    assert seen["path"] == "/api/v1/workspaces/alpha/proxies"
    assert seen["body"] == {
        "direction": "forward",
        "listen_port": 2201,
        "target_port": 22,
        "label": "ssh",
    }
    assert "forward proxy 3" in capsys.readouterr().out


def test_add_remote_posts_the_target(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from msks.client import cli

    client_env(monkeypatch)
    seen = {}

    def handler(req: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(req.content)
        return httpx.Response(200, json=ROW)

    rc = cli.main(
        [
            "proxy",
            "add",
            "alpha",
            "-R",
            "5432:db.internal:5432",
        ],
        transport=httpx.MockTransport(handler),
    )
    assert rc == 0
    assert seen["body"] == {
        "direction": "remote",
        "listen_port": 5432,
        "target_host": "db.internal",
        "target_port": 5432,
    }


def test_add_refuses_zero_and_both_directions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from msks.client import cli

    client_env(monkeypatch)
    transport = httpx.MockTransport(lambda req: httpx.Response(200, json=ROW))
    assert cli.main(["proxy", "add", "alpha"], transport=transport) == 2
    assert (
        cli.main(
            [
                "proxy",
                "add",
                "alpha",
                "-L",
                "2201:localhost:22",
                "-R",
                "9200:h:9200",
            ],
            transport=transport,
        )
        == 2
    )


def test_ls_prints_table_and_json(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from msks.client import cli

    client_env(monkeypatch)
    failed = {
        **ROW,
        "status": "failed",
        "reason": "address in use",
        "label": None,
    }
    transport = httpx.MockTransport(
        lambda req: httpx.Response(200, json=[ROW, failed])
    )
    rc = cli.main(["proxy", "ls"], transport=transport)
    assert rc == 0
    table = capsys.readouterr().out
    assert "guest:22" in table
    assert "listening" in table
    assert "failed (address in use)" in table
    rc = cli.main(["proxy", "ls", "--json"], transport=transport)
    assert rc == 0
    assert json.loads(capsys.readouterr().out) == [ROW, failed]


def test_rm_deletes_on_the_workspace_route(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from msks.client import cli

    client_env(monkeypatch)
    seen = {}

    def handler(req: httpx.Request) -> httpx.Response:
        seen["method"] = req.method
        seen["path"] = req.url.path
        return httpx.Response(200, json={"removed": True})

    rc = cli.main(
        ["proxy", "rm", "alpha", "3"], transport=httpx.MockTransport(handler)
    )
    assert rc == 0
    assert seen == {
        "method": "DELETE",
        "path": "/api/v1/workspaces/alpha/proxies/3",
    }
    assert "removed proxy 3" in capsys.readouterr().out


def test_add_and_rm_json(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """--json on the mutating proxy commands (#841): add emits the
    stored row, rm emits one line."""
    from msks.client import cli, jsonio

    client_env(monkeypatch)
    rc = cli.main(
        ["proxy", "add", "alpha", "-L", "2201:localhost:22", "--json"],
        transport=httpx.MockTransport(
            lambda req: httpx.Response(200, json=ROW)
        ),
    )
    assert rc == 0
    assert json.loads(capsys.readouterr().out)["id"] == ROW["id"]
    assert jsonio.json_mode() is True

    rc = cli.main(
        ["proxy", "rm", "alpha", "3", "--json"],
        transport=httpx.MockTransport(
            lambda req: httpx.Response(200, json={"removed": 3})
        ),
    )
    assert rc == 0
    assert capsys.readouterr().out == '{"removed": 3}\n'
