"""The egress attachment lifecycle (#52), against stub tools and fake
services — the real DHCP/DNS protocols have their own suites."""

import asyncio
import json
from ipaddress import IPv4Network
from pathlib import Path

import pytest
from msks.app import build_app
from msks.microvm import VmSpec
from msks.microvm.errors import MicrovmError
from msks.net import alloc
from msks.net import manager as manager_mod
from msks.net.manager import NetManager
from msks.settings import (
    LlmSettings,
    NetSettings,
    ServerSettings,
    Settings,
)
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
    # it too. Co-residency (#304): `shared` marks addresses two
    # live names resolved to; tests set it to drive the pin skip.
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

    def shared(self, ip) -> bool:
        return ip in getattr(self, "shared_ips", set())


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
    # The deployment's sysctl.d setting, as the daemon reads it.
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


async def test_shared_addresses_carry_no_allow_pins(gated_app) -> None:
    """Co-residency (#304): a NAME pin on an address two live names
    resolved to installs nothing — the element is keyed by address
    alone and would cover the co-resident; the next SYN gates at
    the queue under the naming memory. An address-literal verdict
    (given on the address itself) still pins."""
    app, _consumers, nft_log = gated_app
    net = app.state.net
    await net.start()
    await net.attach("ws-i", want=True, policy=interactive_policy())
    net._services["ws-i"].dns.shared_ips = {"10.1.2.3"}
    await net.consent_allow("ws-i", "10.1.2.3", 443, 60)
    assert not any(
        "add element" in line and "allows" in line
        for line in log_lines(nft_log)
    )
    await net.consent_allow("ws-i", "10.1.2.3", 443, 60, named=False)
    assert any(
        "add element" in line and "allows_port" in line
        for line in log_lines(nft_log)
    )


async def test_a_named_deny_on_a_shared_address_refuses_only_its_flow(
    gated_app,
) -> None:
    """A name deny on a shared address pins the per-flow element —
    keyed by the connection's source port — so the co-resident's
    connections keep gating (#304); unnamed and literal denies pin
    the blanket element."""
    app, _consumers, nft_log = gated_app
    net = app.state.net
    await net.start()
    await net.attach("ws-i", want=True, policy=interactive_policy())
    net._services["ws-i"].dns.shared_ips = {"10.1.2.3"}
    await net.consent_reject(
        "ws-i", "10.1.2.3", 443, 5, sport=40000, named=True
    )
    added = [
        line.split()[4]
        for line in log_lines(nft_log)
        if line.startswith("add element")
    ]
    assert "rejects_flow" in added
    assert "rejects" not in added
    await net.consent_reject(
        "ws-i", "10.1.2.3", 443, 5, sport=40000, named=False
    )
    added = [
        line.split()[4]
        for line in log_lines(nft_log)
        if line.startswith("add element")
    ]
    assert added[-1] == "rejects"


async def test_retract_consent_pins_clears_one_address(
    gated_app, monkeypatch
) -> None:
    """The co-residency retraction (#304): every element naming the
    address dies — the all-ports allow directly, the port-keyed sets
    by listing and destroying their matching elements — and another
    address's elements stay."""
    app, _consumers, nft_log = gated_app
    net = app.state.net
    await net.start()
    await net.attach("ws-i", want=True, policy=interactive_policy())

    async def fake_json(settings, args):
        if args[5] == "allows_port":
            return json.dumps(
                {
                    "nftables": [
                        {
                            "set": {
                                "elem": [
                                    {
                                        "elem": {
                                            "val": {
                                                "concat": ["10.1.2.3", 443]
                                            },
                                            "timeout": 60,
                                        }
                                    },
                                    {
                                        "elem": {
                                            "val": {
                                                "concat": ["10.9.9.9", 80]
                                            },
                                            "timeout": 60,
                                        }
                                    },
                                ]
                            }
                        }
                    ]
                }
            )
        return None

    monkeypatch.setattr(manager_mod.nft, "nft_json", fake_json)
    await net.retract_consent_pins("ws-i", ["10.1.2.3"])
    lines = log_lines(nft_log)
    assert any(
        "delete element" in line and "allows_any" in line for line in lines
    )
    cleared = [
        line
        for line in lines
        if "delete element" in line and "10.1.2.3" in line
    ]
    assert any("allows_port" in line for line in cleared)
    assert not any("10.9.9.9" in line for line in cleared)


