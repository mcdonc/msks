"""The resolver gate, the naming layer, and the answer cache (#69)."""

import asyncio
import socket
import struct
from pathlib import Path

import pytest
from msks.app import build_app
from msks.consent.specs import MODE_ALLOW, MODE_INTERACTIVE, MODE_STATIC
from msks.microvm import VmSpec
from msks.model.egress_consent import DECISION_ALLOWED, DECISION_DENIED
from msks.net import dns, dnsmsg
from msks.settings import ServerSettings, Settings


def query_for(name: str, ident: int = 0xABCD, qtype: int = 1) -> bytes:
    qname = (
        b"".join(
            bytes([len(label)]) + label.encode() for label in name.split(".")
        )
        + b"\x00"
    )
    return (
        struct.pack("!HHHHHH", ident, 0x0100, 1, 0, 0, 0)
        + qname
        + struct.pack("!HH", qtype, 1)
    )


def answer_for(name: str, ip: str, ttl: int = 300, ident: int = 0xABCD):
    qname = (
        b"".join(
            bytes([len(label)]) + label.encode() for label in name.split(".")
        )
        + b"\x00"
    )
    rdata = bytes(int(p) for p in ip.split("."))
    return (
        struct.pack("!HHHHHH", ident, 0x8180, 1, 1, 0, 0)
        + qname
        + struct.pack("!HH", 1, 1)
        + qname
        + struct.pack("!HHIH", 1, 1, ttl, len(rdata))
        + rdata
    )


class RecordingNet:
    """The consent seam the gate learns and retracts through."""

    def __init__(self) -> None:
        self.pins: list[tuple] = []
        self.retractions: list[tuple] = []

    async def consent_allow(self, workspace_id, ip, port, ttl_s):
        self.pins.append((workspace_id, ip, port, ttl_s))

    async def consent_reject(self, *args):
        self.pins.append(args)

    async def retract_consent_pins(self, workspace_id, ips):
        self.retractions.append((workspace_id, tuple(ips)))


@pytest.fixture
async def gated(tmp_path: Path):
    """An app with workspaces in each mode; a forwarder pair each."""
    app = build_app(Settings(server=ServerSettings(db_path=tmp_path / "g.db")))
    app.state.model.migrate()
    for wid, mode, specs in (
        ("ws-static", MODE_STATIC, (".debian.org",)),
        ("ws-interactive", MODE_INTERACTIVE, (".allowed.example",)),
        ("ws-allow", MODE_ALLOW, (".allowed.example",)),
    ):
        await app.state.model.create_workspace(
            VmSpec(
                workspace_id=wid,
                kernel=Path("/k"),
                rootfs=Path("/r"),
                egress_mode=mode,
                egress_allowlist=specs,
            )
        )
    net = RecordingNet()
    app.state.net = net
    try:
        yield app, net
    finally:
        await app.state.model.close()


def gate_for(
    app, net, workspace_id: str, mode: str, specs=()
) -> dns.ResolverGate:
    from msks.consent.specs import EgressPolicy

    return dns.ResolverGate(
        EgressPolicy(workspace_id, mode, tuple(specs)),
        app.state.consent,
        app.state.model.egress_consent,
        net,
    )


async def classify(gate, name: str):
    return await gate.classify(name)


async def test_static_allowlist_learns(gated) -> None:
    app, net = gated
    gate = gate_for(app, net, "ws-static", MODE_STATIC, (".debian.org",))
    decision = await classify(gate, "deb.debian.org")
    assert decision.action == dns.LEARN
    assert decision.ports is None
    await gate.learn([("199.7.171.11", 300)], decision.ports, decision.cap)
    assert net.pins == [("ws-static", "199.7.171.11", None, 300.0)]


async def test_learn_caps_by_verdict_window(gated) -> None:
    app, net = gated
    gate_for(app, net, "ws-interactive", MODE_INTERACTIVE)
    decision = dns.QueryDecision(dns.LEARN, {443}, 90.0)
    gate = gate_for(app, net, "ws-interactive", MODE_INTERACTIVE)
    await gate.learn([("203.0.113.7", 300)], decision.ports, decision.cap)
    # The verdict's remaining window (90s) caps the DNS TTL (300s).
    assert net.pins == [("ws-interactive", "203.0.113.7", 443, 90.0)]


