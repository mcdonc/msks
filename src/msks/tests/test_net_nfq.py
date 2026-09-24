"""The NFQUEUE consumer (#69) — packet parsing, fast paths, verdicts."""

import asyncio
import struct
from pathlib import Path

import pytest
from msks.app import build_app
from msks.consent.specs import MODE_INTERACTIVE
from msks.microvm import VmSpec
from msks.microvm.errors import MicrovmError
from msks.net import nfq
from msks.settings import NetSettings, ServerSettings, Settings


def syn_packet(
    src: str = "172.31.0.1",
    sport: int = 40000,
    dst: str = "203.0.113.7",
    dport: int = 443,
    proto: int = 6,
) -> bytes:
    """A minimal queued payload: IPv4 header + L4 ports."""
    src_b = bytes(int(p) for p in src.split("."))
    dst_b = bytes(int(p) for p in dst.split("."))
    header = struct.pack(
        "!BBHHHBBH4s4s",
        0x45,
        0,
        40,
        0,
        0,
        64,
        proto,
        0,
        src_b,
        dst_b,
    )
    return header + struct.pack("!HH", sport, dport) + b"\x00" * 12


class FakePkt:
    """A queued packet with a recorded verdict."""

    def __init__(self, payload: bytes) -> None:
        self.payload = payload
        self.verdict = "held"

    def get_payload(self) -> bytes:
        return self.payload

    def accept(self) -> None:
        self.verdict = "accept"

    def drop(self) -> None:
        self.verdict = "drop"

    def retain(self) -> None:
        self.verdict = "retained"


class FakeNet:
    """The manager's consent seam, recorded."""

    def __init__(self, app) -> None:
        self.app = app
        self.allows: list[tuple] = []
        self.rejects: list[tuple] = []

    async def consent_allow(self, workspace_id, ip, port, ttl_s):
        self.allows.append((workspace_id, ip, port, ttl_s))

    async def consent_reject(self, workspace_id, ip, port, ttl_s):
        self.rejects.append((workspace_id, ip, port, ttl_s))

    def host_for(self, workspace_id, ip):
        return None


@pytest.fixture
async def consumer_app(tmp_path: Path):
    app = build_app(
        Settings(
            server=ServerSettings(db_path=tmp_path / "q.db"),
            net=NetSettings(consent_timeout_s=10.0),
        )
    )
    app.state.model.migrate()
    await app.state.model.create_workspace(
        VmSpec(
            workspace_id="ws",
            kernel=Path("/k"),
            rootfs=Path("/r"),
            egress_mode=MODE_INTERACTIVE,
        )
    )
    net = FakeNet(app)
    app.state.net = net
    try:
        yield app, net
    finally:
        await app.state.model.close()


def consumer(app, net) -> nfq.FlowConsumer:
    return nfq.FlowConsumer("ws", 1100, net)


def test_parse_packet_reads_tcp_udp_and_rejects_garbage() -> None:
    parsed = nfq.parse_packet(syn_packet())
    assert parsed == ("172.31.0.1", 40000, "203.0.113.7", 443, 6)
    udp = nfq.parse_packet(syn_packet(proto=17))
    assert udp[2] == "203.0.113.7" and udp[3] == 443
    other = nfq.parse_packet(syn_packet(proto=1))
    assert other[3] == 0 and other[4] == 1  # portless key
    assert nfq.parse_packet(b"\x00" * 8) is None
    assert nfq.parse_packet(b"\x06" * 40) is None  # version nibble


def test_parse_packet_sniffs_an_ethernet_prefix() -> None:
    eth = (b"\xaa\xbb\xcc\xdd\xee\xff" * 2 + b"\x08\x00") + syn_packet()
    assert nfq.parse_packet(eth) == nfq.parse_packet(syn_packet())


async def test_unparseable_packet_drops(consumer_app) -> None:
    app, _net = consumer_app
    flow = consumer(app, app.state.net)
    pkt = FakePkt(b"junk")
    flow.on_packet(pkt)
    assert pkt.verdict == "drop"


async def test_missing_binding_names_the_refusal(
    consumer_app, monkeypatch
) -> None:
    """The binding ships with every install, but an exotic one
    without the library must refuse the boot by name, not run an
    unanswered queue."""
    app, _net = consumer_app
    flow = consumer(app, app.state.net)
    monkeypatch.setattr(nfq, "NetfilterQueue", None)
    with pytest.raises(MicrovmError, match="netfilterqueue"):
        flow.start()


