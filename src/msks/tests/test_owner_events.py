"""Owner-scoped event frames (#1095): the whole relay, not the
``secret.*`` half alone. Every publish that names a workspace
attributes to that workspace's owner — the frame reaches the
owner's subscribers and the unscoped (bypass) ones alone, while
the daemon-wide families (the image catalog, the disk-pressure
transitions) keep broadcasting. The #1090 suite pinned the
``secret.*`` families; these tests pin the rest beside the same
socket shape: two peers' sockets side by side, and the
daemon-uid socket that stands for the bypass viewer."""

import asyncio
import json
import socket
from pathlib import Path

from ext4tools import realtools
from msks.app import build_app
from msks.microvm import VmSpec
from msks.server.api import build_api
from msks.settings import (
    NetSettings,
    ServerSettings,
    Settings,
    VmmSettings,
)
from msks.spec.egress import DECISION_ALLOWED, MODE_INTERACTIVE
from test_api import TOKEN, StubMicrovm
from test_owner_filter import ALICE, BOB, auth
from test_owner_secrets import peer_from_header


def owner_daemon(tmp_path: Path):
    """One daemon over a scratch state dir, its microvm seam
    stubbed — the TestClient tests' shared base."""
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
    return app, api


def create_body(wid: str) -> dict:
    """A minimal workspace create the stub seam accepts."""
    return {"id": wid, "kernel": "/k", "rootfs": "/r"}


def free_port() -> int:
    """A port the kernel confirms free (bound then released)."""
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def plant_volume(app, workspace_id: str, mib: int) -> None:
    """A genuinely formatted home volume under the state dir —
    test_api's async planter, sync for the TestClient tests
    (mkfs itself runs to completion either way)."""
    import subprocess

    home = (
        app.state.settings.vmm.state_dir / "volumes" / f"{workspace_id}.ext4"
    )
    home.parent.mkdir(parents=True, exist_ok=True)
    with home.open("wb") as handle:
        handle.truncate(mib * 1024 * 1024)
    subprocess.run(
        ["mkfs.ext4", "-q", "-F", "-L", "msks-home", str(home)],
        check=True,
    )


def frames_of(ws, count: int) -> list[tuple[str, str]]:
    """The socket's next *count* frames as (event, workspace id)
    pairs, oldest first — every family under test names its
    workspace in ``data.id`` or ``data.workspace_id``."""
    frames = [json.loads(ws.receive_text()) for _ in range(count)]
    return [(frame["event"], frame["data"].get("id")) for frame in frames]


def test_the_live_tail_scopes_workspace_frames_by_owner(
    tmp_path: Path,
) -> None:
    """#1095, the #1090 shape: the sockets open first, then alice
    drives her workspace's whole lifecycle (create, start, mounts
    set, stop, delete) beside bob's create. Each frame names its
    workspace: alice's socket receives her lifecycle alone, bob's
    receives his own create alone, and the daemon-uid socket
    receives both owners'."""
    from fastapi.testclient import TestClient

    _app, api = owner_daemon(tmp_path)
    client = TestClient(peer_from_header(api))
    with client:
        alice = {**auth(), "x-test-peer": str(ALICE)}
        bob = {**auth(), "x-test-peer": str(BOB)}
        with (
            client.websocket_connect(
                "/api/v1/events", headers=alice
            ) as alice_ws,
            client.websocket_connect("/api/v1/events", headers=bob) as bob_ws,
            client.websocket_connect(
                "/api/v1/events", headers=auth()
            ) as daemon_ws,
        ):
            created = client.post(
                "/api/v1/workspaces",
                json=create_body("ws-alice"),
                headers=alice,
            )
            assert created.status_code == 201, created.text
            wid = created.json()["id"]
            for verb in ("start", "stop"):
                moved = client.post(
                    f"/api/v1/workspaces/{wid}/{verb}", headers=alice
                )
                assert moved.status_code == 200, moved.text
            mounts = client.post(
                f"/api/v1/workspaces/{wid}/mounts",
                json={"mounts": []},
                headers=alice,
            )
            assert mounts.status_code == 200, mounts.text
            removed = client.delete(f"/api/v1/workspaces/{wid}", headers=alice)
            assert removed.status_code == 200, removed.text
            bobbed = client.post(
                "/api/v1/workspaces",
                json=create_body("ws-bob"),
                headers=bob,
            )
            assert bobbed.status_code == 201, bobbed.text

            lifecycle = [
                ("workspace.created", wid),
                ("workspace.status", wid),
                ("workspace.status", wid),
                ("workspace.status", wid),
                ("workspace.status", wid),
                ("workspace.mounts", wid),
                ("workspace.removed", wid),
            ]
            bob_wid = bobbed.json()["id"]
            assert frames_of(alice_ws, len(lifecycle)) == lifecycle
            assert frames_of(bob_ws, 1) == [("workspace.created", bob_wid)]
            assert frames_of(daemon_ws, len(lifecycle) + 1) == (
                lifecycle + [("workspace.created", bob_wid)]
            )