async def test_session_and_forever_gates(gated) -> None:
    app, net = gated
    engine = app.state.consent
    gate = gate_for(
        app, net, "ws-interactive", MODE_INTERACTIVE, (".allowed.example",)
    )
    # A session deny (all ports) blocks the whole name.
    engine.session.deny("ws-interactive", "denied.example", None, 60.0)
    assert (await classify(gate, "denied.example")).action == dns.NXDOMAIN
    # A session allow covers the name with its cap.
    engine.session.allow("ws-interactive", "api.example", 443, 60.0)
    decision = await classify(gate, "api.example")
    assert decision.action == dns.LEARN
    assert decision.ports == {443}
    # A portless session allow covers every port.
    engine.session.allow("ws-interactive", "wide.example", None, 60.0)
    assert (await classify(gate, "wide.example")).ports is None
    assert decision.cap is not None and decision.cap <= 60.0
    # A forever deny row blocks the name (any port).
    row = await app.state.model.egress_consent.create_request(
        "ws-interactive", "evil.example", 443
    )
    await app.state.model.egress_consent.decide(
        row["id"], DECISION_DENIED, "token", "forever"
    )
    assert (await classify(gate, "evil.example")).action == dns.NXDOMAIN
    # A forever allow row learns all ports, uncapped.
    row = await app.state.model.egress_consent.create_request(
        "ws-interactive", "good.example", 443
    )
    await app.state.model.egress_consent.decide(
        row["id"], DECISION_ALLOWED, "token", "forever"
    )
    decision = await classify(gate, "good.example")
    assert decision.action == dns.LEARN
    assert decision.ports is None and decision.cap is None
    # An allow wins over a same-name deny: allow checked first? No —
    # deny gates come first (block more is the safe direction), so a
    # name with both stays blocked.


async def test_static_off_list_nxdomains_and_records(gated) -> None:
    app, _net = gated
    gate = gate_for(app, _net, "ws-static", MODE_STATIC, (".debian.org",))
    decision = await classify(gate, "off.list.example")
    assert decision.action == dns.NXDOMAIN
    rows = await app.state.model.egress_consent.list_requests("ws-static")
    assert [r["dest_host"] for r in rows] == ["off.list.example"]
    assert rows[0]["decision"] == "denied" and rows[0]["decided_by"] is None
    # The dedup keeps the table flat under a flood.
    await classify(gate, "off.list.example")
    assert (
        len(await app.state.model.egress_consent.list_requests("ws-static"))
        == 1
    )


async def test_allow_and_interactive_record(gated) -> None:
    app, net = gated
    allow_gate = gate_for(
        app, net, "ws-allow", MODE_ALLOW, (".allowed.example",)
    )
    assert (await classify(allow_gate, "allowed.example")).action == dns.LEARN
    assert (await classify(allow_gate, "any.example")).action == dns.RECORD
    rows = await app.state.model.egress_consent.list_requests("ws-allow")
    assert [r["decision"] for r in rows] == ["allowed"]  # off-list recorded
    interactive = gate_for(
        app, net, "ws-interactive", MODE_INTERACTIVE, (".allowed.example",)
    )
    # Interactive off-list resolves (the SYN is held at the queue,
    # not the query); no row here — the hold creates it.
    assert (await classify(interactive, "fresh.example")).action == dns.RECORD
    assert (
        await app.state.model.egress_consent.list_requests("ws-interactive")
        == []
    )