async def test_static_pins_survive_a_shared_address(gated_app) -> None:
    """A static chain has no queue to gate a withdrawn pin's
    connections: its allowlist pins stand on a shared address
    (#304 review, round 2)."""
    app, _consumers, nft_log = gated_app
    net = app.state.net
    await net.start()
    from msks.consent.specs import EgressPolicy

    await net.attach(
        "ws-a", want=True, policy=EgressPolicy("ws-a", "static", ())
    )
    net._services["ws-a"].dns.shared_ips = {"10.1.2.3"}
    await net.consent_allow("ws-a", "10.1.2.3", 443, 60)
    assert any(
        "add element" in line and "allows_port" in line
        for line in log_lines(nft_log)
    )


async def test_a_racing_allow_pin_is_withdrawn_when_shared_flips(
    gated_app, monkeypatch
) -> None:
    """The TOCTOU reconcile (#304 review, round 2): a second name
    resolving while the pin's nft add runs leaves the element
    over-broad — the post-add re-check withdraws it."""
    app, _consumers, nft_log = gated_app
    net = app.state.net
    await net.start()
    await net.attach("ws-i", want=True, policy=interactive_policy())
    real_add = manager_mod.nft.allow_element

    async def flipping_add(settings, workspace_id, ip, port, ttl_s):
        net._services["ws-i"].dns.shared_ips = {ip}
        await real_add(settings, workspace_id, ip, port, ttl_s)

    monkeypatch.setattr(manager_mod.nft, "allow_element", flipping_add)
    await net.consent_allow("ws-i", "10.1.2.3", 443, 60)
    lines = log_lines(nft_log)
    assert any(
        "add element" in line and "allows_port" in line for line in lines
    )
    assert any(
        "delete element" in line and "allows_any" in line for line in lines
    )


async def test_a_racing_blanket_reject_swaps_for_its_own_flow(
    gated_app, monkeypatch
) -> None:
    """The deny-side reconcile: a blanket RST that lands on a
    newly shared address is withdrawn and re-pinned per-flow (#304
    review, round 2)."""
    app, _consumers, nft_log = gated_app
    net = app.state.net
    await net.start()
    await net.attach("ws-i", want=True, policy=interactive_policy())
    real_reject = manager_mod.nft.reject_element

    async def flipping_reject(settings, workspace_id, ip, port, ttl_s):
        net._services["ws-i"].dns.shared_ips = {ip}
        await real_reject(settings, workspace_id, ip, port, ttl_s)

    monkeypatch.setattr(manager_mod.nft, "reject_element", flipping_reject)
    await net.consent_reject(
        "ws-i", "10.1.2.3", 443, 5, sport=40000, named=True
    )
    lines = log_lines(nft_log)
    assert any(
        "delete element" in line and "rejects" in line for line in lines
    )
    assert any(
        "add element" in line and "rejects_flow" in line for line in lines
    )


