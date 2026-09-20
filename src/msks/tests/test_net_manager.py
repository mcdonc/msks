"""The egress attachment lifecycle (#52), against stub tools and fake
services — the real DHCP/DNS protocols have their own suites."""

import asyncio
from ipaddress import IPv4Network
from pathlib import Path

import pytest
from msks.app import build_app
from msks.microvm import VmSpec
from msks.microvm.errors import MicrovmError
from msks.net import alloc
from msks.net import manager as manager_mod
from msks.net.manager import NetManager
from msks.settings import NetSettings, ServerSettings, Settings
from netstubs import NFT_FAIL_AT, NFT_STDERR, log_lines, stub_ip, stub_nft


class FakeService:
    """The DHCP/DNS service surface: start/stop/serve, recorded."""

    def __init__(self, *args, **kwargs) -> None:
        self.args = args
        self.kwargs = kwargs
        self.started = False
        self.stopped = False
        self._names = {}

    async def start(self, sock=None) -> None:
        self.started = True

    def stop(self) -> None:
        self.stopped = True

    async def serve(self) -> None:
        await asyncio.sleep(3600)  # cancelled by teardown

    # The naming memory (#69): the forwarder surface the consent
    # seam reads, kept in the same fake so gated-mode attaches get
    # it too.
    def remember(self, name, records) -> None:
        self._names = {ip: name for ip, _ttl in records}

    def host_for(self, ip):
        return self._names.get(ip)

    def ips_for(self, host):
        return [ip for ip, name in self._names.items() if name == host]

    def forget(self, host):
        self._names = {
            ip: name for ip, name in self._names.items() if name != host
        }


@pytest.fixture
async def net_app(tmp_path: Path, monkeypatch):
    """An app whose egress is enabled and armed with stub tools.

    Real /proc writes and real service sockets stay out: forwarding
    is patched, and the manager runs the fake services. The DHCP and
    DNS constructors' arguments are still asserted through the fakes.
    The model is a real one on a scratch database: claim_slice
    records pool slices on workspace rows (#70 review).
    """
    ip_log = tmp_path / "ip.log"
    nft_log = tmp_path / "nft.log"
    settings = Settings(
        net=NetSettings(
            enabled=True,
            ip_tool=str(stub_ip(tmp_path, ip_log)),
            nft_tool=str(stub_nft(tmp_path, nft_log)),
            dns_upstream="10.9.9.9",
        ),
        server=ServerSettings(db_path=tmp_path / "net.db"),
    )
    app = build_app(settings)
    monkeypatch.setattr(
        manager_mod, "verify_forwarding", lambda path=None: None
    )
    manager = NetManager(
        app, dhcp_factory=FakeService, dns_factory=FakeService
    )
    app.state.net = manager
    app.state.model.migrate()
    for wid in ("ws-a", "ws-b", "ws-c"):
        await app.state.model.create_workspace(
            VmSpec(
                workspace_id=wid,
                kernel=Path("/k"),
                rootfs=Path("/r"),
                egress=True,
            )
        )
    return app, ip_log, nft_log


async def ready(app) -> NetManager:
    await app.state.net.start()
    return app.state.net


async def test_start_disabled_leaves_everything_off(tmp_path: Path) -> None:
    app = build_app(Settings(net=NetSettings(enabled=False)))
    await app.state.net.start()
    assert app.state.net._state == "disabled"
    assert await app.state.net.attach("ws-a", want=False) is None


async def test_attach_before_start_names_the_state(tmp_path: Path) -> None:
    app = build_app(Settings(net=NetSettings(enabled=True)))
    with pytest.raises(MicrovmError, match="never started"):
        await app.state.net.attach("ws-a", want=True)


async def test_attach_when_disabled_names_the_setting(net_app) -> None:
    app, _ip, _nft = net_app
    await ready(app)
    # A fresh, disabled manager on the same app: the armed one proves
    # the tools work; the disabled one names the honest cause.
    disabled = NetManager(build_app(Settings(net=NetSettings(enabled=False))))
    await disabled.start()
    with pytest.raises(MicrovmError, match="MSKSD_EGRESS_ENABLED"):
        await disabled.attach("ws-a", want=True)


