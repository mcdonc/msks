"""Owner-scoped secrets (#1083): the #339 coverage model with one
word changed — ``[]`` (daemon-wide) became owner-wide. A mint
records the minting peer's uid, an owner-wide row reaches its own
owner's workspaces alone (create-time arming and row-only
re-resolution alike), the listing serves the caller's own mints,
and the audit log names the owner per event."""

from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from ext4tools import realtools  # noqa: F401 — the tools stay real here
from msks.app import build_app
from msks.interceptor import Interceptor
from msks.microvm import VmSpec
from msks.model.workspaces import daemon_uid
from msks.secretstore import backend_ref
from msks.server.api import build_api
from msks.settings import (
    NetSettings,
    ServerSettings,
    Settings,
    VmmSettings,
)
from test_api import TOKEN, StubMicrovm
from test_interceptor_manager import fake_master_factory, tap_ip
from test_owner_filter import ALICE, BOB, ROOT, PeerTransport, auth

INTERCEPTOR_PORT = 8643


class AttachedNet:
    """The NetManager seams the armed path reads, over a fixed
    live set (#1083): every named workspace holds an attachment
    with the manager test's tap table, so a mint's arm pass and
    the teardown's disarm both find their listener facts."""

    def __init__(self, live: set[str]) -> None:
        self.live = live
        self.interceptions: list[tuple[str, int | None]] = []

    async def start(self) -> None:
        return None

    async def stop(self) -> None:
        return None

    def attachment_for(self, workspace_id: str):
        if workspace_id not in self.live:
            return None
        return SimpleNamespace(tap_ip=tap_ip(workspace_id))

    def attached_workspaces(self) -> list[str]:
        return [wid for wid in sorted(self.live)]

    async def apply_interception(self, workspace_id, port) -> None:
        self.interceptions.append((workspace_id, port))


@pytest.fixture
async def owners(tmp_path: Path):
    """One daemon whose api answers through per-peer clients, with
    the real interceptor over a fake master and two attached,
    key-carrying workspaces — alice's and bob's, one each."""
    from test_interceptor_manager import FakeMaster

    settings = Settings(
        vmm=VmmSettings(state_dir=tmp_path / "vms"),
        net=NetSettings(enabled=False),
        server=ServerSettings(
            db_path=tmp_path / "own.db",
            bootstrap_token=TOKEN,
            event_poll_s=10.0,
        ),
    )
    app = build_app(settings)
    app.state.microvm = StubMicrovm()
    app.state.net = AttachedNet({"ws-alice", "ws-bob"})
    app.state.interceptor = Interceptor(
        app, master_factory=fake_master_factory()
    )
    FakeMaster.built.clear()
    api = build_api(app)
    clients: dict[int, httpx.AsyncClient] = {}

    def peer(uid: int) -> httpx.AsyncClient:
        if uid not in clients:
            transport = PeerTransport(app=api, uid=uid)
            clients[uid] = httpx.AsyncClient(
                transport=transport, base_url="https://test"
            )
        return clients[uid]

    async with api.router.lifespan_context(api):
        from testkeys import mint as mint_key

        for owner, wid in ((ALICE, "ws-alice"), (BOB, "ws-bob")):
            _private, public = mint_key("ed25519")
            await app.state.model.create_workspace(
                VmSpec(
                    workspace_id=wid,
                    kernel=Path("/k"),
                    rootfs=Path("/r"),
                    ssh_pubkey=public,
                ),
                owner_uid=owner,
            )
        yield peer, app
    for client in clients.values():
        await client.aclose()


def wide_body(name: str = "token") -> dict:
    """An owner-wide mint body: no workspace target (#339)."""
    return {"name": name, "dests": ["api.example.com"], "value": "v"}


async def covering(app, workspace_id: str) -> dict:
    """The workspace's covering half, recomputed from rows alone —
    the read a daemon restart's re-resolution takes (#1083)."""
    covering_half, _table = await app.state.interceptor.entry_tables(
        workspace_id
    )
    return covering_half


