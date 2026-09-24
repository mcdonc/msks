"""Client CLI tests: the command surface against mocks and the real API.

The mock-transport tests pin the client contract (auth header, method,
path, POST body, output shape, one-line errors); the ASGI tests run
the same ``api_call`` seam against the real daemon surface.
"""

import argparse
import asyncio
import io
import json
import os
import ssl
import sys
import time
from collections.abc import AsyncIterator
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from msks.app import build_app
from msks.client import cli, rest
from msks.server.api import build_api
from msks.settings import NetSettings, ServerSettings, Settings, VmmSettings
from test_api import TOKEN, StubMicrovm

ROWS = [
    {
        "id": "1a2b3c4d",
        "name": "alpha",
        "status": "running",
        "image_hash": "a" * 64,
        "host": "hv1",
    },
    {
        "id": "2b3c4d5e",
        "name": "beta",
        "status": "created",
        "image_hash": None,
        "host": None,
    },
]


def mock(handler) -> httpx.MockTransport:
    return httpx.MockTransport(handler)


def client_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MSKSC_URL", "https://daemon")
    monkeypatch.setenv("MSKSC_TOKEN", "tok")
    # An operator may export MSKSC_EXPECTED_IMAGE by hand — without
    # this, every client test would depend on ambient shell state.
    monkeypatch.delenv("MSKSC_EXPECTED_IMAGE", raising=False)


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
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
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
        ["key", "alpha"],
        transport=mock(lambda req: httpx.Response(200, json=KEY_BODY)),
    )
    assert rc == 0
    assert "ecdsa-sha2-nistp256" in capsys.readouterr().out