async def test_start_records_an_unavailable_daemon(
    net_app, monkeypatch, capsys
) -> None:
    app, _ip, _nft = net_app

    def no_privilege(path=None):
        raise MicrovmError("net.ipv4.ip_forward is not enabled (reads '0')")

    monkeypatch.setattr(manager_mod, "verify_forwarding", no_privilege)
    await app.state.net.start()
    assert app.state.net._state == "unavailable"
    assert "egress unavailable" in capsys.readouterr().out
    # The per-workspace refusal names both halves of the contract:
    # the capability set and the sysctl key (#101).
    with pytest.raises(
        MicrovmError, match=r"CAP_NET_ADMIN.*net\.ipv4\.ip_forward"
    ):
        await app.state.net.attach("ws-a", want=True)


async def test_require_ready_names_an_unknown_state(net_app) -> None:
    app, _ip, _nft = net_app
    app.state.net._state = "half-armed"
    with pytest.raises(MicrovmError, match="half-armed"):
        await app.state.net.attach("ws-a", want=True)


async def test_attach_arms_the_whole_path(net_app) -> None:
    app, ip_log, nft_log = net_app
    manager = await ready(app)
    attachment = await manager.attach("ws-a", want=True)
    net = alloc.slice_net(
        manager.app.state.settings.net.pool, alloc.slice_index("ws-a", 16384)
    )
    assert attachment.tap == alloc.tap_name("ws-a")
    assert attachment.mac == alloc.guest_mac("ws-a")
    assert attachment.guest_ip == str(alloc.guest_addr(net))
    assert attachment.tap_ip == str(alloc.tap_addr(net))
    assert f"tuntap add dev {attachment.tap} mode tap" in log_lines(ip_log)
    assert (
        f"addr add {attachment.tap_ip}/30 dev {attachment.tap}"
        in log_lines(ip_log)
    )
    assert "-f -" in log_lines(nft_log)  # base + per-vm rulesets
    services = manager._services["ws-a"]
    assert services.dhcp.started and services.dns.started
    # The DHCP service gets the tap device; the forwarder the tap IP.
    assert services.dhcp.kwargs["device"] == attachment.tap
    assert services.dns.kwargs["bind"] == (attachment.tap_ip, 53)
    # The forwarder answers this tap's guest only (#70 review).
    assert services.dns.kwargs["client_ip"] == attachment.guest_ip
    assert services.dns.args[0] == ("10.9.9.9", 53)
    # The attachment persists for the idempotent second boot.
    again = await manager.attach("ws-a", want=True)
    assert again is attachment


async def test_detach_tears_the_path_down(net_app) -> None:
    app, ip_log, nft_log = net_app
    manager = await ready(app)
    attachment = await manager.attach("ws-a", want=True)
    services = manager._services["ws-a"]
    await manager.detach("ws-a")
    assert services.dhcp.stopped and services.dns.stopped
    assert f"link del dev {attachment.tap}" in log_lines(ip_log)
    assert "delete table inet" in " ".join(log_lines(nft_log))
    assert "ws-a" not in manager._attachments
    assert "ws-a" not in manager._services
    # Idempotent: a second detach touches nothing.
    before = len(log_lines(ip_log))
    await manager.detach("ws-a")
    assert len(log_lines(ip_log)) == before


async def test_slice_collisions_walk_forward_and_pools_exhaust(
    net_app, monkeypatch
) -> None:
    app, _ip, _nft = net_app
    # A /29 pool: two slices, and every workspace starts at slice 0.
    app.state.settings.net.pool = IPv4Network("10.99.0.0/29")
    monkeypatch.setattr(alloc, "slice_index", lambda ws, count: 0)
    manager = await ready(app)
    first = await manager.attach("ws-a", want=True)
    second = await manager.attach("ws-b", want=True)
    assert first.guest_ip == "10.99.0.1"
    assert second.guest_ip == "10.99.0.5"  # walked past the collision
    with pytest.raises(MicrovmError, match="pool exhausted"):
        await manager.attach("ws-c", want=True)


async def test_a_failed_build_unwinds_its_plumbing(
    net_app, monkeypatch
) -> None:
    app, ip_log, nft_log = net_app
    manager = await ready(app)
    monkeypatch.setenv(NFT_FAIL_AT, "-f -")
    monkeypatch.setenv(NFT_STDERR, "syntax error")
    with pytest.raises(MicrovmError):
        await manager.attach("ws-a", want=True)
    monkeypatch.delenv(NFT_FAIL_AT)
    # The tap came back down and the slice freed: a clean retry works.
    assert any(line.startswith("link del dev") for line in log_lines(ip_log))
    attachment = await manager.attach("ws-a", want=True)
    assert attachment.guest_ip


