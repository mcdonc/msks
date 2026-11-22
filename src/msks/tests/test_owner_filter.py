"""The owner filter over the API (#1082): peer-credential callers
see and address their own workspaces alone, root and the daemon's
uid see everything, and cross-owner access answers 404 exactly
like an absent row."""

import os
from pathlib import Path

import httpx
import pytest
from ext4tools import realtools  # noqa: F401 — the tools stay real here
from msks.app import build_app
from msks.server.api import build_api
from msks.settings import (
    NetSettings,
    ServerSettings,
    Settings,
    VmmSettings,
)
from test_api import TOKEN, StubMicrovm

#: Two uids no account on this host holds — the peer policy fails
#: them closed (#555), so each request also carries the bearer
#: token: authenticated, but still named by its peer credential
#: (the one fact the owner filter keys on).
ALICE = 4242
BOB = 4243

ROOT = 0


def auth(token: str = TOKEN) -> dict:
    return {"Authorization": f"Bearer {token}"}


class PeerTransport(httpx.ASGITransport):
    """ASGITransport with the scope's client pair set to a unix
    peer credential — the marker the wrapped protocols set on a
    real local-listener connection (#555) — so the API reads the
    caller the kernel would name."""

    def __init__(self, app, uid: int) -> None:
        super().__init__(app=app)
        self.uid = uid

    async def handle_async_request(self, request):
        real = self.app

        async def scoped(scope, receive, send):
            if scope["type"] in ("http", "websocket"):
                scope["client"] = ("unix", self.uid)
            await real(scope, receive, send)

        self.app = scoped
        try:
            return await super().handle_async_request(request)
        finally:
            self.app = real


@pytest.fixture
async def daemon(tmp_path: Path):
    """One app whose every route is reachable through per-peer
    clients, plus the unauthenticated TCP client (no peer
    credential — the daemon-uid mapping)."""
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
    api = build_api(app)
    clients = {}

    def peer(uid: int) -> httpx.AsyncClient:
        if uid not in clients:
            transport = PeerTransport(app=api, uid=uid)
            clients[uid] = httpx.AsyncClient(
                transport=transport, base_url="https://test"
            )
        return clients[uid]

    async with api.router.lifespan_context(api):
        yield peer, app, api
    for client in clients.values():
        await client.aclose()


async def create(peer, name: str) -> httpx.Response:
    return await peer.post(
        "/api/v1/workspaces",
        json={"name": name, "kernel": "/k", "rootfs": "/r"},
        headers=auth(),
    )


async def test_two_peers_hold_one_name(daemon) -> None:
    """Two peer uids create same-named workspaces; each listing
    shows only that caller's rows, and each row carries its
    owner."""
    peer, _app, _api = daemon
    assert (await create(peer(ALICE), "build")).status_code == 201
    assert (await create(peer(BOB), "build")).status_code == 201
    alice_rows = (
        await peer(ALICE).get("/api/v1/workspaces", headers=auth())
    ).json()
    bob_rows = (
        await peer(BOB).get("/api/v1/workspaces", headers=auth())
    ).json()
    assert [row["name"] for row in alice_rows] == ["build"]
    assert [row["name"] for row in bob_rows] == ["build"]
    assert alice_rows[0]["id"] != bob_rows[0]["id"]
    assert alice_rows[0]["owner_uid"] == ALICE
    assert bob_rows[0]["owner_uid"] == BOB


async def test_the_same_owner_cannot_hold_a_name_twice(daemon) -> None:
    """The create conflict means the name is held by the caller
    (#1082): a second same-name create by the same peer answers
    the 409."""
    peer, _app, _api = daemon
    assert (await create(peer(ALICE), "build")).status_code == 201
    reply = await create(peer(ALICE), "build")
    assert reply.status_code == 409
    assert reply.json()["detail"] == "workspace exists"


async def test_cross_owner_access_answers_404(daemon) -> None:
    """Read, stop, and remove by id answer 404 to another owner —
    indistinguishable from an absent row end to end — and root
    performs all three."""
    peer, _app, _api = daemon
    mine = (await create(peer(ALICE), "build")).json()
    bob = peer(BOB)
    assert (
        await bob.get(f"/api/v1/workspaces/{mine['id']}", headers=auth())
    ).status_code == 404
    assert (
        await bob.post(f"/api/v1/workspaces/{mine['id']}/stop", headers=auth())
    ).status_code == 404
    assert (
        await bob.delete(f"/api/v1/workspaces/{mine['id']}", headers=auth())
    ).status_code == 404
    # Root sees the row, stops it, and removes it.
    root = peer(ROOT)
    assert (
        await root.get(f"/api/v1/workspaces/{mine['id']}", headers=auth())
    ).status_code == 200
    assert (
        await root.post(
            f"/api/v1/workspaces/{mine['id']}/stop", headers=auth()
        )
    ).status_code == 200
    assert (
        await root.delete(f"/api/v1/workspaces/{mine['id']}", headers=auth())
    ).status_code == 200


async def test_cross_owner_ssh_key_answers_404(daemon) -> None:
    """The identity endpoint takes the same mask: another owner's
    workspace answers 404, the owner's own answers 404 only for
    the missing identity (a row without a key)."""
    peer, _app, _api = daemon
    mine = (await create(peer(ALICE), "build")).json()
    assert (
        await peer(BOB).get(
            f"/api/v1/workspaces/{mine['id']}/ssh-key", headers=auth()
        )
    ).status_code == 404
    assert (
        await peer(ALICE).get(
            f"/api/v1/workspaces/{mine['id']}/ssh-key", headers=auth()
        )
    ).status_code == 404