async def test_a_literal_forever_pin_survives_a_retraction(
    gated_app,
) -> None:
    """The retraction cannot tell a literal pin from a named one,
    so every literal forever verdict re-pins after the clear (#304
    review, round 2)."""
    app, _consumers, nft_log = gated_app
    net = app.state.net
    await net.start()
    await net.attach("ws-i", want=True, policy=interactive_policy())
    model = app.state.model.egress_consent
    row = await model.create_request("ws-i", "10.1.2.3", 0)
    await model.decide(row["id"], "allowed", "t", "forever")
    await net.retract_consent_pins("ws-i", ["10.1.2.3"])
    lines = log_lines(nft_log)
    assert any(
        "delete element" in line and "allows_any" in line for line in lines
    )
    # The literal forever verdict re-pinned after the withdrawal.
    assert any(
        "add element" in line and "allows_any" in line for line in lines
    )
    assert lines.index(
        next(
            line
            for line in lines
            if "add element" in line and "allows_any" in line
        )
    ) > lines.index(
        next(
            line
            for line in lines
            if "delete element" in line and "allows_any" in line
        )
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


async def test_revoke_flushes_the_per_flow_rsts(gated_app) -> None:
    """Revocation empties the per-flow RST set wholesale (#304):
    its elements name connections, not verdicts, and a dropped one
    re-pins on the flow's next retransmit through the session gate.
    A static workspace defines no such set — its revoke clears its
    elements without a flush."""
    app, _consumers, nft_log = gated_app
    net = app.state.net
    await net.start()
    await net.attach("ws-i", want=True, policy=interactive_policy())
    net._services["ws-i"].dns.remember("api.example", [("203.0.113.7", 300)])
    app.state.settings.net.conntrack_tool = "/nonexistent/conntrack"
    await net.clear_consent_dest("ws-i", "api.example", 443)
    assert any(
        "flush set" in line and "rejects_flow" in line
        for line in log_lines(nft_log)
    )
    # A static workspace defines no per-flow set: its revoke clears
    # its elements without a flush.
    from msks.consent.specs import EgressPolicy

    await net.attach(
        "ws-a", want=True, policy=EgressPolicy("ws-a", "static", ())
    )
    net._services["ws-a"].dns.remember("static.example", [("10.5.5.5", 300)])
    before = len(log_lines(nft_log))
    await net.clear_consent_dest("ws-a", "static.example", 443)
    assert not any("flush set" in line for line in log_lines(nft_log)[before:])


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


# --- the interceptor wiring (#199) ------------------------------------------


class FakeInterceptor:
    """The interceptor surface the net manager touches, recorded."""

    def __init__(self) -> None:
        self.refreshes: list[str] = []
        self.detaches: list[str] = []

    async def refresh(self, workspace_id: str) -> None:
        self.refreshes.append(workspace_id)

    async def on_detach(self, workspace_id: str) -> None:
        self.detaches.append(workspace_id)


async def test_attach_ends_in_an_interceptor_refresh(net_app) -> None:
    """Arming is placeholder-driven: the attachment completes, then
    the interceptor re-evaluates (it arms only if a placeholder is
    live)."""
    app, _ip, _nft = net_app
    recorder = FakeInterceptor()
    app.state.interceptor = recorder
    manager = await ready(app)
    await manager.attach("ws-a", want=True)
    assert recorder.refreshes == ["ws-a"]


async def test_detach_drops_the_interceptor_first(net_app) -> None:
    """The listener goes before the table dies, so an armed
    workspace's redirected flows never reach a proxy whose entries
    are already gone."""
    app, _ip, nft_log = net_app
    recorder = FakeInterceptor()
    app.state.interceptor = recorder
    manager = await ready(app)
    await manager.attach("ws-a", want=True)
    open(nft_log, "w").close()
    await manager.detach("ws-a")
    assert recorder.detaches == ["ws-a"]
    # The table deletion still ran after the disarm.
    assert any(line.startswith("delete table") for line in log_lines(nft_log))


async def test_apply_interception_swaps_the_table(net_app) -> None:
    """Armed: the redirect, the widened input, and the QUIC drop all
    land in one transaction; disarmed: they leave the same way, and
    the consent shape survives both."""
    app, _ip, nft_log = net_app
    manager = await ready(app)
    await manager.attach("ws-a", want=True)
    stdin = Path(str(nft_log) + ".stdin")

    def last_apply() -> str:
        blocks = stdin.read_text().split("--- -f -\n")
        return blocks[-1]

    open(nft_log, "w").close()
    stdin.write_text("")
    await manager.apply_interception("ws-a", 8643)
    assert "redirect to :8643" in last_apply()
    assert "udp dport 443 drop" in last_apply()
    assert "tcp dport 8643 accept" in last_apply()

    stdin.write_text("")
    await manager.apply_interception("ws-a", None)
    assert "redirect" not in last_apply()
    assert "udp dport 443" not in last_apply()
    assert "delete table" in last_apply()  # one transaction, not two


async def test_apply_interception_skips_an_unattached_workspace(
    net_app,
) -> None:
    app, _ip, nft_log = net_app
    manager = await ready(app)
    open(nft_log, "w").close()
    await manager.apply_interception("ws-gone", 8643)
    assert log_lines(nft_log) == []


async def test_attachment_for_answers_the_live_attachment(net_app) -> None:
    app, _ip, _nft = net_app
    manager = await ready(app)
    assert app.state.net.attachment_for("ws-a") is None
    attachment = await manager.attach("ws-a", want=True)
    assert app.state.net.attachment_for("ws-a") is attachment


async def test_a_gated_swap_carries_consent_elements_across(
    gated_app, monkeypatch
) -> None:
    """The whole-table swap preserves the kernel-side consent
    elements (#260 review): a gated workspace's verdict pins are
    dumped before the swap and restored after it."""
    from msks.net import nft as nft_mod

    app, _consumers, nft_log = gated_app
    await app.state.net.start()
    await app.state.net.attach("ws-i", want=True, policy=interactive_policy())

    async def fake_dump(settings, workspace_id):
        return {"allows_any": [("10.1.2.3", 45)]}

    monkeypatch.setattr(nft_mod, "dump_consent_elements", fake_dump)
    stdin = Path(str(nft_log) + ".stdin")
    stdin.write_text("")
    await app.state.net.apply_interception("ws-i", 8643)
    applied = stdin.read_text()
    # One transaction: the table swap and the element carry ride the
    # same file (#260 review) — the sets exist when the adds apply.
    assert "redirect to :8643" in applied
    assert "add element inet" in applied
    assert "10.1.2.3 timeout 45s" in applied
    assert applied.index("table inet") < applied.index("add element")


async def test_an_allow_mode_swap_dumps_nothing(net_app) -> None:
    """An ungated workspace has no consent sets: the swap skips the
    dump entirely."""
    app, _ip, nft_log = net_app
    manager = await ready(app)
    await manager.attach("ws-a", want=True)
    open(nft_log, "w").close()
    await manager.apply_interception("ws-a", 8643)
    assert not any(line.startswith("list set") for line in log_lines(nft_log))
    assert "redirect to :8643" in Path(str(nft_log) + ".stdin").read_text()


async def test_a_disarming_swap_carries_consent_elements_too(
    gated_app, monkeypatch
) -> None:
    """Disarm swaps the same table: the consent carry rides that
    transaction as well."""
    from msks.net import nft as nft_mod

    app, _consumers, nft_log = gated_app
    await app.state.net.start()
    await app.state.net.attach("ws-i", want=True, policy=interactive_policy())

    async def fake_dump(settings, workspace_id):
        return {"rejects": [("10.2.3.4 . 25", 9)]}

    monkeypatch.setattr(nft_mod, "dump_consent_elements", fake_dump)
    stdin = Path(str(nft_log) + ".stdin")
    stdin.write_text("")
    await app.state.net.apply_interception("ws-i", None)
    applied = stdin.read_text()
    assert "redirect" not in applied
    assert "10.2.3.4 . 25 timeout 9s" in applied


# --- the per-tap LLM listener (#259) -----------------------------------------


class RecordingListener:
    """The llm seam's listener: records its lifecycle, refuses on
    demand."""

    def __init__(self, tap_ip: str, *, fail: bool = False) -> None:
        self.tap_ip = tap_ip
        self.fail = fail
        self.started = 0
        self.stopped = 0

    async def start(self) -> None:
        if self.fail:
            raise OSError("address in use")
        self.started += 1

    async def stop(self) -> None:
        self.stopped += 1


async def llm_seamed_net_app(tmp_path: Path, monkeypatch, listeners: dict):
    """The net_app fixture's shape plus an llm_factory that hands out
    RecordingListeners (one per attachment), so no real socket
    binds."""
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
        llm=LlmSettings(models=("*:http://up.stream/v1:sk-x",)),
    )
    app = build_app(settings)
    monkeypatch.setattr(
        manager_mod, "verify_forwarding", lambda path=None: None
    )

    def factory(attachment):
        listener = RecordingListener(attachment.tap_ip)
        listeners[attachment.workspace_id] = listener
        return listener

    manager = NetManager(
        app,
        dhcp_factory=FakeService,
        dns_factory=FakeService,
        llm_factory=factory,
    )
    app.state.net = manager
    app.state.model.migrate()
    for wid in ("ws-a", "ws-b"):
        await app.state.model.create_workspace(
            VmSpec(
                workspace_id=wid,
                kernel=Path("/k"),
                rootfs=Path("/r"),
                egress=True,
            )
        )
    return app