async def test_stop_detaches_every_workspace(net_app) -> None:
    app, ip_log, _nft = net_app
    manager = await ready(app)
    await manager.attach("ws-a", want=True)
    await manager.attach("ws-b", want=True)
    await manager.stop()
    # Two distinct taps torn down (the create-time sweeps appear too).
    taps = {
        line.split()[-1]
        for line in log_lines(ip_log)
        if line.startswith("link del dev")
    }
    assert taps == {alloc.tap_name("ws-a"), alloc.tap_name("ws-b")}
    assert not manager._attachments


def test_netmask_is_the_slice_mask(net_app) -> None:
    app, _ip, _nft = net_app
    assert app.state.net.netmask() == "255.255.255.252"


async def test_dns_upstream_prefers_the_setting(net_app, monkeypatch) -> None:
    app, _ip, _nft = net_app
    manager = await ready(app)
    assert manager.dns_upstream() == ("10.9.9.9", 53)
    app.state.settings.net.dns_upstream = None
    monkeypatch.setattr(
        "msks.net.dns.upstream_from_resolv",
        lambda path=None: ("192.168.1.1", 53),
    )
    assert manager.dns_upstream() == ("192.168.1.1", 53)
    monkeypatch.setattr(
        "msks.net.dns.upstream_from_resolv", lambda path=None: None
    )
    with pytest.raises(MicrovmError, match="MSKSD_EGRESS_DNS_UPSTREAM"):
        manager.dns_upstream()


def test_verify_forwarding_accepts_a_routing_kernel(tmp_path: Path) -> None:
    # The appliance's sysctl.d setting, as the daemon reads it.
    sysctl = tmp_path / "ip_forward"
    sysctl.write_text("1\n")
    manager_mod.verify_forwarding(sysctl)  # no refusal


def test_verify_forwarding_names_the_sysctl_when_off(tmp_path: Path) -> None:
    # A kernel that does not route: the refusal names the key, so
    # the operator knows which sysctl to set (#101).
    sysctl = tmp_path / "ip_forward"
    sysctl.write_text("0\n")
    with pytest.raises(MicrovmError, match="net.ipv4.ip_forward"):
        manager_mod.verify_forwarding(sysctl)


def test_verify_forwarding_names_an_unreadable_sysctl(tmp_path: Path) -> None:
    # A directory: read_text raises OSError (EISDIR), the named error.
    with pytest.raises(
        MicrovmError, match="could not read net.ipv4.ip_forward"
    ):
        manager_mod.verify_forwarding(tmp_path)


class HalfBrokenDns(FakeService):
    """A DNS service whose start fails (bind refused, say)."""

    async def start(self, sock=None) -> None:
        raise OSError("address in use")


async def test_a_failed_service_start_stops_its_sibling(net_app) -> None:
    app, _ip, _nft = net_app
    app.state.net._dns_factory = HalfBrokenDns
    made: list[FakeService] = []

    def dhcp_factory(*args, **kwargs):
        service = FakeService(*args, **kwargs)
        made.append(service)
        return service

    app.state.net._dhcp_factory = dhcp_factory
    manager = await ready(app)
    with pytest.raises(MicrovmError, match="egress services"):
        await manager.attach("ws-a", want=True)
    assert made and made[0].stopped  # the started sibling came down
    assert "ws-a" not in manager._attachments
    assert "ws-a" not in manager._services


async def test_detach_tolerates_a_missing_services_record(net_app) -> None:
    app, _ip, _nft = net_app
    manager = await ready(app)
    await manager.attach("ws-a", want=True)
    manager._services.pop("ws-a")  # the invariant-break backstop
    await manager.detach("ws-a")
    assert "ws-a" not in manager._attachments


class PanickedDns(FakeService):
    """A DNS service that fails outside OSError (a bug, a cancel)."""

    async def start(self, sock=None) -> None:
        raise RuntimeError("bug")


