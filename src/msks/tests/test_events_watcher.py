"""Events hub, WSS channel, and the status watcher."""

import asyncio
import json
import time
from pathlib import Path

import pytest
from fastapi import WebSocketDisconnect
from fastapi.testclient import TestClient
from msks.app import build_app
from msks.microvm import MicrovmError, VmSpec
from msks.microvm.spec import VmInfo, VmStatus
from msks.server import events
from msks.server import watcher as watcher_mod
from msks.server.api import build_api, decider_loop
from msks.server.events import EventHub, close_all, relay
from msks.server.watcher import scan_once, scan_workspace, watch_loop
from msks.settings import NetSettings, ServerSettings, Settings, VmmSettings
from test_api import TOKEN, StubMicrovm, auth


async def test_hub_pubsub() -> None:
    hub = EventHub()
    queue = hub.subscribe()
    await hub.publish("workspace.status", {"id": "a", "status": "running"})
    other = hub.subscribe()
    await hub.publish("x", {})
    assert json.loads(queue.get_nowait())["event"] == "workspace.status"
    assert not other.empty()
    hub.unsubscribe(other)
    await hub.publish("y", {})
    # "x" was delivered while subscribed; "y" must not arrive after.
    assert other.qsize() == 1
    assert not queue.empty()
    hub.unsubscribe(queue)


async def test_relay_and_close_all() -> None:
    hub = EventHub()
    queue = hub.subscribe()
    sent: list = []

    async def send(message: dict) -> None:
        sent.append(message)

    task = asyncio.create_task(relay(queue, send))
    await hub.publish("e", {"n": 1})
    close_all(hub)
    await asyncio.wait_for(task, 1)
    assert sent[0]["text"] == json.dumps({"event": "e", "data": {"n": 1}})


def api_with_stub(tmp_path: Path):
    settings = Settings(
        net=NetSettings(enabled=False),
        server=ServerSettings(
            db_path=tmp_path / "ws.db",
            bootstrap_token=TOKEN,
            event_poll_s=0.05,
        ),
    )
    app = build_app(settings)
    stub = StubMicrovm()
    app.state.microvm = stub
    return build_api(app), app, stub


def test_websocket_receives_transitions(tmp_path: Path) -> None:
    api, app, stub = api_with_stub(tmp_path)
    with TestClient(api) as client:
        with client.websocket_connect(
            "/api/v1/events", subprotocols=["bearer", TOKEN]
        ) as socket:
            # Create a workspace, then flip the seam underneath it: the
            # watcher must publish the transition within a few polls.
            created = client.post(
                "/api/v1/workspaces",
                json={"id": "ws-w", "kernel": "/k", "rootfs": "/r"},
                headers=auth(),
            )
            wid = created.json()["id"]
            stub.statuses[wid] = VmStatus.RUNNING
            seen = False
            for _ in range(80):
                message = socket.receive_json()
                if message == {
                    "event": "workspace.status",
                    "data": {"id": wid, "status": "running"},
                }:
                    seen = True
                    break
            assert seen


async def test_scan_once_publishes_transition(tmp_path: Path) -> None:
    api, app, stub = api_with_stub(tmp_path)
    async with api.router.lifespan_context(api):
        await app.state.model.create_workspace(
            VmSpec(workspace_id="ws-s", kernel=Path("/k"), rootfs=Path("/r"))
        )
        await app.state.model.create_workspace(
            VmSpec(workspace_id="ws-t", kernel=Path("/k"), rootfs=Path("/r"))
        )
        stub.statuses["ws-s"] = VmStatus.STOPPED
        stub.statuses["ws-t"] = VmStatus.RUNNING
        hub = api.state.hub
        published = await scan_once(app, hub)
        assert published == 2
        assert (await app.state.model.get_workspace("ws-s"))[
            "status"
        ] == "stopped"
        assert (await app.state.model.get_workspace("ws-t"))[
            "status"
        ] == "running"
        assert await scan_once(app, hub) == 0


async def test_watch_loop_cancellable(tmp_path: Path) -> None:
    api, app, stub = api_with_stub(tmp_path)
    async with api.router.lifespan_context(api):
        task = asyncio.create_task(watch_loop(app, api.state.hub))
        await asyncio.sleep(0.2)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        else:
            raise AssertionError("watch_loop ignored cancellation")