@realtools
def test_the_live_tail_scopes_home_volume_frames_by_owner(
    tmp_path: Path,
) -> None:
    """#1095: the home-volume move frames attribute to the moving
    workspace's owner — alice's export reaches her socket alone,
    bob's import of her bytes reaches his alone, and the
    follow-up starts (the known frames that would displace a
    leak) stay on their own sockets."""
    from fastapi.testclient import TestClient

    app, api = owner_daemon(tmp_path)
    client = TestClient(peer_from_header(api))
    with client:
        alice = {**auth(), "x-test-peer": str(ALICE)}
        bob = {**auth(), "x-test-peer": str(BOB)}
        created = client.post(
            "/api/v1/workspaces", json=create_body("ws-alice"), headers=alice
        )
        assert created.status_code == 201, created.text
        bobbed = client.post(
            "/api/v1/workspaces", json=create_body("ws-bob"), headers=bob
        )
        assert bobbed.status_code == 201, bobbed.text
        alice_wid = created.json()["id"]
        bob_wid = bobbed.json()["id"]
        with (
            client.websocket_connect(
                "/api/v1/events", headers=alice
            ) as alice_ws,
            client.websocket_connect("/api/v1/events", headers=bob) as bob_ws,
            client.websocket_connect(
                "/api/v1/events", headers=auth()
            ) as daemon_ws,
        ):
            plant_volume(app, alice_wid, 8)
            plant_volume(app, bob_wid, 8)
            with client.stream(
                "GET",
                f"/api/v1/workspaces/{alice_wid}/home",
                headers=alice,
            ) as response:
                assert response.status_code == 200
                volume = b"".join(response.iter_bytes())
            imported = client.put(
                f"/api/v1/workspaces/{bob_wid}/home",
                content=volume,
                headers=bob,
            )
            assert imported.status_code == 200, imported.text
            for headers, wid in ((alice, alice_wid), (bob, bob_wid)):
                started = client.post(
                    f"/api/v1/workspaces/{wid}/start", headers=headers
                )
                assert started.status_code == 200, started.text

            assert frames_of(alice_ws, 3) == [
                ("home.exported", alice_wid),
                ("workspace.status", alice_wid),
                ("workspace.status", alice_wid),
            ]
            assert frames_of(bob_ws, 3) == [
                ("home.imported", bob_wid),
                ("workspace.status", bob_wid),
                ("workspace.status", bob_wid),
            ]
            assert frames_of(daemon_ws, 6) == [
                ("home.exported", alice_wid),
                ("home.imported", bob_wid),
                ("workspace.status", alice_wid),
                ("workspace.status", alice_wid),
                ("workspace.status", bob_wid),
                ("workspace.status", bob_wid),
            ]


