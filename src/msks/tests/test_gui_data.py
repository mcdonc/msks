"""The GUI's REST surface (#924 phase 2): ``GuiData`` runs the
shared data layer against the same transport fakes the TUI suite
uses — one scripted ``httpx.MockTransport``, the exit-contract
failures carried (#904), and the shared TLS context's contract.
"""

import json
from pathlib import Path

import httpx
import pytest
from msks.client import data as clientdata
from msks.client import env as env_mod
from msks.client.gui import data as data_mod

URL = "https://127.0.0.1:9660"


def gui_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """The connection the scripted transport serves."""
    monkeypatch.setenv("MSKSC_URL", URL)
    monkeypatch.setenv("MSKSC_TOKEN", "t" * 32)
    env_mod._SHARED_SSL[0] = None


def scripted(
    replies: list[tuple[str, str, httpx.Response]],
    seen: list[tuple[str, str, dict | None]],
) -> httpx.MockTransport:
    """A transport answering the named (method, path) exchanges
    in order and recording each request's method, path, and
    body."""
    queue = list(replies)

    def handler(request: httpx.Request) -> httpx.Response:
        body = (
            json.loads(request.content.decode()) if request.content else None
        )
        seen.append((request.method, request.url.path, body))
        method, path, reply = queue.pop(0)
        assert (request.method, request.url.path) == (method, path)
        return reply

    return httpx.MockTransport(handler)


# One row per screen action: the call to make, the exchange the
# daemon must see, and the reply the screen receives. The whole
# surface walks the same door, so the table pins each method's
# method-path-body shape in one place.
CASES = [
    ("workspaces", (), "GET", "/api/v1/workspaces", None),
    ("storage", (), "GET", "/api/v1/storage", None),
    ("images", (), "GET", "/api/v1/images", None),
    ("create_defaults", (), "GET", "/api/v1/create-defaults", None),
    (
        "remove_image",
        ("sha256:abc",),
        "DELETE",
        "/api/v1/images/sha256:abc",
        None,
    ),
    (
        "import_image",
        ("https://ch/msks.tar",),
        "POST",
        "/api/v1/images",
        {"source": "https://ch/msks.tar"},
    ),
    (
        "set_default_image",
        ("sha256:abc",),
        "POST",
        "/api/v1/images/default",
        {"ref": "sha256:abc"},
    ),
    (
        "clone",
        ("ws-1", "copy"),
        "POST",
        "/api/v1/workspaces/ws-1/clone",
        {"name": "copy"},
    ),
    (
        "resize",
        ("ws-1", {"root_gib": 20}),
        "POST",
        "/api/v1/workspaces/ws-1/resize",
        {"root_gib": 20},
    ),
    (
        "set_mounts",
        ("ws-1", [{"source": "/tmp", "target": "/mnt"}]),
        "POST",
        "/api/v1/workspaces/ws-1/mounts",
        {"mounts": [{"source": "/tmp", "target": "/mnt"}]},
    ),
    ("start", ("ws-1",), "POST", "/api/v1/workspaces/ws-1/start", None),
    ("stop", ("ws-1",), "POST", "/api/v1/workspaces/ws-1/stop", None),
    ("remove", ("ws-1",), "DELETE", "/api/v1/workspaces/ws-1", None),
    (
        "set_egress_mode",
        ("ws-1", "filtered"),
        "PUT",
        "/api/v1/workspaces/ws-1/egress/policy",
        {"mode": "filtered"},
    ),
    (
        "set_egress_mode",
        ("ws-1", "static", True),
        "PUT",
        "/api/v1/workspaces/ws-1/egress/policy",
        {"mode": "static", "confirm_empty": True},
    ),
    (
        "egress_rules",
        ("ws-1",),
        "GET",
        "/api/v1/workspaces/ws-1/egress",
        None,
    ),
    (
        "decide",
        ("ws-1", "req-9", "allow", "1h"),
        "POST",
        "/api/v1/workspaces/ws-1/egress/requests/req-9",
        {"decision": "allow", "duration": "1h"},
    ),
    (
        "revoke",
        ("ws-1", "req-9"),
        "DELETE",
        "/api/v1/workspaces/ws-1/egress/requests/req-9",
        None,
    ),
    ("secrets", (), "GET", "/api/v1/secrets", None),
    ("secret", (7,), "GET", "/api/v1/secrets/7", None),
    ("revoke_secret", (7,), "DELETE", "/api/v1/secrets/7", None),
    (
        "renew_secret",
        (7, 3600),
        "POST",
        "/api/v1/secrets/7/renew",
        {"ttl_s": 3600},
    ),
    (
        "mint_secret",
        ({"name": "n", "ttl_s": 60},),
        "POST",
        "/api/v1/secrets",
        {"name": "n", "ttl_s": 60},
    ),
    ("pending_secrets", (), "GET", "/api/v1/secrets/pending", None),
    (
        "unlock_secrets",
        ([{"id": 7, "value": "v"}],),
        "POST",
        "/api/v1/secrets/unlock",
        {"values": [{"id": 7, "value": "v"}]},
    ),
    ("secret_audit", (), "GET", "/api/v1/secrets/audit", None),
    (
        "workspace_proxies",
        ("ws-1",),
        "GET",
        "/api/v1/workspaces/ws-1/proxies",
        None,
    ),
    (
        "create_proxy",
        ("ws-1", {"listen_port": 9}),
        "POST",
        "/api/v1/workspaces/ws-1/proxies",
        {"listen_port": 9},
    ),
    (
        "update_proxy",
        ("ws-1", 3, {"listen_port": 10}),
        "PATCH",
        "/api/v1/workspaces/ws-1/proxies/3",
        {"listen_port": 10},
    ),
    (
        "remove_proxy",
        ("ws-1", 3),
        "DELETE",
        "/api/v1/workspaces/ws-1/proxies/3",
        None,
    ),
]


