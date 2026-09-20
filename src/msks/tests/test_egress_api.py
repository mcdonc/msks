"""The egress consent REST + WSS surface (#69)."""

import asyncio
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from msks.app import build_app
from msks.microvm import VmSpec
from msks.server import api as api_mod
from msks.server.api import build_api
from msks.settings import NetSettings, ServerSettings, Settings, VmmSettings
from test_api import TOKEN, StubMicrovm, auth


@pytest.fixture
async def consent_client(tmp_path: Path):
    settings = Settings(
        vmm=VmmSettings(state_dir=tmp_path / "vms"),
        net=NetSettings(enabled=False, consent_timeout_s=5.0),
        server=ServerSettings(
            db_path=tmp_path / "consent.db",
            bootstrap_token=TOKEN,
            event_poll_s=10.0,
        ),
    )
    app = build_app(settings)
    stub = StubMicrovm()
    app.state.microvm = stub
    api = build_api(app)
    async with api.router.lifespan_context(api):
        yield api, app, stub


async def create_workspace(
    http,
    workspace_id: str,
    mode: str | None,
    allowlist: list[str] | None = None,
) -> dict:
    body = {"id": workspace_id, "kernel": "/k", "rootfs": "/r"}
    if mode is not None:
        body["egress_mode"] = mode
    if allowlist is not None:
        body["egress_allowlist"] = allowlist
    reply = await http.post("/api/v1/workspaces", json=body, headers=auth())
    assert reply.status_code == 201, reply.text
    return reply.json()


async def test_create_records_mode_and_allowlist(consent_client) -> None:
    import httpx

    api, app, _stub = consent_client
    transport = httpx.ASGITransport(app=api)
    async with httpx.AsyncClient(
        transport=transport, base_url="https://test"
    ) as http:
        row = await create_workspace(
            http,
            "ws-gated",
            "interactive",
            [".debian.org", "10.0.0.0/8:443"],
        )
        assert row["egress_mode"] == "interactive"
        assert row["egress_allowlist"] == [".debian.org", "10.0.0.0/8:443"]
        seen = await app.state.model.get_workspace("ws-gated")
        assert seen["egress_allowlist"] == [".debian.org", "10.0.0.0/8:443"]


async def test_create_refuses_unknown_mode_and_bad_specs(
    consent_client,
) -> None:
    import httpx

    api, _app, _stub = consent_client
    transport = httpx.ASGITransport(app=api)
    async with httpx.AsyncClient(
        transport=transport, base_url="https://test"
    ) as http:
        reply = await http.post(
            "/api/v1/workspaces",
            json={
                "id": "ws-bad",
                "kernel": "/k",
                "rootfs": "/r",
                "egress_mode": "permissive",
            },
            headers=auth(),
        )
        assert reply.status_code == 400
        assert "permissive" in reply.json()["detail"]
        reply = await http.post(
            "/api/v1/workspaces",
            json={
                "id": "ws-bad2",
                "kernel": "/k",
                "rootfs": "/r",
                "egress_allowlist": ["two words"],
            },
            headers=auth(),
        )
        assert reply.status_code == 400
        assert "two words" in reply.json()["detail"]


async def test_decide_and_revoke_endpoints(consent_client) -> None:
    import httpx

    api, app, _stub = consent_client
    transport = httpx.ASGITransport(app=api)
    async with httpx.AsyncClient(
        transport=transport, base_url="https://test"
    ) as http:
        await create_workspace(http, "ws-dec", "interactive")
        engine = app.state.consent
        app.state.deciders.register(1, "ws-dec")
        future = await engine.hold("ws-dec", "api.example", 443)
        rows = await app.state.model.egress_consent.list_requests("ws-dec")
        request_id = rows[0]["id"]
        # An unknown workspace 404s first.
        reply = await http.post(
            f"/api/v1/workspaces/nope/egress/requests/{request_id}",
            json={"decision": "allow", "duration": "5m"},
            headers=auth(),
        )
        assert reply.status_code == 404
        reply = await http.post(
            f"/api/v1/workspaces/ws-dec/egress/requests/{request_id}",
            json={"decision": "allow", "duration": "5m"},
            headers=auth(),
        )
        assert reply.status_code == 200
        assert reply.json()["verdict"]["decision"] == "allow"
        assert (await asyncio_wait(future))["duration"] == "5m"
        # The rules view shows the in-effect verdict.
        reply = await http.get(
            "/api/v1/workspaces/ws-dec/egress", headers=auth()
        )
        assert reply.status_code == 200
        assert reply.json()["allowed"][0]["dest_host"] == "api.example"
        # Revoke; the idempotent second revoke still 200s.
        for _ in range(2):
            reply = await http.delete(
                f"/api/v1/workspaces/ws-dec/egress/requests/{request_id}",
                headers=auth(),
            )
            assert reply.status_code == 200
        # A pending-but-not-held row answers 404 (nothing to decide).
        orphan = await app.state.model.egress_consent.create_request(
            "ws-dec", "orphan.example", 443
        )
        reply = await http.post(
            f"/api/v1/workspaces/ws-dec/egress/requests/{orphan['id']}",
            json={"decision": "deny", "duration": "once"},
            headers=auth(),
        )
        assert reply.status_code == 404
        # Revoke of a pending row refuses.
        reply = await http.delete(
            f"/api/v1/workspaces/ws-dec/egress/requests/{orphan['id']}",
            headers=auth(),
        )
        assert reply.status_code == 404