async def test_attach_starts_the_llm_listener_and_detaches_stop_it(
    tmp_path: Path, monkeypatch
) -> None:
    listeners: dict = {}
    app = await llm_seamed_net_app(tmp_path, monkeypatch, listeners)
    await app.state.net.start()
    attachment = await app.state.net.attach("ws-a", want=True)
    proxy = app.state.llm
    listener = listeners["ws-a"]
    assert listener.started == 1
    # The proxy's mapping answers auth: this tap's address is this
    # workspace.
    assert proxy._by_tap_ip[attachment.tap_ip] == "ws-a"
    await app.state.net.detach("ws-a")
    assert listener.stopped == 1
    assert attachment.tap_ip not in proxy._by_tap_ip


async def test_attach_without_models_starts_no_listener(
    net_app,
) -> None:
    """The default factory path with an unconfigured daemon: no
    listener, no mapping (and the boot still succeeds)."""
    app, _ip, _nft = net_app
    await ready(app)
    await app.state.net.attach("ws-a", want=True)
    assert app.state.llm._listeners == {}
    await app.state.net.detach("ws-a")


async def test_a_refused_llm_bind_leaves_the_boot_alive(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    listeners: dict = {}

    def factory(attachment):
        listener = RecordingListener(attachment.tap_ip, fail=True)
        listeners[attachment.workspace_id] = listener
        return listener

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
        llm=LlmSettings(models=("*:http://up.stream/v1:sk-x",)),
    )
    app = build_app(settings)
    monkeypatch.setattr(
        manager_mod, "verify_forwarding", lambda path=None: None
    )
    manager = NetManager(
        app,
        dhcp_factory=FakeService,
        dns_factory=FakeService,
        llm_factory=factory,
    )
    app.state.net = manager
    app.state.model.migrate()
    await app.state.model.create_workspace(
        VmSpec(
            workspace_id="ws-a",
            kernel=Path("/k"),
            rootfs=Path("/r"),
            egress=True,
        )
    )
    await app.state.net.start()
    attachment = await app.state.net.attach("ws-a", want=True)
    # The boot proceeded; the warning says what happened.
    assert attachment is not None
    assert "did not start" in capsys.readouterr().out
    assert app.state.llm._listeners == {}
    await app.state.net.detach("ws-a")