async def test_a_panicked_service_start_stops_its_sibling(net_app) -> None:
    app, _ip, _nft = net_app
    app.state.net._dns_factory = PanickedDns
    made: list[FakeService] = []

    def dhcp_factory(*args, **kwargs):
        service = FakeService(*args, **kwargs)
        made.append(service)
        return service

    app.state.net._dhcp_factory = dhcp_factory
    manager = await ready(app)
    with pytest.raises(RuntimeError):
        await manager.attach("ws-a", want=True)
    assert made and made[0].stopped
    assert "ws-a" not in manager._attachments


async def test_detach_releases_the_slice_for_the_next_boot(net_app) -> None:
    """A stop/start cycle reassembles the SAME /30 (#52): the slice is
    released on detach and re-derived (not walked past) on attach."""
    app, _ip, _nft = net_app
    manager = await ready(app)
    first = await manager.attach("ws-a", want=True)
    await manager.detach("ws-a")
    second = await manager.attach("ws-a", want=True)
    assert second.guest_ip == first.guest_ip
    assert second.tap_ip == first.tap_ip
    assert second.slice == first.slice
    await manager.detach("ws-a")


async def test_recorded_slice_survives_a_daemon_restart(net_app) -> None:
    """The slice is recorded on the row (#70 review): a fresh manager
    on the same database reassembles the same /30 — no walk, no
    swap."""
    app, _ip, _nft = net_app
    first = await (await ready(app)).attach("ws-a", want=True)
    await app.state.net.detach("ws-a")
    restarted = NetManager(
        app, dhcp_factory=FakeService, dns_factory=FakeService
    )
    await restarted.start()
    again = await restarted.attach("ws-a", want=True)
    assert again.slice == first.slice
    assert again.guest_ip == first.guest_ip


async def test_recorded_slice_conflict_fails_closed(net_app) -> None:
    """A recorded slice another live workspace holds is a named
    refusal, not a silent address share."""
    app, _ip, _nft = net_app
    manager = await ready(app)
    await app.state.model.set_egress_slice("ws-b", 1234)
    manager._used_slices.add(1234)  # some live workspace holds it
    with pytest.raises(MicrovmError, match="held by another live workspace"):
        await manager.attach("ws-b", want=True)


# --- The forward dial (#109) ---


class RecorderDialer:
    """A dialer the tests aim: dial attempts recorded, outcomes fed."""

    def __init__(self, outcomes: list) -> None:
        self.outcomes = list(outcomes)
        self.calls: list[tuple[str, int]] = []

    async def __call__(self, host: str, port: int):
        self.calls.append((host, port))
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def stream_pair() -> tuple:
    """A stand-in (reader, writer) the dialer hands back."""
    return (object(), object())


async def test_forward_stream_without_an_attachment_names_it(net_app) -> None:
    app, _ip, _nft = net_app
    await ready(app)
    with pytest.raises(MicrovmError, match="no live network attachment"):
        await app.state.net.forward_stream("ws-a", 22)


async def test_forward_stream_dials_the_guest_address(net_app) -> None:
    app, _ip, _nft = net_app
    manager = await ready(app)
    attachment = await manager.attach("ws-a", want=True)
    pair = stream_pair()
    manager.dialer = RecorderDialer([pair])
    assert await manager.forward_stream("ws-a", 22) == pair
    assert manager.dialer.calls == [(attachment.guest_ip, 22)]


async def test_forward_stream_retries_a_refused_dial(net_app) -> None:
    app, _ip, _nft = net_app
    manager = await ready(app)
    await manager.attach("ws-a", want=True)
    pair = stream_pair()
    # First refusal is the bring-up state (DHCP/service race); the
    # second dial wins — the retry must keep going under the deadline.
    manager.dialer = RecorderDialer([ConnectionRefusedError(), pair])
    assert await manager.forward_stream("ws-a", 22) == pair
    assert len(manager.dialer.calls) == 2


async def test_forward_stream_names_the_deadline(net_app) -> None:
    app, _ip, _nft = net_app
    manager = await ready(app)
    await manager.attach("ws-a", want=True)
    app.state.settings.vmm.forward_wait_timeout_s = 0.05
    manager.dialer = RecorderDialer([OSError("refused"), OSError("refused")])
    with pytest.raises(MicrovmError, match=r"unavailable.*refused"):
        await manager.forward_stream("ws-a", 22)