async def test_cached_verdict_reuses(consumer_app) -> None:
    app, _net = consumer_app
    flow = consumer(app, app.state.net)
    import time as time_mod

    flow._verdicts[(40000, "203.0.113.7", 443)] = (
        "allow",
        time_mod.time() + 60,
    )
    pkt = FakePkt(syn_packet())
    flow.on_packet(pkt)
    assert pkt.verdict == "accept"
    flow._verdicts[(40000, "203.0.113.7", 443)] = (
        "deny",
        time_mod.time() + 60,
    )
    denied = FakePkt(syn_packet())
    flow.on_packet(denied)
    assert denied.verdict == "drop"


async def test_expired_cache_re_prompts(consumer_app) -> None:
    app, _net = consumer_app
    flow = consumer(app, app.state.net)
    flow._verdicts[(40000, "203.0.113.7", 443)] = ("allow", 0.0)
    pkt = FakePkt(syn_packet())
    app.state.deciders.register(1, "ws")
    flow.on_packet(pkt)
    assert pkt.verdict == "retained"
    row = await pending_row(app)
    await app.state.consent.resolve(row["id"], "denied", "t", "once")
    await flow_quiesce(flow)


async def flow_quiesce(flow: nfq.FlowConsumer) -> None:
    """Let spawned tasks finish (errors are logged by the reaper,
    never re-raised here)."""
    while flow._tasks:
        tasks = list(flow._tasks)
        await asyncio.wait(tasks, timeout=5.0)
        for task in tasks:
            flow._tasks.discard(task)


async def pending_row(app, workspace_id: str = "ws") -> dict:
    """Poll until a hold is fully registered (the row exists AND
    the engine holds it — create_request and register_hold are two
    steps, and a verdict before the second is a no-op)."""
    engine = app.state.consent
    for _ in range(400):
        rows = await app.state.model.egress_consent.list_requests(
            workspace_id, decision="pending"
        )
        if rows and rows[0]["id"] in engine._holds:
            return rows[0]
        await asyncio.sleep(0.005)
    raise AssertionError("no held row appeared")


async def test_inflight_retransmit_drops(consumer_app) -> None:
    app, _net = consumer_app
    flow = consumer(app, app.state.net)
    app.state.deciders.register(1, "ws")
    first = FakePkt(syn_packet())
    flow.on_packet(first)
    assert first.verdict == "retained"
    retransmit = FakePkt(syn_packet())
    flow.on_packet(retransmit)
    assert retransmit.verdict == "drop"
    # Resolve the in-flight hold (a deny) and drain its task.
    row = await pending_row(app)
    await app.state.consent.resolve(row["id"], "denied", "token", "once")
    await flow_quiesce(flow)
    assert first.verdict == "drop"


async def test_session_gates_cover_before_prompting(consumer_app) -> None:
    app, net = consumer_app
    flow = consumer(app, app.state.net)
    engine = app.state.consent
    # An allow covering the name on the port: accept + pin.
    engine.session.allow("ws", "api.example", 443, 60.0)
    allowed = FakePkt(syn_packet(dst="203.0.113.9"))
    # Name the address through the forwarder memory the net seam owns.
    net.host_for = lambda ws, ip: (
        "api.example" if ip == "203.0.113.9" else None
    )
    flow.on_packet(allowed)
    assert allowed.verdict == "accept"
    await flow_quiesce(flow)
    assert net.allows == [
        ("ws", "203.0.113.9", 443, pytest.approx(60.0, abs=5))
    ]
    # A deny covering the name: fast deny with a reject pin.
    engine.session.deny("ws", "denied.example", 443, 60.0)
    net.host_for = lambda ws, ip: "denied.example"
    denied = FakePkt(syn_packet(dst="198.51.100.4"))
    flow.on_packet(denied)
    assert denied.verdict == "drop"
    await flow_quiesce(flow)
    assert net.rejects and net.rejects[0][1] == "198.51.100.4"
    # A portless SYN never consults the session gates; it prompts.
    app.state.deciders.register(1, "ws")
    portless = FakePkt(syn_packet(dport=0))
    flow.on_packet(portless)
    assert portless.verdict == "retained"
    row = await pending_row(app)
    await app.state.consent.resolve(row["id"], "denied", "t", "once")
    await flow_quiesce(flow)