async def asyncio_wait(future, timeout: float = 2.0):
    import asyncio

    return await asyncio.wait_for(future, timeout)


async def test_requests_listing_filters_by_decision(consent_client) -> None:
    import httpx

    api, app, _stub = consent_client
    transport = httpx.ASGITransport(app=api)
    async with httpx.AsyncClient(
        transport=transport, base_url="https://test"
    ) as http:
        await create_workspace(http, "ws-list", "static")
        await app.state.consent.hold("ws-list", "off.example", 80)
        reply = await http.get(
            "/api/v1/workspaces/ws-list/egress/requests",
            headers=auth(),
        )
        assert [r["decision"] for r in reply.json()] == ["denied"]
        reply = await http.get(
            "/api/v1/workspaces/ws-list/egress/requests?decision=pending",
            headers=auth(),
        )
        assert reply.json() == []
        reply = await http.get(
            "/api/v1/workspaces/ws-list/egress/requests?decision=wat",
            headers=auth(),
        )
        assert reply.status_code == 400


def test_events_decider_registration_and_frames(tmp_path: Path) -> None:
    """A decider frame registers; the socket gets the snapshot and
    the live request fanout; closing the socket deregisters."""
    import asyncio
    import threading

    settings = Settings(
        vmm=VmmSettings(state_dir=tmp_path / "vms"),
        net=NetSettings(enabled=False, consent_timeout_s=5.0),
        server=ServerSettings(
            db_path=tmp_path / "ev.db",
            bootstrap_token=TOKEN,
            event_poll_s=10.0,
        ),
    )
    app = build_app(settings)
    stub = StubMicrovm()
    app.state.microvm = stub
    api = build_api(app)

    def hold_off_thread() -> None:
        """Create + resolve a hold on a private loop (the engine's
        publish is fire-and-forget, so frames cross into the app's
        hub from any thread)."""

        async def run() -> None:
            engine = app.state.consent
            future = await engine.hold("ws-ev", "live.example", 443)
            assert not future.done()
            await asyncio.sleep(0.2)

        asyncio.run(run())

    with TestClient(api) as client:
        reply = client.post(
            "/api/v1/workspaces",
            json={
                "id": "ws-ev",
                "kernel": "/k",
                "rootfs": "/r",
                "egress_mode": "interactive",
            },
            headers=auth(),
        )
        assert reply.status_code == 201

        with client.websocket_connect(f"/api/v1/events?token={TOKEN}") as ws:
            # Registration: the pending snapshot (empty) then the
            # rules view.
            ws.send_text(
                json.dumps({"type": "egress.decider", "workspace": "ws-ev"})
            )
            rules = json.loads(ws.receive_text())
            assert rules["event"] == "egress.rules"
            assert rules["data"]["mode"] == "interactive"
            # An unknown workspace is rejected (not silently
            # ignored): drain that frame here so the live-hold read
            # below sees the request, not the rejection.
            ws.send_text(
                json.dumps({"type": "egress.decider", "workspace": "ghost"})
            )
            assert (
                json.loads(ws.receive_text())["event"]
                == "egress.decider_rejected"
            )
            ws.send_text("not json")
            ws.send_text(json.dumps({"type": "other"}))
            # A live hold fans out to every events socket.
            thread = threading.Thread(target=hold_off_thread)
            thread.start()
            frame = json.loads(ws.receive_text())
            assert frame["event"] == "egress.request"
            assert frame["data"]["request"]["dest_host"] == "live.example"
            thread.join(5.0)
        # The socket closed: the decider registration ended with it.
        assert not app.state.deciders.has_decider("ws-ev")