async def test_attach_and_detach_work_without_an_llm_subsystem(
    net_app,
) -> None:
    """A NetManager whose app carries no llm subsystem (the seam a
    stripped-down host would present) attaches and detaches
    untouched: the listener step is absent, not fatal."""
    app, _ip, _nft = net_app
    app.state.llm = None
    await ready(app)
    attachment = await app.state.net.attach("ws-a", want=True)
    assert attachment is not None
    await app.state.net.detach("ws-a")


async def test_a_boot_survives_an_unbindable_llm_address(
    net_app, monkeypatch, capsys
) -> None:
    """The proxy is an auxiliary surface: whatever keeps its
    listener from binding — an address the host cannot bind, a
    port another daemon holds — logs loudly and the workspace
    still boots (#259). Model entries themselves parse at the
    first request, never here."""
    app, _ip, _nft = net_app
    app.state.settings.llm.models = ("*:http://up.stream/v1:sk-x",)
    monkeypatch.setattr(
        manager_mod, "verify_forwarding", lambda path=None: None
    )
    manager = NetManager(
        app, dhcp_factory=FakeService, dns_factory=FakeService
    )
    app.state.net = manager
    await manager.start()
    attachment = await manager.attach("ws-a", want=True)
    assert attachment is not None
    assert "did not start" in capsys.readouterr().out
    await manager.detach("ws-a")


# --- the live mode switch (#280) --------------------------------------


def applied_rulesets(nft_log: Path) -> list[str]:
    """Every ``-f -`` application's stdin, in order — the last is
    the table the stub now enforces."""
    stdin = nft_log.with_name(nft_log.name + ".stdin").read_text()
    blocks = stdin.split("--- -f -\n")
    return [block for block in blocks if block.strip()]


async def held_request(app, workspace_id: str, host: str) -> dict:
    """One live hold in the engine (the switch-out path's probe)."""
    return await app.state.model.egress_consent.create_request(
        workspace_id, host, 443
    )