async def test_wait_for_disconnect_returns_on_disconnect() -> None:
    class DisconnectingSocket:
        async def receive(self):
            raise WebSocketDisconnect(code=1000)

        async def receive_text(self):
            raise WebSocketDisconnect(code=1000)

    assert await decider_loop(None, DisconnectingSocket(), 1) is None


def test_websocket_rejects_bad_token(tmp_path: Path) -> None:
    # The bad-token shape changed with #116: the daemon accepts bare
    # and closes 4401 (the close-code contract the clients' refused
    # handling keys on) instead of rejecting the HTTP upgrade.
    api, _app, _stub = api_with_stub(tmp_path)
    with TestClient(api) as client:
        with client.websocket_connect(
            "/api/v1/events", subprotocols=["bearer", "wrong"]
        ) as ws:
            with pytest.raises(WebSocketDisconnect) as caught:
                ws.receive_text()
        assert caught.value.code == 4401


def test_websocket_rejects_a_lone_auth_subprotocol(
    tmp_path: Path,
) -> None:
    # The offer is ["bearer", <token>] (#116): the name without a
    # token after it authenticates nothing.
    api, _app, _stub = api_with_stub(tmp_path)
    with TestClient(api) as client:
        with client.websocket_connect(
            "/api/v1/events", subprotocols=["bearer"]
        ) as ws:
            with pytest.raises(WebSocketDisconnect) as caught:
                ws.receive_text()
        assert caught.value.code == 4401


async def test_watch_loop_survives_scan_errors(tmp_path: Path) -> None:
    api, app, stub = api_with_stub(tmp_path)
    async with api.router.lifespan_context(api):

        class FlakyInfo:
            def __init__(self) -> None:
                self.calls = 0

            async def info(self, workspace_id: str) -> VmInfo:
                self.calls += 1
                if self.calls == 1:
                    raise MicrovmError("transient")
                return VmInfo(workspace_id, VmStatus.ABSENT)

        await app.state.model.create_workspace(
            VmSpec(workspace_id="ws-f", kernel=Path("/k"), rootfs=Path("/r"))
        )
        flaky = FlakyInfo()
        app.state.microvm = flaky
        task = asyncio.create_task(watch_loop(app, api.state.hub))
        await asyncio.sleep(0.2)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        assert flaky.calls >= 2


def test_deliver_drops_oldest_when_full() -> None:
    queue: asyncio.Queue = asyncio.Queue(maxsize=1)
    events.deliver(queue, "one")
    events.deliver(queue, "two")
    assert queue.get_nowait() == "two"


async def test_scan_workspace_reports_probe_failure(tmp_path: Path) -> None:
    api, app, _stub = api_with_stub(tmp_path)
    async with api.router.lifespan_context(api):
        row = await app.state.model.create_workspace(
            VmSpec(workspace_id="ws-p", kernel=Path("/k"), rootfs=Path("/r"))
        )

        class Broken:
            async def info(self, workspace_id: str) -> VmInfo:
                raise MicrovmError("probe failed")

        app.state.microvm = Broken()
        assert await scan_workspace(app, api.state.hub, row) is False


async def test_scan_skips_absent_over_created(tmp_path: Path) -> None:
    api, app, stub = api_with_stub(tmp_path)
    async with api.router.lifespan_context(api):
        row = await app.state.model.create_workspace(
            VmSpec(workspace_id="ws-c", kernel=Path("/k"), rootfs=Path("/r"))
        )
        assert await scan_workspace(app, api.state.hub, row) is False
        assert (await app.state.model.get_workspace("ws-c"))[
            "status"
        ] == "created"


async def test_scan_publishes_when_row_vanishes(tmp_path: Path) -> None:
    api, app, stub = api_with_stub(tmp_path)
    async with api.router.lifespan_context(api):
        row = await app.state.model.create_workspace(
            VmSpec(workspace_id="ws-v", kernel=Path("/k"), rootfs=Path("/r"))
        )
        await app.state.model.delete_workspace("ws-v")
        stub.statuses["ws-v"] = VmStatus.RUNNING
        assert await scan_workspace(app, api.state.hub, row) is False


async def test_watch_loop_logs_scan_failure(tmp_path: Path) -> None:
    api, app, _stub = api_with_stub(tmp_path)
    async with api.router.lifespan_context(api):

        async def exploding(app, hub):
            raise RuntimeError("scan exploded")

        monkey_target = watcher_mod.scan_once
        watcher_mod.scan_once = exploding
        task = asyncio.create_task(watch_loop(app, api.state.hub))
        await asyncio.sleep(0.15)
        task.cancel()
        watcher_mod.scan_once = monkey_target
        try:
            await task
        except asyncio.CancelledError:
            pass