def test_decider_loop_stops_on_disconnect(tmp_path: Path) -> None:
    from msks.server.api import decider_loop
    from starlette.websockets import WebSocketDisconnect

    class Closing:
        async def receive_text(self):
            raise WebSocketDisconnect(code=1000)

    import asyncio

    assert asyncio.run(decider_loop(None, Closing(), 1)) is None


def test_decider_frames_reach_the_snapshot_and_ignore_junk(
    tmp_path: Path,
) -> None:
    """The register path's every arm: a pending hold lands on the
    socket at registration, and junk frames never crash the loop."""
    import threading

    settings = Settings(
        vmm=VmmSettings(state_dir=tmp_path / "vms"),
        net=NetSettings(enabled=False, consent_timeout_s=5.0),
        server=ServerSettings(
            db_path=tmp_path / "snap.db",
            bootstrap_token=TOKEN,
            event_poll_s=10.0,
        ),
    )
    app = build_app(settings)
    app.state.microvm = StubMicrovm()
    api = build_api(app)

    def hold_off_thread() -> None:
        import asyncio

        async def run() -> None:
            engine = app.state.consent
            app.state.deciders.register(99, "ws-snap")
            await engine.hold("ws-snap", "held.example", 443)
            # Leave the hold registered long enough for the test's
            # socket to register and receive the snapshot under
            # parallel-suite load.
            await asyncio.sleep(2.0)
            # Fail the hold closed before the loop ends: its timeout
            # task must not be torn down mid-sleep by run()'s exit.
            rows = await app.state.model.egress_consent.list_requests(
                "ws-snap", decision="pending"
            )
            for row in rows:
                await engine.fail_close(row["id"], reason="thread-done")
            app.state.deciders.deregister(99)

        asyncio.run(run())

    with TestClient(api) as client:
        reply = client.post(
            "/api/v1/workspaces",
            json={
                "id": "ws-snap",
                "kernel": "/k",
                "rootfs": "/r",
                "egress_mode": "interactive",
            },
            headers=auth(),
        )
        assert reply.status_code == 201
        # Seed a pending hold through another decider's registration.
        thread = threading.Thread(target=hold_off_thread)
        thread.start()
        import time as _t

        for _ in range(200):
            if app.state.deciders.has_decider("ws-snap"):
                break
            _t.sleep(0.01)
        with client.websocket_connect(f"/api/v1/events?token={TOKEN}") as ws:
            ws.send_text(
                json.dumps({"type": "egress.decider", "workspace": "ws-snap"})
            )
            # The snapshot replays the other decider's pending hold…
            # then the rules view (the hold's hub fanout may also
            # arrive around them — read until the rules frame).
            frames = []
            for _ in range(5):
                frame = json.loads(ws.receive_text())
                frames.append(frame)
                if frame["event"] == "egress.rules":
                    break
            requests = [f for f in frames if f["event"] == "egress.request"]
            assert requests
            assert (
                requests[0]["data"]["request"]["dest_host"] == "held.example"
            )
            assert frames[-1]["event"] == "egress.rules"
            # Junk arms: no workspace key, a non-string — ignored
            # without closing the socket; an unknown workspace is
            # told it was rejected (a typo'd decider must not wait
            # on a silent, promptless connection).
            ws.send_text(json.dumps({"type": "egress.decider"}))
            ws.send_text(
                json.dumps({"type": "egress.decider", "workspace": 1234})
            )
            ws.send_text(
                json.dumps({"type": "egress.decider", "workspace": "ghost"})
            )
            rejected = json.loads(ws.receive_text())
            assert rejected["event"] == "egress.decider_rejected"
            assert rejected["data"]["reason"] == "unknown workspace"
        thread.join(5.0)