async def test_apply_policy_switches_a_live_workspace_into_interactive(
    gated_app,
) -> None:
    app, consumers, nft_log = gated_app
    await app.state.net.start()
    from msks.consent.specs import EgressPolicy

    await app.state.net.attach(
        "ws-i", want=True, policy=EgressPolicy("ws-i", "static", (".a.de",))
    )
    switched = await app.state.net.apply_policy(
        "ws-i", EgressPolicy("ws-i", "interactive", (".a.de",))
    )
    assert switched is True
    # The queue bound before the chain references it (the boot
    # rule, run against a live tap) and the applied ruleset carries
    # the queue gate.
    assert consumers and consumers[0].started
    last = applied_rulesets(nft_log)[-1]
    assert f"queue num {consumers[0].queue_num}" in last
    services = app.state.net._services["ws-i"]
    assert services.queue_num == consumers[0].queue_num
    assert services.policy.interactive
    # The resolver gate flipped with the chain.
    assert services.gate.policy.interactive


async def test_apply_policy_out_of_interactive_fail_closes_and_unbinds(
    gated_app,
) -> None:
    app, consumers, nft_log = gated_app
    await app.state.net.start()
    await app.state.net.attach("ws-i", want=True, policy=interactive_policy())
    from msks.consent.specs import EgressPolicy

    request = await held_request(app, "ws-i", "203.0.113.9")
    hold = app.state.consent.register_hold(request)
    switched = await app.state.net.apply_policy(
        "ws-i", EgressPolicy("ws-i", "allow", ())
    )
    assert switched is True
    # The hold answered deny (reason names the switch), the row
    # expired, and the consumer unbound after the queue-less table
    # applied — the last ruleset carries no queue gate.
    assert hold.result()["decision"] == "deny"
    assert hold.result()["reason"] == "mode switch"
    row = await app.state.model.egress_consent.get_request(request["id"])
    assert row["decision"] == "expired"
    assert consumers[0].stopped
    last = applied_rulesets(nft_log)[-1]
    assert "queue num" not in last
    services = app.state.net._services["ws-i"]
    assert services.consumer is None
    assert services.gate.policy.mode == "allow"


async def test_apply_policy_carries_elements_between_gated_modes(
    gated_app, monkeypatch
) -> None:
    app, _consumers, nft_log = gated_app
    await app.state.net.start()
    await app.state.net.attach("ws-i", want=True, policy=interactive_policy())
    from msks.consent.specs import EgressPolicy

    async def dumped(settings, workspace_id):
        return {"allows_any": [("10.1.2.3", 60)]}

    monkeypatch.setattr(manager_mod.nft, "dump_consent_elements", dumped)
    await app.state.net.apply_policy(
        "ws-i", EgressPolicy("ws-i", "static", (".a.de",))
    )
    last = applied_rulesets(nft_log)[-1]
    assert "add element inet" in last and "allows_any" in last
    # A switch to allow carries nothing: the allow-mode table has
    # no consent sets, and an element statement naming an absent
    # set would fail the whole transaction.
    await app.state.net.apply_policy("ws-i", EgressPolicy("ws-i", "allow", ()))
    last = applied_rulesets(nft_log)[-1]
    assert "add element" not in last


async def test_apply_policy_into_gated_replays_address_verdicts(
    gated_app,
) -> None:
    app, _consumers, nft_log = gated_app
    await app.state.net.start()
    from msks.consent.specs import EgressPolicy

    await app.state.net.attach(
        "ws-i", want=True, policy=EgressPolicy("ws-i", "allow", ())
    )
    # A durable address verdict from the workspace's interactive
    # past: the fresh sets need it pinned.
    request = await app.state.model.egress_consent.create_request(
        "ws-i", "198.51.100.7", 0
    )
    await app.state.model.egress_consent.decide(
        request["id"], "allowed", "token", "forever"
    )
    await app.state.net.apply_policy(
        "ws-i", EgressPolicy("ws-i", "static", (".a.de",))
    )
    lines = log_lines(nft_log)
    assert any(
        "add element" in line and "allows_any" in line for line in lines
    )


async def test_apply_policy_interactive_to_interactive_keeps_the_consumer(
    gated_app,
) -> None:
    app, consumers, nft_log = gated_app
    await app.state.net.start()
    await app.state.net.attach("ws-i", want=True, policy=interactive_policy())
    from msks.consent.specs import EgressPolicy

    await app.state.net.apply_policy(
        "ws-i", EgressPolicy("ws-i", "interactive", (".b.de",))
    )
    assert len(consumers) == 1  # no second bind; the first still runs
    assert not consumers[0].stopped
    last = applied_rulesets(nft_log)[-1]
    assert ".b.de" not in last  # name specs gate at the resolver...
    services = app.state.net._services["ws-i"]
    assert services.policy.host_specs[0].host == "b.de"  # ...not the chain
    assert services.gate.policy.mode == "interactive"