async def test_scan_storage_publishes_pressure_changes(tmp_path: Path) -> None:
    """The state-disk probe is edge-triggered (#184): a pressure
    change publishes once, a steady state publishes nothing, and the
    floor can move the pressure back the other way."""
    from msks.server.watcher import scan_storage

    settings = Settings(
        vmm=VmmSettings(state_dir=tmp_path),
        net=NetSettings(enabled=False),
        server=ServerSettings(
            db_path=tmp_path / "pressure.db",
            bootstrap_token=TOKEN,
            event_poll_s=10.0,
        ),
    )
    app = build_app(settings)
    hub = EventHub()
    queue = hub.subscribe()
    # tmp_path sits on a real filesystem with plenty free: warn past
    # any percentage once the warn line is 1.
    app.state.settings.vmm.storage_warn_pct = 1
    assert await scan_storage(app, hub) is True
    message = json.loads(queue.get_nowait())
    assert message["event"] == "storage.pressure"
    assert message["data"]["pressure"] == "warn"
    assert message["data"]["free"] > 0
    # Steady state: no second publish.
    assert await scan_storage(app, hub) is False
    assert queue.empty()
    # An absurd floor drags it to critical — one more publish. (A
    # mere terabyte is not absurd enough: the host tmp filesystem
    # can carry more free than that.)
    app.state.settings.vmm.storage_floor_mib = (1 << 50) // (1024 * 1024)
    assert await scan_storage(app, hub) is True
    assert json.loads(queue.get_nowait())["data"]["pressure"] == "critical"


async def test_scan_storage_publishes_a_baseline(tmp_path: Path) -> None:
    """A fresh daemon announces its first known pressure (ok counts):
    an operator watching the event stream sees the daemon's starting
    condition without waiting for a transition."""
    from msks.server.watcher import scan_storage

    settings = Settings(
        vmm=VmmSettings(state_dir=tmp_path),
        net=NetSettings(enabled=False),
        server=ServerSettings(
            db_path=tmp_path / "baseline.db",
            bootstrap_token=TOKEN,
            event_poll_s=10.0,
        ),
    )
    app = build_app(settings)
    hub = EventHub()
    queue = hub.subscribe()
    assert await scan_storage(app, hub) is True
    message = json.loads(queue.get_nowait())
    assert message["event"] == "storage.pressure"
    assert message["data"]["pressure"] in ("ok", "warn")


async def test_resize_publishes_the_event(tmp_path: Path) -> None:
    """A completed resize announces workspace.resized with the new
    sizes and a non-empty change list (#187 review: the payload was
    unpinned)."""
    import httpx

    api, app, _stub = api_with_stub(tmp_path)
    async with api.router.lifespan_context(api):
        import subprocess as sp

        volume = app.state.settings.vmm.state_dir / "volumes" / "ws-evt.ext4"
        volume.parent.mkdir(parents=True, exist_ok=True)
        with volume.open("wb") as handle:
            handle.truncate(64 * 1024 * 1024)
        sp.run(
            ["mkfs.ext4", "-q", "-F", "-L", "msks-home", str(volume)],
            check=True,
        )
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=api), base_url="https://t"
        ) as http:
            created = await http.post(
                "/api/v1/workspaces",
                json={
                    "id": "ws-evt",
                    "kernel": "/k",
                    "rootfs": "/r",
                    "home_mib": 64,
                },
                headers=auth(),
            )
            assert created.status_code == 201
            queue = api.state.hub.subscribe()
            try:
                resized = await http.post(
                    "/api/v1/workspaces/ws-evt/resize",
                    json={"home_mib": 128, "cpus": 4},
                    headers=auth(),
                )
                assert resized.status_code == 200
                published = None
                for _ in range(20):
                    message = json.loads(queue.get_nowait())
                    if message["event"] == "workspace.resized":
                        published = message
                        break
            finally:
                api.state.hub.unsubscribe(queue)
    assert published is not None
    assert published["data"]["home_mib"] == 128
    assert published["data"]["cpus"] == 4
    assert published["data"]["changes"]