async def test_gated_exchange_records_without_learning() -> None:
    """The record path with a live upstream: nothing pins, the
    naming memory still learns."""
    import socket

    from msks.net import dns

    upstream = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    upstream.bind(("127.0.0.1", 0))
    upstream.setblocking(False)
    gate = RecordingGate()
    forwarder = dns.DnsForwarder(
        upstream.getsockname(),
        1.0,
        bind=("127.0.0.1", 0),
        client_ip="127.0.0.1",
        gate=gate,
    )
    await forwarder.start()
    client = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    client.bind(("127.0.0.1", 0))
    client.settimeout(2.0)
    serve = asyncio_create(forwarder.serve())

    async def answer():
        loop = asyncio.get_running_loop()
        data, peer = await loop.sock_recvfrom(upstream, 65535)
        parsed = parse_query_of(data)
        upstream.sendto(answer_of(parsed, "203.0.113.9"), peer)

    try:
        relay = asyncio_create(answer())
        await asyncio.to_thread(
            client.sendto,
            query_wire("record.example"),
            forwarder._sock.getsockname(),
        )
        reply, _ = await asyncio.to_thread(client.recvfrom, 65535)
        await asyncio.wait_for(relay, 2.0)
        assert reply[2:4] == b"\x81\x80"
        assert gate.pins == []  # record: no enforcement
        assert forwarder.host_for("203.0.113.9") == "record.example"
    finally:
        serve.cancel()
        forwarder.stop()
        client.close()
        upstream.close()


class RecordingGate:
    """A gate stub: everything records, nothing pins."""

    def __init__(self) -> None:
        self.pins: list[tuple] = []

    async def classify(self, qname: str):
        from msks.net.dns import RECORD, QueryDecision

        return QueryDecision(RECORD)

    async def learn(self, records, ports, cap):
        self.pins.append((records, ports, cap))


def asyncio_create(coro):
    import asyncio

    return asyncio.create_task(coro)


def parse_query_of(data: bytes):
    from msks.net import dnsmsg

    return dnsmsg.parse_query(data)


def answer_of(question, ip: str) -> bytes:
    import struct

    qname = question.wire[12:-4]  # the encoded name, minus qtype/qclass
    rdata = bytes(int(p) for p in ip.split("."))
    return (
        struct.pack("!HHHHHH", question.id, 0x8180, 1, 1, 0, 0)
        + qname
        + struct.pack("!HH", 1, 1)
        + qname
        + struct.pack("!HHIH", 1, 1, 300, len(rdata))
        + rdata
    )


def query_wire(name: str) -> bytes:
    import struct

    qname = (
        b"".join(
            bytes([len(part)]) + part.encode() for part in name.split(".")
        )
        + b"\x00"
    )
    return (
        struct.pack("!HHHHHH", 0x1234, 0x0100, 1, 0, 0, 0)
        + qname
        + struct.pack("!HH", 1, 1)
    )


async def test_register_decider_lands_the_snapshot_directly(
    tmp_path: Path,
) -> None:
    """The registration handshake, unit-shaped: the socket receives
    the pending snapshot then the rules view, sent directly (not
    through the hub)."""
    settings = Settings(
        vmm=VmmSettings(state_dir=tmp_path / "vms"),
        net=NetSettings(enabled=False),
        server=ServerSettings(
            db_path=tmp_path / "reg.db",
            bootstrap_token=TOKEN,
            event_poll_s=10.0,
        ),
    )
    app = build_app(settings)
    app.state.microvm = StubMicrovm()
    app.state.model.migrate()
    await app.state.model.create_workspace(
        VmSpec(
            workspace_id="ws-reg",
            kernel=Path("/k"),
            rootfs=Path("/r"),
            egress_mode="interactive",
        )
    )
    engine = app.state.consent
    app.state.deciders.register(7, "ws-reg")
    await engine.hold("ws-reg", "held.example", 443)

    class RecordingSocket:
        def __init__(self) -> None:
            self.sent: list[dict] = []

        async def send_json(self, payload) -> None:
            self.sent.append(payload)

    socket = RecordingSocket()
    await api_mod.register_decider(
        app,
        socket,
        client_id=1,
        message={"workspace": "ws-reg"},
    )
    events = [payload["event"] for payload in socket.sent]
    assert events == ["egress.request", "egress.rules"]
    assert socket.sent[0]["data"]["request"]["dest_host"] == "held.example"
    # An unknown workspace is told so (one rejection frame) — never
    # registered.
    empty = RecordingSocket()
    await api_mod.register_decider(
        app, empty, client_id=1, message={"workspace": "ghost"}
    )
    assert [f["event"] for f in empty.sent] == ["egress.decider_rejected"]
    assert not app.state.deciders.has_decider("ghost")