@pytest.mark.parametrize(
    "method,args,verb,path,body", CASES, ids=[row[0] for row in CASES]
)
async def test_every_screen_action_makes_its_exchange(
    monkeypatch: pytest.MonkeyPatch,
    method: str,
    args: tuple,
    verb: str,
    path: str,
    body: dict | None,
) -> None:
    """Each data-layer method makes exactly the exchange the matching
    CLI subcommand makes — verb, path, and body — and hands the
    daemon's reply to the screen untouched."""
    gui_env(monkeypatch)
    seen: list[tuple[str, str, dict | None]] = []
    reply = {"ok": True}
    data = data_mod.GuiData(
        transport=scripted(
            [(verb, path, httpx.Response(200, json=reply))], seen
        )
    )
    assert await getattr(data, method)(*args) == reply
    assert seen == [(verb, path, body)]


async def test_egress_confirm_empty_is_sent_only_when_set(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The confirm flag joins the body only when the caller set
    it — the daemon's refusal names it, so an unset flag must not
    reach the wire."""
    gui_env(monkeypatch)
    seen: list[tuple[str, str, dict | None]] = []
    data = data_mod.GuiData(
        transport=scripted(
            [
                (
                    "PUT",
                    "/api/v1/workspaces/ws-1/egress/policy",
                    httpx.Response(200, json={}),
                )
            ],
            seen,
        )
    )
    await data.set_egress_mode("ws-1", "open", confirm_empty=True)
    assert seen[0][2] == {"mode": "open", "confirm_empty": True}


async def test_create_lands_the_invoking_user(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A create without a ``user`` lands the operator's own
    account (#248), the same default the CLI applies, and the
    configured public key (#495) supplies the identity half —
    pubkey_file outranks the encrypted private half, so its
    refusal never fires."""
    from testkeys import encrypted, mint

    gui_env(monkeypatch)
    monkeypatch.setattr(clientdata, "invoking_user", lambda: "pat")
    pem, public = mint("ed25519")
    (tmp_path / "id.pub").write_text(f"{public} me@host\n")
    locked = tmp_path / "id"
    locked.write_text(encrypted(pem))
    monkeypatch.setenv("MSKSC_IDENTITY_FILE", str(locked))
    monkeypatch.setenv("MSKSC_PUBKEY_FILE", str(tmp_path / "id.pub"))
    seen: list[tuple[str, str, dict | None]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(
            ("POST", "/api/v1/workspaces", json.loads(request.content))
        )
        return httpx.Response(200, json={"id": "ws-9"})

    data = data_mod.GuiData(transport=httpx.MockTransport(handler))
    row = await data.create({"name": "dev"})
    assert row == {"id": "ws-9"}
    assert seen[0][2]["user"] == "pat"
    assert seen[0][2]["ssh_pubkey"].startswith("ssh-ed25519")


async def test_the_data_layer_carries_the_exit_contract_failures(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#904's contract for the GUI: the unreachable line and the
    HTTP refusal both arrive as ``RestFailure`` carrying the
    message the CLI prints — the daemon-down shape with
    ``reach`` set (the daemon answered nothing), the refusal with
    it clear (the daemon answered)."""
    gui_env(monkeypatch)

    def dead(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("all attempts refused")

    data = data_mod.GuiData(transport=httpx.MockTransport(dead))
    with pytest.raises(clientdata.RestFailure) as reached:
        await data.workspaces()
    assert "cannot reach" in str(reached.value)
    assert reached.value.reach

    def refused(request: httpx.Request) -> httpx.Response:
        return httpx.Response(409, json={"detail": "workspace is running"})

    data = data_mod.GuiData(transport=httpx.MockTransport(refused))
    with pytest.raises(clientdata.RestFailure) as conflict:
        await data.stop("ws-1")
    assert "workspace is running" in str(conflict.value)
    assert not conflict.value.reach


async def test_health_is_best_effort_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``fetch_health`` owns the every-failure-is-None contract
    (#717): a daemon that will not answer the probe leaves the
    screen every row, not a failure to flash."""
    gui_env(monkeypatch)

    def dead(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("all attempts refused")

    data = data_mod.GuiData(transport=httpx.MockTransport(dead))
    assert await data.health() is None


def test_the_tls_context_is_shared_and_local_needs_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One context serves the GUI's lifetime (the TOFU warning
    prints once), and the local listener skips TLS entirely
    (#555): no context exists to build, so none is."""
    gui_env(monkeypatch)
    first = env_mod.shared_ssl()
    assert first is not None
    assert env_mod.shared_ssl() is first
    monkeypatch.setenv("MSKSC_URL", "")
    monkeypatch.setenv("MSKSC_SOCKET", "/run/msks/msksd.sock")
    assert env_mod.shared_ssl() is None


async def test_the_images_page_gathers_and_filters(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The images tab's gathered read (#964): the daemon's
    catalog, the machine word off /health, then the channel's
    manifest — already narrowed to the rows the daemon's own
    machine boots, the presenter's arch guard run on the worker
    — as one answer the tab refreshes whole. In that order, so a
    refused catalog read sends no manifest request at all."""
    gui_env(monkeypatch)
    catalog = [
        {
            "name": "debian",
            "version": "13.6",
            "hash": "a" * 64,
            "default": True,
            "imported": "2026-01-02T03:04:05",
        }
    ]
    channel = "https://ch.example/manifest.json"
    seen: list[tuple[str, str, dict | None]] = []
    data = data_mod.GuiData(
        transport=scripted(
            [
                (
                    "GET",
                    "/api/v1/images",
                    httpx.Response(200, json=catalog),
                ),
                (
                    "GET",
                    "/api/v1/health",
                    httpx.Response(200, json={"machine": "x86_64"}),
                ),
                (
                    "GET",
                    "/manifest.json",
                    httpx.Response(
                        200,
                        json=[
                            {
                                "ref": "amd64:1",
                                "url": "https://ch.example/a.tar",
                                "arch": "x86_64",
                            },
                            {
                                "ref": "arm64:1",
                                "url": "https://ch.example/b.tar",
                                "arch": "aarch64",
                            },
                            {
                                "ref": "any:1",
                                "url": "https://ch.example/c.tar",
                            },
                        ],
                    ),
                ),
            ],
            seen,
        )
    )
    page = await data.images_page(channel)
    assert page == {
        "catalog": catalog,
        "published": [
            {
                "ref": "amd64:1",
                "url": "https://ch.example/a.tar",
                "arch": "x86_64",
            },
            {"ref": "any:1", "url": "https://ch.example/c.tar"},
        ],
    }
    assert seen == [
        ("GET", "/api/v1/images", None),
        ("GET", "/api/v1/health", None),
        ("GET", "/manifest.json", None),
    ]


async def test_the_images_page_carries_a_failed_manifest(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A channel that will not answer (#964, #904's contract):
    the manifest's fetch failure arrives carried like every
    other call's — the daemon-down shape with ``reach`` set, the
    fetch's own one-line refusal as the message."""
    gui_env(monkeypatch)
    seen: list[tuple[str, str, dict | None]] = []
    data = data_mod.GuiData(
        transport=scripted(
            [
                ("GET", "/api/v1/images", httpx.Response(200, json=[])),
                ("GET", "/api/v1/health", httpx.Response(200, json={})),
                (
                    "GET",
                    "/manifest.json",
                    httpx.Response(404, json={"detail": "gone"}),
                ),
            ],
            seen,
        )
    )
    with pytest.raises(clientdata.RestFailure) as failed:
        await data.images_page("https://ch.example/manifest.json")
    assert "cannot fetch the image manifest" in str(failed.value)
    assert failed.value.reach


async def test_the_secrets_page_gathers_both_halves(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The secrets tab's gathered read (#966): the placeholder
    listing with its lock states, then the recorded audit rows —
    one answer the tab refreshes whole, both daemon reads in
    that order."""
    gui_env(monkeypatch)
    secrets = [
        {
            "id": 1,
            "workspaces": [],
            "name": "github_api",
            "dests": ["api.github.com"],
            "created_at": "2026-01-02T03:04:05",
            "expires_at": None,
            "locked": True,
        }
    ]
    audit = [
        {
            "id": 3,
            "kind": "mint",
            "workspaces": [],
            "name": "github_api",
            "dests": ["api.github.com"],
            "created_at": "2026-01-02T03:04:05",
        }
    ]
    seen: list[tuple[str, str, dict | None]] = []
    data = data_mod.GuiData(
        transport=scripted(
            [
                ("GET", "/api/v1/secrets", httpx.Response(200, json=secrets)),
                (
                    "GET",
                    "/api/v1/secrets/audit",
                    httpx.Response(200, json=audit),
                ),
            ],
            seen,
        )
    )
    page = await data.secrets_page()
    assert page == {"secrets": secrets, "audit": audit}
    assert [request[:2] for request in seen] == [
        ("GET", "/api/v1/secrets"),
        ("GET", "/api/v1/secrets/audit"),
    ]