async def test_watch_loop_sweeps_consent_on_its_deadline(
    tmp_path: Path,
) -> None:
    """The retention sweep rides the watch loop on its own wall
    clock (hourly); a deadline already past sweeps on the first
    pass."""
    api, app, _stub = api_with_stub(tmp_path)
    app.state.settings.server.event_poll_s = 0.01
    watcher_mod.PRUNE_INTERVAL_S = -1.0  # the deadline is always due
    swept = []

    async def fake_prune(now=None):
        swept.append(True)
        return 2

    app.state.model.egress_consent.prune = fake_prune
    async with api.router.lifespan_context(api):
        # Poll until the sweep lands instead of sleeping out a
        # fixed second: the loop ticks every 10ms, so a healthy
        # run finishes in tens of ms — the 1s bound is only the
        # patience a loaded CI runner needs.
        deadline = time.monotonic() + 1.0
        while not swept and time.monotonic() < deadline:
            await asyncio.sleep(0.01)
    watcher_mod.PRUNE_INTERVAL_S = 3600.0
    assert swept


async def test_sweep_consent_logs_deletions(tmp_path: Path) -> None:
    api, app, _stub = api_with_stub(tmp_path)

    async def fake_prune(now=None):
        return 3

    app.state.model.egress_consent.prune = fake_prune
    await watcher_mod.sweep_consent(app)  # logged, not raised


async def test_sweep_consent_quiet_when_nothing_pruned(tmp_path: Path) -> None:
    api, app, _stub = api_with_stub(tmp_path)

    async def fake_prune(now=None):
        return 0

    app.state.model.egress_consent.prune = fake_prune
    await watcher_mod.sweep_consent(app)


# --- placeholder expiry sweep (#198) --------------------------------------


from datetime import UTC, datetime, timedelta  # noqa: E402

from msks.secretstore import backend_ref, new_sentinel  # noqa: E402
from msks.server.watcher import sweep_expired_placeholders  # noqa: E402
from msks.settings import SecretStoreSettings  # noqa: E402


async def seed_placeholder(
    app, workspace_id: str, name: str, expires_at=None
) -> dict:
    """A placeholder row plus the manifest declaration beside it."""
    await app.state.model.create_workspace(
        VmSpec(workspace_id=workspace_id, kernel=Path("/k"), rootfs=Path("/r"))
    )
    row = await app.state.model.create_placeholder(
        workspace_id,
        name,
        new_sentinel(),
        ["api.example.com"],
        backend_ref(workspace_id, name),
        expires_at,
    )
    return row


def sweep_app(tmp_path: Path):
    """An app whose secret store sits on the real file provider."""
    settings = Settings(
        net=NetSettings(enabled=False),
        server=ServerSettings(
            db_path=tmp_path / "sweep.db",
            bootstrap_token=TOKEN,
            # A slow poll keeps the background watch loop out of the
            # manual sweep's way (the api fixture's 10.0, same reason).
            event_poll_s=10.0,
        ),
        secret_store=SecretStoreSettings(root=tmp_path / "store"),
    )
    app = build_app(settings)
    # A stubbed seam lets the watch loop's first pass finish during
    # lifespan startup (the real seam's socket probes would delay it
    # into the test body, racing the manual sweep).
    app.state.microvm = StubMicrovm()
    return build_api(app), app


async def test_sweep_retires_expired_and_keeps_live_rows(tmp_path) -> None:
    """An expired row is audited, removed, and its store value and
    manifest declaration cleaned; a live row and an unbounded row
    survive."""
    api, app = sweep_app(tmp_path)
    async with api.router.lifespan_context(api):
        # The watch loop's first pass can interleave with the test
        # body at any await; these tests drive the sweep by hand, so
        # the background task is cancelled up front.
        api.state.watcher.cancel()
        past = await seed_placeholder(
            app, "ws-a", "expired", datetime.now(UTC) - timedelta(seconds=1)
        )
        live = await seed_placeholder(
            app, "ws-b", "live", datetime.now(UTC) + timedelta(hours=1)
        )
        forever = await seed_placeholder(app, "ws-c", "forever")
        refs = await app.state.model.placeholder_refs()
        app.state.secrets.sync_manifest(refs)
        for row in (past, live, forever):
            await app.state.secrets.write(row["backend_ref"], "value")
        hub = api.state.hub
        queue = hub.subscribe()
        swept = await sweep_expired_placeholders(app, hub)
        assert swept == 1
        rows = await app.state.model.list_placeholders()
        assert {row["name"] for row in rows} == {"live", "forever"}
        audit = await app.state.model.list_audit()
        assert [event["kind"] for event in audit] == ["expiry"]
        assert audit[0]["name"] == "expired"
        assert audit[0]["dests"] == ["api.example.com"]
        manifest = (tmp_path / "store" / "secretspec.toml").read_text()
        assert "MSKSWS_WS_A_EXPIRED" not in manifest
        assert "MSKSWS_WS_B_LIVE" in manifest
        stored = tmp_path / "store" / "msks" / "default"
        assert not (stored / "MSKSWS_WS_A_EXPIRED").exists()
        assert (stored / "MSKSWS_WS_B_LIVE").exists()
        event = json.loads(queue.get_nowait())
        assert event["event"] == "secret.expiry"
        assert event["data"]["workspace_id"] == "ws-a"
        assert event["data"]["name"] == "expired"
        assert event["data"]["placeholder_id"] == past["id"]
        assert event["data"]["ts"] > 0.0