async def entry(app, workspace_id: str, sentinel: str):
    """One sentinel's entry in the workspace's full table, None
    when the table holds it not even for detection."""
    _covering, table = await app.state.interceptor.entry_tables(workspace_id)
    return table.get(sentinel)


async def test_an_owner_wide_mint_reaches_only_its_owners_workspaces(
    owners,
) -> None:
    """Alice's ``[]``-coverage mint arms her own attached workspace
    and never bob's: the master's mode list holds her tap alone,
    her workspace's table carries the covering entry, and bob's
    holds the sentinel for detection only — a cross-owner leak is
    announced, never serviced."""
    peer, app = owners
    from test_interceptor_manager import FakeMaster

    minted = (
        await peer(ALICE).post(
            "/api/v1/secrets", json=wide_body(), headers=auth()
        )
    ).json()
    # Create-time injection: the arm pass refreshed alice's
    # workspace alone, and the master serves one listener.
    assert len(FakeMaster.built) == 1
    master = FakeMaster.built[0]
    assert master.options.mode == [
        f"transparent@{tap_ip('ws-alice')}:{INTERCEPTOR_PORT}"
    ]
    alice_entry = await entry(app, "ws-alice", minted["sentinel"])
    assert alice_entry is not None
    assert alice_entry.dests == ("api.example.com",)
    bob_entry = await entry(app, "ws-bob", minted["sentinel"])
    assert bob_entry is not None
    assert bob_entry.dests == ()
    # Daemon-restart re-resolution: the covering halves recomputed
    # from rows alone answer the same split.
    assert set(await covering(app, "ws-alice")) == {minted["sentinel"]}
    assert await covering(app, "ws-bob") == {}
    # The blobs reached alice's key alone: the recipient set is
    # owner-scoped at mint (#514's daemon-wide rule, #1083's owner).
    blobs = await app.state.model.secret_blobs(minted["id"])
    assert [blob["recipient"] for blob in blobs] == ["ws-alice"]


async def test_bobs_wide_mint_arms_his_own_alike(owners) -> None:
    """Both owners' resolution paths hold (#1083): bob's own wide
    mint arms his workspace beside alice's armed one, and the two
    same-named rows coexist — the label namespace is per owner."""
    peer, app = owners
    from test_interceptor_manager import FakeMaster

    alice_mint = (
        await peer(ALICE).post(
            "/api/v1/secrets", json=wide_body(), headers=auth()
        )
    ).json()
    bob_mint = await peer(BOB).post(
        "/api/v1/secrets", json=wide_body(), headers=auth()
    )
    assert bob_mint.status_code == 201
    bob = bob_mint.json()
    assert bob["id"] != alice_mint["id"]
    # The master now serves both owners' taps — one listener each.
    master = FakeMaster.built[0]
    assert master.options.mode == sorted(
        [
            f"transparent@{tap_ip('ws-alice')}:{INTERCEPTOR_PORT}",
            f"transparent@{tap_ip('ws-bob')}:{INTERCEPTOR_PORT}",
        ]
    )
    assert set(await covering(app, "ws-bob")) == {bob["sentinel"]}
    assert set(await covering(app, "ws-alice")) == {alice_mint["sentinel"]}
    # The two owners' same-named rows hold distinct refs — distinct
    # cache keys, never one owner's plaintext behind the other's
    # sentinel.
    rows = {
        row["owner_uid"]: row
        for row in await app.state.model.list_placeholders()
        if row["name"] == "token"
    }
    assert rows[ALICE]["backend_ref"] == backend_ref(ALICE, [], "token")
    assert rows[BOB]["backend_ref"] == backend_ref(BOB, [], "token")


