"""Events hub, WSS channel, and the status watcher."""

import asyncio
import json
from pathlib import Path

from fastapi import WebSocketDisconnect
from fastapi.testclient import TestClient
from msks.app import build_app
from msks.microvm import VmSpec
from msks.microvm.spec import VmStatus
from msks.server.api import build_api, wait_for_disconnect
from msks.server.events import EventHub, close_all, relay
from msks.server.watcher import scan_once, watch_loop
from msks.settings import ServerSettings, Settings
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
        server=ServerSettings(
            db_path=tmp_path / "ws.db", bootstrap_token=TOKEN, event_poll_s=0.05
        )
    )
    app = build_app(settings)
    stub = StubMicrovm()
    app.state.microvm = stub
    return build_api(app), app, stub


def test_websocket_receives_transitions(tmp_path: Path) -> None:
    api, app, stub = api_with_stub(tmp_path)
    with TestClient(api) as client:
        with client.websocket_connect("/api/v1/events") as socket:
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
        assert (await app.state.model.get_workspace("ws-s"))["status"] == "stopped"
        assert (await app.state.model.get_workspace("ws-t"))["status"] == "running"
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