async def test_verdicts_apply_pins(consumer_app) -> None:
    app, net = consumer_app
    flow = consumer(app, app.state.net)
    app.state.deciders.register(1, "ws")
    pkt = FakePkt(syn_packet())
    flow.on_packet(pkt)
    row = await pending_row(app)
    # A timed allow pins the port for the duration.
    await app.state.consent.resolve(row["id"], "allowed", "token", "5m")
    await flow_quiesce(flow)
    assert pkt.verdict == "accept"
    assert net.allows[-1][1] == "203.0.113.7"
    assert net.allows[-1][2] == 443
    assert net.allows[-1][3] == pytest.approx(300.0, abs=5)
    # A once deny on a different destination: the first allow's
    # session memory covers its own host, so a re-SYN there would
    # shortcut past a hold.
    denied = FakePkt(syn_packet(sport=40001, dst="198.51.100.9"))
    flow.on_packet(denied)
    row = await pending_row(app)
    await app.state.consent.resolve(row["id"], "denied", "token", "once")
    await flow_quiesce(flow)
    assert denied.verdict == "drop"
    assert net.rejects[-1][1] == "198.51.100.9"
    assert net.rejects[-1][3] <= nfq.VERDICT_CACHE_TTL + 1


async def test_verdict_cache_bound_clears(consumer_app) -> None:
    app, _net = consumer_app
    flow = consumer(app, app.state.net)
    flow._verdicts.update(
        {
            (i, "10.0.0.1", 80): ("allow", 1.0)
            for i in range(nfq.VERDICT_CACHE_MAX + 1)
        }
    )
    assert len(flow._verdicts) > nfq.VERDICT_CACHE_MAX
    # The next decided verdict clears the flood wholesale.
    app.state.deciders.register(1, "ws")
    pkt = FakePkt(syn_packet())
    flow.on_packet(pkt)
    row = await pending_row(app)
    await app.state.consent.resolve(row["id"], "denied", "t", "once")
    await flow_quiesce(flow)
    assert len(flow._verdicts) == 1


async def test_hold_error_fails_the_packet_closed(consumer_app) -> None:
    app, _net = consumer_app
    flow = consumer(app, app.state.net)
    app.state.consent.interactive_hold = None  # a bug: not callable
    pkt = FakePkt(syn_packet())
    flow.on_packet(pkt)
    assert pkt.verdict == "retained"
    await flow_quiesce(flow)
    assert pkt.verdict == "drop"


async def test_spawn_logs_failed_enforcement(consumer_app) -> None:
    app, net = consumer_app
    flow = consumer(app, app.state.net)

    async def boom():
        raise RuntimeError("nft gone")

    flow.spawn(boom())
    await flow_quiesce(flow)  # the error is reaped, not raised


def test_stop_without_start_is_a_noop() -> None:
    flow = nfq.FlowConsumer("ws", 1, None)
    flow.stop()


class FakeNfq:
    """The netfilterqueue binding surface, recorded."""

    bound: list[tuple[int, object]] = []

    def __init__(self) -> None:
        self.queue = None
        self.callback = None
        self.fd = FakeFd()

    def bind(self, queue_num, callback) -> None:
        self.queue = queue_num
        self.callback = callback

    def get_fd(self) -> int:
        return self.fd

    def run(self, block: bool = True) -> None:
        raise RuntimeError("netlink hiccup")  # the drain arm

    def unbind(self) -> None:
        FakeNfq.bound.append((self.queue, self.callback))


class FakeFd:
    """A real OS pipe end as the queue's fd: the loop's reader
    registry needs a genuine fileno, and a pipe never signals
    readability on its own (the drain test drives it by hand)."""

    _pool: list = []

    def __init__(self) -> None:
        import os

        self._read, self._write = os.pipe()
        FakeFd._pool.append((self._read, self._write))

    def fileno(self) -> int:
        return self._read

    @classmethod
    def close_all(cls) -> None:
        import os

        for read, write in cls._pool:
            os.close(read)
            os.close(write)
        cls._pool = []


async def test_start_binds_and_stop_unbinds(consumer_app, monkeypatch) -> None:
    app, _net = consumer_app
    flow = consumer(app, app.state.net)
    monkeypatch.setattr(nfq, "NetfilterQueue", FakeNfq)
    flow.start()
    assert flow._nfq is not None
    assert flow._nfq.queue == flow.queue_num
    assert flow._nfq.callback == flow.on_packet
    assert flow._loop is not None
    # A readability event drains through the fake (its run raises —
    # the drain arm swallows and defers to the next event).
    flow.drain()
    flow.stop()
    FakeFd.close_all()
    assert FakeNfq.bound and FakeNfq.bound[0][0] == flow.queue_num
    assert flow._nfq is None
    # A second stop is a no-op.
    flow.stop()