def test_the_live_tail_scopes_proxy_frames_by_owner(tmp_path: Path) -> None:
    """#1095: the proxy families attribute to the workspace's
    owner — the API-route frames (created, removed) and the
    manager's own transitions (listening, closed) alike, the
    manager reading the owner off the workspace row at publish."""
    from fastapi.testclient import TestClient

    _app, api = owner_daemon(tmp_path)
    client = TestClient(peer_from_header(api))
    with client:
        alice = {**auth(), "x-test-peer": str(ALICE)}
        bob = {**auth(), "x-test-peer": str(BOB)}
        created = client.post(
            "/api/v1/workspaces",
            json=create_body("ws-alice"),
            headers=alice,
        )
        assert created.status_code == 201, created.text
        bobbed = client.post(
            "/api/v1/workspaces", json=create_body("ws-bob"), headers=bob
        )
        assert bobbed.status_code == 201, bobbed.text
        alice_wid = created.json()["id"]
        bob_wid = bobbed.json()["id"]
        with (
            client.websocket_connect(
                "/api/v1/events", headers=alice
            ) as alice_ws,
            client.websocket_connect("/api/v1/events", headers=bob) as bob_ws,
            client.websocket_connect(
                "/api/v1/events", headers=auth()
            ) as daemon_ws,
        ):

            def proxy_flow(headers: dict, wid: str) -> None:
                made = client.post(
                    f"/api/v1/workspaces/{wid}/proxies",
                    json={
                        "direction": "forward",
                        "listen_port": free_port(),
                        "target_port": 22,
                    },
                    headers=headers,
                )
                assert made.status_code == 200, made.text
                proxy_id = made.json()["id"]
                gone = client.delete(
                    f"/api/v1/workspaces/{wid}/proxies/{proxy_id}",
                    headers=headers,
                )
                assert gone.status_code == 200, gone.text

            proxy_flow(alice, alice_wid)
            proxy_flow(bob, bob_wid)

            def flow(wid: str) -> list[tuple[str, str]]:
                # The manager's bind publishes before the route's
                # own created frame lands.
                return [
                    ("proxy.listening", wid),
                    ("proxy.created", wid),
                    ("proxy.closed", wid),
                    ("proxy.removed", wid),
                ]

            assert frames_of(alice_ws, 4) == flow(alice_wid)
            assert frames_of(bob_ws, 4) == flow(bob_wid)
            assert frames_of(daemon_ws, 8) == flow(alice_wid) + flow(bob_wid)


async def test_consent_frames_scope_to_the_workspace_owner(
    tmp_path: Path,
) -> None:
    """#1095: the consent-hold rule — decisions route to the
    owner's decider session, so the hold's own frames do too. A
    hold on alice's workspace prompts her subscribers alone: the
    request frame, its resolution, and the refreshed rules frame
    never cross a foreign subscriber's socket, while the
    unscoped (bypass) subscription receives all three."""
    settings = Settings(
        vmm=VmmSettings(state_dir=tmp_path / "vms"),
        net=NetSettings(
            enabled=False, consent_timeout_s=300.0, consent_rate_limit=10
        ),
        server=ServerSettings(
            db_path=tmp_path / "consent.db", bootstrap_token=TOKEN
        ),
    )
    app = build_app(settings)
    app.state.model.migrate()
    await app.state.model.create_workspace(
        VmSpec(
            workspace_id="ws-alice",
            kernel=Path("/k"),
            rootfs=Path("/r"),
            egress_mode=MODE_INTERACTIVE,
        ),
        owner_uid=ALICE,
    )
    hub = app.state.hub
    alice_q = hub.subscribe(viewer=ALICE)
    bob_q = hub.subscribe(viewer=BOB)
    bypass_q = hub.subscribe()
    app.state.deciders.register(1, "ws-alice")
    try:
        future = await app.state.consent.hold("ws-alice", "api.example", 443)
        assert not future.done()
        await asyncio.sleep(0.05)  # let the fire-and-forget publish land
        prompt = json.loads(alice_q.get_nowait())
        assert prompt["event"] == "egress.request"
        verdict = await app.state.consent.resolve(
            prompt["data"]["request"]["id"],
            DECISION_ALLOWED,
            "token",
            "5m",
        )
        assert verdict["decision"] == "allow"
        await asyncio.sleep(0.05)

        def drained(queue: asyncio.Queue) -> list[tuple[str, str]]:
            frames = []
            while not queue.empty():
                frame = json.loads(queue.get_nowait())
                frames.append((frame["event"], frame["data"]["workspace_id"]))
            return frames

        hold_frames = [
            ("egress.resolved", "ws-alice"),
            ("egress.rules", "ws-alice"),
        ]
        assert drained(alice_q) == hold_frames
        assert drained(bob_q) == []
        assert drained(bypass_q) == [
            ("egress.request", "ws-alice"),
            *hold_frames,
        ]
    finally:
        await app.state.model.close()