def test_cmd_ls_aligns_columns(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """#271: every row's columns start at the same offset — a long
    name widens its column for all rows instead of shifting that
    row's later columns off the grid."""
    rows = [
        ROWS[0],
        {
            **ROWS[1],
            "name": "a-workspace-name-far-past-twenty-chars",
        },
    ]
    client_env(monkeypatch)
    rc = cli.cmd_ls(transport=mock(lambda req: httpx.Response(200, json=rows)))
    assert rc == 0
    lines = capsys.readouterr().out.splitlines()
    assert len(lines) == 3  # header plus one per row
    assert lines[0].startswith("name")  # the header aligns with its column
    # The id and status columns start at the same offset in both
    # rows: the long name widened its column for the whole grid.
    assert lines[1].index(rows[0]["id"]) == lines[2].index(rows[1]["id"])
    assert lines[1].index("running") == lines[2].index("created")
    assert "a" * 12 in lines[1]  # the image hash is shortened to 12 chars


def test_cmd_ls_rows_fit_the_values(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    client_env(monkeypatch)
    rc = cli.cmd_ls(transport=mock(lambda req: httpx.Response(200, json=ROWS)))
    assert rc == 0
    out = capsys.readouterr().out
    assert "alpha" in out and "running" in out and "hv1" in out
    assert "beta" in out and "created" in out and "-" in out
    # The #246 columns: the label beside the immutable id, which
    # addresses the workspace as surely as the name does.
    assert cli.workspace_cells(ROWS[0]) == [
        "alpha",
        ROWS[0]["id"],
        "running",
        "a" * 12,
        "hv1",
    ]


def test_display_name_prefers_the_label() -> None:
    """#246: the human-facing label is the name, falling back to the
    id for a nameless workspace."""
    assert cli.display_name({"id": "x1", "name": "ws"}) == "ws"
    assert cli.display_name({"id": "x1", "name": None}) == "x1"


def test_created_line_names_the_label_and_the_id() -> None:
    """#246: the create confirmation carries both halves of the
    workspace's identity."""
    row = {"id": "1a2b3c4d5e6f7890", "name": "ws"}
    assert cli.created_line(row) == "created ws (id 1a2b3c4d5e6f7890)"
    nameless = {"id": "1a2b3c4d5e6f7890", "name": None}
    assert cli.created_line(nameless) == "created 1a2b3c4d5e6f7890"
    legacy = {"id": "ws", "name": "ws"}
    assert cli.created_line(legacy) == "created ws"


def test_narrowed_matches_the_name_or_the_id() -> None:
    """#246: the storage table narrows by either reference."""
    rows = [{"id": "1a2b", "name": "ws"}]
    assert cli.narrowed(rows, "ws") == rows
    assert cli.narrowed(rows, "1a2b") == rows
    assert cli.narrowed(rows, "other") == []
    assert cli.narrowed(rows, None) == rows


def test_resolved_workspace_id_names_a_missing_ref() -> None:
    """#246: the secret commands resolve their workspace ref through
    the listing; a ref nothing answers exits naming it."""
    rows = [{"id": "1a2b", "name": "ws"}]
    assert cli.resolved_workspace_id(rows, "ws") == "1a2b"
    with pytest.raises(SystemExit, match="no such workspace: ghost"):
        cli.resolved_workspace_id(rows, "ghost")


OLD_IMAGE = "/nix/store/oldaaaa-msks-appliance"
NEW_IMAGE = "/nix/store/newwwww-msks-appliance"


def route(req: httpx.Request) -> httpx.Response:
    """The ls route pair: workspaces plus a health that serves an
    older image — the drifted-daemon shape #160 names."""
    if req.url.path == "/api/v1/health":
        return httpx.Response(200, json={"status": "ok", "image": OLD_IMAGE})
    return httpx.Response(200, json=ROWS)


def test_cmd_ls_names_a_stale_image(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The drift notice (#160): the daemon serves a different image
    than this tree builds — older OR newer, the wording takes no
    direction — and `msks ls` says so with the fix."""
    client_env(monkeypatch)
    monkeypatch.setenv("MSKSC_EXPECTED_IMAGE", NEW_IMAGE)
    rc = cli.cmd_ls(transport=mock(route))
    assert rc == 0
    err = capsys.readouterr().err
    assert "different image" in err
    assert Path(OLD_IMAGE).name in err and Path(NEW_IMAGE).name in err
    assert "restarting the daemon on the expected image" in err

    # The reverse drift (an older checkout beside a newer running
    # deployment — bisect, a worktree switch) uses the same wording.
    monkeypatch.setenv(
        "MSKSC_EXPECTED_IMAGE", "/nix/store/ancient-msks-appliance"
    )
    cli.cmd_ls(transport=mock(route))
    assert "different image" in capsys.readouterr().err


def test_cmd_ls_stays_silent_when_images_match(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A current image (or a daemon that predates the image
    pair — `image: null`) prints the listing and nothing else."""
    client_env(monkeypatch)
    monkeypatch.setenv("MSKSC_EXPECTED_IMAGE", OLD_IMAGE)
    cli.cmd_ls(transport=mock(route))
    assert capsys.readouterr().err == ""

    monkeypatch.setenv("MSKSC_EXPECTED_IMAGE", NEW_IMAGE)
    cli.cmd_ls(
        transport=mock(
            lambda req: (
                httpx.Response(200, json={"status": "ok", "image": None})
                if req.url.path == "/api/v1/health"
                else httpx.Response(200, json=ROWS)
            )
        )
    )
    assert capsys.readouterr().err == ""


def test_cmd_ls_ignores_a_malformed_health_document(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A /health that answers a non-mapping (a wrong host, a
    proxy) never tracebacks the listing: the probe is best-effort,
    the notice simply stays off. Found live: a tree with built
    deployment presets MSKSC_EXPECTED_IMAGE, so the probe
    fires for every `msks ls` from a devenv shell."""
    client_env(monkeypatch)
    monkeypatch.setenv("MSKSC_EXPECTED_IMAGE", NEW_IMAGE)
    rc = cli.cmd_ls(transport=mock(lambda req: httpx.Response(200, json=ROWS)))
    assert rc == 0
    assert "alpha" in capsys.readouterr().out


@pytest.mark.parametrize("image", [5, True, ["x"], {"a": 1}, "", None])
def test_stale_image_notice_rejects_non_string_images(image: object) -> None:
    """Only a string image pairs with the notice: any other JSON
    value a wrong host or proxy might answer for /health stays
    silent instead of tracebacks `Path(image)` (#163 review)."""
    assert cli.stale_image_notice(NEW_IMAGE, {"image": image}) is None


def test_cmd_ls_skips_the_health_probe_without_an_expected_image(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """No MSKSC_EXPECTED_IMAGE (outside a devenv shell, or before
    the first build): one request — the listing — and no probe."""
    client_env(monkeypatch)
    monkeypatch.delenv("MSKSC_EXPECTED_IMAGE", raising=False)
    seen: list[str] = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(req.url.path)
        return httpx.Response(200, json=ROWS)

    cli.cmd_ls(transport=mock(handler))
    assert seen == ["/api/v1/workspaces"]
    assert capsys.readouterr().err == ""


def test_cmd_ls_json(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    client_env(monkeypatch)
    cli.cmd_ls(
        as_json=True,
        transport=mock(lambda req: httpx.Response(200, json=ROWS)),
    )
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


def test_cmd_create_client_mint_sends_public_only(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    """The create default (#121): the keypair is minted on this
    client, the POST body carries the public half only, the daemon's
    answer is checked against the no-escrow promise, and the private
    half is persisted mode 0600 under the client data root after the
    create."""
    from cryptography.hazmat.primitives import serialization

    client_env(monkeypatch)
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/ssh-key"):
            return httpx.Response(
                200,
                json={
                    "workspace": "ws1",
                    "type": "ssh-ed25519",
                    "public_key": f"{seen['supplied']} msks-client:ws1",
                    "private_key": None,
                },
            )
        seen["body"] = json.loads(request.content)
        seen["supplied"] = seen["body"]["ssh_pubkey"]
        return httpx.Response(201, json={"id": "ws1", "status": "created"})

    rc = cli.cmd_create(
        {"id": "ws1"}, transport=mock(handler), key_type="ed25519"
    )
    assert rc == 0
    supplied = seen["body"]["ssh_pubkey"]
    assert supplied.startswith("ssh-ed25519 ")
    # No private material crosses the wire under any name.
    assert not any("priv" in name for name in seen["body"])
    identity = tmp_path / "msks" / "ws1" / "identity"
    pem = identity.read_text()
    assert pem.startswith("-----BEGIN OPENSSH PRIVATE KEY-----")
    assert identity.stat().st_mode & 0o777 == 0o600
    # The stored half is the pair's other half: it derives the line
    # that was sent.
    loaded = serialization.load_ssh_private_key(pem.encode(), password=b"")
    derived = (
        loaded.public_key()
        .public_bytes(
            encoding=serialization.Encoding.OpenSSH,
            format=serialization.PublicFormat.OpenSSH,
        )
        .decode()
    )
    assert derived == supplied
    out = capsys.readouterr().out
    assert "client identity" in out
    assert str(identity) in out


def test_write_client_identity_names_an_unusable_cache(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A cache the client cannot write into is operator-shaped: one
    SystemExit line naming the path and the recovery, after the
    workspace itself was created."""
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    (tmp_path / "msks").write_text("a file where a directory belongs")
    with pytest.raises(SystemExit, match="could not be written"):
        cli.write_client_identity("ws1", "private material")


def test_write_client_identity_forces_mode_on_a_preexisting_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The identity lands 0600 even when the path already holds a
    world-readable file: the data root is the only home of this half."""
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    target = tmp_path / "msks" / "ws1" / "identity"
    target.parent.mkdir(parents=True)
    target.write_text("stale")
    target.chmod(0o644)
    path = cli.write_client_identity("ws1", "private material")
    assert path == target
    assert target.read_text() == "private material"
    assert target.stat().st_mode & 0o777 == 0o600


def test_create_client_mint_refuses_a_silent_escrow(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    """A daemon one version behind drops the unknown field and mints
    its own pair: the create's follow-up check catches the swapped
    identity and the escrowed half instead of caching a key that does
    not open the workspace."""
    from msks.identity import mint

    client_env(monkeypatch)
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    daemon_private, daemon_public = mint("ed25519")

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/ssh-key"):
            return httpx.Response(
                200,
                json={
                    "workspace": "ws1",
                    "type": "ssh-ed25519",
                    "public_key": f"{daemon_public} msksd:ws1",
                    "private_key": daemon_private,
                },
            )
        return httpx.Response(201, json={"id": "ws1", "status": "created"})

    with pytest.raises(SystemExit, match="no-escrow promise"):
        cli.cmd_create(
            {"id": "ws1"}, transport=mock(handler), key_type="ed25519"
        )
    # Nothing was cached for the half the daemon swapped in.
    assert not (tmp_path / "msks" / "ws1").exists()
    assert "created ws1" in capsys.readouterr().out


def test_write_client_identity_fails_as_one_line_when_unwritable(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Every phase of the write — open, chmod, the write itself —
    exits as one named line: a directory squatting on the identity
    path raises the same operator-shaped SystemExit as an unusable
    cache root."""
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    blocker = tmp_path / "msks" / "ws1" / "identity"
    blocker.parent.mkdir(parents=True)
    blocker.mkdir()  # a directory where the key file belongs
    with pytest.raises(SystemExit, match="could not be written"):
        cli.write_client_identity("ws1", "private material")


def test_cmd_create_client_mint_with_start_boots_after_verification(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    """Client mint composes with --start in order: create, escrow
    verification, identity write, then boot — a failure anywhere
    earlier leaves the workspace created but unbooted, and the boot
    rides the same client."""
    client_env(monkeypatch)
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    paths = []
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        if request.url.path.endswith("/ssh-key"):
            return httpx.Response(
                200,
                json={
                    "workspace": "ws1",
                    "type": "ssh-ed25519",
                    "public_key": f"{seen['supplied']} msks-client:ws1",
                    "private_key": None,
                },
            )
        if request.url.path.endswith("/start"):
            return httpx.Response(200, json={"id": "ws1", "status": "running"})
        seen["supplied"] = json.loads(request.content)["ssh_pubkey"]
        return httpx.Response(201, json={"id": "ws1", "status": "created"})

    rc = cli.cmd_create(
        {"id": "ws1"}, start=True, transport=mock(handler), key_type="ed25519"
    )
    assert rc == 0
    assert paths == [
        "/api/v1/workspaces",
        "/api/v1/workspaces/ws1/ssh-key",
        "/api/v1/workspaces/ws1/start",
    ]
    assert (tmp_path / "msks" / "ws1" / "identity").exists()
    out = capsys.readouterr().out
    assert "created ws1" in out
    assert "msks console ws1" in out


SUPPLIED_PUBKEY = "ssh-ed25519 AAAAc3NzaC1lZDI1 operator@laptop"


def test_create_identity_modes() -> None:
    """The three identity modes (#121 default, #111 daemon, #132
    operator key) resolve to (key type, supplied line), and the
    flag pairings that would look meaningful but are not are
    rejected with the conflict named."""
    parser = cli.build_parser()
    plain = parser.parse_args(["create", "ws1"])
    assert cli.create_identity(plain) == ("ed25519", None)
    # The client and daemon mints share one default — the CLI help,
    # docs/cli.md, and the #121 changelog entry all say "the same
    # default the daemon mints". Pin the two literals together so
    # a one-sided change fails here instead of silently falsifying
    # that prose (#138's split-default hazard).
    assert cli.create_identity(plain) == (VmmSettings().ssh_key_type, None)
    typed = parser.parse_args(["create", "ws1", "--key-type", "rsa"])
    assert cli.create_identity(typed) == ("rsa", None)
    daemon = parser.parse_args(["create", "ws1", "--daemon-mint"])
    assert cli.create_identity(daemon) == (None, None)
    with pytest.raises(
        SystemExit, match="--key-type conflicts with --daemon-mint"
    ):
        cli.create_identity(
            parser.parse_args(
                ["create", "ws1", "--daemon-mint", "--key-type", "rsa"]
            )
        )


def test_create_identity_pubkey_mode(tmp_path: Path) -> None:
    """--pubkey FILE resolves to (None, line) — no mint — and every
    conflicting pairing is rejected before anything runs."""
    parser = cli.build_parser()
    source = tmp_path / "id.pub"
    source.write_text(f"{SUPPLIED_PUBKEY}\n")
    args = parser.parse_args(["create", "ws1", "--pubkey", str(source)])
    assert cli.create_identity(args) == (None, SUPPLIED_PUBKEY)
    with pytest.raises(
        SystemExit, match="--pubkey conflicts with --daemon-mint"
    ):
        cli.create_identity(
            parser.parse_args(
                ["create", "ws1", "--pubkey", str(source), "--daemon-mint"]
            )
        )
    with pytest.raises(SystemExit, match="--key-type needs the client mint"):
        cli.create_identity(
            parser.parse_args(
                ["create", "ws1", "--pubkey", str(source), "--key-type", "rsa"]
            )
        )
    # An explicit empty value still counts as supplied: it conflicts
    # with --daemon-mint instead of silently daemon-minting.
    with pytest.raises(
        SystemExit, match="--pubkey conflicts with --daemon-mint"
    ):
        cli.create_identity(
            parser.parse_args(
                ["create", "ws1", "--pubkey", "", "--daemon-mint"]
            )
        )


def test_read_pubkey_file_stdin_and_rejections(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The line arrives stripped from a file or stdin; a file that
    is not exactly one public key line fails as one readable error
    before any network roundtrip."""
    source = tmp_path / "id.pub"
    source.write_text(f"  {SUPPLIED_PUBKEY}  \n")
    assert cli.read_pubkey(str(source)) == SUPPLIED_PUBKEY
    monkeypatch.setattr("sys.stdin", io.StringIO(f"{SUPPLIED_PUBKEY}\n"))
    assert cli.read_pubkey("-") == SUPPLIED_PUBKEY
    two = tmp_path / "two.pub"
    two.write_text(f"{SUPPLIED_PUBKEY}\nssh-ed25519 AAAA second@host\n")
    with pytest.raises(SystemExit, match="exactly one line"):
        cli.read_pubkey(str(two))
    junk = tmp_path / "junk.pub"
    junk.write_text("lonely\n")
    with pytest.raises(SystemExit, match="does not look like a public key"):
        cli.read_pubkey(str(junk))
    with pytest.raises(SystemExit, match="cannot read public key file"):
        cli.read_pubkey(str(tmp_path / "missing.pub"))


def test_pubkey_and_user_data_stdin_conflict(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``--pubkey -`` and ``--user-data -`` both read stdin; the
    pairing is rejected up front instead of one flag starving the
    other into a confusing daemon-side error."""
    client_env(monkeypatch)
    with pytest.raises(SystemExit, match="both read stdin"):
        cli.main(["create", "ws1", "--pubkey", "-", "--user-data", "-"])


def test_cmd_create_pubkey_sends_the_line_and_writes_nothing(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    """--pubkey (#132): the operator's line is the ssh_pubkey sent,
    the daemon's answer is checked against the no-escrow promise,
    and nothing lands in the client data root — the private half
    stays wherever the operator keeps it."""
    client_env(monkeypatch)
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    source = tmp_path / "id.pub"
    source.write_text(f"{SUPPLIED_PUBKEY}\n")
    seen = {}

    annotated = f"{' '.join(SUPPLIED_PUBKEY.split()[:2])} msks-client:ws1"

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/ssh-key"):
            return httpx.Response(
                200,
                json={
                    "workspace": "ws1",
                    "type": "ssh-ed25519",
                    "public_key": annotated,
                    "private_key": None,
                },
            )
        seen["body"] = json.loads(request.content)
        return httpx.Response(201, json={"id": "ws1", "status": "created"})

    rc = cli.main(
        ["create", "ws1", "--pubkey", str(source)],
        transport=mock(handler),
    )
    assert rc == 0
    assert seen["body"]["ssh_pubkey"] == SUPPLIED_PUBKEY
    assert not (tmp_path / "msks" / "ws1").exists()
    out = capsys.readouterr().out
    assert "created ws1" in out
    assert "client identity" not in out


def test_cmd_create_daemon_mint_sends_no_key(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """--daemon-mint hands the identity to the daemon (#111): the
    body carries no key material and nothing is written
    client-side."""
    client_env(monkeypatch)
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/ssh-key"):
            raise AssertionError("the daemon-mint create fetches no key")
        seen["body"] = json.loads(request.content)
        return httpx.Response(201, json={"id": "ws1", "status": "created"})

    rc = cli.cmd_create({"id": "ws1"}, transport=mock(handler), key_type=None)
    assert rc == 0
    assert "ssh_pubkey" not in seen["body"]
    out = capsys.readouterr().out
    assert "created ws1" in out
    assert "client identity" not in out


def test_cmd_key_private_refused_for_client_minted(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A client-minted workspace serves its public half; the private
    forms explain where that half lives instead of printing None."""
    client_env(monkeypatch)
    body = dict(KEY_BODY, private_key=None)
    transport = mock(lambda req: httpx.Response(200, json=body))
    with pytest.raises(SystemExit, match="holds no private half"):
        cli.cmd_key("alpha", as_private=True, transport=transport)
    with pytest.raises(SystemExit, match="holds no private half"):
        cli.cmd_key("alpha", out="/tmp/never-written", transport=transport)
    rc = cli.cmd_key("alpha", transport=transport)
    assert rc == 0
    assert capsys.readouterr().out.strip() == body["public_key"]


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
        lambda req: httpx.Response(
            503, json={"detail": "workspace ws1 wedged"}
        )
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


def test_cmd_rm_foreign_host_is_one_line(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The 409 host-mismatch detail surfaces verbatim, like shell's
    # close codes — the operator learns where the artifacts live.
    client_env(monkeypatch)
    detail = (
        "home volume for workspace ws1 lives on host hv1; this host is hv2"
    )
    transport = mock(lambda req: httpx.Response(409, json={"detail": detail}))
    with pytest.raises(
        SystemExit, match="msks: 409: home volume.*lives on host hv1"
    ):
        cli.cmd_rm(["ws1"], transport=transport)


def test_api_call_status_error_uses_detail() -> None:
    transport = mock(
        lambda req: httpx.Response(409, json={"detail": "workspace exists"})
    )
    with pytest.raises(SystemExit, match="409: workspace exists"):
        asyncio.run(
            rest.api_call("POST", "https://d", "t", "/x", transport=transport)
        )


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
        match=(
            "body.id: String should match pattern; "
            "Input should be greater than 0"
        ),
    ):
        asyncio.run(
            rest.api_call("POST", "https://d", "t", "/x", transport=transport)
        )


def test_error_detail_empty_validation_list() -> None:
    response = httpx.Response(422, json={"detail": []})
    assert rest.error_detail(response) == "invalid request"


def test_api_call_timeout_names_the_daemon() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("read timed out")

    with pytest.raises(SystemExit, match="timed out talking to"):
        asyncio.run(
            rest.api_call(
                "POST", "https://d", "t", "/x", transport=mock(handler)
            )
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
        asyncio.run(
            cli.api_call("GET", "https://d", "t", "/x", transport=transport)
        )


def test_api_call_non_json_body_falls_back_to_text() -> None:
    transport = mock(lambda req: httpx.Response(503, text="boom"))
    with pytest.raises(SystemExit, match="boom"):
        asyncio.run(
            cli.api_call("GET", "https://d", "t", "/x", transport=transport)
        )


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
        ["ls", "--json"],
        transport=mock(lambda req: httpx.Response(200, json=ROWS)),
    )
    assert rc == 0
    assert json.loads(capsys.readouterr().out) == ROWS


def test_main_create_dispatch(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    client_env(monkeypatch)
    # The create default fills the invoking user's name (#248) —
    # pinned here so the body assertion stays about the dispatch.
    monkeypatch.setattr(cli.getpass, "getuser", lambda: "alice")
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(request.content)
        return httpx.Response(201, json={"id": "ws1", "status": "created"})

    # --daemon-mint keeps this dispatch test off the keygen path
    # (the client mint's own suite covers it).
    rc = cli.main(
        [
            "create",
            "ws1",
            "--image",
            "debian:13",
            "--cpus",
            "4",
            "--daemon-mint",
        ],
        transport=mock(handler),
    )
    assert rc == 0
    assert seen["body"] == {
        "name": "ws1",
        "image": "debian:13",
        "cpus": 4,
        "user": "alice",
    }
    assert "created ws1" in capsys.readouterr().out


def test_main_start_dispatch(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    client_env(monkeypatch)
    rc = cli.main(
        ["start", "ws1"],
        transport=mock(
            lambda req: httpx.Response(200, json={"status": "running"})
        ),
    )
    assert rc == 0
    assert "ws1 running" in capsys.readouterr().out


def test_main_stop_dispatch(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    client_env(monkeypatch)
    rc = cli.main(
        ["stop", "ws1"],
        transport=mock(
            lambda req: httpx.Response(200, json={"status": "stopped"})
        ),
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
    assert row["name"] == "cli-a"
    assert row["status"] == "created"
    rows = await rest.api_call(
        "GET",
        "https://test",
        TOKEN,
        "/api/v1/workspaces",
        transport=api_transport,
    )
    assert [item["name"] for item in rows] == ["cli-a"]
    assert [item["id"] for item in rows] == [row["id"]]


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
    created = await rest.api_call(
        "POST",
        "https://test",
        TOKEN,
        "/api/v1/workspaces",
        json_body={"id": "cli-l", "kernel": "/k", "rootfs": "/r"},
        transport=transport,
    )
    wid = created["id"]
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
    assert stopped == {"id": wid, "status": "stopped"}
    rows = await rest.api_call(
        "GET", "https://test", TOKEN, "/api/v1/workspaces", transport=transport
    )
    assert [row["status"] for row in rows] == ["stopped"]
    deleted = await rest.api_call(
        "DELETE",
        "https://test",
        TOKEN,
        "/api/v1/workspaces/cli-l",
        transport=transport,
    )
    assert deleted == {"deleted": wid}
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
        "DELETE",
        "https://test",
        TOKEN,
        "/api/v1/workspaces/cli-r",
        transport=transport,
    )
    wid = deleted["deleted"]
    assert len(wid) == 10  # the minted id (#246): 10 hex digits
    assert stub.calls.index(("shutdown", wid)) < stub.calls.index(
        ("cleanup", wid)
    )


async def test_api_call_stop_and_rm_missing_are_one_line(
    api_transport,
) -> None:
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
    booted = await rest.ensure_running(
        "cli-b", "https://test", TOKEN, transport=transport
    )
    assert booted is True
    row = await rest.api_call(
        "GET",
        "https://test",
        TOKEN,
        "/api/v1/workspaces/cli-b",
        transport=transport,
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
    booted = await rest.ensure_running(
        "cli-c", "https://test", TOKEN, transport=app_transport
    )
    assert booted is False
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
        return httpx.Response(
            200, json={"id": "ws1", "status": statuses.pop(0)}
        )

    booted = await rest.ensure_running(
        "ws1", "https://d", "t", transport=mock(handler)
    )
    assert booted is True  # a waited-out concurrent boot counts
    assert posts == []
    assert "waiting for the boot" in capsys.readouterr().err


async def test_ensure_running_starts_after_a_waited_boot_failed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The concurrent boot ended stopped (it lost its own race or
    # crashed): the wait falls through and this call starts the
    # workspace itself.
    statuses = iter(["starting", "stopped", "stopped"])
    posts: list[str] = []

    async def fast_sleep(seconds: float) -> None:
        pass

    monkeypatch.setattr(rest.asyncio, "sleep", fast_sleep)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            posts.append(request.url.path)
            return httpx.Response(200, json={"id": "ws1"})
        return httpx.Response(
            200, json={"id": "ws1", "status": next(statuses)}
        )

    booted = await rest.ensure_running(
        "ws1", "https://d", "t", transport=mock(handler)
    )
    assert booted is True
    assert posts == ["/api/v1/workspaces/ws1/start"]


async def test_ensure_running_boot_wait_times_out(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fast_sleep(seconds: float) -> None:
        pass

    monkeypatch.setattr(rest.asyncio, "sleep", fast_sleep)
    monkeypatch.setattr(rest, "BOOT_WAIT_S", 0.0)
    transport = mock(
        lambda req: httpx.Response(
            200, json={"id": "ws1", "status": "starting"}
        )
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
            return httpx.Response(
                503, json={"detail": "VM ws1 already exists"}
            )
        return httpx.Response(
            200, json={"id": "ws1", "status": next(statuses)}
        )

    booted = await rest.ensure_running(
        "ws1", "https://d", "t", transport=mock(handler)
    )
    assert booted is True
    assert calls == [
        "GET /api/v1/workspaces/ws1",
        "POST /api/v1/workspaces/ws1/start",
        "GET /api/v1/workspaces/ws1",
    ]


async def test_ensure_running_lost_race_reports_the_state() -> None:
    statuses = iter(["stopped", "stopped"])

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            return httpx.Response(
                503, json={"detail": "VM ws1 already exists"}
            )
        return httpx.Response(
            200, json={"id": "ws1", "status": next(statuses)}
        )

    with pytest.raises(SystemExit, match="ws1 is stopped"):
        await rest.ensure_running(
            "ws1", "https://d", "t", transport=mock(handler)
        )


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


def image_row(
    name: str, version: str, digest: str, default: bool = False
) -> dict:
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


def test_image_ls_aligns_columns(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """#271: the grid holds when a ref outgrows its column — the
    issue's own two rows, debian and the long nixos ref."""
    rows = [
        {
            **IMAGES[0],
            "name": "debian",
            "version": "13.6",
            "kernel_version": "6.12.107+deb13-amd64",
            "kernel_format": "bzImage",
        },
        {
            **IMAGES[1],
            "name": "nixos",
            "version": "26.05pre-git-w7r3iyyw",
            "hash": "7" * 64,
            "kernel_version": "6.18.50",
            "kernel_format": "bzImage",
        },
    ]
    client_env(monkeypatch)
    rc = cli.cmd_image_ls(
        transport=mock(lambda req: httpx.Response(200, json=rows))
    )
    assert rc == 0
    lines = capsys.readouterr().out.splitlines()
    assert len(lines) == 3  # header plus one per image
    # The hash and kernel columns start at the same offset in both
    # rows — the long ref widens its column for the whole grid.
    assert lines[1].index("a" * 12) == lines[2].index("7" * 12)
    assert lines[1].index("6.12.107") == lines[2].index("6.18.50")
    assert lines[0].startswith("ref")  # the header aligns with its column


def test_image_ls_marks_only_the_default(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    client_env(monkeypatch)
    cli.cmd_image_ls(transport=listing_transport())
    lines = capsys.readouterr().out.splitlines()
    flags = [line.split()[2] for line in lines[1:]]  # line 0 is the header
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
    rc = cli.cmd_image_ls(
        transport=mock(lambda req: httpx.Response(200, json=[]))
    )
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

    rc = cli.cmd_image_import(
        "/srv/images/debian.tar", transport=mock(handler)
    )
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


def test_help_folds_long_tokens_instead_of_cutting_them(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """#271: a help string longer than its column folds at the
    column edge and keeps every character (argparse's own
    break-long-words posture) — a long path never ends in an
    ellipsis cut."""
    monkeypatch.setenv("COLUMNS", "80")
    with pytest.raises(SystemExit) as excinfo:
        cli.main(["create", "--help"])
    assert excinfo.value.code == 0
    text = capsys.readouterr().out
    assert "…" not in text
    flat = "".join(line.lstrip() for line in text.splitlines())
    assert "`~/.local/share/msks/<id>/identity`, or" in flat


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
        return httpx.Response(
            409, json={"detail": "workspace ws1 boots this image"}
        )

    with pytest.raises(
        SystemExit, match=r"msks: 409: workspace ws1 boots this image"
    ):
        cli.cmd_image_rm("debian:13", transport=listing_transport(handler))


def test_image_rm_miss_lists_the_catalog(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client_env(monkeypatch)
    with pytest.raises(SystemExit) as excinfo:
        cli.cmd_image_rm("fedora:40", transport=listing_transport())
    message = str(excinfo.value)
    assert "no image matches 'fedora:40'" in message
    assert "debian:13" in message and "alpine:3.20" in message
    # A bare name that matches nothing takes the same exit line.
    with pytest.raises(SystemExit, match="no image matches 'fedora'"):
        cli.cmd_image_rm("fedora", transport=listing_transport())


def test_image_rm_ambiguous_prefix_is_named(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
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


def test_image_rm_malformed_pin_is_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client_env(monkeypatch)
    for ref in ("debian@zzz", "debian@" + "a" * 12):
        with pytest.raises(
            SystemExit, match=f"malformed image hash in '{ref}'"
        ):
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
def test_image_default_designates_by_every_reference_form(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    ref: str,
) -> None:
    """#270: every reference form rm takes designates the same
    digest — the resolution happens client-side against the
    listing, then one POST carries the resolved hash."""
    client_env(monkeypatch)
    posted = []

    def handler(request: httpx.Request) -> httpx.Response:
        posted.append(
            (request.method, request.url.path, json.loads(request.content))
        )
        return httpx.Response(200, json={"hash": "a" * 64, "ref": "debian:13"})

    rc = cli.cmd_image_default(ref, transport=listing_transport(handler))
    assert rc == 0
    assert posted == [("POST", "/api/v1/images/default", {"ref": "a" * 64})]
    assert (
        "designated debian:13 (" + "a" * 12 + ") as the default image"
        in capsys.readouterr().out
    )


def test_image_default_miss_lists_the_catalog(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#270: a reference that names nothing is the named miss with
    the catalog spelled out — the same lookup rm uses."""
    client_env(monkeypatch)
    with pytest.raises(SystemExit) as excinfo:
        cli.cmd_image_default("fedora:40", transport=listing_transport())
    message = str(excinfo.value)
    assert "no image matches 'fedora:40'" in message
    assert "debian:13" in message and "alpine:3.20" in message


def test_image_default_ambiguous_prefix_is_named(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#270: an ambiguous hash prefix names the images it matches —
    the same refusal rm answers, before any POST leaves."""
    client_env(monkeypatch)
    rows = IMAGES + [image_row("debian", "13.1", "a" * 63 + "e")]
    posted = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method != "GET":
            posted.append(request.url.path)
        return httpx.Response(200, json=rows)

    with pytest.raises(SystemExit) as excinfo:
        cli.cmd_image_default("a" * 63, transport=mock(handler))
    message = str(excinfo.value)
    assert "matches 2 images" in message
    assert "debian:13" in message and "debian:13.1" in message
    assert "use the full hash or name@hash" in message
    assert posted == []


def test_image_default_unset_reports_the_fallback(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """#270: --unset DELETEs the designation and the line reports
    the fallback that applies — the sole entry, or the need for
    --image."""
    client_env(monkeypatch)
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.method, request.url.path))
        return httpx.Response(200, json={"fallback": None})

    rc = cli.cmd_image_default(None, unset=True, transport=mock(handler))
    assert rc == 0
    assert seen == [("DELETE", "/api/v1/images/default")]
    assert "a bare create needs --image" in capsys.readouterr().out
    fallback = {
        "hash": "c" * 64,
        "name": "alpine",
        "version": "3.20",
        "ref": "alpine:3.20",
    }
    cli.cmd_image_default(
        None,
        unset=True,
        transport=mock(
            lambda req: httpx.Response(200, json={"fallback": fallback})
        ),
    )
    assert (
        "falls back to the sole entry alpine:3.20 (" + "c" * 12 + ")"
        in capsys.readouterr().out
    )


def test_image_default_needs_a_reference_or_unset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#270: neither a reference nor --unset is a named usage error,
    and both together are refused."""
    client_env(monkeypatch)
    with pytest.raises(SystemExit, match="or --unset"):
        cli.cmd_image_default(None, transport=listing_transport())
    with pytest.raises(SystemExit, match="not both"):
        cli.cmd_image_default(
            "debian:13", unset=True, transport=listing_transport()
        )


def test_main_dispatches_image_default(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    client_env(monkeypatch)
    rc = cli.main(
        ["image", "default", "alpine"],
        transport=listing_transport(
            lambda req: httpx.Response(200, json={"hash": "c" * 64})
        ),
    )
    assert rc == 0
    assert "designated alpine:3.20" in capsys.readouterr().out


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

    record = await cli.import_image(
        "https://test", TOKEN, str(archive), transport
    )
    assert record["ref"] == "debian:13.6"

    rows = await cli.fetch_images("https://test", TOKEN, transport)
    assert len(rows) == 1
    assert rows[0]["hash"] == record["hash"]
    assert rows[0]["default"] is True  # the sole import is designated

    described = await cli.describe_image(
        "https://test", TOKEN, record["hash"], transport
    )
    assert described["hash"] == record["hash"]

    removed = await cli.remove_image(
        "https://test", TOKEN, "debian:13.6", transport
    )
    assert removed == {"removed": record["hash"]}
    with pytest.raises(SystemExit, match="no image matches"):
        await cli.remove_image("https://test", TOKEN, "debian:13.6", transport)


async def test_image_default_against_the_real_api(api_transport) -> None:
    """#270: designate and unset against the real daemon surface —
    the listing marks the designated row, and the unset reports the
    fallback that applies."""
    from test_imagestore import build_containerdisk

    transport = api_transport
    app = transport.app.state.msks_app
    state_dir = app.state.settings.vmm.state_dir
    state_dir.mkdir(parents=True, exist_ok=True)
    hashes = {}
    for name, version in (("one", "1"), ("two", "2")):
        archive = state_dir / f"{name}.tar"
        build_containerdisk(archive, name=name, version=version)
        imported = await cli.import_image(
            "https://test", TOKEN, str(archive), transport
        )
        hashes[f"{name}:{version}"] = imported["hash"]
    designated = await cli.designate_image(
        "https://test", TOKEN, "two:2", transport
    )
    assert designated["hash"] == hashes["two:2"]
    rows = await cli.fetch_images("https://test", TOKEN, transport)
    flags = {f"{row['name']}:{row['version']}": row["default"] for row in rows}
    assert flags == {"one:1": False, "two:2": True}
    unset = await cli.unset_default_image("https://test", TOKEN, transport)
    assert unset["fallback"] is None
    await cli.remove_image("https://test", TOKEN, hashes["one:1"], transport)
    unset = await cli.unset_default_image("https://test", TOKEN, transport)
    assert unset["fallback"]["ref"] == "two:2"


def test_image_rm_miss_caps_a_large_catalog(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The miss line spells out at most CATALOG_REF_CAP refs."""
    client_env(monkeypatch)
    rows = [image_row("distro", str(n), f"{n:064x}") for n in range(12)]
    with pytest.raises(SystemExit) as excinfo:
        cli.cmd_image_rm(
            "missing",
            transport=mock(lambda req: httpx.Response(200, json=rows)),
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
            "f" * 63 + "e",
            transport=mock(lambda req: httpx.Response(200, json=rows)),
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
            "debian",
            transport=mock(lambda req: httpx.Response(200, json=rows)),
        )
    message = str(excinfo.value)
    assert "matches 2 images" in message
    assert (
        f"debian:13 ({'a' * 12})" in message
        and f"debian:13 ({'b' * 12})" in message
    )


def test_image_rm_on_an_empty_catalog_names_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client_env(monkeypatch)
    with pytest.raises(SystemExit, match=r"\(the catalog is empty\)"):
        cli.cmd_image_rm(
            "debian", transport=mock(lambda req: httpx.Response(200, json=[]))
        )


def test_create_user_data_reads_the_file(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    """--user-data FILE (#41) carries the file's bytes verbatim as the
    create body's user_data."""
    client_env(monkeypatch)
    monkeypatch.setattr(cli.getpass, "getuser", lambda: "alice")
    payload = "#!/bin/sh\necho seeded > /root/stamp\n"
    source = tmp_path / "seed.sh"
    source.write_text(payload)
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(request.content)
        return httpx.Response(201, json={"id": "ws1", "status": "created"})

    rc = cli.main(
        ["create", "ws1", "--user-data", str(source), "--daemon-mint"],
        transport=mock(handler),
    )
    assert rc == 0
    assert seen["body"] == {
        "name": "ws1",
        "user_data": payload,
        "user": "alice",
    }
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

    rc = cli.main(
        ["create", "ws1", "--user-data", "-", "--daemon-mint"],
        transport=mock(handler),
    )
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
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture,
    tmp_path: Path,
) -> None:
    """A binary payload file fails like an unreadable one: one line,
    before any network activity."""
    client_env(monkeypatch)
    source = tmp_path / "seed.bin"
    source.write_bytes(b"\xff\xfe#\x00")
    with pytest.raises(SystemExit, match="cannot read user-data"):
        cli.read_user_data(str(source))


def test_main_home_export_dispatch(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    """``msks home export`` dispatches to the download with the id's
    default filename."""
    client_env(monkeypatch)
    seen = {}

    def handler(req: httpx.Request) -> httpx.Response:
        seen["path"] = req.url.path
        return httpx.Response(200, content=b"vol-bytes")

    monkeypatch.chdir(tmp_path)
    rc = cli.main(["home", "export", "ws1"], transport=mock(handler))
    assert rc == 0
    assert seen["path"] == "/api/v1/workspaces/ws1/home"
    assert (tmp_path / "ws1.ext4").read_bytes() == b"vol-bytes"
    assert "exported ws1" in capsys.readouterr().out


def test_main_home_import_dispatch(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    """``msks home import`` dispatches to the upload with the file's
    bytes as the body."""
    client_env(monkeypatch)
    volume = tmp_path / "v.ext4"
    volume.write_bytes(b"upload-me")
    seen = {}

    def handler(req: httpx.Request) -> httpx.Response:
        seen["path"] = req.url.path
        seen["content_type"] = req.headers.get("content-type")
        return httpx.Response(200, json={"id": "ws1", "bytes": 9})

    rc = cli.main(
        ["home", "import", "ws1", str(volume)], transport=mock(handler)
    )
    assert rc == 0
    assert seen["path"] == "/api/v1/workspaces/ws1/home"
    assert seen["content_type"] == "application/octet-stream"
    assert "imported 9 bytes into ws1" in capsys.readouterr().out


def test_main_home_export_stdout_note(
    monkeypatch: pytest.MonkeyPatch, capsysbinary: pytest.CaptureFixture[bytes]
) -> None:
    """`-` streams the bytes to stdout and keeps the note on stderr,
    so a pipe stays clean for gzip/ssh."""
    client_env(monkeypatch)
    rc = cli.main(
        ["home", "export", "ws1", "-"],
        transport=mock(lambda req: httpx.Response(200, content=b"vol-bytes")),
    )
    assert rc == 0
    captured = capsysbinary.readouterr()
    assert captured.out == b"vol-bytes"
    assert "exported ws1" in captured.err.decode()


def test_main_home_import_missing_file_is_one_line(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An unreadable source fails before any network activity."""
    client_env(monkeypatch)
    with pytest.raises(SystemExit, match="msks: cannot read volume image"):
        cli.main(["home", "import", "ws1", "/no/such/volume.ext4"])


async def test_home_stream_errors_are_one_line(monkeypatch) -> None:
    """The streaming helpers keep ``request``'s one-line error
    contract: a dead dial and a stalled read both name the daemon."""
    client_env(monkeypatch)

    async def body() -> AsyncIterator[bytes]:
        yield b"x"

    async def refused(req: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    async def stalled(req: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow")

    for handler in (refused, stalled):
        async with rest.api_client(
            "https://daemon", "tok", transport=mock(handler)
        ) as client:
            with pytest.raises(
                SystemExit, match="msks: (cannot reach|timed out)"
            ):
                await rest.download(
                    client, "/api/v1/workspaces/x/home", io.BytesIO()
                )
            with pytest.raises(
                SystemExit, match="msks: (cannot reach|timed out)"
            ):
                await rest.upload(client, "/api/v1/workspaces/x/home", body())


async def test_home_export_unwritable_out_is_one_line(monkeypatch) -> None:
    """An output path the command cannot create fails with one line
    before any bytes move."""
    client_env(monkeypatch)
    with pytest.raises(SystemExit, match="msks: cannot write"):
        await cli.run_home_export(
            "https://daemon",
            "tok",
            "ws1",
            "/no/such/dir/v.ext4",
            mock(lambda req: httpx.Response(200, content=b"vol")),
        )


async def test_volume_source_stdin(monkeypatch) -> None:
    """`-` reads stdin and keeps its lifecycle: the stream yields the
    bytes and closes nothing."""
    data = io.BytesIO(b"stdin-bytes")
    monkeypatch.setattr(sys, "stdin", SimpleNamespace(buffer=data))
    assert cli.volume_source("-") is data
    assert [window async for window in cli.file_windows(data)] == [
        b"stdin-bytes"
    ]
    assert not data.closed


def test_home_export_broken_pipe_is_one_line(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A reader that vanishes mid-stream (`- | head`, a compressor
    on a full disk) is one line and a non-zero exit, not a
    traceback — the CLI's error contract holds on the pipe path."""
    client_env(monkeypatch)

    class BrokenSink:
        def write(self, data):
            raise BrokenPipeError

    # A spare fd stands in for the real stdout: broken_pipe_line's
    # dup2-to-devnull must not clobber the test session's captured fd 1.
    spare = os.open(os.devnull, os.O_WRONLY)
    monkeypatch.setattr(
        sys,
        "stdout",
        SimpleNamespace(buffer=BrokenSink(), fileno=lambda: spare),
    )
    with pytest.raises(SystemExit, match="reader closed early"):
        cli.main(
            ["home", "export", "ws1", "-"],
            transport=mock(lambda req: httpx.Response(200, content=b"vol")),
        )


def test_home_import_reply_without_a_count_is_one_line(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A 2xx reply that carries no byte count is a protocol break,
    not a KeyError traceback."""
    client_env(monkeypatch)
    volume = Path("/dev/null")
    with pytest.raises(SystemExit, match="carried no byte count"):
        cli.main(
            ["home", "import", "ws1", str(volume)],
            transport=mock(lambda req: httpx.Response(200, json={})),
        )


STORAGE_BODY = {
    "state": {
        "total": 40 * 1024**3,
        "used": int(23.4 * 1024**3),
        "free": int(16.6 * 1024**3),
        "pressure": "ok",
        "floor_mib": 512,
        "warn_pct": 90,
    },
    "workspaces": [
        {
            "id": "alpha",
            "root_mib": 10240,
            "home_mib": 2048,
            "root_bytes": int(3.1 * 1024**3),
            "home_bytes": 812 * 1024**2,
        }
    ],
    "images": [
        {
            "hash": "a" * 64,
            "name": "debian",
            "version": "13",
            "bytes": int(3.0 * 1024**3),
        }
    ],
}


def test_cmd_storage_renders_all_three_blocks(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """``msks storage``: the budget line, the per-workspace cost table
    against its ceilings, and the catalog costs (#184)."""
    client_env(monkeypatch)
    rc = cli.cmd_storage(
        transport=mock(lambda req: httpx.Response(200, json=STORAGE_BODY))
    )
    out = capsys.readouterr().out
    assert rc == 0
    assert "state disk    used 23.4G of 40G" in out
    assert "free 16.6G" in out
    assert "pressure ok" in out
    assert "root cost/ceiling" in out
    assert "alpha" in out
    assert "3.1G / 10G" in out
    assert "812M / 2G" in out
    assert "debian:13" in out


def test_cmd_storage_narrows_to_one_workspace(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    client_env(monkeypatch)
    rc = cli.cmd_storage(
        "alpha",
        transport=mock(lambda req: httpx.Response(200, json=STORAGE_BODY)),
    )
    out = capsys.readouterr().out
    assert rc == 0
    assert "alpha" in out


def test_cmd_storage_unknown_workspace_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client_env(monkeypatch)
    with pytest.raises(SystemExit, match="no such workspace"):
        cli.cmd_storage(
            "ghost",
            transport=mock(lambda req: httpx.Response(200, json=STORAGE_BODY)),
        )


def test_cmd_storage_json_is_verbatim(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    client_env(monkeypatch)
    rc = cli.cmd_storage(
        as_json=True,
        transport=mock(lambda req: httpx.Response(200, json=STORAGE_BODY)),
    )
    out = capsys.readouterr().out
    assert rc == 0
    assert json.loads(out) == STORAGE_BODY


def test_image_cost_table_shows_import_times() -> None:
    """Same-reference rows read as distinct through their import
    times (#186), rendered in the operator's local time; a row
    without a time — or one the clock cannot parse — renders a
    dash in its place."""
    when = datetime.fromisoformat("2026-09-21T14:03:00+00:00")
    rows = [
        {
            "name": "debian",
            "version": "13.6",
            "bytes": 3 * 1024**3,
            "imported": "2026-09-21T14:03:00+00:00",
        },
        {
            "name": "debian",
            "version": "13.6",
            "bytes": 3 * 1024**3,
            "imported": "2026-08-01T09:00:00+00:00",
        },
        {
            "name": "debian",
            "version": "13.6",
            "bytes": 3 * 1024**3,
            "imported": None,
        },
        {
            "name": "debian",
            "version": "13.6",
            "bytes": 3 * 1024**3,
            "imported": "not a date",
        },
        # A non-string is a daemon that sent JSON where a moment
        # belongs: a dash, not a traceback.
        {
            "name": "debian",
            "version": "13.6",
            "bytes": 3 * 1024**3,
            "imported": 12345,
        },
    ]
    text = cli.image_cost_table(rows)
    lines = text.splitlines()
    assert lines[0].split() == ["image", "imported", "cost"]
    expect = when.astimezone().strftime("%Y-%m-%d %H:%M")
    older = (
        datetime.fromisoformat("2026-08-01T09:00:00+00:00")
        .astimezone()
        .strftime("%Y-%m-%d %H:%M")
    )
    # Every row's cost column starts at the same offset — the
    # measured grid holds across the rows (#271).
    costs = [line.index("3G") for line in lines[1:]]
    assert costs == [costs[0]] * 5
    assert [line.split("  ")[1] for line in lines[1:]] == [
        expect,
        older,
        "-",
        "-",
        "-",
    ]


@pytest.fixture
def western_zone():
    """A UTC-4 host, POSIX-style (offset sign is positive-west),
    whatever zone the runner itself sits in."""
    old = os.environ.get("TZ")
    os.environ["TZ"] = "GMT4"
    time.tzset()
    yield
    if old is None:
        os.environ.pop("TZ", None)
    else:
        os.environ["TZ"] = old
    time.tzset()


def test_imported_cell_degrades_extreme_stamps(western_zone) -> None:
    """A year-1 stamp has no representation four hours west of UTC
    (#186 review round 2): the cell degrades to a dash, never a
    traceback. The zone is forced — a UTC runner would render the
    stamp and hide the crash the western host sees."""
    row = {"imported": "0001-01-01T00:00:01+00:00"}
    assert cli.imported_cell(row) == "-"


def test_image_cost_table_without_times_keeps_two_columns() -> None:
    """A daemon predating stamps (#186) still gets its table — the
    two-column shape, without a column of dashes."""
    rows = [{"name": "debian", "version": "13", "bytes": int(3.0 * 1024**3)}]
    assert cli.image_cost_table(rows).splitlines() == [
        "image      cost",
        "debian:13  3G",
    ]


def test_human_bytes_units() -> None:
    """The storage tables' units: whole numbers when the decimal adds
    nothing, one decimal when it carries information."""
    assert cli.human_bytes(512 * 1024) == "512K"
    assert cli.human_bytes(812 * 1024**2) == "812M"
    assert cli.human_bytes(2048 * 1024**2) == "2G"
    assert cli.human_bytes(int(3.1 * 1024**3)) == "3.1G"
    assert cli.human_bytes(int(23.4 * 1024**3)) == "23.4G"
    assert cli.human_bytes(40 * 1024**3) == "40G"


def test_render_storage_empty_tables_collapse(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A daemon with no workspaces and no images prints the budget
    line alone."""
    report = {
        "state": {
            "total": 4 * 1024**3,
            "used": 1024**3,
            "free": 3 * 1024**3,
            "pressure": "ok",
            "floor_mib": 512,
            "warn_pct": 90,
        },
        "workspaces": [],
        "images": [],
    }
    text = cli.render_storage(report, as_json=False)
    assert text == ("state disk    used 1G of 4G    free 3G    pressure ok")


RESIZED_ROW = {
    "id": "ws1",
    "root_mib": 10240,
    "home_mib": 4096,
    "cpus": 2,
    "mem_mib": 1024,
    "changes": ["home grew to 4096 MiB"],
}

RESIZED_ROW_WITH_ROOT = {
    **RESIZED_ROW,
    "changes": ["root grew to 10240 MiB", "home grew to 4096 MiB"],
}

RESIZED_ROW_WITH_TOPOLOGY = {
    **RESIZED_ROW,
    "cpus": 4,
    "mem_mib": 4096,
    "changes": ["cpus set to 4", "mem set to 4096 MiB"],
}

RESIZED_ROW_WITH_ROOT_AND_TOPOLOGY = {
    **RESIZED_ROW_WITH_TOPOLOGY,
    "changes": [
        "root grew to 10240 MiB",
        "cpus set to 4",
    ],
}


def test_cmd_resize_prints_the_new_sizes(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    client_env(monkeypatch)
    rc = cli.cmd_resize(
        "ws1",
        {"home_mib": 4096},
        transport=mock(lambda req: httpx.Response(200, json=RESIZED_ROW)),
    )
    out = capsys.readouterr().out
    assert rc == 0
    assert "resized ws1" in out
    assert "home 4096 MiB" in out
    assert "next boot" not in out  # a home resize moved the bytes already


def test_run_resize_refuses_an_empty_body(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client_env(monkeypatch)
    args = cli.build_parser().parse_args(["resize", "ws1"])
    with pytest.raises(SystemExit, match="nothing to resize"):
        cli.run_resize(args, mock(lambda req: httpx.Response(200, json={})))


def test_resize_command_wires_flags(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    client_env(monkeypatch)
    seen = {}

    def handler(req: httpx.Request) -> httpx.Response:
        seen["path"] = req.url.path
        seen["body"] = json.loads(req.read())
        return httpx.Response(200, json=RESIZED_ROW)

    rc = cli.main(
        ["resize", "ws1", "--home-mib", "4096"],
        transport=mock(handler),
    )
    assert rc == 0
    assert seen["path"] == "/api/v1/workspaces/ws1/resize"
    assert seen["body"] == {"home_mib": 4096}
    # The topology flags ride the same body (#277).
    rc = cli.main(
        [
            "resize",
            "ws1",
            "--cpus",
            "4",
            "--mem-mib",
            "4096",
            "--root-mib",
            "20480",
        ],
        transport=mock(handler),
    )
    assert rc == 0
    assert seen["body"] == {"cpus": 4, "mem_mib": 4096, "root_mib": 20480}


def test_cmd_resize_notes_the_root_boot_fill(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Only the root's guest-side fill waits for a boot; a home-only
    resize never claims it does (#187 review)."""
    client_env(monkeypatch)
    rc = cli.cmd_resize(
        "ws1",
        {"root_mib": 20480, "home_mib": 4096},
        transport=mock(
            lambda req: httpx.Response(200, json=RESIZED_ROW_WITH_ROOT)
        ),
    )
    out = capsys.readouterr().out
    assert rc == 0
    assert "fills the larger root on its next boot" in out
    # A root flag whose root did not move never claims the boot fill:
    # the daemon's changes list decides, not the request's flags.
    rc = cli.cmd_resize(
        "ws1",
        {"root_mib": 10240, "home_mib": 4096},
        transport=mock(lambda req: httpx.Response(200, json=RESIZED_ROW)),
    )
    out = capsys.readouterr().out
    assert rc == 0
    assert "next boot" not in out


def test_cmd_resize_prints_the_new_topology(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A topology resize shows the new cpus and memory with the
    boot note — the values apply at the next start, and the line
    says so (#277)."""
    client_env(monkeypatch)
    rc = cli.cmd_resize(
        "ws1",
        {"cpus": 4, "mem_mib": 4096},
        transport=mock(
            lambda req: httpx.Response(200, json=RESIZED_ROW_WITH_TOPOLOGY)
        ),
    )
    out = capsys.readouterr().out
    assert rc == 0
    assert "cpus 4, mem 4096 MiB" in out
    assert "the new topology applies on its next boot" in out


def test_cmd_resize_combines_the_boot_notes(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A resize that moved the root and the topology names both
    waits in one note."""
    client_env(monkeypatch)
    rc = cli.cmd_resize(
        "ws1",
        {"root_mib": 20480, "cpus": 4},
        transport=mock(
            lambda req: httpx.Response(
                200, json=RESIZED_ROW_WITH_ROOT_AND_TOPOLOGY
            )
        ),
    )
    out = capsys.readouterr().out
    assert rc == 0
    assert "the guest fills the larger root and the new topology " in out
    assert "applies on its next boot" in out


# --- egress consent commands (#69) ------------------------------------


def test_egress_rules_renders(monkeypatch, capsys) -> None:
    client_env(monkeypatch)
    rules = {
        "workspace_id": "ws1",
        "mode": "interactive",
        "allow_list": [".debian.org"],
        "allowed": [
            {
                "id": "a" * 8,
                "dest_host": "api.example",
                "dest_port": 443,
                "duration": "forever",
            }
        ],
        "denied": [
            {
                "id": "b" * 8,
                "dest_host": "203.0.113.7",
                "dest_port": 0,
                "duration": "5m",
            }
        ],
    }
    rc = cli.main(
        ["egress", "rules", "ws1"],
        transport=mock(lambda req: httpx.Response(200, json=rules)),
    )
    out = capsys.readouterr().out
    assert rc == 0
    assert "mode interactive" in out
    assert "allowlist: .debian.org" in out
    # The verdict rows render on the measured grid: one offset for
    # every destination, the header row included (#271).
    assert "verdict  destination" in out
    assert "allowed  api.example:443" in out
    assert "denied   203.0.113.7 (all ports)  5m" in out


def test_egress_requests_filter_and_decide(monkeypatch, capsys) -> None:
    client_env(monkeypatch)
    seen = {}

    def handler(req: httpx.Request) -> httpx.Response:
        seen["path"] = req.url.path
        seen["body"] = json.loads(req.read()) if req.content else None
        return httpx.Response(
            200,
            json={
                "id": "req",
                "verdict": {"decision": "allow", "duration": "5m"},
            },
        )

    rows = [
        {
            "id": "c" * 8,
            "dest_host": "db.internal",
            "dest_port": 5432,
            "decision": "pending",
            "duration": None,
            "requested_at": 1700000000.0,
        },
        # A bracketed SNI is data, never markup: the row prints
        # verbatim, on the same measured grid as its neighbor.
        {
            "id": "d" * 8,
            "dest_host": "x[/]y[bold]",
            "dest_port": 443,
            "decision": "pending",
            "duration": None,
            "requested_at": 1700000001.0,
        },
    ]

    def list_handler(req: httpx.Request) -> httpx.Response:
        seen["path"] = req.url.path + (
            "?" + str(req.url.params) if req.url.params else ""
        )
        return httpx.Response(200, json=rows)

    rc = cli.main(
        ["egress", "requests", "ws1", "--decision", "pending"],
        transport=mock(list_handler),
    )
    assert rc == 0
    assert seen["path"].endswith("egress/requests?decision=pending")
    out = capsys.readouterr().out
    lines = out.splitlines()
    assert "db.internal:5432" in lines[1]
    expect = f"{'d' * 8}  x[/]y[bold]:443   pending   -         1700000001"
    assert lines[2] == expect

    rc = cli.main(
        ["egress", "requests", "ws1"],
        transport=mock(lambda req: httpx.Response(200, json=[])),
    )
    assert rc == 0
    assert capsys.readouterr().out == ""  # no rows, no header

    rc = cli.main(
        ["egress", "decide", "ws1", "cccc", "allow", "--duration", "5m"],
        transport=mock(handler),
    )
    assert rc == 0
    assert seen["path"].endswith("/egress/requests/cccc")
    assert seen["body"] == {"decision": "allow", "duration": "5m"}
    assert "allow" in capsys.readouterr().out


def test_egress_decide_rejects_bad_values(monkeypatch) -> None:
    from msks.client import egress as egress_mod

    client_env(monkeypatch)
    with pytest.raises(SystemExit, match="allow or deny"):
        asyncio.run(egress_mod.run_decide("ws1", "r", "maybe", "once"))
    with pytest.raises(SystemExit, match="duration"):
        asyncio.run(egress_mod.run_decide("ws1", "r", "allow", "2h"))


def test_egress_revoke(monkeypatch, capsys) -> None:
    client_env(monkeypatch)
    seen = {}

    def handler(req: httpx.Request) -> httpx.Response:
        seen["method"] = req.method
        seen["path"] = req.url.path
        return httpx.Response(200, json={"id": "r", "revoked": True})

    rc = cli.main(["egress", "revoke", "ws1", "cccc"], transport=mock(handler))
    assert rc == 0
    assert seen["method"] == "DELETE"
    assert seen["path"].endswith("/egress/requests/cccc")
    assert "revoked" in capsys.readouterr().out


def test_create_carries_the_consent_flags(monkeypatch) -> None:
    client_env(monkeypatch)
    seen = {}

    def handler(req: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(req.read())
        return httpx.Response(201, json={"id": "ws1"})

    rc = cli.main(
        [
            "create",
            "ws1",
            "--kernel",
            "/k",
            "--rootfs",
            "/r",
            "--egress-mode",
            "static",
            "--allow",
            ".debian.org",
            "--allow",
            "10.0.0.0/8:443",
            "--daemon-mint",
        ],
        transport=mock(handler),
    )
    assert rc == 0
    assert seen["body"]["egress_mode"] == "static"
    assert seen["body"]["egress_allowlist"] == [
        ".debian.org",
        "10.0.0.0/8:443",
    ]


async def test_handle_frame_prints_and_prompts(capsys, monkeypatch) -> None:
    from msks.client import egress as eg

    await eg.handle_frame(
        {
            "event": "egress.request",
            "data": {
                "request": {
                    "id": "d" * 8,
                    "dest_host": "api.example",
                    "dest_port": 443,
                    "decision": "pending",
                    "requested_at": 1.0,
                }
            },
        },
        decide=False,
        duration="once",
        url="https://x",
        token="t",
    )
    assert "api.example:443" in capsys.readouterr().out
    await eg.handle_frame(
        {
            "event": "egress.resolved",
            "data": {"request_id": "d" * 8, "decision": "allowed"},
        },
        decide=False,
        duration="once",
        url="https://x",
        token="t",
    )
    assert "resolved: allowed" in capsys.readouterr().out


async def test_maybe_decide_posts_the_verdict(monkeypatch) -> None:
    """The --decide prompt posts an allow on y, nothing on n."""
    from msks.client import egress as eg

    posted = {}

    class FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return None

    async def fake_request(client, method, path, body=None):
        posted["method"] = method
        posted["path"] = path
        posted["body"] = body

    monkeypatch.setattr(eg, "api_client", lambda *_a, **_k: FakeClient())
    monkeypatch.setattr(eg, "request", fake_request)
    row = {
        "id": "d" * 8,
        "workspace_id": "ws1",
        "dest_host": "api.example",
        "dest_port": 443,
    }

    async def answer(prompt):
        return "y"

    async def no(prompt):
        return "n"

    real_to_thread = asyncio.to_thread
    monkeypatch.setattr(
        eg.asyncio,
        "to_thread",
        lambda fn, *a: answer(None) if fn is input else real_to_thread(fn, *a),
    )
    await eg.maybe_decide(row, "once", "https://x", "t")
    assert posted["method"] == "POST"
    assert posted["path"].endswith("/egress/requests/" + "d" * 8)
    assert posted["body"] == {"decision": "allow", "duration": "once"}

    posted.clear()
    monkeypatch.setattr(eg.asyncio, "to_thread", lambda fn, *a: no(None))
    await eg.maybe_decide(row, "once", "https://x", "t")
    # Anything but y/yes denies now (fail-fast), not silence.
    assert posted["body"] == {"decision": "deny", "duration": "once"}


def test_events_url_shapes_the_query() -> None:
    from msks.client.egress import events_url

    assert (
        events_url("https://d:8660", "tok")
        == "wss://d:8660/api/v1/events?token=tok"
    )
    assert (
        events_url("http://d:8660/", "a b")
        == "ws://d:8660/api/v1/events?token=a+b"
    )


# --- egress watch: the decider stream (#69) -------------------------------


class FakeWS:
    """One websocket connection yielding scripted frames."""

    def __init__(self, frames: list[str]) -> None:
        self.frames = frames
        self.sent: list[str] = []

    async def send(self, text: str) -> None:
        self.sent.append(text)

    def __aiter__(self):
        return self

    async def __anext__(self):
        if not self.frames:
            raise StopAsyncIteration
        return self.frames.pop(0)


class FakeConnect:
    """The websockets.connect surface: scripted connections, then
    done."""

    def __init__(self, connections: list[FakeWS]) -> None:
        self.connections = connections

    def __call__(self, **_kwargs):
        self.kwargs = _kwargs
        return self

    def __aiter__(self):
        return self

    async def __anext__(self):
        if not self.connections:
            raise StopAsyncIteration
        return self.connections.pop(0)


def test_egress_requests_without_a_filter(monkeypatch, capsys) -> None:
    client_env(monkeypatch)
    rows = [
        {
            "id": "e" * 8,
            "dest_host": "any.example",
            "dest_port": 0,
            "decision": "expired",
            "duration": "once",
            "requested_at": 1700000000.0,
        }
    ]
    rc = cli.main(
        ["egress", "requests", "ws1"],
        transport=mock(lambda req: httpx.Response(200, json=rows)),
    )
    assert rc == 0
    assert "any.example" in capsys.readouterr().out


def test_events_url_handles_a_bare_host() -> None:
    from msks.client.egress import events_url

    assert events_url("bare.example", "t") == (
        "wss://bare.example/api/v1/events?token=t"
    )


async def test_run_watch_registers_and_streams(monkeypatch, capsys) -> None:
    """The watch loop: announce on connect, print frames, and
    reconnect when the server closes a connection."""
    from msks.client import egress as eg

    ws1 = FakeWS(
        [
            json.dumps(
                {
                    "event": "egress.request",
                    "data": {
                        "request": {
                            "id": "r1",
                            "workspace_id": "ws1",
                            "dest_host": "api.example",
                            "dest_port": 443,
                            "decision": "pending",
                            "requested_at": 1.0,
                        }
                    },
                }
            ),
            json.dumps(
                {
                    "event": "egress.rules",
                    "data": {
                        "workspace_id": "ws1",
                        "mode": "interactive",
                        "allow_list": [],
                        "allowed": [],
                        "denied": [],
                    },
                }
            ),
        ]
    )
    ws2 = FakeWS([])
    connect = FakeConnect([ws1, ws2])
    monkeypatch.setattr(eg.websockets, "connect", connect)
    monkeypatch.setattr(eg, "env_token", lambda: "tok")
    monkeypatch.setattr(eg, "env_url", lambda: "https://d:8660")
    rc = await eg.run_watch("ws1", decide=False, duration="once")
    assert rc == 0
    assert ws1.sent == [
        json.dumps({"type": "egress.decider", "workspace": "ws1"})
    ]
    # The connection kwargs took the events URL and TLS context.
    assert connect.kwargs["uri"].endswith("events?token=tok")
    assert connect.kwargs["ssl"] is not None
    out = capsys.readouterr().out
    assert "api.example:443" in out
    assert "mode interactive" in out


async def test_watch_one_without_a_workspace_streams_anonymously(
    monkeypatch, capsys
) -> None:
    from msks.client import egress as eg

    ws = FakeWS(
        [
            json.dumps(
                {
                    "event": "egress.resolved",
                    "data": {"request_id": "r2", "decision": "denied"},
                }
            )
        ]
    )
    connect = FakeConnect([ws])
    monkeypatch.setattr(eg.websockets, "connect", connect)
    monkeypatch.setattr(eg, "env_token", lambda: "tok")
    monkeypatch.setattr(eg, "env_url", lambda: "http://d:8660")
    rc = await eg.run_watch(None, decide=False, duration="once")
    assert rc == 0
    assert ws.sent == []  # never announced: not a decider
    assert connect.kwargs["ssl"] is None  # a plain-ws daemon
    assert "resolved: denied" in capsys.readouterr().out


async def test_handle_frame_prompts_on_decide(monkeypatch, capsys) -> None:
    from msks.client import egress as eg

    prompted = []

    async def fake_maybe(row, duration, url, token):
        prompted.append((row["dest_host"], duration))

    monkeypatch.setattr(eg, "maybe_decide", fake_maybe)
    frame = {
        "event": "egress.request",
        "data": {
            "request": {
                "id": "r9",
                "dest_host": "ask.example",
                "dest_port": 443,
                "decision": "pending",
                "requested_at": 1.0,
            }
        },
    }
    await eg.handle_frame(
        frame, decide=True, duration="15m", url="https://x", token="t"
    )
    assert prompted == [("ask.example", "15m")]
    assert "ask.example:443" in capsys.readouterr().out


async def test_run_watch_reconnects_after_a_closed_connection(
    monkeypatch,
) -> None:
    from msks.client import egress as eg

    class ClosingWS:
        def __init__(self) -> None:
            self.sent = []

        async def send(self, _text) -> None:
            pass

        def __aiter__(self):
            return self

        async def __anext__(self):
            raise eg.websockets.ConnectionClosed(None, None)

    ws = ClosingWS()
    connect = FakeConnect([ws])
    monkeypatch.setattr(eg.websockets, "connect", connect)
    monkeypatch.setattr(eg, "env_token", lambda: "tok")
    monkeypatch.setattr(eg, "env_url", lambda: "https://d")
    # The reconnect loop spins on the closed connection until the
    # connect iterator ends; both connections consumed = the arm ran.
    rc = await eg.run_watch("ws1", decide=False, duration="once")
    assert rc == 0


async def test_handle_frame_ignores_unknown_events(capsys) -> None:
    from msks.client import egress as eg

    await eg.handle_frame(
        {"event": "workspace.status", "data": {}},
        decide=False,
        duration="once",
        url="https://x",
        token="t",
    )
    assert capsys.readouterr().out == ""


async def test_maybe_decide_denies_on_a_no(monkeypatch) -> None:
    """The --decide prompt sends a deny for anything but y/yes: the
    held connection fails fast instead of waiting out the timeout."""
    from msks.client import egress as eg

    posted = {}

    class FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return None

    async def fake_request(client, method, path, body=None):
        posted["body"] = body

    monkeypatch.setattr(eg, "api_client", lambda *_a, **_k: FakeClient())
    monkeypatch.setattr(eg, "request", fake_request)
    row = {
        "id": "n" * 8,
        "workspace_id": "ws1",
        "dest_host": "no.example",
        "dest_port": 443,
    }

    async def no(prompt):
        return "n"

    real_to_thread = asyncio.to_thread
    monkeypatch.setattr(
        eg.asyncio,
        "to_thread",
        lambda fn, *a: no(None) if fn is input else real_to_thread(fn, *a),
    )
    await eg.maybe_decide(row, "once", "https://x", "t")
    assert posted["body"] == {"decision": "deny", "duration": "once"}
    # The all-ports label for a portless destination.
    assert (
        eg.dest_label({"dest_host": "raw.example", "dest_port": 0})
        == "raw.example (all ports)"
    )


# --- secret commands (#198) -----------------------------------------------


def secret_rows() -> list[dict]:
    return [
        {
            "id": 3,
            "workspace_id": "ws-sec",
            "name": "github_api",
            "dests": ["api.github.com"],
            "created_at": "2026-01-01T00:00:00+00:00",
            "expires_at": None,
        }
    ]


def workspace_rows() -> list[dict]:
    """The listing the secret commands resolve the workspace ref
    against (#246)."""
    return [
        {
            "id": "ws-sec",
            "name": "ws-sec",
            "status": "created",
        }
    ]


def test_cmd_secret_mint_posts_and_prints_the_sentinel_once(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    """mint reads the secret from a file, posts it in the body, and
    prints the sentinel exactly once."""
    client_env(monkeypatch)
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        seen["body"] = json.loads(request.content)
        return httpx.Response(
            201, json={**secret_rows()[0], "sentinel": "mskssec1_abc"}
        )

    secret_file = tmp_path / "token"
    secret_file.write_text("ghp-real-token\n")
    code = cli.main(
        [
            "secret",
            "mint",
            "ws-sec",
            "--name",
            "github_api",
            "--dest",
            "api.github.com",
            "--secret-file",
            str(secret_file),
        ],
        transport=mock(handler),
    )
    assert code == 0
    assert seen["path"] == "/api/v1/secrets"
    assert seen["body"]["secret"] == "ghp-real-token"
    assert "ttl_s" not in seen["body"]
    out = capsys.readouterr().out
    assert "mskssec1_abc" in out
    assert out.count("mskssec1_abc") == 1


def test_cmd_secret_mint_reads_stdin_and_sends_ttl(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """``--secret-file -`` consumes piped stdin; ``--ttl`` rides the
    body."""
    client_env(monkeypatch)
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(request.content)
        return httpx.Response(
            201, json={**secret_rows()[0], "sentinel": "mskssec1_x"}
        )

    monkeypatch.setattr(sys.stdin, "read", lambda: "piped-token\n")
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False, raising=False)
    code = cli.main(
        [
            "secret",
            "mint",
            "ws-sec",
            "--name",
            "github_api",
            "--dest",
            ".github.com",
            "--ttl",
            "3600",
            "--secret-file",
            "-",
        ],
        transport=mock(handler),
    )
    assert code == 0
    assert seen["body"]["secret"] == "piped-token"
    assert seen["body"]["ttl_s"] == 3600
    assert seen["body"]["dests"] == [".github.com"]


def test_cmd_secret_mint_refuses_a_tty_stdin(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No pipe, no hang: a terminal stdin is a named error."""
    client_env(monkeypatch)
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True, raising=False)
    with pytest.raises(SystemExit, match="expects the secret on stdin"):
        cli.main(
            [
                "secret",
                "mint",
                "ws-sec",
                "--name",
                "x",
                "--dest",
                "a.com",
                "--secret-file",
                "-",
            ],
            transport=mock(lambda request: httpx.Response(201, json={})),
        )


def test_cmd_secret_mint_refuses_an_empty_secret(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """An empty file fails before any network roundtrip."""
    client_env(monkeypatch)
    empty = tmp_path / "empty"
    empty.write_text("  \n")
    with pytest.raises(SystemExit, match="empty"):
        cli.main(
            [
                "secret",
                "mint",
                "ws-sec",
                "--name",
                "x",
                "--dest",
                "a.com",
                "--secret-file",
                str(empty),
            ],
            transport=mock(lambda request: httpx.Response(201, json={})),
        )


def test_cmd_secret_ls_lists_without_sentinels(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    client_env(monkeypatch)

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/v1/secrets"
        return httpx.Response(200, json=secret_rows())

    code = cli.main(["secret", "ls"], transport=mock(handler))
    assert code == 0
    out = capsys.readouterr().out
    assert "ws-sec/github_api" in out
    assert "api.github.com" in out
    assert "never" in out
    assert "mskssec1_" not in out


def test_cmd_secret_revoke_and_renew_resolve_the_label(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """revoke/renew address (workspace, name); the CLI resolves the
    id through the listing first."""
    client_env(monkeypatch)
    calls: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append((request.method, request.url.path))
        if request.url.path == "/api/v1/workspaces":
            return httpx.Response(200, json=workspace_rows())
        if request.method == "DELETE":
            return httpx.Response(
                200, json={"revoked": 3, "store_cleaned": True}
            )
        if request.url.path.endswith("/renew"):
            return httpx.Response(
                200,
                json={**secret_rows()[0], "expires_at": "2027-01-01"},
            )
        return httpx.Response(200, json=secret_rows())

    code = cli.main(
        ["secret", "revoke", "ws-sec", "--name", "github_api"],
        transport=mock(handler),
    )
    assert code == 0
    assert ("DELETE", "/api/v1/secrets/3") in calls
    assert "left behind" not in capsys.readouterr().out

    code = cli.main(
        [
            "secret",
            "renew",
            "ws-sec",
            "--name",
            "github_api",
            "--ttl",
            "60",
        ],
        transport=mock(handler),
    )
    assert code == 0
    assert ("POST", "/api/v1/secrets/3/renew") in calls
    assert "2027-01-01" in capsys.readouterr().out


def test_cmd_secret_revoke_reports_a_leftover_value(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    client_env(monkeypatch)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v1/workspaces":
            return httpx.Response(200, json=workspace_rows())
        if request.method == "DELETE":
            return httpx.Response(
                200, json={"revoked": 3, "store_cleaned": False}
            )
        return httpx.Response(200, json=secret_rows())

    code = cli.main(
        ["secret", "revoke", "ws-sec", "--name", "github_api"],
        transport=mock(handler),
    )
    assert code == 0
    assert "left behind" in capsys.readouterr().out


def test_cmd_secret_check(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    client_env(monkeypatch)

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert request.url.path == "/api/v1/secrets/check"
        return httpx.Response(200, json={"provider": "file", "ok": True})

    code = cli.main(["secret", "check"], transport=mock(handler))
    assert code == 0
    assert "secret store (file): ok" in capsys.readouterr().out


def test_cmd_secret_ls_json(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """``--json`` answers one document."""
    client_env(monkeypatch)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=secret_rows())

    code = cli.main(["secret", "ls", "--json"], transport=mock(handler))
    assert code == 0
    assert json.loads(capsys.readouterr().out) == secret_rows()


def test_cmd_secret_ls_empty_prints_nothing(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A daemon holding no placeholders prints nothing — the empty
    table contract every listing shares."""
    client_env(monkeypatch)
    code = cli.main(
        ["secret", "ls"],
        transport=mock(lambda req: httpx.Response(200, json=[])),
    )
    assert code == 0
    assert capsys.readouterr().out == ""


def test_cmd_secret_revoke_names_a_missing_label(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An unknown (workspace, name) pair exits naming it."""
    client_env(monkeypatch)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v1/workspaces":
            return httpx.Response(200, json=workspace_rows())
        # A listing that neither starts with nor holds the match: the
        # scan walks every row before naming the absence.
        other = {**secret_rows()[0], "workspace_id": "ws-other"}
        return httpx.Response(200, json=[other])

    with pytest.raises(SystemExit, match="no placeholder missing_api"):
        cli.main(
            ["secret", "revoke", "ws-sec", "--name", "missing_api"],
            transport=mock(handler),
        )


def test_cmd_secret_mint_names_an_unreadable_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A missing secret file fails before any network roundtrip."""
    client_env(monkeypatch)
    with pytest.raises(SystemExit, match="cannot read secret file"):
        cli.main(
            [
                "secret",
                "mint",
                "ws-sec",
                "--name",
                "x",
                "--dest",
                "a.com",
                "--secret-file",
                str(tmp_path / "absent"),
            ],
            transport=mock(lambda request: httpx.Response(201, json={})),
        )


# --- create --user (#248) ---


def test_create_user_flag_rides_the_body(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An explicit --user is the create body's user, verbatim."""
    client_env(monkeypatch)
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(request.content)
        return httpx.Response(201, json={"id": "ws1", "status": "created"})

    rc = cli.main(
        ["create", "ws1", "--user", "alice", "--daemon-mint"],
        transport=mock(handler),
    )
    assert rc == 0
    assert seen["body"]["user"] == "alice"


def test_create_defaults_the_user_to_the_invoking_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No --user: the body carries the invoking user's name — the
    workspace seeds that account as its own."""
    client_env(monkeypatch)
    monkeypatch.setattr(cli.getpass, "getuser", lambda: "chrism")
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(request.content)
        return httpx.Response(201, json={"id": "ws1", "status": "created"})

    rc = cli.main(["create", "ws1", "--daemon-mint"], transport=mock(handler))
    assert rc == 0
    assert seen["body"]["user"] == "chrism"


def test_create_refuses_an_unusable_invoking_name(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A username the guest could never carry (a capitalized one) is
    a named local refusal pointing at --user, before any wire."""
    client_env(monkeypatch)
    monkeypatch.setattr(cli.getpass, "getuser", lambda: "Chris")

    def no_calls(request: httpx.Request) -> httpx.Response:
        raise AssertionError("the refusal must precede any request")

    with pytest.raises(SystemExit, match="pass --user"):
        cli.main(["create", "ws1", "--daemon-mint"], transport=mock(no_calls))


def test_create_refuses_an_off_charset_user_locally(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An explicit --user off the login-name charset fails with the
    local line, not the daemon's pattern error."""
    client_env(monkeypatch)
    with pytest.raises(SystemExit, match="not a valid login name"):
        cli.main(
            [
                "create",
                "ws1",
                "--user",
                "not a name",
                "--daemon-mint",
            ],
            transport=mock(
                lambda req: httpx.Response(201, json={"id": "ws1"})
            ),
        )


def test_create_refuses_when_no_invoking_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An environment that names no user (getpass finds nothing)
    is a named refusal pointing at --user, not a traceback."""
    client_env(monkeypatch)

    def no_name() -> str:
        raise OSError("no username in the environment")

    monkeypatch.setattr(cli.getpass, "getuser", no_name)
    with pytest.raises(SystemExit, match="pass --user"):
        cli.main(
            ["create", "ws1", "--daemon-mint"],
            transport=mock(
                lambda req: httpx.Response(201, json={"id": "ws1"})
            ),
        )


def test_cmd_image_check_routes_to_the_conformance_pass(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``msks image check`` delegates to the local conformance pass
    (#258); its exit code is the command's."""
    import argparse

    from msks import conformance

    seen: list[argparse.Namespace] = []

    def fake_run(args):
        seen.append(args)
        return 1

    monkeypatch.setattr(conformance, "run_check", fake_run)
    assert cli.cmd_image_check(argparse.Namespace(archive="x.tar")) == 1
    assert seen[0].archive == "x.tar"


def test_cmd_llm_token_prints_and_remints(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """``msks llm-token`` prints the workspace credential; a null
    token explains the remint path instead of printing None."""
    client_env(monkeypatch)
    seen: dict = {}

    def handler(req: httpx.Request) -> httpx.Response:
        seen["method"] = req.method
        seen["path"] = req.url.path
        if req.method == "POST":
            return httpx.Response(
                200, json={"workspace": "alpha", "token": "msksllm1_new"}
            )
        return httpx.Response(
            200, json={"workspace": "alpha", "token": "msksllm1_old"}
        )

    rc = cli.cmd_llm_token("alpha", transport=mock(handler))
    assert rc == 0
    assert seen["path"] == "/api/v1/workspaces/alpha/llm-token"
    assert capsys.readouterr().out.strip() == "msksllm1_old"
    rc = cli.cmd_llm_token("alpha", remint=True, transport=mock(handler))
    assert rc == 0
    assert seen["method"] == "POST"
    assert capsys.readouterr().out.strip() == "msksllm1_new"


def test_cmd_llm_token_explains_a_missing_token(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    client_env(monkeypatch)

    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"workspace": "alpha", "token": None})

    with pytest.raises(SystemExit, match="--remint"):
        cli.cmd_llm_token("alpha", transport=mock(handler))