async def test_sweep_survives_a_failing_store_delete(tmp_path) -> None:
    """The row still retires when the store cannot clean up: the
    leftover value is inert, and the sweep still re-syncs the
    manifest."""
    api, app = sweep_app(tmp_path)
    async with api.router.lifespan_context(api):
        # The watch loop's first pass can interleave with the test
        # body at any await; these tests drive the sweep by hand, so
        # the background task is cancelled up front.
        api.state.watcher.cancel()
        await seed_placeholder(
            app, "ws-a", "expired", datetime.now(UTC) - timedelta(seconds=1)
        )
        refs = await app.state.model.placeholder_refs()
        app.state.secrets.sync_manifest(refs)
        app.state.settings.secret_store.cli = "/nonexistent/secretspec"
        swept = await sweep_expired_placeholders(app, api.state.hub)
        assert swept == 1
        assert await app.state.model.list_placeholders() == []
        audit = await app.state.model.list_audit()
        assert audit[0]["kind"] == "expiry"


async def test_placeholder_predicate_honors_expiry(tmp_path) -> None:
    """The per-request predicate: present-and-unexpired swaps; a
    past deadline does not (the naive-UTC round-trip included)."""
    api, app = sweep_app(tmp_path)
    async with api.router.lifespan_context(api):
        # The watch loop's first pass can interleave with the test
        # body at any await; these tests drive the sweep by hand, so
        # the background task is cancelled up front.
        api.state.watcher.cancel()
        past = await seed_placeholder(
            app, "ws-a", "expired", datetime.now(UTC) - timedelta(hours=1)
        )
        live = await seed_placeholder(
            app, "ws-b", "live", datetime.now(UTC) + timedelta(hours=1)
        )
        forever = await seed_placeholder(app, "ws-c", "forever")
        model = app.state.model
        assert await model.placeholder_valid(past["sentinel"]) is False
        assert await model.placeholder_valid(live["sentinel"]) is True
        assert await model.placeholder_valid(forever["sentinel"]) is True
        assert await model.placeholder_valid("mskssec1_unknown") is False


async def test_record_audit_rejects_unknown_kinds(tmp_path) -> None:
    api, app = sweep_app(tmp_path)
    async with api.router.lifespan_context(api):
        # The watch loop's first pass can interleave with the test
        # body at any await; these tests drive the sweep by hand, so
        # the background task is cancelled up front.
        api.state.watcher.cancel()
        row = await seed_placeholder(app, "ws-a", "x")
        with pytest.raises(ValueError, match="unknown audit kind"):
            await app.state.model.record_audit("leak", row)


async def test_list_workspace_audit_scopes_orders_and_bounds(
    tmp_path,
) -> None:
    """One workspace's audit rows, oldest first, bounded to the
    newest (#305) — the source the decider registration replays."""
    api, app = sweep_app(tmp_path)
    async with api.router.lifespan_context(api):
        api.state.watcher.cancel()
        row_a = await seed_placeholder(app, "ws-a", "x")
        row_b = await seed_placeholder(app, "ws-b", "y")
        model = app.state.model
        await model.record_audit("mint", row_a)
        await model.record_audit("mint", row_b)
        await model.record_audit("revoke", row_a)
        await model.record_audit("expiry", row_a)
        scoped = await model.list_workspace_audit("ws-a")
        assert [row["kind"] for row in scoped] == ["mint", "revoke", "expiry"]
        assert all(row["workspace_id"] == "ws-a" for row in scoped)
        newest = await model.list_workspace_audit("ws-a", limit=2)
        assert [row["kind"] for row in newest] == ["revoke", "expiry"]