async def test_stop_cancels_spawned_tasks(consumer_app, monkeypatch) -> None:
    """Unbinding cancels the spawned verdict tasks: a held SYN's
    task must not outlive the queue it serves."""

    app, _net = consumer_app
    flow = consumer(app, app.state.net)
    monkeypatch.setattr(nfq, "NetfilterQueue", FakeNfq)
    flow.start()
    stuck = asyncio.Event()

    async def hang():
        await stuck.wait()

    task = asyncio.create_task(hang())
    flow._tasks.add(task)
    flow.stop()
    FakeFd.close_all()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, 0.2)
    stuck.set()


async def test_decide_cancelled_drops_the_retained_packet(
    consumer_app,
) -> None:
    app, _net = consumer_app
    flow = consumer(app, app.state.net)
    app.state.deciders.register(1, "ws")
    pkt = FakePkt(syn_packet())
    flow.on_packet(pkt)
    # Wait until the hold is registered (the task is parked on the
    # verdict future), then cancel it — teardown's shape: the
    # retained SYN drops and the cancellation propagates.
    row = await pending_row(app)
    tasks = list(flow._tasks)
    tasks[0].cancel()
    await asyncio.wait(tasks, timeout=2.0)
    assert tasks[0].cancelled()
    assert pkt.verdict == "drop"
    assert (
        row["decision"] == "pending"
    )  # the orphan; startup's reaper takes it


async def test_decide_denies_when_the_engine_gate_raises(
    consumer_app, monkeypatch
) -> None:
    app, _net = consumer_app

    async def explode(*_args, **_kw):
        raise RuntimeError("engine bug")

    monkeypatch.setattr(app.state.consent, "hold", explode)
    flow = consumer(app, app.state.net)
    pkt = FakePkt(syn_packet())
    flow.on_packet(pkt)
    await flow_quiesce(flow)
    assert pkt.verdict == "drop"


async def test_a_once_allow_pins_nothing(consumer_app) -> None:
    app, net = consumer_app
    flow = consumer(app, app.state.net)
    app.state.deciders.register(1, "ws")
    pkt = FakePkt(syn_packet())
    flow.on_packet(pkt)
    row = await pending_row(app)
    await app.state.consent.resolve(row["id"], "allowed", "token", "once")
    await flow_quiesce(flow)
    assert pkt.verdict == "accept"
    assert net.allows == []  # once: this connection only


def test_the_import_guard_is_defense_not_default() -> None:
    """The binding ships with every install; the ImportError arm is
    the exotic-install guard and cannot fire on the test host."""
    assert nfq.NetfilterQueue is not None


async def test_a_once_deny_pins_only_the_fail_fast_window(
    consumer_app,
) -> None:
    app, net = consumer_app
    flow = consumer(app, app.state.net)
    app.state.deciders.register(1, "ws")
    pkt = FakePkt(syn_packet(dst="198.51.100.20", sport=41000))
    flow.on_packet(pkt)
    row = await pending_row(app)
    await app.state.consent.resolve(row["id"], "denied", "token", "once")
    await flow_quiesce(flow)
    assert pkt.verdict == "drop"
    assert net.rejects[-1][3] <= nfq.ONCE_REJECT_S


async def test_a_portless_deny_pins_nothing(consumer_app) -> None:
    app, net = consumer_app
    flow = consumer(app, app.state.net)
    app.state.deciders.register(1, "ws")
    pkt = FakePkt(syn_packet(dport=0, sport=42000))
    flow.on_packet(pkt)
    row = await pending_row(app)
    await app.state.consent.resolve(row["id"], "denied", "token", "5m")
    await flow_quiesce(flow)
    assert pkt.verdict == "drop"
    assert net.rejects == []  # an RST is TCP-only


async def test_stop_before_start_leaves_no_loop(consumer_app) -> None:
    """The stop path with no loop registered (never started): only
    the unbind/task sweep runs."""
    app, _net = consumer_app
    flow = consumer(app, app.state.net)
    flow._nfq = None
    flow.stop()


async def test_session_gate_skips_portless_flows(consumer_app) -> None:
    """A portless SYN never consults the session gates (no port to
    match): straight to the hold."""
    app, _net = consumer_app
    flow = consumer(app, app.state.net)
    app.state.consent.session.allow("ws", "wide.example", None, 60.0)
    app.state.net.host_for = lambda ws, ip: (
        "wide.example" if ip == "203.0.113.9" else None
    )
    app.state.deciders.register(1, "ws")
    pkt = FakePkt(syn_packet(dst="203.0.113.9", dport=0, sport=43000))
    flow.on_packet(pkt)
    row = await pending_row(app)
    await app.state.consent.resolve(row["id"], "denied", "t", "once")
    await flow_quiesce(flow)
    assert pkt.verdict == "drop"


