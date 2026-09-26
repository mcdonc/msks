"""API-level tests over ASGITransport with a stubbed microvm seam."""

import asyncio
import contextlib
import json
from datetime import UTC
from pathlib import Path

import httpx
import pytest
from msks.app import build_app
from msks.microvm.errors import MicrovmError, MicrovmTimeoutError
from msks.microvm.spec import VmInfo, VmSpec, VmStatus
from msks.secretstore import new_sentinel
from msks.server import api as api_mod
from msks.server.api import build_api
from msks.settings import (
    NetSettings,
    SecretStoreSettings,
    ServerSettings,
    Settings,
    VmmSettings,
)
from sqlalchemy.exc import IntegrityError, OperationalError

TOKEN = "test-token"


def auth(token: str = TOKEN) -> dict:
    return {"Authorization": f"Bearer {token}"}


class StubMicrovm:
    """Records seam calls; reports a controllable status per workspace."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []
        # The specs prepare/boot ran with — the #259 token ride and
        # any other create-time fact the tests pin.
        self.prepared: list[VmSpec] = []
        self.statuses: dict[str, VmStatus] = {}
        self.fail_prepare = False
        self.seen_specs: dict[str, VmSpec] = {}

    async def prepare(self, spec: VmSpec) -> None:
        if self.fail_prepare:
            raise MicrovmError("prepare boom")
        self.calls.append(("prepare", spec.workspace_id))
        self.prepared.append(spec)
        self.seen_specs[spec.workspace_id] = spec

    async def launch(self, spec: VmSpec) -> None:
        self.calls.append(("launch", spec.workspace_id))
        self.seen_specs[spec.workspace_id] = spec
        self.statuses[spec.workspace_id] = VmStatus.RUNNING

    async def info(self, workspace_id: str) -> VmInfo:
        status = self.statuses.get(workspace_id, VmStatus.ABSENT)
        return VmInfo(workspace_id, status)

    async def shutdown(
        self, workspace_id: str, timeout_s: float | None = None
    ) -> None:
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
        secret_store=SecretStoreSettings(root=tmp_path / "store"),
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


@contextlib.asynccontextmanager
async def catalog_daemon(
    state_dir: Path, db_path: Path, default_image: str = ""
):
    """One daemon over the given paths — the restart tests build a
    second over the same state dir in sequence (#270)."""
    settings = Settings(
        vmm=VmmSettings(state_dir=state_dir, default_image=default_image),
        net=NetSettings(enabled=False),
        server=ServerSettings(
            db_path=db_path, bootstrap_token=TOKEN, event_poll_s=10.0
        ),
    )
    app = build_app(settings)
    app.state.microvm = StubMicrovm()
    api = build_api(app)
    async with api.router.lifespan_context(api):
        transport = httpx.ASGITransport(app=api)
        async with httpx.AsyncClient(
            transport=transport, base_url="https://test"
        ) as http:
            yield http


async def test_lifespan_startup_failure_closes_engine(tmp_path: Path) -> None:
    # A startup step can raise after the engine exists (locked db,
    # full disk): the lifespan must still dispose it, or the GC emits
    # the unclosed-database warning this suite keeps at zero.
    settings = Settings(
        vmm=VmmSettings(state_dir=tmp_path / "vms"),
        net=NetSettings(enabled=False),
        server=ServerSettings(
            db_path=tmp_path / "f.db", bootstrap_token=TOKEN
        ),
    )
    app = build_app(settings)
    model = app.state.model

    async def bootstrap_boom() -> None:
        model.engine()  # the real bootstrap creates the engine first
        raise OperationalError(
            "statement", {}, Exception("database is locked")
        )

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
    response = await http.get(
        "/api/v1/tokens", headers={"Authorization": "weird"}
    )
    assert response.status_code == 401


async def test_token_admin(client) -> None:
    http, _app, _stub = client
    created = await http.post(
        "/api/v1/tokens", json={"name": "cli"}, headers=auth()
    )
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
    # The #246 split: the label becomes the row's name, and the
    # daemon mints the immutable id the artifacts key on.
    wid = created.json()["id"]
    assert wid != "ws-a"
    assert created.json()["name"] == "ws-a"
    # Created with the workspace (#14): the artifacts are prepared at
    # create, and the row records its host and artifact sizes.
    assert ("prepare", wid) in stub.calls
    assert created.json()["host"]
    assert created.json()["root_mib"] == 10240
    assert created.json()["home_mib"] == 20480
    dup = await http.post(
        "/api/v1/workspaces",
        json={"id": "ws-a", "kernel": "/k", "rootfs": "/r"},
        headers=auth(),
    )
    assert dup.status_code == 409
    started = await http.post("/api/v1/workspaces/ws-a/start", headers=auth())
    assert started.json()["status"] == "running"
    assert started.json()["id"] == wid
    assert ("launch", wid) in stub.calls
    listed = await http.get("/api/v1/workspaces", headers=auth())
    assert [row["id"] for row in listed.json()] == [wid]
    assert [row["name"] for row in listed.json()] == ["ws-a"]
    one = await http.get("/api/v1/workspaces/ws-a", headers=auth())
    assert one.json()["cpus"] == 2
    assert one.json()["mem_mib"] == 256  # explicit at create
    # The immutable id addresses the same workspace (#246).
    by_id = await http.get(f"/api/v1/workspaces/{wid}", headers=auth())
    assert by_id.json()["id"] == wid
    stopped = await http.post("/api/v1/workspaces/ws-a/stop", headers=auth())
    assert stopped.json()["status"] == "stopped"
    # Stop keeps the data (#14): no cleanup, no reset.
    assert ("cleanup", wid) not in stub.calls
    deleted = await http.delete("/api/v1/workspaces/ws-a", headers=auth())
    assert deleted.status_code == 200
    missing = await http.get("/api/v1/workspaces/ws-a", headers=auth())
    assert missing.status_code == 404
    start_missing = await http.post(
        "/api/v1/workspaces/ghost/start", headers=auth()
    )
    assert start_missing.status_code == 404


async def test_create_defaults_to_the_real_use_sizes(client) -> None:
    """A create that names neither memory nor /home size starts
    sized for real use (#276): 8192 MiB of memory and a 20480 MiB
    /home — both cheap at idle (lazily committed guest memory, a
    sparse volume)."""
    http, _app, _stub = client
    created = await http.post(
        "/api/v1/workspaces",
        json={"id": "ws-def", "kernel": "/k", "rootfs": "/r"},
        headers=auth(),
    )
    assert created.status_code == 201
    assert created.json()["mem_mib"] == 8192
    assert created.json()["home_mib"] == 20480


async def test_create_accepts_the_new_name_field(client) -> None:
    """The #246 create shape: ``name`` is the label; the daemon
    mints the immutable id. The legacy ``id`` spelling means the
    same thing, and the two may be sent together only when they
    agree."""
    http, _app, _stub = client
    created = await http.post(
        "/api/v1/workspaces",
        json={"name": "ws-new", "kernel": "/k", "rootfs": "/r"},
        headers=auth(),
    )
    assert created.status_code == 201
    assert created.json()["name"] == "ws-new"
    assert created.json()["id"]
    legacy = await http.post(
        "/api/v1/workspaces",
        json={"id": "ws-old-spelling", "kernel": "/k", "rootfs": "/r"},
        headers=auth(),
    )
    assert legacy.status_code == 201
    assert legacy.json()["name"] == "ws-old-spelling"
    agreeing = await http.post(
        "/api/v1/workspaces",
        json={
            "name": "ws-both",
            "id": "ws-both",
            "kernel": "/k",
            "rootfs": "/r",
        },
        headers=auth(),
    )
    assert agreeing.status_code == 201
    disagreeing = await http.post(
        "/api/v1/workspaces",
        json={
            "name": "ws-a",
            "id": "ws-b",
            "kernel": "/k",
            "rootfs": "/r",
        },
        headers=auth(),
    )
    assert disagreeing.status_code == 400
    assert "disagree" in disagreeing.json()["detail"]


async def test_create_without_a_name_mints_an_id_only_workspace(
    client,
) -> None:
    """A nameless create (#246): the API accepts it, the row carries
    no label, and the id is the only reference."""
    http, _app, _stub = client
    created = await http.post(
        "/api/v1/workspaces",
        json={"kernel": "/k", "rootfs": "/r"},
        headers=auth(),
    )
    assert created.status_code == 201
    wid = created.json()["id"]
    assert created.json()["name"] is None
    by_id = await http.get(f"/api/v1/workspaces/{wid}", headers=auth())
    assert by_id.status_code == 200
    # The minted id is 10 hex digits (5 random bytes) — path-safe by
    # construction and short enough to copy from a listing.
    assert len(wid) == 10
    assert all(ch in "0123456789abcdef" for ch in wid)


async def test_minted_id_rerolls_past_live_collisions(
    client, monkeypatch
) -> None:
    """The 10-hex mint re-rolls while a candidate already answers on
    the daemon (#246): a live workspace's id AND its name both block
    — ref resolution prefers the id, so a workspace named like
    another's id would be shadowed by it."""
    from msks.server import api as api_module

    http, app, _stub = client
    await app.state.model.create_workspace(
        VmSpec(workspace_id="deadbeef01", kernel="/k", rootfs="/r")
    )
    await app.state.model.create_workspace(
        VmSpec(workspace_id="other", kernel="/k", rootfs="/r"),
        name="cafe1234",
    )
    rolled = iter(("deadbeef01", "cafe1234", "0123abcd56"))
    monkeypatch.setattr(
        api_module.secrets, "token_hex", lambda _: next(rolled)
    )
    created = await http.post(
        "/api/v1/workspaces",
        json={"name": "fresh", "kernel": "/k", "rootfs": "/r"},
        headers=auth(),
    )
    assert created.status_code == 201, created.text
    assert created.json()["id"] == "0123abcd56"


async def test_recreated_name_is_a_new_instance(client) -> None:
    """The #246 acceptance: delete a workspace, create another under
    the same name, and nothing from the first instance is
    reachable or collideable — a different id, different artifact
    paths, and a key endpoint that stamps the new instance."""
    http, app, _stub = client
    state_dir = app.state.settings.vmm.state_dir
    first = await http.post(
        "/api/v1/workspaces",
        json={"id": "ws-again", "kernel": "/k", "rootfs": "/r"},
        headers=auth(),
    )
    assert first.status_code == 201
    first_id = first.json()["id"]
    first_key = (
        await http.get("/api/v1/workspaces/ws-again/ssh-key", headers=auth())
    ).json()
    await http.delete("/api/v1/workspaces/ws-again", headers=auth())
    second = await http.post(
        "/api/v1/workspaces",
        json={"id": "ws-again", "kernel": "/k", "rootfs": "/r"},
        headers=auth(),
    )
    assert second.status_code == 201
    second_id = second.json()["id"]
    assert second_id != first_id
    assert second.json()["name"] == "ws-again"
    # Every keyed surface follows the id: the artifact directories
    # are distinct, and the key endpoint serves the second instance's
    # identity and stamp.
    assert (state_dir / "vms" / first_id) != (state_dir / "vms" / second_id)
    second_key = (
        await http.get("/api/v1/workspaces/ws-again/ssh-key", headers=auth())
    ).json()
    assert second_key["id"] == second_id
    assert second_key["created_at"] != first_key["created_at"]
    assert second_key["public_key"] != first_key["public_key"]


async def test_workspace_ref_resolution_prefers_the_id(client) -> None:
    """A workspace is addressable by id and by its unique name
    (#246); the id is the canonical answer in every response."""
    http, _app, _stub = client
    created = await http.post(
        "/api/v1/workspaces",
        json={"id": "ws-ref", "kernel": "/k", "rootfs": "/r"},
        headers=auth(),
    )
    wid = created.json()["id"]
    by_name = await http.get("/api/v1/workspaces/ws-ref", headers=auth())
    by_id = await http.get(f"/api/v1/workspaces/{wid}", headers=auth())
    assert by_name.json() == by_id.json()
    assert by_name.json()["id"] == wid
    key = await http.get("/api/v1/workspaces/ws-ref/ssh-key", headers=auth())
    assert key.json()["workspace"] == wid
    assert key.json()["name"] == "ws-ref"


async def test_create_mints_identity(client) -> None:
    """Create mints the identity (#111): the spec the seam saw
    carries the public line (so the seed plants it), and the halves
    the key endpoint serves re-derive each other — one keypair."""
    from cryptography.hazmat.primitives import serialization
    from msks.identity import KEY_TYPES

    http, _app, stub = client
    created = await http.post(
        "/api/v1/workspaces",
        json={"id": "ws-id", "kernel": "/k", "rootfs": "/r"},
        headers=auth(),
    )
    assert created.status_code == 201
    assert created.json()["ssh_pubkey"].startswith(f"{KEY_TYPES['ed25519']} ")
    wid = created.json()["id"]
    assert created.json()["ssh_pubkey"].endswith(f"msksd:{wid}")
    spec_seen = stub.seen_specs[wid]
    assert spec_seen.ssh_pubkey == created.json()["ssh_pubkey"]
    key = await http.get("/api/v1/workspaces/ws-id/ssh-key", headers=auth())
    assert key.status_code == 200
    body = key.json()
    assert body["type"] == KEY_TYPES["ed25519"]
    assert body["public_key"] == created.json()["ssh_pubkey"]
    loaded = serialization.load_ssh_private_key(
        body["private_key"].encode(), password=b""
    )
    derived = (
        loaded.public_key()
        .public_bytes(
            encoding=serialization.Encoding.OpenSSH,
            format=serialization.PublicFormat.OpenSSH,
        )
        .decode()
    )
    assert derived in body["public_key"]


async def test_ssh_key_endpoint_auth_and_missing(client) -> None:
    """The key fetch is token-gated; a missing workspace and a
    pre-#111 row are different 404s."""
    http, app, _stub = client
    unauthorized = await http.get("/api/v1/workspaces/ws-x/ssh-key")
    assert unauthorized.status_code == 401
    absent = await http.get("/api/v1/workspaces/ghost/ssh-key", headers=auth())
    assert absent.status_code == 404
    assert "no such workspace" in absent.json()["detail"]
    # A row without an identity: minted-identity 404, distinct detail.
    await app.state.model.create_workspace(
        VmSpec(workspace_id="ws-old", kernel="/k", rootfs="/r")
    )
    legacy = await http.get(
        "/api/v1/workspaces/ws-old/ssh-key", headers=auth()
    )
    assert legacy.status_code == 404
    assert "no minted identity" in legacy.json()["detail"]


async def test_create_with_client_supplied_pubkey(client) -> None:
    """The no-escrow create (#121): the daemon validates the supplied
    public line, seeds and stores it annotated with its own provenance
    comment, and holds no private half — the key endpoint answers
    private_key: null for the client that minted the pair."""
    from msks.identity import mint

    http, _app, stub = client
    _private, public = mint("ed25519")
    created = await http.post(
        "/api/v1/workspaces",
        json={
            "id": "ws-cm",
            "kernel": "/k",
            "rootfs": "/r",
            "ssh_pubkey": f"{public} operator@laptop",
        },
        headers=auth(),
    )
    assert created.status_code == 201
    row = created.json()
    # The caller's comment is replaced by the daemon's provenance
    # marker, exactly as the minted mode annotates its own lines.
    assert row["ssh_pubkey"].startswith("ssh-ed25519 ")
    assert row["ssh_pubkey"].endswith("msks-client:ws-cm")
    assert "operator@laptop" not in row["ssh_pubkey"]
    # The seed carries it (the spec the seam saw) and the row holds
    # no private half.
    assert stub.seen_specs[row["id"]].ssh_pubkey == row["ssh_pubkey"]
    key = await http.get("/api/v1/workspaces/ws-cm/ssh-key", headers=auth())
    assert key.status_code == 200
    body = key.json()
    assert body["type"] == "ssh-ed25519"
    assert body["public_key"] == row["ssh_pubkey"]
    assert body["private_key"] is None


async def test_create_accepts_any_supplied_pubkey_type(client) -> None:
    """A supplied line passes at any key type (#132): another ECDSA
    curve and a hardware-key label the daemon cannot mint both
    store, seed, and serve exactly like a mintable type — sshd is
    the authority on what it authenticates."""
    import base64

    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ec

    http, _app, stub = client
    p384 = (
        ec.generate_private_key(ec.SECP384R1())
        .public_key()
        .public_bytes(
            encoding=serialization.Encoding.OpenSSH,
            format=serialization.PublicFormat.OpenSSH,
        )
        .decode()
    )
    sk = "sk-ssh-ed25519@openssh.com"
    sk_line = (
        f"{sk} "
        + base64.b64encode(
            len(sk).to_bytes(4, "big") + sk.encode() + b"rest"
        ).decode()
    )
    for index, line in enumerate((p384, sk_line)):
        wid = f"ws-any-{index}"
        created = await http.post(
            "/api/v1/workspaces",
            json={
                "id": wid,
                "kernel": "/k",
                "rootfs": "/r",
                "ssh_pubkey": f"{line} operator@laptop",
            },
            headers=auth(),
        )
        assert created.status_code == 201, created.json()
        assert created.json()["ssh_pubkey"].startswith(f"{line.split()[0]} ")
        assert created.json()["ssh_pubkey"].endswith(f"msks-client:{wid}")
        assert (
            stub.seen_specs[created.json()["id"]].ssh_pubkey
            == created.json()["ssh_pubkey"]
        )
        key = await http.get(
            f"/api/v1/workspaces/{wid}/ssh-key", headers=auth()
        )
        assert key.json()["private_key"] is None


async def test_create_rejects_a_malformed_pubkey(client) -> None:
    """A supplied line that does not validate is a 400 before any
    artifact or row exists — the id stays free for a corrected
    create."""
    http, _app, _stub = client
    created = await http.post(
        "/api/v1/workspaces",
        json={
            "id": "ws-bad",
            "kernel": "/k",
            "rootfs": "/r",
            "ssh_pubkey": "nonsense",
        },
        headers=auth(),
    )
    assert created.status_code == 400
    assert "public key line" in created.json()["detail"]
    missing = await http.get("/api/v1/workspaces/ws-bad", headers=auth())
    assert missing.status_code == 404


async def test_concurrent_same_id_creates_serialize(
    client, monkeypatch
) -> None:
    """Two concurrent creates of one id (#111): exactly one 201, the
    loser the honest 409 — and the winner's mint never interleaves
    with the loser's prepare, the pair (row, seed) comes from one
    racer. The stub's prepare records entry/exit so the test can pin
    the serialization, not just the outcome."""
    http, app, stub = client
    events: list[tuple[str, str]] = []

    async def traced_prepare(spec: VmSpec) -> None:
        events.append(("enter", spec.workspace_id))
        await asyncio.sleep(0.05)  # widen the window the lock closes
        events.append(("exit", spec.workspace_id))

    monkeypatch.setattr(stub, "prepare", traced_prepare)
    replies = await asyncio.gather(
        http.post(
            "/api/v1/workspaces",
            json={"id": "ws-race", "kernel": "/k", "rootfs": "/r"},
            headers=auth(),
        ),
        http.post(
            "/api/v1/workspaces",
            json={"id": "ws-race", "kernel": "/k", "rootfs": "/r"},
            headers=auth(),
        ),
    )
    codes = sorted(reply.status_code for reply in replies)
    assert codes == [201, 409]
    # Serialized: prepares alternate enter→exit — one racer's
    # mint→prepare→insert sequence never interleaves with the
    # other's (the corruption window the lock closes).
    kinds = [kind for kind, _ in events]
    assert kinds in (
        ["enter", "exit"],  # loser saw the row first: no prepare
        ["enter", "exit", "enter", "exit"],  # loser entered after the winner
    )
    # The winner's row carries exactly one identity, served whole.
    key = await http.get("/api/v1/workspaces/ws-race/ssh-key", headers=auth())
    assert key.status_code == 200
    assert key.json()["public_key"].endswith(f"msksd:{key.json()['id']}")


async def test_create_with_bad_key_type_setting_is_500(
    client, monkeypatch
) -> None:
    """A directly-built Settings carrying an unknown key type (env
    loading validates first) fails the create with the named error,
    not a bare traceback."""
    http, app, _stub = client
    monkeypatch.setattr(app.state.settings.vmm, "ssh_key_type", "bogus")
    created = await http.post(
        "/api/v1/workspaces",
        json={"id": "ws-bad", "kernel": "/k", "rootfs": "/r"},
        headers=auth(),
    )
    assert created.status_code == 500
    assert "unknown ssh key type" in created.json()["detail"]


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


async def test_health_reports_the_booted_image(
    client, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The daemon names the image it booted from (#160):
    the msksd.image pair off the kernel cmdline, None without one
    (a bare msksd, or a daemon predating the pair)."""
    http, _app, _stub = client
    cmdline = tmp_path / "cmdline"
    monkeypatch.setattr(api_mod, "CMDLINE", cmdline)

    cmdline.write_text(
        "console=ttyS0 root=/dev/vda ro "
        "msksd.image=/nix/store/x-msks-appliance\n"
    )
    body = (await http.get("/api/v1/health")).json()
    assert body["image"] == "/nix/store/x-msks-appliance"

    cmdline.write_text("console=ttyS0 root=/dev/vda ro\n")
    body = (await http.get("/api/v1/health")).json()
    assert body["image"] is None


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


async def test_failed_start_leaves_the_row_startable(client) -> None:
    """A 503 start does not corrupt the row (#158): the workspace
    keeps its prior status -- ``stopped`` -- so the next client
    boot retries instead of meeting a status that matches neither
    the disk nor the VM.
    """
    http, _app, stub = client
    await http.post(
        "/api/v1/workspaces",
        json={"id": "ws-f", "kernel": "/k", "rootfs": "/r"},
        headers=auth(),
    )
    await http.post("/api/v1/workspaces/ws-f/stop", headers=auth())
    row = await http.get("/api/v1/workspaces/ws-f", headers=auth())
    assert row.json()["status"] == "stopped"

    async def explode(spec):
        raise MicrovmError("boom")

    stub.launch = explode
    response = await http.post("/api/v1/workspaces/ws-f/start", headers=auth())
    assert response.status_code == 503
    row = await http.get("/api/v1/workspaces/ws-f", headers=auth())
    assert row.json()["status"] == "stopped"


async def test_delete_falls_back_to_kill(client) -> None:
    http, _app, stub = client
    created = await http.post(
        "/api/v1/workspaces",
        json={"id": "ws-k", "kernel": "/k", "rootfs": "/r"},
        headers=auth(),
    )

    async def wedged(workspace_id, timeout_s=None):
        raise MicrovmTimeoutError("wedged")

    stub.shutdown = wedged
    response = await http.delete("/api/v1/workspaces/ws-k", headers=auth())
    assert response.status_code == 200
    assert ("kill", created.json()["id"]) in stub.calls


async def test_create_race_maps_to_409(client, monkeypatch) -> None:
    http, app, stub = client

    async def lose(spec, **kwargs):
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
    assert not any(call[0] == "cleanup" for call in stub.calls)


async def test_create_race_after_prepare_answers_409(
    client, monkeypatch
) -> None:
    """A racer that won between the name pre-check and a
    strict-prepare refusal turns the 503 into the honest 409."""
    http, app, _stub = client
    checks = 0

    async def first_free_then_row(ref):
        nonlocal checks
        checks += 1
        if checks <= 2:
            # The pre-check (free), then the mint's candidate probe
            # (free) — before the racer wins the name.
            return None
        return {"id": "racer"}  # the racer has since won it

    monkeypatch.setattr(app.state.model, "get_workspace", first_free_then_row)
    _stub.fail_prepare = True
    response = await http.post(
        "/api/v1/workspaces",
        json={"id": "ws-lost", "kernel": "/k", "rootfs": "/r"},
        headers=auth(),
    )
    assert response.status_code == 409
    assert response.json()["detail"] == "workspace exists"


async def test_create_refuses_a_name_equal_to_a_live_id(client) -> None:
    """One ref namespace (#246): a name equal to a live workspace's
    id is refused at create — ref resolution prefers the id, so such
    a workspace would be silently shadowed (every command with its
    name aiming at the other workspace, rm included)."""
    http, _app, _stub = client
    created = await http.post(
        "/api/v1/workspaces",
        json={"id": "real", "kernel": "/k", "rootfs": "/r"},
        headers=auth(),
    )
    assert created.status_code == 201
    live_id = created.json()["id"]
    shadow = await http.post(
        "/api/v1/workspaces",
        json={"id": live_id, "kernel": "/k", "rootfs": "/r"},
        headers=auth(),
    )
    assert shadow.status_code == 409
    assert shadow.json()["detail"] == "workspace exists"


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
    wid = created.json()["id"]
    app.state.settings.vmm.host_name = "some-other-host"
    response = await http.post("/api/v1/workspaces/ws-x/start", headers=auth())
    assert response.status_code == 409
    assert (
        f"home volume for workspace {wid} lives on host {recorded_host}"
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
    created = await http.post(
        "/api/v1/workspaces",
        json={"id": "ws-r", "kernel": "/k", "rootfs": "/r"},
        headers=auth(),
    )
    wid = created.json()["id"]
    await http.post("/api/v1/workspaces/ws-r/start", headers=auth())
    response = await http.post("/api/v1/workspaces/ws-r/reset", headers=auth())
    assert response.status_code == 200
    assert response.json() == {"id": wid, "status": "created"}
    calls = stub.calls
    assert ("reset", wid) in calls
    # Reset stops the VM (the overlay is the running root device) and
    # removes none of the persistent data itself.
    assert calls.index(("shutdown", wid)) < calls.index(("reset", wid))
    assert ("cleanup", wid) not in calls


async def test_reset_falls_back_to_kill(client) -> None:
    http, _app, stub = client
    created = await http.post(
        "/api/v1/workspaces",
        json={"id": "ws-w", "kernel": "/k", "rootfs": "/r"},
        headers=auth(),
    )

    async def wedged(workspace_id, timeout_s=None):
        raise MicrovmTimeoutError("wedged")

    stub.shutdown = wedged
    response = await http.post("/api/v1/workspaces/ws-w/reset", headers=auth())
    assert response.status_code == 200
    assert ("kill", created.json()["id"]) in stub.calls


async def test_reset_missing_workspace_is_404(client) -> None:
    http, _app, _stub = client
    response = await http.post(
        "/api/v1/workspaces/ghost/reset", headers=auth()
    )
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
    response = await http.post(
        "/api/v1/workspaces/ws-foreign/reset", headers=auth()
    )
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
    assert created.json()["id"] in blocked.json()["detail"]
    deleted = await http.delete("/api/v1/workspaces/ws-img", headers=auth())
    assert deleted.status_code == 200
    released = await http.delete(f"/api/v1/images/{digest}", headers=auth())
    assert released.status_code == 200


async def import_named_images(http, state_dir: Path, entries) -> dict:
    """Import one archive per (name, version); ref → hash."""
    from test_imagestore import build_containerdisk

    state_dir.mkdir(parents=True, exist_ok=True)
    hashes = {}
    for name, version in entries:
        archive = state_dir / f"{name}.tar"
        build_containerdisk(archive, name=name, version=version)
        imported = await http.post(
            "/api/v1/images", json={"source": str(archive)}, headers=auth()
        )
        assert imported.status_code == 201, imported.text
        hashes[f"{name}:{version}"] = imported.json()["hash"]
    return hashes


async def test_default_designation_names_the_boot_image(client) -> None:
    """#270: POST /api/v1/images/default resolves the reference
    against the catalog, the next listing marks the designated row,
    and a bare create boots it; a miss and a malformed pin are named
    errors."""
    http, app, _stub = client
    state_dir = app.state.settings.vmm.state_dir
    hashes = await import_named_images(
        http, state_dir, (("one", "1"), ("two", "2"))
    )
    designated = await http.post(
        "/api/v1/images/default", json={"ref": "two:2"}, headers=auth()
    )
    assert designated.status_code == 200, designated.text
    assert designated.json()["ref"] == "two:2"
    assert designated.json()["hash"] == hashes["two:2"]
    listed = await http.get("/api/v1/images", headers=auth())
    flags = {
        f"{row['name']}:{row['version']}": row["default"]
        for row in listed.json()
    }
    assert flags == {"one:1": False, "two:2": True}
    created = await http.post(
        "/api/v1/workspaces", json={"id": "ws-default"}, headers=auth()
    )
    assert created.status_code == 201, created.text
    assert created.json()["image_hash"] == hashes["two:2"]
    missed = await http.post(
        "/api/v1/images/default", json={"ref": "three"}, headers=auth()
    )
    assert missed.status_code == 404
    assert "no such image" in missed.json()["detail"]
    pinned = await http.post(
        "/api/v1/images/default", json={"ref": "two@zz"}, headers=auth()
    )
    assert pinned.status_code == 400
    assert "malformed image hash" in pinned.json()["detail"]


async def test_image_listing_carries_the_import_time(client) -> None:
    """#283: GET /api/v1/images stamps each row with its ``imported``
    moment (ISO 8601, UTC), and a cache whose stamp file is gone
    still reports the directory's mtime — not an empty field."""
    from datetime import datetime, timedelta

    from msks.imagestore import IMPORTED_STAMP

    http, app, _stub = client
    state_dir = app.state.settings.vmm.state_dir
    hashes = await import_named_images(http, state_dir, (("one", "1"),))
    listed = await http.get("/api/v1/images", headers=auth())
    row = listed.json()[0]
    assert row["imported"].endswith("+00:00")  # ISO 8601, UTC
    recent = datetime.now(UTC) - timedelta(hours=1)
    assert datetime.fromisoformat(row["imported"]) > recent
    # An entry that predates stamps answers the cache's mtime.
    (state_dir / "images" / hashes["one:1"] / IMPORTED_STAMP).unlink()
    relisted = await http.get("/api/v1/images", headers=auth())
    fallback = relisted.json()[0]["imported"]
    assert fallback.endswith("+00:00")
    assert datetime.fromisoformat(fallback) > recent


@pytest.mark.parametrize(
    "form", ["name:version", "bare name", "hash", "name@hash"]
)
async def test_default_designation_accepts_every_reference_form(
    client, form: str
) -> None:
    """#270: the endpoint resolves the same forms a create's image
    field takes."""
    http, app, _stub = client
    state_dir = app.state.settings.vmm.state_dir
    hashes = await import_named_images(http, state_dir, (("two", "2"),))
    digest = hashes["two:2"]
    ref = {
        "name:version": "two:2",
        "bare name": "two",
        "hash": digest,
        "name@hash": f"two@{digest}",
    }[form]
    designated = await http.post(
        "/api/v1/images/default", json={"ref": ref}, headers=auth()
    )
    assert designated.status_code == 200, designated.text
    assert designated.json()["hash"] == digest


async def test_default_unset_reports_the_fallback(client) -> None:
    """#270: DELETE /api/v1/images/default clears the designation;
    the answer reports the fallback a bare create now takes — none on
    a multi-image catalog (the create answers the named refusal),
    the sole entry once only one image remains."""
    http, app, _stub = client
    state_dir = app.state.settings.vmm.state_dir
    hashes = await import_named_images(
        http, state_dir, (("one", "1"), ("two", "2"))
    )
    cleared = await http.delete("/api/v1/images/default", headers=auth())
    assert cleared.status_code == 200
    assert cleared.json()["fallback"] is None
    refused = await http.post(
        "/api/v1/workspaces", json={"id": "ws-nodefault"}, headers=auth()
    )
    assert refused.status_code == 400
    assert "a default image" in refused.json()["detail"]
    removed = await http.delete(
        f"/api/v1/images/{hashes['one:1']}", headers=auth()
    )
    assert removed.status_code == 200
    cleared = await http.delete("/api/v1/images/default", headers=auth())
    fallback = cleared.json()["fallback"]
    assert fallback["hash"] == hashes["two:2"]
    assert fallback["ref"] == "two:2"
    created = await http.post(
        "/api/v1/workspaces", json={"id": "ws-sole"}, headers=auth()
    )
    assert created.status_code == 201, created.text
    assert created.json()["image_hash"] == hashes["two:2"]


async def test_default_designation_survives_a_restart(
    tmp_path: Path,
) -> None:
    """#270: the designation is the pointer file — a second daemon
    over the same state dir serves it, and a warm
    MSKSD_DEFAULT_IMAGE hit on another image leaves it alone (the
    fresh-import reclaim is the one startup path that moves it)."""
    state_dir = tmp_path / "vms"
    db_path = tmp_path / "restart.db"
    async with catalog_daemon(state_dir, db_path) as http:
        hashes = await import_named_images(
            http, state_dir, (("one", "1"), ("two", "2"))
        )
        designated = await http.post(
            "/api/v1/images/default", json={"ref": "two:2"}, headers=auth()
        )
        assert designated.status_code == 200, designated.text
    async with catalog_daemon(
        state_dir, db_path, default_image=str(state_dir / "one.tar")
    ) as http:
        listed = await http.get("/api/v1/images", headers=auth())
        flags = {
            f"{row['name']}:{row['version']}": row["default"]
            for row in listed.json()
        }
        assert flags == {"one:1": False, "two:2": True}
        created = await http.post(
            "/api/v1/workspaces", json={"id": "ws-restart"}, headers=auth()
        )
        assert created.status_code == 201, created.text
        assert created.json()["image_hash"] == hashes["two:2"]


async def test_default_designation_requires_a_token(client) -> None:
    """#270: the designation endpoints are token-gated."""
    http, _app, _stub = client
    posted = await http.post("/api/v1/images/default", json={"ref": "x"})
    assert posted.status_code in (401, 403)
    deleted = await http.delete("/api/v1/images/default")
    assert deleted.status_code in (401, 403)


async def test_delete_never_started_workspace(client) -> None:
    http, _app, _stub = client
    await http.post(
        "/api/v1/workspaces",
        json={"id": "never-started", "kernel": "/k", "rootfs": "/r"},
        headers=auth(),
    )
    response = await http.delete(
        "/api/v1/workspaces/never-started", headers=auth()
    )
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
        json={
            "id": "ws-quiet",
            "kernel": "/k",
            "rootfs": "/r",
            "egress": False,
        },
        headers=auth(),
    )
    assert quiet.json()["egress"] is False


async def test_create_with_user_data_reaches_the_row(client) -> None:
    """user_data (#41) rides the create into the row and the seam's
    spec: the payload is echoed verbatim and prepared for boot."""
    http, _app, stub = client
    payload = "#!/bin/sh\necho seeded > /root/stamp\n"
    created = await http.post(
        "/api/v1/workspaces",
        json={
            "id": "ws-ud",
            "kernel": "/k",
            "rootfs": "/r",
            "user_data": payload,
        },
        headers=auth(),
    )
    assert created.status_code == 201, created.text
    assert created.json()["user_data"] == payload
    assert stub.seen_specs[created.json()["id"]].user_data == payload
    fetched = await http.get("/api/v1/workspaces/ws-ud", headers=auth())
    assert fetched.json()["user_data"] == payload


async def test_create_with_user_reaches_the_row_and_spec(client) -> None:
    """The login user (#248) rides the create into the row and the
    seam's spec — the spec is what the seed builds from, so the
    account reaches the guest's first boot — and the key endpoint
    serves it back as the client's default login."""
    http, _app, stub = client
    created = await http.post(
        "/api/v1/workspaces",
        json={"id": "ws-u", "kernel": "/k", "rootfs": "/r", "user": "alice"},
        headers=auth(),
    )
    assert created.status_code == 201, created.text
    assert created.json()["login_user"] == "alice"
    assert stub.seen_specs[created.json()["id"]].login_user == "alice"
    key = await http.get("/api/v1/workspaces/ws-u/ssh-key", headers=auth())
    assert key.status_code == 200
    assert key.json()["user"] == "alice"


async def test_create_rejects_a_login_name_off_the_charset(client) -> None:
    """A user the guest could never carry is a named 422 at create
    — not a first-boot surprise. The crafted shapes pin the guard
    the charset exists for: a quote-bearing name must never reach
    the seed script's quoted assignment."""
    http, _app, _stub = client
    for bad in (
        "Alice",
        "alice'; rm -rf /; '",
        'alice\nprintf "pwned"',
        "alice ",
        "x" * 33,
    ):
        created = await http.post(
            "/api/v1/workspaces",
            json={"id": "ws-bad", "kernel": "/k", "rootfs": "/r", "user": bad},
            headers=auth(),
        )
        assert created.status_code == 422, bad


async def test_ssh_key_serves_the_legacy_user_for_old_rows(client) -> None:
    """A row created before per-workspace users (#248) answers the
    image's own login user, so every workspace serves one name and
    the client never falls back on its own."""
    http, app, _stub = client
    await app.state.model.create_workspace(
        VmSpec(
            workspace_id="ws-old",
            kernel="/k",
            rootfs="/r",
            ssh_pubkey="ssh-ed25519 AAAA msksd:ws-old",
        ),
        ssh_privkey="-----BEGIN OPENSSH PRIVATE KEY-----\n...\n",
    )
    row = await app.state.model.get_workspace("ws-old")
    assert row["login_user"] is None
    key = await http.get("/api/v1/workspaces/ws-old/ssh-key", headers=auth())
    assert key.status_code == 200
    assert key.json()["user"] == "msks"


async def test_create_rejects_empty_user_data(client) -> None:
    http, _app, _stub = client
    empty = await http.post(
        "/api/v1/workspaces",
        json={
            "id": "ws-ud",
            "kernel": "/k",
            "rootfs": "/r",
            "user_data": "  \n",
        },
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
        (
            "ws-bare",
            {"kernel": "/k", "rootfs": "/r", "user_data": cloud_config},
        ),
    ):
        created = await http.post(
            "/api/v1/workspaces",
            json={"id": wid, **body_extra},
            headers=auth(),
        )
        assert created.status_code == 201, created.text
        assert created.json()["user_data"] == body_extra["user_data"]


async def test_workspace_mutation_is_refused_with_a_named_error(
    client,
) -> None:
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


async def test_create_defaults_report_the_settings_sizes(client) -> None:
    """GET /api/v1/create-defaults names the root/home sizes a
    create lands on when its body leaves them unset — the settings'
    values, so a configured daemon hints its own defaults."""
    http, app, _stub = client
    reply = await http.get("/api/v1/create-defaults", headers=auth())
    assert reply.status_code == 200
    assert reply.json() == {
        "root_mib": app.state.settings.vmm.root_mib,
        "home_mib": app.state.settings.vmm.home_mib,
    }


async def test_storage_report_lists_consumers(client) -> None:
    """GET /api/v1/storage names the budget, every workspace's cost
    against its ceilings, and the catalog's (#184) — each image with
    its import time beside its cost (#186)."""
    http, app, _stub = client
    state_dir = app.state.settings.vmm.state_dir
    state_dir.mkdir(parents=True, exist_ok=True)
    created = await http.post(
        "/api/v1/workspaces",
        json={"id": "ws-st", "kernel": "/k", "rootfs": "/r"},
        headers=auth(),
    )
    assert created.status_code == 201
    report = await http.get("/api/v1/storage", headers=auth())
    assert report.status_code == 200
    body = report.json()
    assert body["state"]["pressure"] in ("ok", "warn", "critical", "unknown")
    assert body["state"]["total"] >= 0
    assert (
        body["state"]["floor_mib"] == app.state.settings.vmm.storage_floor_mib
    )
    assert body["state"]["warn_pct"] == app.state.settings.vmm.storage_warn_pct
    ws = next(row for row in body["workspaces"] if row["name"] == "ws-st")
    assert ws["id"] == created.json()["id"]
    assert ws["root_mib"] == app.state.settings.vmm.root_mib
    assert ws["home_mib"] == app.state.settings.vmm.home_mib
    assert isinstance(ws["root_bytes"], int)
    assert isinstance(ws["home_bytes"], int)
    assert body["images"] == []
    # A catalog entry carries its import time (#186): an ISO-8601
    # moment an aware parser reads back.
    from datetime import datetime

    from test_imagestore import build_containerdisk

    archive = state_dir / "ws-storage.tar"
    build_containerdisk(archive)
    imported = await http.post(
        "/api/v1/images", json={"source": str(archive)}, headers=auth()
    )
    assert imported.status_code == 201, imported.text
    report = await http.get("/api/v1/storage", headers=auth())
    entry = report.json()["images"][0]
    when = datetime.fromisoformat(entry["imported"])
    assert when.tzinfo is not None


async def test_storage_report_requires_a_token(client) -> None:
    http, _app, _stub = client
    refused = await http.get("/api/v1/storage")
    assert refused.status_code in (401, 403)


async def test_create_refused_below_the_storage_floor(
    client, monkeypatch
) -> None:
    """A state disk at critical pressure answers a named 507 (#184)
    instead of accepting a create whose artifacts would wedge it."""
    http, app, _stub = client
    app.state.settings.vmm.state_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(
        app.state.settings.vmm,
        "storage_floor_mib",
        (1 << 50) // (1024 * 1024),
    )
    refused = await http.post(
        "/api/v1/workspaces",
        json={"id": "ws-full", "kernel": "/k", "rootfs": "/r"},
        headers=auth(),
    )
    assert refused.status_code == 507
    detail = refused.json()["detail"]
    assert "MSKSD_STORAGE_FLOOR_MIB" in detail
    assert "state disk" in detail
    # Nothing was created and nothing was left behind.
    listed = await http.get("/api/v1/workspaces", headers=auth())
    assert listed.json() == []


async def test_image_import_refused_below_the_storage_floor(
    client, monkeypatch
) -> None:
    """An image import copies a whole archive and unpacks its cache:
    the floor refuses it with the same named 507 as a create."""
    http, app, _stub = client
    app.state.settings.vmm.state_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(
        app.state.settings.vmm,
        "storage_floor_mib",
        (1 << 50) // (1024 * 1024),
    )
    refused = await http.post(
        "/api/v1/images", json={"source": "/x.tar"}, headers=auth()
    )
    assert refused.status_code == 507
    assert "before importing images" in refused.json()["detail"]


async def test_home_import_refused_below_the_storage_floor(
    client, monkeypatch
) -> None:
    """A home-volume import streams a whole volume at the state disk:
    the floor refuses it before the body installs anything."""
    http, app, _stub = client
    app.state.settings.vmm.state_dir.mkdir(parents=True, exist_ok=True)
    created = await http.post(
        "/api/v1/workspaces",
        json={"id": "ws-home-floor", "kernel": "/k", "rootfs": "/r"},
        headers=auth(),
    )
    assert created.status_code == 201
    monkeypatch.setattr(
        app.state.settings.vmm,
        "storage_floor_mib",
        (1 << 50) // (1024 * 1024),
    )
    refused = await http.put(
        "/api/v1/workspaces/ws-home-floor/home",
        content=b"\x53\xef",
        headers=auth(),
    )
    assert refused.status_code == 507
    assert "before importing a home volume" in refused.json()["detail"]


async def test_image_import_refused_by_incoming_size(
    client, monkeypatch, tmp_path
) -> None:
    """Free space above the floor still refuses an import whose
    incoming bytes cannot fit: the archive is counted twice (the
    retained copy plus its unpacked cache)."""
    from msks.server import api as api_module

    http, app, _stub = client
    app.state.settings.vmm.state_dir.mkdir(parents=True, exist_ok=True)
    archive = tmp_path / "big.tar"
    with archive.open("wb") as handle:
        handle.truncate(100 * 1024 * 1024)  # sparse: only st_size matters
    monkeypatch.setattr(
        api_module.storage,
        "state_usage",
        lambda path: {
            "total": 40 * 1024**3,
            "used": int(39.4 * 1024**3),
            "free": 600 * 1024**2,
        },
    )
    # 600 MiB free clears the 512 MiB floor, but the import needs
    # 512 + 2x100 = 712 MiB: the named 507 names the incoming bytes.
    refused = await http.post(
        "/api/v1/images", json={"source": str(archive)}, headers=auth()
    )
    assert refused.status_code == 507
    assert "incoming bytes" in refused.json()["detail"]


async def test_home_import_refused_by_content_length(
    client, monkeypatch
) -> None:
    """A sized upload is admitted only when free space covers the
    floor plus the body's bytes."""
    from msks.server import api as api_module

    http, app, _stub = client
    app.state.settings.vmm.state_dir.mkdir(parents=True, exist_ok=True)
    created = await http.post(
        "/api/v1/workspaces",
        json={"id": "ws-cl", "kernel": "/k", "rootfs": "/r"},
        headers=auth(),
    )
    assert created.status_code == 201
    monkeypatch.setattr(
        api_module.storage,
        "state_usage",
        lambda path: {
            "total": 40 * 1024**3,
            "used": int(39.4 * 1024**3),
            "free": 600 * 1024**2,
        },
    )
    # 600 MiB free clears the 512 MiB floor; the 100 MiB body's
    # Content-Length does not fit above it.
    refused = await http.put(
        "/api/v1/workspaces/ws-cl/home",
        content=b"x" * (100 * 1024 * 1024),
        headers=auth(),
    )
    assert refused.status_code == 507
    assert "incoming bytes" in refused.json()["detail"]


async def plant_volume(app, workspace_id: str, mib: int) -> None:
    """A genuinely formatted home volume under the state dir."""
    import subprocess as sp

    home = (
        app.state.settings.vmm.state_dir / "volumes" / f"{workspace_id}.ext4"
    )
    home.parent.mkdir(parents=True, exist_ok=True)
    with home.open("wb") as handle:
        handle.truncate(mib * 1024 * 1024)
    sp.run(["mkfs.ext4", "-q", "-F", "-L", "msks-home", str(home)], check=True)


async def plant_qcow2(app, workspace_id: str, mib: int) -> None:
    """A genuinely formatted qcow2 overlay under the state dir."""
    import subprocess as sp

    overlay = (
        app.state.settings.vmm.state_dir / "vms" / workspace_id / "root.qcow2"
    )
    overlay.parent.mkdir(parents=True, exist_ok=True)
    sp.run(
        ["qemu-img", "create", "-f", "qcow2", str(overlay), f"{mib}M"],
        check=True,
    )


async def test_resize_grows_the_home_volume(client) -> None:
    http, app, _stub = client
    created = await http.post(
        "/api/v1/workspaces",
        json={"id": "ws-rs", "kernel": "/k", "rootfs": "/r", "home_mib": 64},
        headers=auth(),
    )
    assert created.status_code == 201
    wid = created.json()["id"]
    await plant_volume(app, wid, 64)
    resized = await http.post(
        "/api/v1/workspaces/ws-rs/resize",
        json={"home_mib": 128},
        headers=auth(),
    )
    assert resized.status_code == 200
    body = resized.json()
    assert body["home_mib"] == 128
    volume = app.state.settings.vmm.state_dir / "volumes" / f"{wid}.ext4"
    assert volume.stat().st_size == 128 * 1024 * 1024


async def test_resize_shrinks_the_home_volume(client) -> None:
    http, app, _stub = client
    created = await http.post(
        "/api/v1/workspaces",
        json={"id": "ws-sh", "kernel": "/k", "rootfs": "/r", "home_mib": 128},
        headers=auth(),
    )
    assert created.status_code == 201
    wid = created.json()["id"]
    await plant_volume(app, wid, 128)
    resized = await http.post(
        "/api/v1/workspaces/ws-sh/resize",
        json={"home_mib": 64},
        headers=auth(),
    )
    assert resized.status_code == 200
    volume = app.state.settings.vmm.state_dir / "volumes" / f"{wid}.ext4"
    assert volume.stat().st_size == 64 * 1024 * 1024


async def test_resize_grows_the_overlay(client) -> None:
    import json as json_mod
    import subprocess as sp

    http, app, _stub = client
    created = await http.post(
        "/api/v1/workspaces",
        json={"id": "ws-ov", "kernel": "/k", "rootfs": "/r", "root_mib": 256},
        headers=auth(),
    )
    assert created.status_code == 201
    wid = created.json()["id"]
    await plant_qcow2(app, wid, 256)
    resized = await http.post(
        "/api/v1/workspaces/ws-ov/resize",
        json={"root_mib": 512},
        headers=auth(),
    )
    assert resized.status_code == 200
    assert resized.json()["root_mib"] == 512
    overlay = app.state.settings.vmm.state_dir / "vms" / wid / "root.qcow2"
    info = sp.run(
        ["qemu-img", "info", "--output=json", str(overlay)],
        capture_output=True,
        text=True,
        check=True,
    )
    assert json_mod.loads(info.stdout)["virtual-size"] == 512 * 1024 * 1024


async def test_resize_refuses_a_running_workspace(client) -> None:
    http, app, stub = client
    created = await http.post(
        "/api/v1/workspaces",
        json={"id": "ws-run", "kernel": "/k", "rootfs": "/r"},
        headers=auth(),
    )
    assert created.status_code == 201
    wid = created.json()["id"]
    await app.state.model.set_status(wid, "running")
    refused = await http.post(
        "/api/v1/workspaces/ws-run/resize",
        json={"home_mib": 128},
        headers=auth(),
    )
    assert refused.status_code == 409
    assert "stop it before" in refused.json()["detail"]


async def test_resize_refuses_overlay_shrink(client) -> None:
    http, app, _stub = client
    created = await http.post(
        "/api/v1/workspaces",
        json={
            "id": "ws-nos",
            "kernel": "/k",
            "rootfs": "/r",
            "root_mib": 512,
        },
        headers=auth(),
    )
    assert created.status_code == 201
    wid = created.json()["id"]
    await plant_qcow2(app, wid, 512)
    refused = await http.post(
        "/api/v1/workspaces/ws-nos/resize",
        json={"root_mib": 256},
        headers=auth(),
    )
    assert refused.status_code == 400
    assert "only grows" in refused.json()["detail"]


async def test_resize_is_idempotent_on_identical_sizes(client) -> None:
    http, app, _stub = client
    created = await http.post(
        "/api/v1/workspaces",
        json={"id": "ws-noop", "kernel": "/k", "rootfs": "/r"},
        headers=auth(),
    )
    assert created.status_code == 201
    wid = created.json()["id"]
    empty = await http.post(
        "/api/v1/workspaces/ws-noop/resize", json={}, headers=auth()
    )
    assert empty.status_code == 400
    assert "nothing to resize" in empty.json()["detail"]
    await plant_volume(app, wid, 2048)
    same = await http.post(
        "/api/v1/workspaces/ws-noop/resize",
        json={"home_mib": 2048},
        headers=auth(),
    )
    assert same.status_code == 200
    assert same.json()["changes"] == []
    assert same.json()["home_mib"] == 2048


async def test_resize_without_a_volume_updates_the_row(client) -> None:
    """The heal contract: a missing volume means the next start
    rebuilds a blank one at the new size — the row is the truth."""
    http, _app, _stub = client
    created = await http.post(
        "/api/v1/workspaces",
        json={"id": "ws-heal", "kernel": "/k", "rootfs": "/r", "home_mib": 64},
        headers=auth(),
    )
    assert created.status_code == 201
    resized = await http.post(
        "/api/v1/workspaces/ws-heal/resize",
        json={"home_mib": 128},
        headers=auth(),
    )
    assert resized.status_code == 200
    assert resized.json()["home_mib"] == 128


async def test_resize_maps_a_refused_shrink_to_409(client, tmp_path) -> None:
    """resize2fs's refusal (an fs too full) reaches the caller as a
    named 409 with the remediation spelled out."""
    http, app, _stub = client
    failing = tmp_path / "resize2fs"
    failing.write_text("#!/bin/sh\necho 'new size too small' >&2\nexit 1\n")
    failing.chmod(0o755)
    app.state.settings.vmm.resize2fs = str(failing)
    created = await http.post(
        "/api/v1/workspaces",
        json={
            "id": "ws-full",
            "kernel": "/k",
            "rootfs": "/r",
            "home_mib": 128,
        },
        headers=auth(),
    )
    assert created.status_code == 201
    wid = created.json()["id"]
    await plant_volume(app, wid, 128)
    refused = await http.post(
        "/api/v1/workspaces/ws-full/resize",
        json={"home_mib": 64},
        headers=auth(),
    )
    assert refused.status_code == 409
    assert "the shrink refused" in refused.json()["detail"]
    assert "shrink less" in refused.json()["detail"]


async def test_resize_root_without_an_overlay_updates_the_row(client) -> None:
    """The heal contract for the overlay: a missing file means the
    next start builds it at the new size — the row is the truth."""
    http, _app, _stub = client
    created = await http.post(
        "/api/v1/workspaces",
        json={"id": "ws-nov", "kernel": "/k", "rootfs": "/r", "root_mib": 256},
        headers=auth(),
    )
    assert created.status_code == 201
    resized = await http.post(
        "/api/v1/workspaces/ws-nov/resize",
        json={"root_mib": 512},
        headers=auth(),
    )
    assert resized.status_code == 200
    assert resized.json()["root_mib"] == 512


async def test_resize_names_a_vanished_row(client, monkeypatch) -> None:
    """A row that vanishes under the move-lock answers 404, not a
    None crash."""
    http, app, _stub = client
    created = await http.post(
        "/api/v1/workspaces",
        json={"id": "ws-gone", "kernel": "/k", "rootfs": "/r", "home_mib": 64},
        headers=auth(),
    )
    assert created.status_code == 201
    real_get = app.state.model.get_workspace
    calls = {"n": 0}

    async def vanishing(workspace_id):
        # The guard calls see the row; the final read answers None —
        # the delete won the race between them.
        calls["n"] += 1
        if calls["n"] > 2:
            return None
        return await real_get(workspace_id)

    monkeypatch.setattr(app.state.model, "get_workspace", vanishing)
    resized = await http.post(
        "/api/v1/workspaces/ws-gone/resize",
        json={"home_mib": 128},
        headers=auth(),
    )
    assert resized.status_code == 404


async def test_resize_refuses_below_the_overlays_virtual_size(client) -> None:
    """The file is the truth, not the row: create clamps the overlay
    to the base image's size, so a request above the row can still be
    below the overlay — answer the grow-only 400, not a qemu-img 503
    (#187 review)."""
    import subprocess as sp

    http, app, _stub = client
    created = await http.post(
        "/api/v1/workspaces",
        json={
            "id": "ws-clamp",
            "kernel": "/k",
            "rootfs": "/r",
            "root_mib": 256,
        },
        headers=auth(),
    )
    assert created.status_code == 201
    wid = created.json()["id"]
    overlay = app.state.settings.vmm.state_dir / "vms" / wid / "root.qcow2"
    overlay.parent.mkdir(parents=True, exist_ok=True)
    sp.run(
        ["qemu-img", "create", "-f", "qcow2", str(overlay), "512M"],
        check=True,
    )
    refused = await http.post(
        "/api/v1/workspaces/ws-clamp/resize",
        json={"root_mib": 384},
        headers=auth(),
    )
    assert refused.status_code == 400
    assert "512 MiB" in refused.json()["detail"]
    assert "only grows" in refused.json()["detail"]


async def test_resize_combined_failure_leaves_nothing_moved(client) -> None:
    """The overlay grow runs first: its failure strands no half-moved
    home side, and the row keeps its old sizes."""
    http, app, _stub = client
    failing_qemu = app.state.settings.vmm.state_dir / "qemu-img-fail"
    failing_qemu.parent.mkdir(parents=True, exist_ok=True)
    failing_qemu.write_text("#!/bin/sh\nexit 1\n")
    failing_qemu.chmod(0o755)
    app.state.settings.vmm.qemu_img = str(failing_qemu)
    created = await http.post(
        "/api/v1/workspaces",
        json={
            "id": "ws-combined",
            "kernel": "/k",
            "rootfs": "/r",
            "root_mib": 256,
            "home_mib": 64,
        },
        headers=auth(),
    )
    assert created.status_code == 201
    wid = created.json()["id"]
    await plant_volume(app, wid, 64)
    overlay = app.state.settings.vmm.state_dir / "vms" / wid / "root.qcow2"
    overlay.parent.mkdir(parents=True, exist_ok=True)
    overlay.write_bytes(b"")
    failed = await http.post(
        "/api/v1/workspaces/ws-combined/resize",
        json={"root_mib": 512, "home_mib": 128},
        headers=auth(),
    )
    assert failed.status_code == 503
    row = await app.state.model.get_workspace(wid)
    assert row["root_mib"] == 256
    assert row["home_mib"] == 64
    volume = app.state.settings.vmm.state_dir / "volumes" / f"{wid}.ext4"
    assert volume.stat().st_size == 64 * 1024 * 1024


async def test_resize_refuses_a_foreign_host(client, monkeypatch) -> None:
    """The placement refusal every artifact route shares: artifacts
    on another host never move from here."""
    http, app, _stub = client
    created = await http.post(
        "/api/v1/workspaces",
        json={
            "id": "ws-foreign",
            "kernel": "/k",
            "rootfs": "/r",
            "home_mib": 64,
        },
        headers=auth(),
    )
    assert created.status_code == 201
    monkeypatch.setattr(app.state.settings.vmm, "host_name", "hv-elsewhere")
    refused = await http.post(
        "/api/v1/workspaces/ws-foreign/resize",
        json={"home_mib": 128},
        headers=auth(),
    )
    assert refused.status_code == 409
    assert "lives on host" in refused.json()["detail"]


async def test_resize_refuses_a_corrupt_overlay(client) -> None:
    """A zero-length or garbage overlay probes as raw/0 bytes — a
    grow would silently truncate garbage into a false 200. The route
    names the corrupt file as the 503 it is (#187 review round 2)."""
    http, app, _stub = client
    created = await http.post(
        "/api/v1/workspaces",
        json={
            "id": "ws-corrupt",
            "kernel": "/k",
            "rootfs": "/r",
            "root_mib": 256,
        },
        headers=auth(),
    )
    assert created.status_code == 201
    wid = created.json()["id"]
    overlay = app.state.settings.vmm.state_dir / "vms" / wid / "root.qcow2"
    overlay.parent.mkdir(parents=True, exist_ok=True)
    overlay.write_bytes(b"")
    refused = await http.post(
        "/api/v1/workspaces/ws-corrupt/resize",
        json={"root_mib": 512},
        headers=auth(),
    )
    assert refused.status_code == 503
    assert "not a readable qcow2" in refused.json()["detail"]


async def test_resize_maps_an_e2fsck_operational_failure_to_503(
    client, tmp_path
) -> None:
    """e2fsck exit 8 (operational error) is a daemon fault even on a
    row-classified shrink: it keeps its 503, never the client-fixable
    409 (#187 review round 2)."""
    http, app, _stub = client
    e2fsck = tmp_path / "e2fsck8"
    e2fsck.write_text("#!/bin/sh\necho 'I/O error' >&2\nexit 8\n")
    e2fsck.chmod(0o755)
    app.state.settings.vmm.e2fsck = str(e2fsck)
    created = await http.post(
        "/api/v1/workspaces",
        json={
            "id": "ws-op",
            "kernel": "/k",
            "rootfs": "/r",
            "home_mib": 128,
        },
        headers=auth(),
    )
    assert created.status_code == 201
    wid = created.json()["id"]
    await plant_volume(app, wid, 128)
    failed = await http.post(
        "/api/v1/workspaces/ws-op/resize",
        json={"home_mib": 64},
        headers=auth(),
    )
    assert failed.status_code == 503
    assert "e2fsck" in failed.json()["detail"]
    assert "free data" not in failed.json()["detail"]


async def test_resize_maps_a_grow_failure_to_503(client, tmp_path) -> None:
    """A grow-direction tool failure stays the daemon's 503 (the
    client-fixable 409 belongs to executed shrinks alone)."""
    http, app, _stub = client
    failing = tmp_path / "resize2fs"
    failing.write_text("#!/bin/sh\necho 'no room' >&2\nexit 1\n")
    failing.chmod(0o755)
    app.state.settings.vmm.resize2fs = str(failing)
    created = await http.post(
        "/api/v1/workspaces",
        json={
            "id": "ws-grow",
            "kernel": "/k",
            "rootfs": "/r",
            "home_mib": 64,
        },
        headers=auth(),
    )
    assert created.status_code == 201
    wid = created.json()["id"]
    await plant_volume(app, wid, 64)
    failed = await http.post(
        "/api/v1/workspaces/ws-grow/resize",
        json={"home_mib": 128},
        headers=auth(),
    )
    assert failed.status_code == 503
    assert "free data" not in failed.json()["detail"]


async def test_resize_follows_the_file_not_the_row(client) -> None:
    """A home import can swap the volume in above the row's size: the
    move classifies by the file (a shrink here), and a matching
    request reconciles the row to the file's truth (#187 review
    round 2)."""
    http, app, _stub = client
    created = await http.post(
        "/api/v1/workspaces",
        json={
            "id": "ws-big",
            "kernel": "/k",
            "rootfs": "/r",
            "home_mib": 64,
        },
        headers=auth(),
    )
    assert created.status_code == 201
    wid = created.json()["id"]
    # The imported volume: 128 MiB under a 64 MiB row.
    await plant_volume(app, wid, 128)
    reconciled = await http.post(
        "/api/v1/workspaces/ws-big/resize",
        json={"home_mib": 96},
        headers=auth(),
    )
    assert reconciled.status_code == 200
    body = reconciled.json()
    assert body["home_mib"] == 96
    assert body["changes"] == ["home shrank to 96 MiB"]
    volume = app.state.settings.vmm.state_dir / "volumes" / f"{wid}.ext4"
    assert volume.stat().st_size == 96 * 1024 * 1024
    # A second identical resize is a no-op: the row now agrees with
    # the file, and the heal path records nothing.
    again = await http.post(
        "/api/v1/workspaces/ws-big/resize",
        json={"home_mib": 96},
        headers=auth(),
    )
    assert again.status_code == 200
    assert again.json()["changes"] == []


async def test_resize_combined_success_reports_each_side(client) -> None:
    """A combined resize reports what moved, root first."""
    http, app, _stub = client
    created = await http.post(
        "/api/v1/workspaces",
        json={
            "id": "ws-both",
            "kernel": "/k",
            "rootfs": "/r",
            "root_mib": 256,
            "home_mib": 128,
        },
        headers=auth(),
    )
    assert created.status_code == 201
    wid = created.json()["id"]
    await plant_volume(app, wid, 128)
    await plant_qcow2(app, wid, 256)
    resized = await http.post(
        "/api/v1/workspaces/ws-both/resize",
        json={"root_mib": 512, "home_mib": 64},
        headers=auth(),
    )
    assert resized.status_code == 200
    body = resized.json()
    assert body["root_mib"] == 512
    assert body["home_mib"] == 64
    assert body["changes"] == ["root grew to 512 MiB", "home shrank to 64 MiB"]


async def test_resize_row_catches_up_to_the_overlay(client) -> None:
    """A request equal to the overlay's virtual size moves nothing —
    only the row catches up (the file was already there, above it)."""
    http, app, _stub = client
    created = await http.post(
        "/api/v1/workspaces",
        json={
            "id": "ws-catch",
            "kernel": "/k",
            "rootfs": "/r",
            "root_mib": 256,
        },
        headers=auth(),
    )
    assert created.status_code == 201
    wid = created.json()["id"]
    await plant_qcow2(app, wid, 512)
    caught = await http.post(
        "/api/v1/workspaces/ws-catch/resize",
        json={"root_mib": 512},
        headers=auth(),
    )
    assert caught.status_code == 200
    assert caught.json()["root_mib"] == 512
    assert caught.json()["changes"] == []


async def test_resize_identical_on_both_sides_is_a_noop(client) -> None:
    """Naming both sides at their current values moves nothing and
    records nothing — the honest idempotent 200."""
    http, _app, _stub = client
    created = await http.post(
        "/api/v1/workspaces",
        json={"id": "ws-quiet", "kernel": "/k", "rootfs": "/r"},
        headers=auth(),
    )
    assert created.status_code == 201
    row = created.json()
    again = await http.post(
        "/api/v1/workspaces/ws-quiet/resize",
        json={"root_mib": row["root_mib"], "home_mib": row["home_mib"]},
        headers=auth(),
    )
    assert again.status_code == 200
    assert again.json()["changes"] == []


async def test_resize_updates_the_topology(client) -> None:
    """cpus and mem_mib move through the resize route (#277): the
    row records them — the create-time bounds, the one-sided
    keeps-column writes, and the named changes."""
    http, app, _stub = client
    created = await http.post(
        "/api/v1/workspaces",
        json={"id": "ws-top", "kernel": "/k", "rootfs": "/r"},
        headers=auth(),
    )
    assert created.status_code == 201
    wid = created.json()["id"]
    resized = await http.post(
        "/api/v1/workspaces/ws-top/resize",
        json={"cpus": 4, "mem_mib": 4096},
        headers=auth(),
    )
    assert resized.status_code == 200
    body = resized.json()
    assert body["cpus"] == 4
    assert body["mem_mib"] == 4096
    assert body["changes"] == ["cpus set to 4", "mem set to 4096 MiB"]
    row = await app.state.model.get_workspace(wid)
    assert row["cpus"] == 4
    assert row["mem_mib"] == 4096
    # One side alone keeps the other.
    one_side = await http.post(
        "/api/v1/workspaces/ws-top/resize",
        json={"cpus": 8},
        headers=auth(),
    )
    assert one_side.status_code == 200
    assert one_side.json()["mem_mib"] == 4096


async def test_resize_topology_is_idempotent(client) -> None:
    """Values equal to the row's record nothing — the honest
    idempotent 200, the disk sides' twin."""
    http, _app, _stub = client
    created = await http.post(
        "/api/v1/workspaces",
        json={"id": "ws-same", "kernel": "/k", "rootfs": "/r"},
        headers=auth(),
    )
    assert created.status_code == 201
    row = created.json()
    again = await http.post(
        "/api/v1/workspaces/ws-same/resize",
        json={"cpus": row["cpus"], "mem_mib": row["mem_mib"]},
        headers=auth(),
    )
    assert again.status_code == 200
    assert again.json()["changes"] == []


async def test_resize_topology_bounds_mirror_create(client) -> None:
    """The resize body carries create's floors and ceilings: a
    value outside them answers the body-validation 422."""
    http, _app, _stub = client
    created = await http.post(
        "/api/v1/workspaces",
        json={"id": "ws-bounds", "kernel": "/k", "rootfs": "/r"},
        headers=auth(),
    )
    assert created.status_code == 201
    for body in (
        {"cpus": 0},
        {"cpus": 65},
        {"mem_mib": 63},
        {"mem_mib": 32769},
    ):
        refused = await http.post(
            "/api/v1/workspaces/ws-bounds/resize",
            json=body,
            headers=auth(),
        )
        assert refused.status_code == 422, body


async def test_resize_mixes_disks_and_topology(client) -> None:
    """One invocation moves the disks and the topology together
    (#277): each side lands, and the changes report each."""
    http, app, _stub = client
    created = await http.post(
        "/api/v1/workspaces",
        json={
            "id": "ws-mix",
            "kernel": "/k",
            "rootfs": "/r",
            "home_mib": 128,
        },
        headers=auth(),
    )
    assert created.status_code == 201
    wid = created.json()["id"]
    await plant_volume(app, wid, 128)
    resized = await http.post(
        "/api/v1/workspaces/ws-mix/resize",
        json={"home_mib": 256, "cpus": 4},
        headers=auth(),
    )
    assert resized.status_code == 200
    body = resized.json()
    assert body["home_mib"] == 256
    assert body["cpus"] == 4
    assert body["changes"] == ["home grew to 256 MiB", "cpus set to 4"]


async def test_a_resized_topology_boots(client) -> None:
    """The next start builds its VmSpec from the row: a resized
    topology boots at the new cpus and memory."""
    http, _app, stub = client
    created = await http.post(
        "/api/v1/workspaces",
        json={"id": "ws-boot", "kernel": "/k", "rootfs": "/r"},
        headers=auth(),
    )
    assert created.status_code == 201
    wid = created.json()["id"]
    resized = await http.post(
        "/api/v1/workspaces/ws-boot/resize",
        json={"cpus": 6, "mem_mib": 8192},
        headers=auth(),
    )
    assert resized.status_code == 200
    started = await http.post(
        "/api/v1/workspaces/ws-boot/start", headers=auth()
    )
    assert started.status_code == 200
    spec = stub.seen_specs[wid]
    assert spec.cpus == 6
    assert spec.mem_mib == 8192


async def test_resize_names_a_vanished_volume(client, monkeypatch) -> None:
    """A volume that vanishes between the check and the move answers
    a named 503, not a bare 500."""
    from msks.server import api as api_module

    http, app, _stub = client
    created = await http.post(
        "/api/v1/workspaces",
        json={
            "id": "ws-van",
            "kernel": "/k",
            "rootfs": "/r",
            "home_mib": 64,
        },
        headers=auth(),
    )
    assert created.status_code == 201
    wid = created.json()["id"]
    await plant_volume(app, wid, 64)

    async def vanish(target, home_mib, settings):
        raise FileNotFoundError(str(target))

    monkeypatch.setattr(api_module.persist, "volume_move", vanish)
    failed = await http.post(
        "/api/v1/workspaces/ws-van/resize",
        json={"home_mib": 128},
        headers=auth(),
    )
    assert failed.status_code == 503
    assert "unreachable mid-resize" in failed.json()["detail"]


# --- secrets (#198) ------------------------------------------------------


async def seed_workspace(app, workspace_id: str = "ws-sec") -> None:
    """A workspace row for minting against."""
    await app.state.model.create_workspace(
        VmSpec(workspace_id=workspace_id, kernel=Path("/k"), rootfs=Path("/r"))
    )


def mint_body(**overrides) -> dict:
    body = {
        "workspace_id": "ws-sec",
        "name": "github_api",
        "dests": ["api.github.com"],
        "secret": "ghp-real-token",
    }
    body.update(overrides)
    return body


async def test_mint_stores_and_answers_the_sentinel_once(client) -> None:
    http, app, _stub = client
    await seed_workspace(app)
    response = await http.post(
        "/api/v1/secrets", json=mint_body(), headers=auth()
    )
    assert response.status_code == 201
    row = response.json()
    assert row["sentinel"].startswith("mskssec1_")
    assert "secret" not in row
    assert row["dests"] == ["api.github.com"]
    # The real value landed in the store, under the derived ref.
    stored = (
        app.state.settings.secret_store.root
        / "msks"
        / "default"
        / "MSKSWS_WS_SEC_GITHUB_API"
    )
    assert stored.read_text() == "ghp-real-token"
    assert (
        "MSKSWS_WS_SEC_GITHUB_API"
        in (
            app.state.settings.secret_store.root / "secretspec.toml"
        ).read_text()
    )
    # Mint is the only view that ever carries the sentinel.
    listing = await http.get("/api/v1/secrets", headers=auth())
    assert "sentinel" not in listing.json()[0]
    audit = await http.get("/api/v1/secrets/audit", headers=auth())
    assert audit.json()[0]["kind"] == "mint"
    assert audit.json()[0]["dests"] == ["api.github.com"]


async def test_mint_validates_name_dests_and_workspace(client) -> None:
    http, app, _stub = client
    await seed_workspace(app)
    bad_name = await http.post(
        "/api/v1/secrets", json=mint_body(name="1abc"), headers=auth()
    )
    assert bad_name.status_code == 422
    bad_dest = await http.post(
        "/api/v1/secrets",
        json=mint_body(dests=["ht!tp://x"]),
        headers=auth(),
    )
    assert bad_dest.status_code == 422
    assert "dest" in bad_dest.json()["detail"]
    no_dests = await http.post(
        "/api/v1/secrets", json=mint_body(dests=[]), headers=auth()
    )
    assert no_dests.status_code == 422
    unknown = await http.post(
        "/api/v1/secrets",
        json=mint_body(workspace_id="nope"),
        headers=auth(),
    )
    assert unknown.status_code == 404


async def test_mint_rejects_a_duplicate_label(client) -> None:
    http, app, _stub = client
    await seed_workspace(app)
    first = await http.post(
        "/api/v1/secrets", json=mint_body(), headers=auth()
    )
    assert first.status_code == 201
    again = await http.post(
        "/api/v1/secrets", json=mint_body(), headers=auth()
    )
    assert again.status_code == 409


async def test_mint_reports_an_unreachable_store(client) -> None:
    http, app, _stub = client
    await seed_workspace(app)
    app.state.settings.secret_store.cli = "/nonexistent/secretspec"
    failed = await http.post(
        "/api/v1/secrets", json=mint_body(), headers=auth()
    )
    assert failed.status_code == 503
    assert "secret store" in failed.json()["detail"]


async def test_renew_extends_in_place(client) -> None:
    http, app, _stub = client
    await seed_workspace(app)
    row = (
        await http.post("/api/v1/secrets", json=mint_body(), headers=auth())
    ).json()
    renewed = await http.post(
        f"/api/v1/secrets/{row['id']}/renew",
        json={"ttl_s": 3600},
        headers=auth(),
    )
    assert renewed.status_code == 200
    assert renewed.json()["expires_at"] is not None
    missing = await http.post(
        "/api/v1/secrets/999/renew", json={"ttl_s": 60}, headers=auth()
    )
    assert missing.status_code == 404


async def test_revoke_cleans_row_store_and_manifest(client) -> None:
    http, app, _stub = client
    await seed_workspace(app)
    row = (
        await http.post("/api/v1/secrets", json=mint_body(), headers=auth())
    ).json()
    revoked = await http.delete(f"/api/v1/secrets/{row['id']}", headers=auth())
    assert revoked.status_code == 200
    assert revoked.json()["store_cleaned"] is True
    listing = await http.get("/api/v1/secrets", headers=auth())
    assert listing.json() == []
    root = app.state.settings.secret_store.root
    manifest = (root / "secretspec.toml").read_text()
    assert "MSKSWS_WS_SEC_GITHUB_API" not in manifest
    assert not (
        root / "msks" / "default" / "MSKSWS_WS_SEC_GITHUB_API"
    ).exists()
    audit = await http.get("/api/v1/secrets/audit", headers=auth())
    kinds = [event["kind"] for event in audit.json()]
    assert kinds == ["revoke", "mint"]


async def test_revoke_survives_a_failing_store_cleanup(client) -> None:
    """The row dies first: a leftover value is inert, and the
    response says it was left behind."""
    http, app, _stub = client
    await seed_workspace(app)
    row = (
        await http.post("/api/v1/secrets", json=mint_body(), headers=auth())
    ).json()
    app.state.settings.secret_store.cli = "/nonexistent/secretspec"
    revoked = await http.delete(f"/api/v1/secrets/{row['id']}", headers=auth())
    assert revoked.status_code == 200
    assert revoked.json()["store_cleaned"] is False
    listing = await http.get("/api/v1/secrets", headers=auth())
    assert listing.json() == []


async def test_store_check_endpoint(client) -> None:
    http, _app, _stub = client
    ok = await http.post("/api/v1/secrets/check", headers=auth())
    assert ok.status_code == 200
    assert ok.json() == {"provider": "file", "ok": True}


async def test_store_check_names_a_broken_store(client) -> None:
    http, app, _stub = client
    app.state.settings.secret_store.cli = "/nonexistent/secretspec"
    failed = await http.post("/api/v1/secrets/check", headers=auth())
    assert failed.status_code == 503


async def test_secret_routes_require_a_token(client) -> None:
    http, _app, _stub = client
    assert (await http.get("/api/v1/secrets")).status_code == 401
    assert (
        await http.post("/api/v1/secrets", json=mint_body())
    ).status_code == 401


async def test_revoke_unknown_placeholder_is_a_404(client) -> None:
    http, _app, _stub = client
    response = await http.delete("/api/v1/secrets/999", headers=auth())
    assert response.status_code == 404


async def test_mint_survives_the_insert_race(client, monkeypatch) -> None:
    """Two same-label mints racing past both pre-checks: the
    loser's row insert answers 409 on the unique index, and the
    winner's certified value owns the store entry (the winner
    re-writes after its insert, so a last-write by the loser
    cannot stand)."""
    http, app, _stub = client
    await seed_workspace(app)
    first = await http.post(
        "/api/v1/secrets", json=mint_body(), headers=auth()
    )
    assert first.status_code == 201
    real = app.state.model.placeholder_for

    async def blind(workspace_id, name):
        return None if name == "github_api" else await real(workspace_id, name)

    async def blind_by_ref(ref):
        # The race slips past both pre-checks; the unique index on
        # backend_ref is the backstop the loser then hits.
        return None

    monkeypatch.setattr(app.state.model, "placeholder_for", blind)
    monkeypatch.setattr(app.state.model, "placeholder_by_ref", blind_by_ref)
    # The loser carries DIFFERENT bytes: nothing it sends may ever
    # reach the store behind the winner's certified row.
    raced = await http.post(
        "/api/v1/secrets",
        json=mint_body(secret="LOSER-UNCERTIFIED"),
        headers=auth(),
    )
    assert raced.status_code == 409
    assert "collision" in raced.json()["detail"]
    root = app.state.settings.secret_store.root
    stored = root / "msks" / "default" / "MSKSWS_WS_SEC_GITHUB_API"
    assert stored.read_text() == "ghp-real-token"
    assert (
        await app.state.secrets.read("MSKSWS_WS_SEC_GITHUB_API")
        == "ghp-real-token"
    )
    listing = await http.get("/api/v1/secrets", headers=auth())
    assert len(listing.json()) == 1


async def test_mint_dedupes_repeated_dests(client) -> None:
    http, app, _stub = client
    await seed_workspace(app)
    response = await http.post(
        "/api/v1/secrets",
        json=mint_body(dests=["API.GitHub.com", "api.github.com"]),
        headers=auth(),
    )
    assert response.status_code == 201
    assert response.json()["dests"] == ["api.github.com"]


async def test_mint_answers_409_on_a_ref_collision(client) -> None:
    """Labels that sanitize to one ref (foo vs FOO on one workspace)
    collide on the backend ref: the second mint answers 409 BEFORE
    anything touches the manifest or the shared store entry."""
    http, app, _stub = client
    await seed_workspace(app)
    first = await http.post(
        "/api/v1/secrets",
        json=mint_body(name="foo", secret="first-secret"),
        headers=auth(),
    )
    assert first.status_code == 201
    second = await http.post(
        "/api/v1/secrets",
        json=mint_body(name="FOO", secret="second-secret"),
        headers=auth(),
    )
    assert second.status_code == 409
    assert "collides" in second.json()["detail"]
    # The winner's value and the manifest are intact.
    root = app.state.settings.secret_store.root
    stored = root / "msks" / "default" / "MSKSWS_WS_SEC_FOO"
    assert stored.read_text() == "first-secret"
    manifest = (root / "secretspec.toml").read_text()
    assert manifest.count("MSKSWS_WS_SEC_FOO") == 1


async def test_mint_refuses_a_whitespace_secret(client) -> None:
    http, app, _stub = client
    await seed_workspace(app)
    response = await http.post(
        "/api/v1/secrets", json=mint_body(secret="  \n"), headers=auth()
    )
    assert response.status_code == 422
    assert "empty" in response.json()["detail"]


async def test_renew_loses_gracefully_to_the_expiry_sweep(
    client, monkeypatch
) -> None:
    """A renew racing the sweep answers 404, not a 500 from a None
    row."""
    http, app, _stub = client
    await seed_workspace(app)
    row = (
        await http.post("/api/v1/secrets", json=mint_body(), headers=auth())
    ).json()
    real = app.state.model.get_placeholder
    fetched: list[int] = []

    async def vanishing(placeholder_id):
        fetched.append(placeholder_id)
        if len(fetched) == 2:  # the re-fetch after the renew write
            return None  # the sweep retired the row in between
        return await real(placeholder_id)

    monkeypatch.setattr(app.state.model, "get_placeholder", vanishing)
    response = await http.post(
        f"/api/v1/secrets/{row['id']}/renew",
        json={"ttl_s": 60},
        headers=auth(),
    )
    assert response.status_code == 404


async def test_migrated_schema_keeps_the_unique_indexes(client) -> None:
    """The daemon's real DB comes up through migrate(), not
    create_all(): the uniqueness the collision story relies on must
    exist in the MIGRATED schema, not only the ORM's."""
    import sqlite3

    http, app, _stub = client
    db = str(app.state.settings.server.db_path)
    conn = sqlite3.connect(db)
    try:
        sql = {
            row[0]
            for row in conn.execute(
                "SELECT sql FROM sqlite_master WHERE type='index'"
            )
            if row[0] is not None  # auto-indexes carry no SQL text
        }
    finally:
        conn.close()  # `with connect(...)` commits, never closes
    assert any("ix_placeholders_sentinel" in s and "UNIQUE" in s for s in sql)
    assert any(
        "ix_placeholders_backend_ref" in s and "UNIQUE" in s for s in sql
    )


async def test_concurrent_mints_leave_one_intact_manifest(client) -> None:
    """Two different-label mints in flight at once: the store lock
    serializes their refs-snapshot → manifest-write pairs, so the
    manifest ends declaring both — never a stale snapshot's one."""
    http, app, _stub = client
    await seed_workspace(app)
    first = http.post(
        "/api/v1/secrets",
        json=mint_body(name="alpha", dests=["a.example.com"]),
        headers=auth(),
    )
    second = http.post(
        "/api/v1/secrets",
        json=mint_body(name="beta", dests=["b.example.com"]),
        headers=auth(),
    )
    done = await asyncio.gather(first, second)
    assert {response.status_code for response in done} == {201}
    manifest = (
        app.state.settings.secret_store.root / "secretspec.toml"
    ).read_text()
    assert "MSKSWS_WS_SEC_ALPHA" in manifest
    assert "MSKSWS_WS_SEC_BETA" in manifest


class RefreshRecorder:
    """The interceptor surface the secret routes touch, recorded."""

    def __init__(self) -> None:
        self.refreshes: list[str] = []

    async def refresh(self, workspace_id: str) -> None:
        self.refreshes.append(workspace_id)

    async def on_detach(self, workspace_id: str) -> None:  # pragma: no cover
        raise AssertionError("no attachment exists in the api fixture")

    async def stop(self) -> None:  # pragma: no cover
        raise AssertionError("lifespan teardown replaces the recorder")


async def test_mint_publishes_and_refreshes_the_interceptor(client) -> None:
    """The lifecycle events reach the stream (#199): mint announces,
    and the armed state re-evaluates — the workspace is not running
    here, so the refresh is the no-op half."""
    http, app, _stub = client
    await seed_workspace(app)
    recorder = RefreshRecorder()
    real = app.state.interceptor
    app.state.interceptor = recorder
    queue = app.state.hub.subscribe()
    try:
        response = await http.post(
            "/api/v1/secrets", json=mint_body(), headers=auth()
        )
        assert response.status_code == 201
        await http.delete(
            f"/api/v1/secrets/{response.json()['id']}", headers=auth()
        )
    finally:
        app.state.interceptor = real
    events = []
    while not queue.empty():
        events.append(await queue.get())
    kinds = [json.loads(event)["event"] for event in events]
    assert kinds == ["secret.mint", "secret.revoke"]
    assert recorder.refreshes == ["ws-sec", "ws-sec"]


async def test_mint_and_revoke_events_carry_the_placeholder_identity(
    client,
) -> None:
    """The lifecycle events name the placeholder's row id and a
    timestamp (#201): the stream's readers tie an event to its
    placeholder even after the row retires."""
    http, app, _stub = client
    await seed_workspace(app)
    queue = app.state.hub.subscribe()
    minted = (
        await http.post("/api/v1/secrets", json=mint_body(), headers=auth())
    ).json()
    await http.delete(f"/api/v1/secrets/{minted['id']}", headers=auth())
    drained = []
    while not queue.empty():
        drained.append(json.loads(queue.get_nowait()))
    mint, revoke = (event["data"] for event in drained)
    assert mint["placeholder_id"] == minted["id"]
    assert mint["dests"] == ["api.github.com"]
    assert mint["ts"] > 0.0
    assert mint["name"] == revoke["name"] == "github_api"
    assert revoke["placeholder_id"] == minted["id"]
    assert revoke["ts"] > 0.0
    # The audit row's id rides the live frame and the decider
    # replay alike (#305): the same fact lands once on the events
    # screen however it arrives.
    audit = await app.state.model.list_audit()
    assert [row["id"] for row in audit] == [
        revoke["audit_id"],
        mint["audit_id"],
    ]


async def test_renew_refreshes_the_interceptor(client) -> None:
    """A renew can revive a workspace's last live placeholder: the
    armed state re-evaluates (#199)."""
    http, app, _stub = client
    await seed_workspace(app)
    row = (
        await http.post("/api/v1/secrets", json=mint_body(), headers=auth())
    ).json()
    recorder = RefreshRecorder()
    real = app.state.interceptor
    app.state.interceptor = recorder
    try:
        renewed = await http.post(
            f"/api/v1/secrets/{row['id']}/renew",
            json={"ttl_s": 600},
            headers=auth(),
        )
    finally:
        app.state.interceptor = real
    assert renewed.status_code == 200
    assert recorder.refreshes == ["ws-sec"]


async def test_a_mint_that_cannot_arm_rolls_back_whole(client) -> None:
    """Arming failure answers 503 with nothing left behind: the row,
    the store value, and the manifest entry all go (#260 review) —
    a live placeholder that cannot redirect would leak its raw
    sentinel toward the wire."""
    http, app, _stub = client
    await seed_workspace(app)

    class RefusingInterceptor:
        async def refresh(self, workspace_id: str) -> None:
            raise RuntimeError("bind failed")

        async def on_detach(self, ws):  # pragma: no cover
            raise AssertionError

        async def stop(self):  # pragma: no cover
            raise AssertionError

    real = app.state.interceptor
    app.state.interceptor = RefusingInterceptor()
    queue = app.state.hub.subscribe()
    try:
        response = await http.post(
            "/api/v1/secrets", json=mint_body(), headers=auth()
        )
    finally:
        app.state.interceptor = real
    assert response.status_code == 503
    assert "could not arm" in response.json()["detail"]
    listing = await http.get("/api/v1/secrets", headers=auth())
    assert listing.json() == []
    # A rolled-back mint never existed: no audit row, no event.
    audit = await http.get("/api/v1/secrets/audit", headers=auth())
    assert audit.json() == []
    events = []
    while not queue.empty():
        events.append(json.loads(await queue.get()))
    assert events == []
    root = app.state.settings.secret_store.root
    assert (
        "MSKSWS_WS_SEC_GITHUB_API"
        not in (root / "secretspec.toml").read_text()
    )
    assert not (
        root / "msks" / "default" / "MSKSWS_WS_SEC_GITHUB_API"
    ).exists()


async def test_revoke_survives_a_failing_refresh(client) -> None:
    """The revoke stands when the armed-state re-evaluation fails:
    the row is already deleted, so nothing swaps either way — the
    stand-down retries on the next placeholder event. (A renew in
    the same position rolls back instead; its own test pins that.)"""
    http, app, _stub = client
    await seed_workspace(app)
    row = (
        await http.post("/api/v1/secrets", json=mint_body(), headers=auth())
    ).json()

    class RefusingInterceptor:
        async def refresh(self, workspace_id: str) -> None:
            raise RuntimeError("nft down")

        async def on_detach(self, ws):  # pragma: no cover
            raise AssertionError

        async def stop(self):  # pragma: no cover
            raise AssertionError

    real = app.state.interceptor
    app.state.interceptor = RefusingInterceptor()
    try:
        revoked = await http.delete(
            f"/api/v1/secrets/{row['id']}", headers=auth()
        )
    finally:
        app.state.interceptor = real
    assert revoked.status_code == 200
    listing = await http.get("/api/v1/secrets", headers=auth())
    assert listing.json() == []


async def test_a_renew_that_cannot_arm_restores_the_deadline(client) -> None:
    """The renew rolls back like a mint does (#260 review, round 5):
    the prior deadline returns, so no live placeholder stands
    without its redirect."""
    http, app, _stub = client
    await seed_workspace(app)
    row = (
        await http.post("/api/v1/secrets", json=mint_body(), headers=auth())
    ).json()
    prior = row["expires_at"]  # unbounded: the mint sent no ttl
    assert prior is None

    class RefusingInterceptor:
        async def refresh(self, workspace_id: str) -> None:
            raise RuntimeError("bind failed")

        async def on_detach(self, ws):  # pragma: no cover
            raise AssertionError

        async def stop(self):  # pragma: no cover
            raise AssertionError

    real = app.state.interceptor
    app.state.interceptor = RefusingInterceptor()
    try:
        response = await http.post(
            f"/api/v1/secrets/{row['id']}/renew",
            json={"ttl_s": 600},
            headers=auth(),
        )
    finally:
        app.state.interceptor = real
    assert response.status_code == 503
    assert "could not arm" in response.json()["detail"]
    restored = await app.state.model.get_placeholder(row["id"])
    assert restored["expires_at"] is None


async def test_a_renew_rollback_restores_a_real_deadline(client) -> None:
    """The restore path with a datetime deadline, not just the
    unbounded None."""
    http, app, _stub = client
    await seed_workspace(app)
    row = (
        await http.post(
            "/api/v1/secrets", json=mint_body(ttl_s=3600), headers=auth()
        )
    ).json()
    assert row["expires_at"] is not None

    class RefusingInterceptor:
        async def refresh(self, workspace_id: str) -> None:
            raise RuntimeError("bind failed")

        async def on_detach(self, ws):  # pragma: no cover
            raise AssertionError

        async def stop(self):  # pragma: no cover
            raise AssertionError

    real = app.state.interceptor
    app.state.interceptor = RefusingInterceptor()
    try:
        response = await http.post(
            f"/api/v1/secrets/{row['id']}/renew",
            json={"ttl_s": 600},
            headers=auth(),
        )
    finally:
        app.state.interceptor = real
    assert response.status_code == 503
    restored = await app.state.model.get_placeholder(row["id"])
    assert restored["expires_at"] is not None
    assert restored["expires_at"] < row["expires_at"]


# --- the workspace LLM proxy credential (#259) -------------------------------


async def test_create_mints_and_seeds_an_llm_token(client) -> None:
    """A create mints the credential: the row carries it (the
    token-gated endpoint serves it), the spec that prepared the
    artifacts carried it (the seed's input), and neither the create
    reply nor a workspace view ever shows it."""
    http, app, stub = client
    created = await http.post(
        "/api/v1/workspaces",
        json={"id": "ws-llm", "kernel": "/k", "rootfs": "/r"},
        headers=auth(),
    )
    assert created.status_code == 201, created.text
    assert "llm_token" not in created.json()
    listing = await http.get("/api/v1/workspaces", headers=auth())
    assert "llm_token" not in listing.text
    minted = (
        await http.get("/api/v1/workspaces/ws-llm/llm-token", headers=auth())
    ).json()
    assert minted["token"].startswith("msksllm1_")
    # The spec the artifacts were prepared from carried the same
    # token: the seed's planted credential and the row's agree.
    assert stub.prepared, stub.calls
    assert stub.prepared[-1].llm_token == minted["token"]


async def test_llm_token_endpoint_shapes_and_404s(client) -> None:
    http, _app, _stub = client
    missing = await http.get(
        "/api/v1/workspaces/nope/llm-token", headers=auth()
    )
    assert missing.status_code == 404
    remint_missing = await http.post(
        "/api/v1/workspaces/nope/llm-token", headers=auth()
    )
    assert remint_missing.status_code == 404
    await http.post(
        "/api/v1/workspaces",
        json={"id": "ws-tok", "kernel": "/k", "rootfs": "/r"},
        headers=auth(),
    )
    first = (
        await http.get("/api/v1/workspaces/ws-tok/llm-token", headers=auth())
    ).json()
    rotated = await http.post(
        "/api/v1/workspaces/ws-tok/llm-token", headers=auth()
    )
    assert rotated.status_code == 200
    assert rotated.json()["token"] != first["token"]
    after = (
        await http.get("/api/v1/workspaces/ws-tok/llm-token", headers=auth())
    ).json()
    assert after["token"] == rotated.json()["token"]


async def test_llm_token_endpoints_demand_a_bearer(client) -> None:
    http, _app, _stub = client
    bare = await http.get("/api/v1/workspaces/any/llm-token")
    assert bare.status_code == 401
    posted = await http.post("/api/v1/workspaces/any/llm-token")
    assert posted.status_code == 401


async def test_launch_heals_a_lost_seed_with_the_row_token(
    client, tmp_path: Path
) -> None:
    """The #259 review's heal gap: a workspace whose seed.img is
    gone at boot rebuilds it carrying the ROW's token — the guest
    keeps its planted credential even though workspace views omit
    it from the row dict spec_for reads."""
    http, app, stub = client
    created = await http.post(
        "/api/v1/workspaces",
        json={"id": "ws-heal", "kernel": "/k", "rootfs": "/r"},
        headers=auth(),
    )
    assert created.status_code == 201, created.text
    wid = created.json()["id"]
    minted = (
        await http.get(f"/api/v1/workspaces/{wid}/llm-token", headers=auth())
    ).json()["token"]
    started = await http.post(
        f"/api/v1/workspaces/{wid}/start", headers=auth()
    )
    assert started.status_code == 200, started.text
    assert stub.seen_specs[wid].llm_token == minted


async def test_healed_spec_returns_a_token_carrying_row_untouched() -> None:
    """The heal's early exit: a spec whose row already carries the
    credential (a direct construction, not the dict path) needs no
    model round-trip."""
    app = build_app(Settings(server=ServerSettings(db_path=Path("/dev/null"))))
    row = {
        "id": "ws-x",
        "kernel": "/k",
        "initrd": None,
        "rootfs": "/r",
        "cmdline": "c",
        "cpus": 1,
        "mem_mib": 64,
        "root_mib": 16,
        "home_mib": 8,
        "llm_token": "msksllm1_present",
    }
    assert (await api_mod.healed_spec(app, row)).llm_token == (
        "msksllm1_present"
    )


async def test_lifespan_migrates_legacy_backend_refs(tmp_path: Path) -> None:
    """#335's stored-format rename at the daemon's own layer: a
    placeholder minted before the rename boots into a daemon that
    moves its row, its stored value, and the manifest before the
    first request is served."""
    store_root = tmp_path / "store"
    settings = Settings(
        vmm=VmmSettings(state_dir=tmp_path / "vms"),
        net=NetSettings(enabled=False),
        server=ServerSettings(
            db_path=tmp_path / "db",
            bootstrap_token=TOKEN,
            event_poll_s=10.0,
        ),
        secret_store=SecretStoreSettings(root=store_root),
    )

    seed = build_app(settings)
    seed.state.model.migrate()
    await seed.state.model.create_placeholder(
        "ws-a",
        "github_api",
        new_sentinel(),
        ["api.github.com"],
        "MSKS_WS_A_GITHUB_API",
        None,
    )
    seed.state.secrets.sync_manifest(
        [("MSKS_WS_A_GITHUB_API", "ws-a/github_api")]
    )
    await seed.state.secrets.write("MSKS_WS_A_GITHUB_API", "ghp-old")
    await seed.state.model.close()

    app = build_app(settings)
    app.state.microvm = StubMicrovm()
    api = build_api(app)
    async with api.router.lifespan_context(api):
        (row,) = await app.state.model.list_placeholders()
        assert row["backend_ref"] == "MSKSWS_WS_A_GITHUB_API"
    stored = store_root / "msks" / "default"
    assert (stored / "MSKSWS_WS_A_GITHUB_API").read_text() == "ghp-old"
    assert not (stored / "MSKS_WS_A_GITHUB_API").exists()
    manifest = (store_root / "secretspec.toml").read_text()
    assert "MSKSWS_WS_A_GITHUB_API" in manifest
    assert "MSKS_" not in manifest