async def test_detach_ends_live_forwards(net_app) -> None:
    app, _ip, _nft = net_app
    manager = await ready(app)
    await manager.attach("ws-a", want=True)
    pair = stream_pair()
    manager.dialer = RecorderDialer([pair])
    assert await manager.forward_stream("ws-a", 22) == pair

    class ClosedWriter:
        closed = False

        def close(self):
            self.closed = True

    live = ClosedWriter()
    manager.track_forward("ws-a", live)
    await manager.detach("ws-a")
    assert live.closed is True


async def test_untrack_forgets_a_released_stream(net_app) -> None:
    app, _ip, _nft = net_app
    manager = await ready(app)
    await manager.attach("ws-a", want=True)
    pair = stream_pair()
    manager.dialer = RecorderDialer([pair])
    await manager.forward_stream("ws-a", 22)
    manager.track_forward("ws-a", pair[1])
    other = stream_pair()
    manager.track_forward("ws-a", other[1])  # two live forwards
    manager.untrack_forward("ws-a", pair[1])
    assert manager._forwards == {"ws-a": [other[1]]}  # one still live
    manager.untrack_forward("ws-a", other[1])
    manager.untrack_forward("ws-b", other[1])  # an unknown workspace: quiet
    await manager.detach("ws-a")  # nothing tracked: no error, no work
    assert manager._forwards == {}


async def test_forward_stream_bounds_each_attempt_by_the_deadline(
    net_app,
) -> None:
    app, _ip, _nft = net_app
    manager = await ready(app)
    await manager.attach("ws-a", want=True)
    app.state.settings.vmm.forward_wait_timeout_s = 0.05

    class SilentDialer:
        """A dial that never answers — a dropped SYN in dialer shape."""

        calls = 0

        async def __call__(self, host, port):
            type(self).calls += 1
            await asyncio.sleep(30)
            raise AssertionError("the deadline must end the attempt first")

    manager.dialer = SilentDialer()
    with pytest.raises(MicrovmError, match="unavailable"):
        await manager.forward_stream("ws-a", 22)
    assert SilentDialer.calls == 1


# --- consent wiring (#69) ---------------------------------------------


class FakeConsumer:
    """The NFQUEUE consumer surface, recorded."""

    def __init__(self, workspace_id, queue_num, net) -> None:
        self.workspace_id = workspace_id
        self.queue_num = queue_num
        self.started = False
        self.stopped = False

    def start(self) -> None:
        self.started = True

    def stop(self) -> None:
        self.stopped = True


@pytest.fixture
async def gated_app(tmp_path: Path, monkeypatch):
    """The net app, with an injectable consumer factory."""
    ip_log = tmp_path / "ip.log"
    nft_log = tmp_path / "nft.log"
    settings = Settings(
        net=NetSettings(
            enabled=True,
            ip_tool=str(stub_ip(tmp_path, ip_log)),
            nft_tool=str(stub_nft(tmp_path, nft_log)),
            dns_upstream="10.9.9.9",
        ),
        server=ServerSettings(db_path=tmp_path / "gated.db"),
    )
    app = build_app(settings)
    monkeypatch.setattr(
        manager_mod, "verify_forwarding", lambda path=None: None
    )
    consumers: list[FakeConsumer] = []

    def factory(workspace_id, queue_num, net):
        consumer = FakeConsumer(workspace_id, queue_num, net)
        consumers.append(consumer)
        return consumer

    manager = NetManager(
        app,
        dhcp_factory=FakeService,
        dns_factory=FakeService,
        consumer_factory=factory,
    )
    app.state.net = manager
    app.state.model.migrate()
    await app.state.model.create_workspace(
        VmSpec(
            workspace_id="ws-i",
            kernel=Path("/k"),
            rootfs=Path("/r"),
            egress_mode="interactive",
        )
    )
    try:
        yield app, consumers, nft_log
    finally:
        await manager.stop()
        await app.state.model.close()


def interactive_policy():
    from msks.consent.specs import EgressPolicy

    return EgressPolicy("ws-i", "interactive", ())