async def test_a_timed_deny_pins_its_full_window(consumer_app) -> None:
    app, net = consumer_app
    flow = consumer(app, app.state.net)
    app.state.deciders.register(1, "ws")
    pkt = FakePkt(syn_packet(dst="198.51.100.30", sport=44000))
    flow.on_packet(pkt)
    row = await pending_row(app)
    await app.state.consent.resolve(row["id"], "denied", "token", "5m")
    await flow_quiesce(flow)
    assert pkt.verdict == "drop"
    assert net.rejects[-1][3] == pytest.approx(300.0, abs=5)


async def test_stop_without_a_registered_loop(
    consumer_app, monkeypatch
) -> None:
    """A start that bound the queue but never registered the loop
    (the pathological middle) still unbinds cleanly."""
    app, _net = consumer_app
    flow = consumer(app, app.state.net)
    monkeypatch.setattr(nfq, "NetfilterQueue", FakeNfq)
    flow.start()
    flow._loop = None  # the registration never landed
    flow.stop()
    FakeFd.close_all()


async def test_enforcement_failure_denies_and_logs(
    consumer_app, monkeypatch, caplog
) -> None:
    """An nft failure while applying a verdict denies the held SYN
    (logged), never strands it in the queue."""
    import logging

    app, net = consumer_app

    async def boom(*_args, **_kw):
        raise RuntimeError("table gone")

    monkeypatch.setattr(net, "consent_allow", boom)
    flow = consumer(app, net)
    app.state.deciders.register(1, "ws")
    pkt = FakePkt(syn_packet())
    flow.on_packet(pkt)
    row = await pending_row(app)
    with caplog.at_level(logging.ERROR):
        await app.state.consent.resolve(row["id"], "allowed", "t", "5m")
    await flow_quiesce(flow)
    assert pkt.verdict == "drop"
    assert any("applying a verdict" in r.message for r in caplog.records)


async def test_a_cached_deny_refreshes_the_rst_pin(consumer_app) -> None:
    """A retry hitting the verdict cache after the original RST pin
    lapsed still fails fast: the cache re-pins before dropping."""
    app, net = consumer_app
    flow = consumer(app, net)
    import time as time_mod

    flow._verdicts[(40000, "203.0.113.7", 443)] = (
        "deny",
        time_mod.time() + 60,
    )
    pkt = FakePkt(syn_packet())
    flow.on_packet(pkt)
    assert pkt.verdict == "drop"
    await flow_quiesce(flow)
    assert net.rejects and net.rejects[-1][1] == "203.0.113.7"
    assert net.rejects[-1][3] <= nfq.ONCE_REJECT_S


async def test_a_cached_portless_deny_drops_without_a_pin(
    consumer_app,
) -> None:
    """A cached deny for a portless flow re-drops with no RST pin —
    an RST needs a port."""
    app, net = consumer_app
    flow = consumer(app, app.state.net)
    import time as time_mod

    flow._verdicts[(44000, "203.0.113.7", 0)] = (
        "deny",
        time_mod.time() + 60,
    )
    pkt = FakePkt(syn_packet(dport=0, sport=44000))
    flow.on_packet(pkt)
    assert pkt.verdict == "drop"
    await flow_quiesce(flow)
    assert net.rejects == []


async def test_on_packet_exception_drops_and_logs(
    consumer_app, monkeypatch, caplog
) -> None:
    """An exception inside the packet handler drops the SYN (fail-
    closed) and logs the traceback instead of silently hanging."""
    import logging

    app, _net = consumer_app
    flow = consumer(app, app.state.net)

    def exploding_route(pkt):
        raise RuntimeError("unexpected bug")

    monkeypatch.setattr(flow, "route_packet", exploding_route)
    pkt = FakePkt(syn_packet())
    with caplog.at_level(logging.ERROR):
        flow.on_packet(pkt)
    assert pkt.verdict == "drop"
    assert any("on_packet failed" in r.message for r in caplog.records)


async def test_drain_logs_on_failure(
    consumer_app, monkeypatch, caplog
) -> None:
    """A drain failure is logged, not silently swallowed."""
    import logging

    app, _net = consumer_app
    flow = consumer(app, app.state.net)
    monkeypatch.setattr(nfq, "NetfilterQueue", FakeNfq)
    flow.start()
    with caplog.at_level(logging.ERROR):
        flow.drain()
    flow.stop()
    FakeFd.close_all()
    assert any("drain failed" in r.message for r in caplog.records)