async def test_apply_policy_without_an_attachment_is_row_only(
    gated_app,
) -> None:
    app, _consumers, nft_log = gated_app
    await app.state.net.start()
    from msks.consent.specs import EgressPolicy

    before = len(log_lines(nft_log))
    switched = await app.state.net.apply_policy(
        "ws-i", EgressPolicy("ws-i", "interactive", ())
    )
    assert switched is False
    assert len(log_lines(nft_log)) == before  # no table touched


async def test_allow_mode_boot_skips_the_forever_replay(
    gated_app,
) -> None:
    app, _consumers, nft_log = gated_app
    await app.state.net.start()
    from msks.consent.specs import EgressPolicy

    request = await app.state.model.egress_consent.create_request(
        "ws-i", "198.51.100.7", 0
    )
    await app.state.model.egress_consent.decide(
        request["id"], "allowed", "token", "forever"
    )
    await app.state.net.attach(
        "ws-i", want=True, policy=EgressPolicy("ws-i", "allow", ())
    )
    # The allow-mode table carries no consent sets; the boot pins
    # nothing (reachable only after a live switch left the row in
    # allow mode carrying forever verdicts).
    assert not [ln for ln in log_lines(nft_log) if "add element" in ln]


async def test_a_failed_swap_unbinds_the_fresh_consumer(
    gated_app, monkeypatch
) -> None:
    """A swap whose transaction fails leaves the old table
    enforcing and unbinds a consumer the failed chain never
    referenced — a bound queue no chain points at is a leak
    (#280)."""
    from msks.consent.specs import EgressPolicy

    app, consumers, _nft_log = gated_app
    await app.state.net.start()
    await app.state.net.attach(
        "ws-i", want=True, policy=EgressPolicy("ws-i", "static", ())
    )
    monkeypatch.setenv("NFT_FAIL_AT", "-f -")
    try:
        with pytest.raises(MicrovmError):
            await app.state.net.apply_policy(
                "ws-i", EgressPolicy("ws-i", "interactive", ())
            )
        # The freshly-bound consumer (abort with a consumer) ...
        assert consumers[0].started and consumers[0].stopped
        # ... and a consumer-less switch aborts clean too.
        with pytest.raises(MicrovmError):
            await app.state.net.apply_policy(
                "ws-i", EgressPolicy("ws-i", "allow", ())
            )
    finally:
        monkeypatch.delenv("NFT_FAIL_AT")
    # The old posture is still recorded: nothing committed.
    services = app.state.net._services["ws-i"]
    assert services.consumer is None
    assert services.policy.mode == "static"


async def test_the_switch_pins_bind_before_reference_and_unbind_after(
    gated_app, monkeypatch
) -> None:
    """The ordering invariants as one ordered log (#280 review):
    entering interactive records the consumer's bind BEFORE the
    queue-referencing chain applies; leaving records the queue-less
    table BEFORE the unbind — an unbound queue drops, so the order
    is the whole rule."""
    from msks.consent.specs import EgressPolicy

    app, _consumers, _nft_log = gated_app
    await app.state.net.start()
    manager = app.state.net
    events: list[str] = []

    def factory(workspace_id, queue_num, net):
        consumer = FakeConsumer(workspace_id, queue_num, net)
        start, stop = consumer.start, consumer.stop

        consumer.start = lambda: (events.append("bind"), start())
        consumer.stop = lambda: (events.append("unbind"), stop())
        return consumer

    manager._consumer_factory = factory
    real_install = manager_mod.nft.install_vm

    async def recording_install(*args, **kwargs):
        events.append("install")
        return await real_install(*args, **kwargs)

    monkeypatch.setattr(manager_mod.nft, "install_vm", recording_install)
    await manager.attach(
        "ws-i", want=True, policy=EgressPolicy("ws-i", "static", ())
    )
    await manager.apply_policy("ws-i", EgressPolicy("ws-i", "interactive", ()))
    assert events[-2:] == ["bind", "install"]  # bind BEFORE reference
    await manager.apply_policy("ws-i", EgressPolicy("ws-i", "static", ()))
    assert events[-2:] == ["install", "unbind"]  # unbind AFTER deref