async def test_forwarder_relay_with_a_gate(gated) -> None:
    """End to end through real sockets: static off-list NXDOMAINs,
    allowlisted names forward and learn, and the naming memory
    records the pairing."""
    app, net = gated
    upstream = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    upstream.bind(("127.0.0.1", 0))
    upstream.setblocking(False)
    gate = gate_for(app, net, "ws-static", MODE_STATIC, (".debian.org",))
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
    serve = asyncio.create_task(forwarder.serve())

    async def answer_once(name, ip):
        data, peer = await asyncio.wait_for(
            dns.recvfrom(asyncio.get_running_loop(), upstream, 65535), 2.0
        )
        ident = int.from_bytes(data[:2], "big")
        parsed = dnsmsg.parse_query(data)
        upstream.sendto(answer_for(parsed.name, ip, ident=ident), peer)

    async def ask(name: str) -> bytes:
        await asyncio.to_thread(
            client.sendto, query_for(name), forwarder._sock.getsockname()
        )
        reply, _addr = await asyncio.to_thread(client.recvfrom, 65535)
        return bytes(reply)

    try:
        # Off-list: NXDOMAIN, no upstream round trip.
        nxdomain = await ask("off.list.example")
        assert nxdomain[2:4] == b"\x81\x83"
        # Allowlisted: forwarded, learned, cached, named.
        relay = asyncio.create_task(
            answer_once("deb.debian.org", "199.7.171.11")
        )
        reply = await ask("deb.debian.org")
        await asyncio.wait_for(relay, 2.0)
        assert reply[2:4] == b"\x81\x80"
        assert net.pins == [("ws-static", "199.7.171.11", None, 300.0)]
        assert forwarder.host_for("199.7.171.11") == "deb.debian.org"
        assert forwarder.ips_for("deb.debian.org") == ["199.7.171.11"]
        # A repeat answers from the cache under a fresh id, without
        # the upstream seeing it.
        await asyncio.to_thread(
            client.sendto,
            query_for("deb.debian.org", ident=0x4242),
            forwarder._sock.getsockname(),
        )
        reply, _addr = await asyncio.to_thread(client.recvfrom, 65535)
        assert reply[:2] == b"\x42\x42"
        # A malformed datagram is dropped silently.
        await asyncio.to_thread(
            client.sendto, b"junkjunk", forwarder._sock.getsockname()
        )
        client.settimeout(0.2)
        with pytest.raises(TimeoutError):
            await asyncio.to_thread(client.recvfrom, 65535)
    finally:
        serve.cancel()
        forwarder.stop()
        client.close()
        upstream.close()


async def test_coresident_resolution_retracts_address_pins(gated) -> None:
    """#304, end to end through real sockets: a session-covered name
    resolves and its address pins; a SECOND name resolving to the
    same address retracts those pins (an address-keyed element
    would cover the co-resident) and pins nothing of its own."""
    app, net = gated
    app.state.consent.session.allow("ws-interactive", "a.example", None, 60.0)
    upstream = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    upstream.bind(("127.0.0.1", 0))
    upstream.setblocking(False)
    gate = gate_for(app, net, "ws-interactive", MODE_INTERACTIVE)
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
    serve = asyncio.create_task(forwarder.serve())

    async def answer_once(name: str, ip: str) -> None:
        data, peer = await asyncio.wait_for(
            dns.recvfrom(asyncio.get_running_loop(), upstream, 65535), 2.0
        )
        ident = int.from_bytes(data[:2], "big")
        parsed = dnsmsg.parse_query(data)
        upstream.sendto(answer_for(parsed.name, ip, ident=ident), peer)

    async def ask(name: str) -> None:
        await asyncio.to_thread(
            client.sendto, query_for(name), forwarder._sock.getsockname()
        )
        await asyncio.to_thread(client.recvfrom, 65535)

    try:
        relay = asyncio.create_task(answer_once("a.example", "10.2.3.4"))
        await ask("a.example")
        await asyncio.wait_for(relay, 2.0)
        assert net.pins == [
            ("ws-interactive", "10.2.3.4", None, pytest.approx(60.0, abs=5))
        ]
        assert net.retractions == []
        relay = asyncio.create_task(answer_once("b.example", "10.2.3.4"))
        await ask("b.example")
        await asyncio.wait_for(relay, 2.0)
        assert net.retractions == [("ws-interactive", ("10.2.3.4",))]
        # The co-resident's own query pinned nothing.
        assert len(net.pins) == 1
        assert forwarder.shared("10.2.3.4") is True
        assert forwarder.host_for("10.2.3.4") == "b.example"
    finally:
        serve.cancel()
        forwarder.stop()
        client.close()
        upstream.close()


async def test_naming_memory_lifecycle() -> None:
    forwarder = dns.DnsForwarder(("127.0.0.1", 53))
    forwarder.remember("a.example", [("10.0.0.1", 0)])