async def test_interactive_attach_binds_the_queue_before_the_chain(
    gated_app,
) -> None:
    app, consumers, nft_log = gated_app
    await app.state.net.start()
    attachment = await app.state.net.attach(
        "ws-i", want=True, policy=interactive_policy()
    )
    assert consumers and consumers[0].started
    assert consumers[0].queue_num == app.state.settings.net.queue_base + (
        attachment.slice
    )
    # The ruleset names the same queue number.
    applied = nft_log.with_name(nft_log.name + ".stdin").read_text()
    assert f"queue num {consumers[0].queue_num}" in applied
    # Detach unbinds the consumer and clears tilrestart verdicts.
    await app.state.net.detach("ws-i")
    assert consumers[0].stopped


async def test_allow_attach_runs_no_consumer(gated_app) -> None:
    app, consumers, _nft_log = gated_app
    await app.state.net.start()
    from msks.consent.specs import EgressPolicy

    await app.state.net.attach(
        "ws-i", want=True, policy=EgressPolicy("ws-i", "allow", ())
    )
    assert consumers == []


async def test_queue_for_refuses_pools_past_the_queue_range(
    net_app,
) -> None:
    app, _ip, _nft = net_app
    await app.state.net.start()
    app.state.settings.net.queue_base = 65530
    with pytest.raises(MicrovmError, match="pool too large"):
        app.state.net.queue_for(100)


async def test_consent_helpers_pin_through_the_chain(gated_app) -> None:
    app, _consumers, nft_log = gated_app
    await app.state.net.start()
    await app.state.net.attach("ws-i", want=True, policy=interactive_policy())
    await app.state.net.consent_allow("ws-i", "10.1.2.3", None, 60)
    await app.state.net.consent_reject("ws-i", "10.1.2.3", 443, 5)
    # A detached workspace pins nothing (no table to pin into).
    await app.state.net.consent_allow("ws-missing", "10.9.9.9", None, 60)
    lines = log_lines(nft_log)
    assert any(
        "add element inet" in line and "allows_any" in line for line in lines
    )
    assert any(
        "add element inet" in line and "rejects" in line for line in lines
    )


async def test_host_for_reads_the_services_naming(gated_app) -> None:
    app, _consumers, _nft_log = gated_app
    await app.state.net.start()
    await app.state.net.attach("ws-i", want=True, policy=interactive_policy())
    services = app.state.net._services["ws-i"]
    assert app.state.net.host_for("ws-i", "10.2.3.4") is None
    services.dns.remember("api.example", [("10.2.3.4", 60)])
    assert app.state.net.host_for("ws-i", "10.2.3.4") == "api.example"


async def test_replay_forever_pins_address_verdicts(gated_app) -> None:
    app, _consumers, nft_log = gated_app
    await app.state.net.start()
    from msks.model.egress_consent import DECISION_ALLOWED

    model = app.state.model.egress_consent
    allow = await model.create_request("ws-i", "203.0.113.7", 443)
    await model.decide(allow["id"], DECISION_ALLOWED, "token", "forever")
    denied = await model.create_request("ws-i", "198.51.100.9", 443)
    await model.decide(denied["id"], "denied", "token", "forever")
    named = await model.create_request("ws-i", "api.example", 443)
    await model.decide(named["id"], DECISION_ALLOWED, "token", "forever")
    await app.state.net.attach("ws-i", want=True, policy=interactive_policy())
    lines = log_lines(nft_log)
    allows = [line for line in lines if "allows" in line]
    rejects = [line for line in lines if "rejects" in line]
    assert any("203.0.113.7 . 443" in line for line in allows)
    assert any("198.51.100.9 . 443" in line for line in rejects)
    # The named verdict pins nothing here (the resolver gate reads
    # its row live).
    assert not any("api.example" in line for line in lines)


async def test_clear_consent_dest_forgets_names_and_drops_flows(
    gated_app,
    monkeypatch,
    tmp_path: Path,
) -> None:
    app, _consumers, _nft_log = gated_app
    await app.state.net.start()
    await app.state.net.attach("ws-i", want=True, policy=interactive_policy())
    services = app.state.net._services["ws-i"]
    services.dns.remember("api.example", [("203.0.113.7", 300)])
    # A conntrack stub that records its invocation.
    ct_log = tmp_path / "ct.log"
    ct = tmp_path / "conntrack"
    ct.write_text(
        "#!/bin/sh\nprintf '%s\\n' \"$*\" >> " + str(ct_log) + "\nexit 0\n"
    )
    ct.chmod(0o755)
    app.state.settings.net.conntrack_tool = str(ct)
    await app.state.net.clear_consent_dest("ws-i", "api.example", 443)
    assert services.dns.ips_for("api.example") == []
    guest = app.state.net._attachments["ws-i"].guest_ip
    assert log_lines(ct_log) == [
        f"-D -s {guest} -d 203.0.113.7",
    ]
    # A literal-IP verdict clears through the host itself.
    await app.state.net.clear_consent_dest("ws-i", "198.51.100.4", 0)
    assert f"-D -s {guest} -d 198.51.100.4" in log_lines(ct_log)
    # No attachment: nothing to clear, nothing raised.
    await app.state.net.clear_consent_dest("ws-missing", "x", 0)