async def test_the_carry_drops_rejects_leaving_interactive(
    gated_app, monkeypatch
) -> None:
    """A static table defines no rejects set (#280 review): the
    carry into static must not emit an element statement naming
    it — real nft would abort the whole transaction, leaving the
    row and the live table divergent."""
    from msks.consent.specs import EgressPolicy

    app, _consumers, nft_log = gated_app
    await app.state.net.start()
    manager = app.state.net
    await manager.attach("ws-i", want=True, policy=interactive_policy())

    async def dumped(settings, workspace_id):
        return {
            "allows_any": [("10.1.2.3", 60)],
            "rejects": [("203.0.113.9", 600)],
        }

    monkeypatch.setattr(manager_mod.nft, "dump_consent_elements", dumped)
    await manager.apply_policy(
        "ws-i", EgressPolicy("ws-i", "static", (".a.de",))
    )
    last = applied_rulesets(nft_log)[-1]
    assert "allows_any" in last  # the allow pin rides
    assert "rejects" not in last  # the deny pin cannot: no such set


async def test_allow_mode_pins_no_elements(gated_app) -> None:
    """consent_allow/consent_reject skip a live allow-mode
    workspace (#280 review): the resolver still LEARNs under a
    carried forever allow, and the pin would spawn a doomed nft
    run per answer into a table with no consent sets."""
    from msks.consent.specs import EgressPolicy

    app, _consumers, nft_log = gated_app
    await app.state.net.start()
    manager = app.state.net
    await manager.attach(
        "ws-i", want=True, policy=EgressPolicy("ws-i", "allow", ())
    )
    before = len(log_lines(nft_log))
    await manager.consent_allow("ws-i", "10.1.2.3", None, 60)
    await manager.consent_reject("ws-i", "203.0.113.9", 443, 5)
    assert len(log_lines(nft_log)) == before


async def test_workspace_guard_reenters_and_serializes() -> None:
    """The net guard is re-entrant for the task that holds it (the
    interceptor's arm/disarm run under the attach/detach/switch
    that drove them) and serializing for every other writer (#280
    review, round 2)."""
    guard = manager_mod.WorkspaceGuard()
    order: list[str] = []
    async with guard:
        async with guard:  # same task: no block
            order.append("inner")
    assert order == ["inner"]

    held = asyncio.Event()
    release = asyncio.Event()

    async def holder():
        async with guard:
            held.set()
            await release.wait()

    async def waiter():
        await held.wait()
        async with guard:
            order.append("waiter")

    hold_task = asyncio.create_task(holder())
    wait_task = asyncio.create_task(waiter())
    await held.wait()
    await asyncio.sleep(0.01)
    assert order == ["inner"]  # the waiter is still outside
    release.set()
    await asyncio.gather(hold_task, wait_task)
    assert order == ["inner", "waiter"]


async def test_apply_interception_takes_the_workspace_guard(
    gated_app, monkeypatch
) -> None:
    """The interception swap runs under the guard: a mode switch in
    flight holds it off, so a placeholder sweep's install cannot
    land on the switch's table shape (#280 review, round 2)."""
    from msks.consent.specs import EgressPolicy

    app, _consumers, _nft_log = gated_app
    await app.state.net.start()
    manager = app.state.net
    await manager.attach(
        "ws-i", want=True, policy=EgressPolicy("ws-i", "static", ())
    )
    order: list[str] = []
    guard = manager.workspace_guard("ws-i")
    real_swap = manager.swap_interception

    async def swap_after_switch(workspace_id, port):
        order.append("swap")
        return await real_swap(workspace_id, port)

    monkeypatch.setattr(manager, "swap_interception", swap_after_switch)

    async def holding_switch():
        async with guard:
            order.append("switch")
            await asyncio.sleep(0.05)  # the switch's install window

    switch_task = asyncio.create_task(holding_switch())
    await asyncio.sleep(0.01)
    await manager.apply_interception("ws-i", None)
    await switch_task
    assert order == ["switch", "swap"]
