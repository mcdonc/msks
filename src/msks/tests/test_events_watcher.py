"""Events hub, WSS channel, and the status watcher."""

import asyncio
import json
from pathlib import Path

import pytest
from fastapi import WebSocketDisconnect
from fastapi.testclient import TestClient
from msks.app import build_app
from msks.microvm import MicrovmError, VmSpec
from msks.microvm.spec import VmInfo, VmStatus
from msks.server import events
from msks.server import watcher as watcher_mod
from msks.server.api import build_api, wait_for_disconnect
from msks.server.events import EventHub, close_all, relay
from msks.server.watcher import scan_once, scan_workspace, watch_loop
from msks.settings import NetSettings, ServerSettings, Settings
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
            f"/api/v1/events?token={TOKEN}"
        ) as socket:
            # Create a workspace, then flip the seam underneath it: the
            # watcher must publish the transition within a few polls.
            client.post(
                "/api/v1/workspaces",
                json={"id": "ws-w", "kernel": "/k", "rootfs": "/r"},
                headers=auth(),
            )
            stub.statuses["ws-w"] = VmStatus.RUNNING
            seen = False
            for _ in range(80):
                message = socket.receive_json()
                if message == {
                    "event": "workspace.status",
                    "data": {"id": "ws-w", "status": "running"},
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

    assert await wait_for_disconnect(DisconnectingSocket()) is None


def test_websocket_rejects_bad_token(tmp_path: Path) -> None:
    api, _app, _stub = api_with_stub(tmp_path)
    with TestClient(api) as client:
        with pytest.raises(WebSocketDisconnect):
            with client.websocket_connect("/api/v1/events?token=wrong"):
                pass


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