async def test_drop_flows_survives_a_missing_tool(gated_app) -> None:
    """A conntrack tool that is absent logs and moves on — the rule
    clear already happened, so the revoke still lands."""
    app, _consumers, _nft_log = gated_app
    await app.state.net.start()
    await app.state.net.attach("ws-i", want=True, policy=interactive_policy())
    app.state.settings.net.conntrack_tool = "/nonexistent/conntrack"
    attachment = app.state.net._attachments["ws-i"]
    await app.state.net.drop_flows("ws-i", attachment.guest_ip, "10.9.9.9")


async def test_build_failure_stops_the_consumer(
    gated_app, monkeypatch
) -> None:
    """A chain install that fails after the queue bound unwinds the
    consumer too — no queue stays answered-but-orphaned."""
    app, consumers, _nft_log = gated_app
    await app.state.net.start()

    async def failing_install(*_args, **_kw):
        raise MicrovmError("nft apply failed")

    monkeypatch.setattr(manager_mod.nft, "install_vm", failing_install)
    with pytest.raises(MicrovmError, match="nft apply failed"):
        await app.state.net.attach(
            "ws-i", want=True, policy=interactive_policy()
        )
    assert consumers[0].stopped
    assert "ws-i" not in app.state.net._attachments


async def test_replay_survives_a_read_failure(gated_app, monkeypatch) -> None:
    app, _consumers, _nft_log = gated_app
    await app.state.net.start()

    async def explode(*_args, **_kw):
        raise RuntimeError("db down")

    monkeypatch.setattr(
        app.state.model.egress_consent, "forever_rows", explode
    )
    await app.state.net.attach("ws-i", want=True, policy=interactive_policy())
    # The attach itself succeeded; only the replay skipped.


async def test_replay_pins_a_ported_deny(gated_app) -> None:
    app, _consumers, nft_log = gated_app
    await app.state.net.start()
    model = app.state.model.egress_consent
    row = await model.create_request("ws-i", "198.51.100.9", 8443)
    await model.decide(row["id"], "denied", "token", "forever")
    await app.state.net.attach("ws-i", want=True, policy=interactive_policy())
    assert any(
        "198.51.100.9 . 8443" in line and "rejects" in line
        for line in log_lines(nft_log)
    )


async def test_consent_reject_skips_detached_workspaces(gated_app) -> None:
    app, _consumers, nft_log = gated_app
    await app.state.net.start()
    nft_log.read_text()  # drain
    open(nft_log, "w").close()
    await app.state.net.consent_reject("ws-missing", "10.0.0.1", 443, 10)
    assert log_lines(nft_log) == []


async def test_host_for_without_services(gated_app) -> None:
    app, _consumers, _nft_log = gated_app
    await app.state.net.start()
    assert app.state.net.host_for("ws-missing", "10.0.0.1") is None


async def test_dest_addresses_without_a_services_record(gated_app) -> None:
    """The race window between attach and services registration:
    a literal host still clears through itself."""
    app, _consumers, _nft_log = gated_app
    await app.state.net.start()
    assert app.state.net.dest_addresses("ws-i", "203.0.113.7") == [
        "203.0.113.7"
    ]


async def test_replay_row_skips_portless_denies(gated_app) -> None:
    """A forever deny given by address without a port pins nothing:
    an RST needs a port; the row still blocks the name at the
    resolver."""
    app, _consumers, nft_log = gated_app
    await app.state.net.start()
    open(nft_log, "w").close()
    await app.state.net.replay_row(
        "ws-i",
        {
            "dest_host": "198.51.100.9",
            "dest_port": 0,
            "decision": "denied",
        },
    )
    assert log_lines(nft_log) == []