async def test_the_daemon_uid_sees_every_row(daemon) -> None:
    """A peer holding the daemon's own uid passes the socket
    policy with no token (#555) and bypasses the filter: the
    single-user deployment's visibility, unchanged."""
    peer, _app, _api = daemon
    await create(peer(ALICE), "alices")
    await create(peer(BOB), "bobs")
    rows = (await peer(os.geteuid()).get("/api/v1/workspaces")).json()
    assert [row["name"] for row in rows] == ["alices", "bobs"]


async def test_the_tcp_surface_maps_to_the_daemon_uid(daemon) -> None:
    """A caller with no peer credential — the TCP/token surface —
    is the daemon's own uid (#1082): it sees every row, exactly
    the pre-#1082 behavior the dev daemon keeps."""
    peer, _app, api = daemon
    await create(peer(ALICE), "alices")
    await create(peer(BOB), "bobs")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=api),
        base_url="https://test",
    ) as http:
        rows = (await http.get("/api/v1/workspaces", headers=auth())).json()
    assert [row["name"] for row in rows] == ["alices", "bobs"]


async def test_root_addressing_a_plural_name_answers_409(daemon) -> None:
    """A bare name two owners hold refuses to pick for root: the
    409 names both owners and the by-id remedy (#1082; #1084
    formalizes the error kind)."""
    peer, _app, _api = daemon
    await create(peer(ALICE), "build")
    await create(peer(BOB), "build")
    reply = await peer(ROOT).get("/api/v1/workspaces/build", headers=auth())
    assert reply.status_code == 409
    detail = reply.json()["detail"]
    assert str(ALICE) in detail and str(BOB) in detail
    assert "id" in detail


async def test_each_owner_resolves_their_own_name(daemon) -> None:
    """A non-bypassed caller's bare-name resolve is scoped to its
    own rows: both peers read ``build`` and each sees their own
    workspace."""
    peer, _app, _api = daemon
    alice_row = (await create(peer(ALICE), "build")).json()
    bob_row = (await create(peer(BOB), "build")).json()
    a = (
        await peer(ALICE).get("/api/v1/workspaces/build", headers=auth())
    ).json()
    b = (
        await peer(BOB).get("/api/v1/workspaces/build", headers=auth())
    ).json()
    assert a["id"] == alice_row["id"]
    assert b["id"] == bob_row["id"]


async def test_roots_create_is_blocked_by_any_owners_name(daemon) -> None:
    """Root's namespace spans every row (#1082): a create of a
    name any owner holds answers the 409, while a peer's create of
    another owner's name is free."""
    peer, _app, _api = daemon
    await create(peer(ALICE), "build")
    assert (await create(peer(ROOT), "build")).status_code == 409
    assert (await create(peer(BOB), "fresh")).status_code == 201


async def test_a_plural_decider_registration_is_refused() -> None:
    """A decider frame naming a name two owners holds (#1082) is
    refused with the named ambiguity reason — the same say-so
    frame as an unknown workspace, never a silent wait."""
    from types import SimpleNamespace

    from msks.model.workspaces import AmbiguousRef
    from msks.server.api import events as events_mod

    class Socket:
        def __init__(self) -> None:
            self.sent: list[dict] = []
            self.scope = {"client": ("unix", 0)}

        async def send_json(self, payload) -> None:
            self.sent.append(payload)

    async def plural(ref, viewer=None):
        raise AmbiguousRef(ref, [ALICE, BOB])

    socket = Socket()
    app = SimpleNamespace(
        state=SimpleNamespace(model=SimpleNamespace(get_workspace=plural))
    )
    await events_mod.register_decider(
        app, socket, client_id=1, message={"workspace": "build"}
    )
    reason = str(AmbiguousRef("build", [ALICE, BOB]))
    assert socket.sent == [
        {"event": "egress.decider_rejected", "data": {"reason": reason}}
    ]


async def test_the_proxies_listing_rides_the_filter(daemon) -> None:
    """The daemon-wide proxies read (#809) carries the owner filter
    (#1082): a peer sees only its own workspaces' rows — no verb
    leaks another owner's workspace existence — while root sees
    every row."""
    peer, _app, _api = daemon
    mine = (await create(peer(ALICE), "build")).json()
    reply = await peer(ALICE).post(
        f"/api/v1/workspaces/{mine['id']}/proxies",
        json={
            "direction": "forward",
            "listen_port": 8080,
            "target_port": 22,
        },
        headers=auth(),
    )
    assert reply.status_code == 200, reply.text
    alice_rows = (
        await peer(ALICE).get("/api/v1/proxies", headers=auth())
    ).json()
    bob_rows = (await peer(BOB).get("/api/v1/proxies", headers=auth())).json()
    root_rows = (
        await peer(ROOT).get("/api/v1/proxies", headers=auth())
    ).json()
    assert [row["workspace_id"] for row in alice_rows] == [mine["id"]]
    assert bob_rows == []
    assert [row["workspace_id"] for row in root_rows] == [mine["id"]]