async def test_register_decider_when_rules_read_fails(tmp_path: Path) -> None:
    """The rules view is best-effort on registration: a read failure
    sends the snapshot alone."""
    settings = Settings(
        vmm=VmmSettings(state_dir=tmp_path / "vms"),
        net=NetSettings(enabled=False),
        server=ServerSettings(
            db_path=tmp_path / "rr.db",
            bootstrap_token=TOKEN,
            event_poll_s=10.0,
        ),
    )
    app = build_app(settings)
    app.state.microvm = StubMicrovm()
    app.state.model.migrate()
    await app.state.model.create_workspace(
        VmSpec(
            workspace_id="ws-rr",
            kernel=Path("/k"),
            rootfs=Path("/r"),
            egress_mode="interactive",
        )
    )

    async def nothing(_workspace_id):
        return None  # the workspace row vanished mid-registration

    app.state.consent.rules_frame = nothing

    class RecordingSocket:
        def __init__(self) -> None:
            self.sent: list[dict] = []

        async def send_json(self, payload) -> None:
            self.sent.append(payload)

    socket = RecordingSocket()
    await api_mod.register_decider(
        app, socket, client_id=1, message={"workspace": "ws-rr"}
    )
    assert socket.sent == []  # nothing held; rules skipped


async def test_decide_validates_and_binds_the_workspace(
    consent_client,
) -> None:
    """Invalid verdict fields answer a 400 (not a silent deny that
    strands the row), and a request id belonging to another
    workspace answers a 404."""
    import httpx

    api, app, _stub = consent_client
    transport = httpx.ASGITransport(app=api)
    async with httpx.AsyncClient(
        transport=transport, base_url="https://test"
    ) as http:
        await create_workspace(http, "ws-va", "interactive")
        await create_workspace(http, "ws-vb", "interactive")
        engine = app.state.consent
        app.state.deciders.register(1, "ws-va")
        future = await engine.hold("ws-va", "api.example", 443)
        row = (
            await app.state.model.egress_consent.list_requests(
                "ws-va", decision="pending"
            )
        )[0]
        # Bad decision token and bad duration both 400.
        for body in (
            {"decision": "maybe", "duration": "5m"},
            {"decision": "allow", "duration": "2h"},
        ):
            reply = await http.post(
                f"/api/v1/workspaces/ws-va/egress/requests/{row['id']}",
                json=body,
                headers=auth(),
            )
            assert reply.status_code == 400, reply.text
        # The other workspace's path answers 404 and decides
        # nothing (the hold survives).
        reply = await http.post(
            f"/api/v1/workspaces/ws-vb/egress/requests/{row['id']}",
            json={"decision": "deny", "duration": "once"},
            headers=auth(),
        )
        assert reply.status_code == 404
        assert not future.done()
        reply = await http.post(
            f"/api/v1/workspaces/ws-va/egress/requests/{row['id']}",
            json={"decision": "allow", "duration": "once"},
            headers=auth(),
        )
        assert reply.status_code == 200
        # Revoke through the wrong workspace refuses too.
        reply = await http.delete(
            f"/api/v1/workspaces/ws-vb/egress/requests/{row['id']}",
            headers=auth(),
        )
        assert reply.status_code == 404


async def test_create_defaults_the_mode_from_the_setting(
    consent_client,
) -> None:
    """MSKSD_EGRESS_MODE is the fleet default create falls back to
    when the request names no mode."""
    import httpx

    api, app, _stub = consent_client
    app.state.settings.net.egress_mode = "static"
    transport = httpx.ASGITransport(app=api)
    async with httpx.AsyncClient(
        transport=transport, base_url="https://test"
    ) as http:
        row = await create_workspace(http, "ws-fleet", None)
        assert row["egress_mode"] == "static"  # the fleet default
        # An explicit request still wins over the fleet default.
        reply = await http.post(
            "/api/v1/workspaces",
            json={
                "id": "ws-explicit",
                "kernel": "/k",
                "rootfs": "/r",
                "egress_mode": "interactive",
            },
            headers=auth(),
        )
        assert reply.json()["egress_mode"] == "interactive"


async def test_requests_limit_bounds_the_page(consent_client) -> None:
    import httpx

    api, app, _stub = consent_client
    transport = httpx.ASGITransport(app=api)
    async with httpx.AsyncClient(
        transport=transport, base_url="https://test"
    ) as http:
        await create_workspace(http, "ws-lim", "static")
        for i in range(3):
            await app.state.consent.hold("ws-lim", f"h{i}.example", 80)
        reply = await http.get(
            "/api/v1/workspaces/ws-lim/egress/requests?limit=2",
            headers=auth(),
        )
        assert len(reply.json()) == 2