def test_coresident_names_keep_their_own_pairings() -> None:
    """#304: one address may carry several live names — the pairing
    list keeps them all, the most recent resolution names the
    flow, and `shared` marks the address so verdict pins honor the
    co-residency."""
    forwarder = dns.DnsForwarder(("127.0.0.1", 53))
    assert forwarder.remember("a.example", [("10.0.0.1", 300)]) == []
    assert forwarder.remember("b.example", [("10.0.0.1", 300)]) == ["10.0.0.1"]
    assert forwarder.shared("10.0.0.1") is True
    assert forwarder.host_for("10.0.0.1") == "b.example"  # most recent
    # A refresh of the first name re-names the address (and is not
    # a NEW co-residency — nothing to retract).
    assert forwarder.remember("a.example", [("10.0.0.1", 300)]) == []
    assert forwarder.host_for("10.0.0.1") == "a.example"
    assert forwarder.ips_for("a.example") == ["10.0.0.1"]
    assert forwarder.ips_for("b.example") == ["10.0.0.1"]
    # Forgetting one name leaves the other's pairing intact — and
    # the address stops reading as shared.
    forwarder.forget("b.example")
    assert forwarder.shared("10.0.0.1") is False
    assert forwarder.host_for("10.0.0.1") == "a.example"
    # An address no name resolved to never reads as shared.
    assert forwarder.shared("10.0.0.2") is False
    assert forwarder.host_for("10.0.0.1") == "a.example"  # floored TTL
    forwarder.forget("a.example")
    assert forwarder.host_for("10.0.0.1") is None
    # A re-resolve never shortens a longer memory.
    forwarder.remember("b.example", [("10.0.0.2", 300)])
    forwarder.remember("b.example", [("10.0.0.2", 0)])
    assert forwarder.ips_for("b.example") == ["10.0.0.2"]


def test_cache_only_answers_with_records() -> None:
    forwarder = dns.DnsForwarder(("127.0.0.1", 53))
    question = dnsmsg.parse_query(query_for("x.example"))
    forwarder.cache_put(question, b"reply", [])
    assert forwarder.cache_get(question) is None
    forwarder.cache_put(question, b"reply", [("10.0.0.1", 5)])
    assert forwarder.cache_get(question) == b"reply"
    # The same question under a different id hits the same entry.
    other = dnsmsg.parse_query(query_for("x.example", ident=0x4242))
    assert forwarder.cache_get(other) == b"reply"
    # Expired entries stop serving.
    forwarder._cache[question.wire[2:]] = (b"reply", 0.0)
    assert forwarder.cache_get(question) is None
    # The bound clears wholesale.
    forwarder._cache[question.wire[2:]] = (b"reply", 1e18)
    for i in range(dns.CACHE_MAX):
        forwarder._cache[str(i).encode() * 20] = (b"r", 1e18)
    forwarder.cache_put(question, b"reply2", [("10.0.0.1", 5)])
    assert len(forwarder._cache) == 1


def test_gate_without_a_key_is_the_52_relay() -> None:
    """No gate: the forwarder is the verbatim #52 relay."""
    forwarder = dns.DnsForwarder(("127.0.0.1", 53))
    assert forwarder._gate is None


async def test_gated_exchange_when_upstream_times_out(gated) -> None:
    """A gated query whose upstream never answers stays silent (the
    guest's resolver retries or fails; an empty reply would only
    confuse it)."""
    app, _net = gated
    import socket

    dead = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    dead.bind(("127.0.0.1", 0))
    dead.setblocking(False)
    gate = gate_for(app, _net, "ws-interactive", MODE_INTERACTIVE)
    forwarder = dns.DnsForwarder(
        dead.getsockname(),
        0.05,
        bind=("127.0.0.1", 0),
        client_ip="127.0.0.1",
        gate=gate,
    )
    await forwarder.start()
    client = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    client.bind(("127.0.0.1", 0))
    client.settimeout(0.2)
    serve = asyncio.create_task(forwarder.serve())
    try:
        await asyncio.to_thread(
            client.sendto,
            query_for("slow.example"),
            forwarder._sock.getsockname(),
        )
        with pytest.raises(TimeoutError):
            await asyncio.to_thread(client.recvfrom, 65535)
        # Give the relay's timeout arm its moment, then the memory
        # records nothing.
        await asyncio.sleep(0.1)
        assert forwarder.ips_for("slow.example") == []
    finally:
        serve.cancel()
        forwarder.stop()
        client.close()
        dead.close()