async def test_delete_unknown_placeholder_is_false(tmp_path) -> None:
    api, app = sweep_app(tmp_path)
    async with api.router.lifespan_context(api):
        api.state.watcher.cancel()
        assert await app.state.model.delete_placeholder(999) is False


async def test_sweep_caps_retirements_per_pass(tmp_path, monkeypatch) -> None:
    """A bulk expiry retires at most SWEEP_CAP rows per pass: the
    watcher's other duties keep their cadence through a slow store."""
    api, app = sweep_app(tmp_path)
    async with api.router.lifespan_context(api):
        api.state.watcher.cancel()
        monkeypatch.setattr(watcher_mod, "SWEEP_CAP", 1)
        past = datetime.now(UTC) - timedelta(seconds=1)
        await seed_placeholder(app, "ws-a", "one", past)
        await seed_placeholder(app, "ws-b", "two", past)
        first = await sweep_expired_placeholders(app, api.state.hub)
        assert first == 1
        assert len(await app.state.model.list_placeholders()) == 1
        second = await sweep_expired_placeholders(app, api.state.hub)
        assert second == 1
        assert await app.state.model.list_placeholders() == []
        kinds = [event["kind"] for event in await app.state.model.list_audit()]
        assert kinds == ["expiry", "expiry"]


async def test_sweep_refreshes_the_interceptor_once_per_workspace(
    tmp_path,
) -> None:
    """A workspace whose last live placeholder expired stands its
    redirect down (#199): one refresh per workspace, however many of
    its rows retired."""
    api, app = sweep_app(tmp_path)

    class Recorder:
        def __init__(self) -> None:
            self.refreshes: list[str] = []

        async def refresh(self, workspace_id: str) -> None:
            self.refreshes.append(workspace_id)

        async def on_detach(
            self, workspace_id: str
        ) -> None:  # pragma: no cover
            raise AssertionError("no attachment exists in the sweep fixture")

        async def stop(self) -> None:  # pragma: no cover
            raise AssertionError("lifespan teardown replaces the recorder")

    recorder = Recorder()
    real = app.state.interceptor
    app.state.interceptor = recorder
    async with api.router.lifespan_context(api):
        api.state.watcher.cancel()
        try:
            await seed_placeholder(
                app,
                "ws-a",
                "expired",
                datetime.now(UTC) - timedelta(seconds=1),
            )
            await app.state.model.create_placeholder(
                "ws-a",
                "expired2",
                new_sentinel(),
                ["api.example.com"],
                backend_ref("ws-a", "expired2"),
                datetime.now(UTC) - timedelta(seconds=1),
            )
            await sweep_expired_placeholders(app, api.state.hub)
        finally:
            app.state.interceptor = real
    assert recorder.refreshes == ["ws-a"]


async def test_refresh_disarmed_defers_one_workspace_not_its_siblings(
    tmp_path,
) -> None:
    """One workspace's failing stand-down defers only itself — the
    next interval retries it — while its sibling still stands down
    in the same pass (#260 review)."""
    api, app = sweep_app(tmp_path)

    class Recorder:
        def __init__(self) -> None:
            self.refreshes: list[str] = []
            self.refuse: set[str] = set()

        async def refresh(self, workspace_id: str) -> None:
            self.refreshes.append(workspace_id)
            if workspace_id in self.refuse:
                raise RuntimeError("nft down")

        async def on_detach(
            self, workspace_id: str
        ) -> None:  # pragma: no cover
            raise AssertionError("no attachment exists in the sweep fixture")

        async def stop(self) -> None:  # pragma: no cover
            raise AssertionError("lifespan teardown replaces the recorder")

    recorder = Recorder()
    recorder.refuse = {"ws-a"}
    real = app.state.interceptor
    app.state.interceptor = recorder
    from msks.server.watcher import refresh_disarmed

    async with api.router.lifespan_context(api):
        api.state.watcher.cancel()
        try:
            expired = [
                {"workspace_id": "ws-a"},
                {"workspace_id": "ws-b"},
            ]
            await refresh_disarmed(app, expired)
        finally:
            app.state.interceptor = real
    assert recorder.refreshes == ["ws-a", "ws-b"]