async def test_a_scoped_row_keeps_the_owner_match(owners) -> None:
    """A scoped row keeps its workspace array and gains the owner
    match (#1083): alice's scoped mint covers her workspace, reads
    as detection-only from bob's, root's mint at alice's workspace
    — root bypasses the ref filter, so the id resolves — is refused
    by name (a row uid 0 owns covers nothing of alice's), and
    root's mint at ROOT'S OWN workspace records uid 0 verbatim and
    covers it: the recording matches the way workspace rows record
    their creator (#1082)."""
    peer, app = owners
    mine = await peer(ALICE).post(
        "/api/v1/secrets",
        json={**wide_body(), "workspaces": ["ws-alice"]},
        headers=auth(),
    )
    assert mine.status_code == 201
    assert set(await covering(app, "ws-alice")) == {mine.json()["sentinel"]}
    foreign = await entry(app, "ws-bob", mine.json()["sentinel"])
    assert foreign is not None
    assert foreign.dests == ()
    # Root names alice's workspace by id (the bypass sees every
    # row, #1082) and mints a scoped row at it: the refusal names
    # the workspace's owner and the minting uid — never a 201 over
    # a row that covers nothing.
    roots = await peer(ROOT).post(
        "/api/v1/secrets",
        json={**wide_body("roots"), "workspace_id": "ws-alice"},
        headers=auth(),
    )
    assert roots.status_code == 422
    detail = roots.json()["detail"]
    assert "ws-alice" in detail and str(ALICE) in detail
    assert set(await covering(app, "ws-alice")) == {mine.json()["sentinel"]}
    # Root's own workspace records uid 0 at create (#1082); root's
    # mint records uid 0 verbatim, so the row covers it.
    from testkeys import mint as mint_key

    _private, public = mint_key("ed25519")
    await app.state.model.create_workspace(
        VmSpec(
            workspace_id="ws-root",
            kernel=Path("/k"),
            rootfs=Path("/r"),
            ssh_pubkey=public,
        ),
        owner_uid=ROOT,
    )
    roots_own = await peer(ROOT).post(
        "/api/v1/secrets",
        json={**wide_body("roots_own"), "workspace_id": "ws-root"},
        headers=auth(),
    )
    assert roots_own.status_code == 201
    assert roots_own.json()["owner_uid"] == ROOT
    assert set(await covering(app, "ws-root")) == {
        roots_own.json()["sentinel"]
    }


async def test_the_listing_serves_the_callers_own_mints(owners) -> None:
    """``secret ls`` filters by peer uid (#1083): alice and bob see
    their own rows alone — each row naming its owner — while root
    and the TCP surface see both."""
    peer, _app = owners
    await peer(ALICE).post(
        "/api/v1/secrets", json=wide_body("alices"), headers=auth()
    )
    await peer(BOB).post(
        "/api/v1/secrets", json=wide_body("bobs"), headers=auth()
    )
    alices = (await peer(ALICE).get("/api/v1/secrets", headers=auth())).json()
    bobs = (await peer(BOB).get("/api/v1/secrets", headers=auth())).json()
    assert [row["name"] for row in alices] == ["alices"]
    assert alices[0]["owner_uid"] == ALICE
    assert [row["name"] for row in bobs] == ["bobs"]
    assert bobs[0]["owner_uid"] == BOB
    roots = (await peer(ROOT).get("/api/v1/secrets", headers=auth())).json()
    # Root spans every owner — the seeded probe row included
    # (#424): it mints as the daemon's uid.
    assert sorted(row["name"] for row in roots) == [
        "alices",
        "bobs",
        "probe",
    ]


async def test_the_audit_names_the_owner_per_event(owners) -> None:
    """Every mint event records its owner beside the coverage
    (#1083): the minting peer's uid per row."""
    peer, app = owners
    await peer(ALICE).post(
        "/api/v1/secrets", json=wide_body("alices"), headers=auth()
    )
    await peer(BOB).post(
        "/api/v1/secrets", json=wide_body("bobs"), headers=auth()
    )
    rows = {
        row["name"]: row
        for row in await app.state.model.list_audit()
        if row["kind"] == "mint" and row["name"] != "probe"
    }
    assert rows["alices"]["owner_uid"] == ALICE
    assert rows["bobs"]["owner_uid"] == BOB