def test_naming_memory_bound_clears() -> None:
    """A flood of unique names cannot grow the memory without
    bound: past NAMES_MAX the dict clears wholesale (prompt naming
    degrades, enforcement never does)."""
    forwarder = dns.DnsForwarder(("127.0.0.1", 53))
    for i in range(dns.NAMES_MAX):
        forwarder.remember(
            f"h{i}.example", [(f"10.{i // 256}.{i % 256}.1", 60)]
        )
    assert len(forwarder._names) >= dns.NAMES_MAX
    forwarder.remember("overflow.example", [("10.9.9.9", 60)])
    assert forwarder._names == {
        "10.9.9.9": [("overflow.example", forwarder._names["10.9.9.9"][0][1])]
    }


async def test_a_fresh_deny_overrides_a_cached_answer(gated) -> None:
    """Classify runs before the cache: a forever deny lands and the
    next query answers NXDOMAIN even though a positive answer is
    still cached."""
    import socket

    app, net = gated
    from msks.model.egress_consent import DECISION_DENIED

    upstream = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    upstream.bind(("127.0.0.1", 0))
    upstream.setblocking(False)
    gate = gate_for(app, net, "ws-interactive", MODE_INTERACTIVE)
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
    serve = asyncio.create_task(forwarder.serve())

    async def answer_once():
        loop = asyncio.get_running_loop()
        data, peer = await loop.sock_recvfrom(upstream, 65535)
        parsed = dnsmsg.parse_query(data)
        upstream.sendto(answer_for(parsed.name, "203.0.113.7"), peer)

    async def ask(name: str) -> bytes:
        await asyncio.to_thread(
            client.sendto, query_for(name), forwarder._sock.getsockname()
        )
        reply, _addr = await asyncio.to_thread(client.recvfrom, 65535)
        return bytes(reply)

    try:
        relay = asyncio.create_task(answer_once())
        reply = await ask("cachable.example")
        await asyncio.wait_for(relay, 2.0)
        assert reply[2:4] == b"\x81\x80"
        assert forwarder.cache_get(
            dnsmsg.parse_query(query_for("cachable.example"))
        )
        # A forever deny for that name: the cached answer must lose.
        row = await app.state.model.egress_consent.create_request(
            "ws-interactive", "cachable.example", 443
        )
        await app.state.model.egress_consent.decide(
            row["id"], DECISION_DENIED, "token", "forever"
        )
        nxdomain = await ask("cachable.example")
        assert nxdomain[2:4] == b"\x81\x83"
        # And forget() (revocation) empties the answer cache.
        forwarder.forget("cachable.example")
        assert forwarder._cache == {}
    finally:
        serve.cancel()
        forwarder.stop()
        client.close()
        upstream.close()


async def test_multi_question_queries_drop_silently(gated) -> None:
    """A two-question datagram is dropped unread: no upstream
    round-trip, no answer, no pin (fail-closed)."""
    import struct

    app, net = gated
    import socket

    upstream = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    upstream.bind(("127.0.0.1", 0))
    upstream.setblocking(False)
    gate = gate_for(app, net, "ws-static", MODE_STATIC, (".debian.org",))
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
    client.settimeout(0.3)
    serve = asyncio.create_task(forwarder.serve())
    two = (
        query_for("deb.debian.org")[:6]
        + b"\x00\x02"
        + query_for("deb.debian.org")[12:]
        + query_for("evil.example")[12:]
    )
    del struct
    try:
        await asyncio.to_thread(
            client.sendto, two, forwarder._sock.getsockname()
        )
        with pytest.raises(TimeoutError):
            await asyncio.to_thread(client.recvfrom, 65535)
        assert net.pins == []
    finally:
        serve.cancel()
        forwarder.stop()
        client.close()
        upstream.close()
