"""API-level tests over ASGITransport with a stubbed microvm seam."""

import asyncio
from pathlib import Path

import httpx
import pytest
from msks.app import build_app
from msks.microvm.errors import MicrovmError, MicrovmTimeoutError
from msks.microvm.spec import VmInfo, VmSpec, VmStatus
from msks.server import api as api_mod
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
    start_missing = await http.post(
        "/api/v1/workspaces/ghost/start", headers=auth()
    )
    assert start_missing.status_code == 404


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
    assert created.json()["ssh_pubkey"].endswith("msksd:ws-id")
    spec_seen = stub.seen_specs["ws-id"]
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
    assert stub.seen_specs["ws-cm"].ssh_pubkey == row["ssh_pubkey"]
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
        assert stub.seen_specs[wid].ssh_pubkey == created.json()["ssh_pubkey"]
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


async def test_k8s_refuses_client_supplied_pubkey(client) -> None:
    """The same refusal shape as user_data: the runner pod builds no
    seed disks, so a client-supplied key would store a line nothing
    ever plants."""
    from msks.identity import mint

    http, app, _stub = client
    app.state.settings.vmm.driver = "k8s"
    _private, public = mint("ecdsa")
    created = await http.post(
        "/api/v1/workspaces",
        json={
            "id": "ws-k8s-cm",
            "kernel": "/k",
            "rootfs": "/r",
            "egress": False,
            "ssh_pubkey": public,
        },
        headers=auth(),
    )
    assert created.status_code == 400
    assert "not served by the k8s backend" in created.json()["detail"]
    app.state.settings.vmm.driver = "local"


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
    assert key.json()["public_key"].endswith("msksd:ws-race")


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
    """The daemon names the appliance image it booted from (#160):
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

    async def lose(spec, image_hash=None, host=None, ssh_privkey=None):
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


async def test_create_race_after_prepare_answers_409(
    client, monkeypatch
) -> None:
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
    # No minted identity on the k8s backend (#111): the runner pod
    # builds no seed, so nothing would ever plant the key — the key
    # endpoint answers the no-identity 404 instead of serving a key
    # no guest will accept.
    assert response.json()["ssh_pubkey"] is None
    key = await http.get("/api/v1/workspaces/ws-k8s/ssh-key", headers=auth())
    assert key.status_code == 404
    assert "no minted identity" in key.json()["detail"]
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
    assert stub.seen_specs["ws-ud"].user_data == payload
    fetched = await http.get("/api/v1/workspaces/ws-ud", headers=auth())
    assert fetched.json()["user_data"] == payload


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
    ws = next(row for row in body["workspaces"] if row["id"] == "ws-st")
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


async def test_storage_report_refused_on_k8s(client, monkeypatch) -> None:
    """The report describes the local backend's artifact files; k8s
    keeps them on per-workspace claims and gets a named refusal."""
    http, app, _stub = client
    monkeypatch.setattr(app.state.settings.vmm, "driver", "k8s")
    refused = await http.get("/api/v1/storage", headers=auth())
    assert refused.status_code == 400
    assert "not served by the k8s backend" in refused.json()["detail"]


async def test_create_not_refused_below_floor_on_k8s(
    client, monkeypatch
) -> None:
    """A k8s create at a critical local filesystem proceeds: the
    artifacts live on per-workspace claims, not this state disk."""
    http, app, _stub = client
    app.state.settings.vmm.state_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(app.state.settings.vmm, "driver", "k8s")
    monkeypatch.setattr(
        app.state.settings.vmm,
        "storage_floor_mib",
        (1 << 50) // (1024 * 1024),
    )
    created = await http.post(
        "/api/v1/workspaces",
        json={
            "id": "ws-k8s-floor",
            "kernel": "/k",
            "rootfs": "/r",
            "egress": False,
        },
        headers=auth(),
    )
    assert created.status_code == 201


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


async def test_image_import_floor_applies_on_k8s(client, monkeypatch) -> None:
    """The image catalog lives on the daemon's state disk on every
    backend: a k8s daemon below its floor answers the same 507."""
    http, app, _stub = client
    app.state.settings.vmm.state_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(app.state.settings.vmm, "driver", "k8s")
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
    await plant_volume(app, "ws-rs", 64)
    resized = await http.post(
        "/api/v1/workspaces/ws-rs/resize",
        json={"home_mib": 128},
        headers=auth(),
    )
    assert resized.status_code == 200
    body = resized.json()
    assert body["home_mib"] == 128
    volume = app.state.settings.vmm.state_dir / "volumes" / "ws-rs.ext4"
    assert volume.stat().st_size == 128 * 1024 * 1024


async def test_resize_shrinks_the_home_volume(client) -> None:
    http, app, _stub = client
    created = await http.post(
        "/api/v1/workspaces",
        json={"id": "ws-sh", "kernel": "/k", "rootfs": "/r", "home_mib": 128},
        headers=auth(),
    )
    assert created.status_code == 201
    await plant_volume(app, "ws-sh", 128)
    resized = await http.post(
        "/api/v1/workspaces/ws-sh/resize",
        json={"home_mib": 64},
        headers=auth(),
    )
    assert resized.status_code == 200
    volume = app.state.settings.vmm.state_dir / "volumes" / "ws-sh.ext4"
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
    await plant_qcow2(app, "ws-ov", 256)
    resized = await http.post(
        "/api/v1/workspaces/ws-ov/resize",
        json={"root_mib": 512},
        headers=auth(),
    )
    assert resized.status_code == 200
    assert resized.json()["root_mib"] == 512
    overlay = app.state.settings.vmm.state_dir / "vms" / "ws-ov" / "root.qcow2"
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
    await app.state.model.set_status("ws-run", "running")
    refused = await http.post(
        "/api/v1/workspaces/ws-run/resize",
        json={"home_mib": 128},
        headers=auth(),
    )
    assert refused.status_code == 409
    assert "stop it before" in refused.json()["detail"]


async def test_resize_refuses_overlay_shrink(client) -> None:
    http, _app, _stub = client
    created = await http.post(
        "/api/v1/workspaces",
        json={"id": "ws-nos", "kernel": "/k", "rootfs": "/r", "root_mib": 512},
        headers=auth(),
    )
    assert created.status_code == 201
    refused = await http.post(
        "/api/v1/workspaces/ws-nos/resize",
        json={"root_mib": 256},
        headers=auth(),
    )
    assert refused.status_code == 400
    assert "only grows" in refused.json()["detail"]


async def test_resize_refuses_noop_and_empty(client) -> None:
    http, _app, _stub = client
    created = await http.post(
        "/api/v1/workspaces",
        json={"id": "ws-noop", "kernel": "/k", "rootfs": "/r"},
        headers=auth(),
    )
    assert created.status_code == 201
    empty = await http.post(
        "/api/v1/workspaces/ws-noop/resize", json={}, headers=auth()
    )
    assert empty.status_code == 400
    assert "nothing to resize" in empty.json()["detail"]
    same = await http.post(
        "/api/v1/workspaces/ws-noop/resize",
        json={"home_mib": 2048},
        headers=auth(),
    )
    assert same.status_code == 400
    assert "already at those sizes" in same.json()["detail"]


async def test_resize_refused_on_k8s(client, monkeypatch) -> None:
    http, app, _stub = client
    created = await http.post(
        "/api/v1/workspaces",
        json={
            "id": "ws-k8s-rs",
            "kernel": "/k",
            "rootfs": "/r",
            "egress": False,
        },
        headers=auth(),
    )
    assert created.status_code == 201
    monkeypatch.setattr(app.state.settings.vmm, "driver", "k8s")
    refused = await http.post(
        "/api/v1/workspaces/ws-k8s-rs/resize",
        json={"home_mib": 128},
        headers=auth(),
    )
    assert refused.status_code == 400
    assert "not served by the k8s backend" in refused.json()["detail"]


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
    e2fsck = tmp_path / "e2fsck"
    e2fsck.write_text("#!/bin/sh\nexit 0\n")
    e2fsck.chmod(0o755)
    app.state.settings.vmm.resize2fs = str(failing)
    app.state.settings.vmm.e2fsck = str(e2fsck)
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
    volume = app.state.settings.vmm.state_dir / "volumes" / "ws-full.ext4"
    volume.parent.mkdir(parents=True, exist_ok=True)
    volume.write_bytes(b"")
    refused = await http.post(
        "/api/v1/workspaces/ws-full/resize",
        json={"home_mib": 64},
        headers=auth(),
    )
    assert refused.status_code == 409
    assert "resize2fs refused" in refused.json()["detail"]
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
    http, _app, _stub = client
    created = await http.post(
        "/api/v1/workspaces",
        json={"id": "ws-gone", "kernel": "/k", "rootfs": "/r", "home_mib": 64},
        headers=auth(),
    )
    assert created.status_code == 201

    async def vanished(*args, **kwargs) -> bool:
        return False

    monkeypatch.setattr(_app.state.model, "set_sizes", vanished)
    resized = await http.post(
        "/api/v1/workspaces/ws-gone/resize",
        json={"home_mib": 128},
        headers=auth(),
    )
    assert resized.status_code == 404


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