async def test_the_probe_row_stays_the_daemons_own(owners) -> None:
    """The seeded probe row (#424) mints as the daemon's uid
    (#1083): it covers neither owner's workspace, so a fresh
    multi-owner daemon arms nothing on it."""
    _peer, app = owners
    probe = await app.state.model.placeholder_for(
        [], "probe", owner_uid=daemon_uid()
    )
    assert probe is not None
    assert probe["owner_uid"] == daemon_uid()
    assert await covering(app, "ws-alice") == {}
    assert await covering(app, "ws-bob") == {}


async def test_mutations_and_reads_mask_another_owners_rows(owners) -> None:
    """The whole secret surface takes the listing's owner rule
    (#1083): bob's revoke, renew, row fetch, and audit/pending
    reads answer 404 or exclude alice's rows — never a verb, a
    sentinel, or a served marker — while root performs the
    mutation and reads both owners' trails."""
    from test_api import unlock_body

    peer, app = owners
    minted = (
        await peer(ALICE).post(
            "/api/v1/secrets", json=wide_body(), headers=auth()
        )
    ).json()
    row_id = minted["id"]
    bob = peer(BOB)
    assert (
        await bob.delete(f"/api/v1/secrets/{row_id}", headers=auth())
    ).status_code == 404
    assert (
        await bob.post(
            f"/api/v1/secrets/{row_id}/renew",
            json={"ttl_s": 3600},
            headers=auth(),
        )
    ).status_code == 404
    assert (
        await bob.get(f"/api/v1/secrets/{row_id}", headers=auth())
    ).status_code == 404
    # The pending list serves bob none of alice's rows, so the
    # served window the unlock requires never opens for them
    # either: a foreign id answers the unlock's own 404.
    pending = await bob.get("/api/v1/secrets/pending", headers=auth())
    assert pending.json() == []
    refused = await bob.post(
        "/api/v1/secrets/unlock",
        json=unlock_body(row_id, "chosen-plaintext"),
        headers=auth(),
    )
    assert refused.status_code == 404
    # The audit trail reads the same way: bob's view holds none of
    # alice's events, root's holds both owners'.
    bobs_audit = (
        await bob.get("/api/v1/secrets/audit", headers=auth())
    ).json()
    assert [row["name"] for row in bobs_audit if row["name"] != "probe"] == []
    roots_audit = (
        await peer(ROOT).get("/api/v1/secrets/audit", headers=auth())
    ).json()
    assert "token" in [row["name"] for row in roots_audit]
    # Root revokes the row the 404s masked for bob.
    assert (
        await peer(ROOT).delete(f"/api/v1/secrets/{row_id}", headers=auth())
    ).status_code == 200


async def test_the_probe_row_re_arms_under_its_legacy_ref(owners) -> None:
    """An upgraded deployment's probe row keeps the ref spelling it
    minted under (#1083's migration adds the owner column without
    rewriting refs): the boot seed finds the row by owner and
    label, and re-arms it under the ref it still carries."""
    from msks.secretstore import SENTINEL_PREFIX
    from msks.server.api import secrets as secrets_api

    peer, app = owners
    for row in await app.state.model.list_placeholders():
        await app.state.model.delete_placeholder(row["id"])
    await app.state.model.create_placeholder(
        [],
        "probe",
        SENTINEL_PREFIX + "legacy",
        ["secretprobe.msks"],
        "MSKSDAEMON_PROBE",  # the pre-#1083 spelling
        None,
        owner_uid=daemon_uid(),
    )
    await secrets_api.seed_probe_placeholder(app)
    row = await app.state.model.placeholder_for(
        [], "probe", owner_uid=daemon_uid()
    )
    assert row is not None
    assert row["backend_ref"] == "MSKSDAEMON_PROBE"
    # The re-arm cached the public credential under the row's own
    # (legacy) ref: the boot contract holds on the upgrade path.
    from msks.spec.probe import PROBE_SECRET_B64

    assert app.state.secrets.plain.read("MSKSDAEMON_PROBE") == (
        PROBE_SECRET_B64
    )
