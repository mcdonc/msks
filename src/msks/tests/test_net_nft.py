"""The nftables rulesets and their application (#52)."""

from pathlib import Path

import pytest
from msks.microvm.errors import MicrovmError
from msks.net import nft
from msks.net.alloc import table_name
from msks.settings import NetSettings, Settings
from netstubs import NFT_FAIL_AT, NFT_STDERR, log_lines, stub_nft


@pytest.fixture
def tools(tmp_path: Path):
    log = tmp_path / "nft.log"
    settings = Settings(net=NetSettings(nft_tool=str(stub_nft(tmp_path, log))))
    return settings, log


def test_base_ruleset_masquerades_the_uplink() -> None:
    ruleset = nft.base_ruleset("eth0")
    assert f"table inet {nft.BASE_TABLE}" in ruleset
    assert "type nat hook postrouting priority srcnat" in ruleset
    assert 'oifname "eth0" masquerade' in ruleset


def test_vm_ruleset_scopes_the_tap() -> None:
    ruleset = nft.vm_ruleset(
        "ws-a", "msks-tap", "172.31.0.1", "172.31.0.2", "eth0"
    )
    assert f"table inet {table_name('ws-a')}" in ruleset
    # Forward: only this guest's source leaves via the uplink; only
    # established replies come back toward the tap; everything else
    # in either direction across this tap drops (which is also what
    # blocks guest-to-guest hops between two taps).
    assert "type filter hook forward priority filter" in ruleset
    assert (
        'iifname "msks-tap" ip saddr 172.31.0.1 oifname "eth0" accept'
        in ruleset
    )
    assert 'oifname "msks-tap" ct state established,related accept' in ruleset
    assert 'oifname "msks-tap" drop' in ruleset
    assert 'iifname "msks-tap" drop' in ruleset
    # Input: the guest reaches exactly DHCP and the resolver —
    # nothing else on the host — and the replies to connections
    # the host itself opened into the guest (the forward's
    # dial, #109) return on their conntrack state; a guest-initiated
    # connection arrives state NEW and never matches it.
    assert "type filter hook input priority filter" in ruleset
    assert 'iifname "msks-tap" udp dport 67 accept' in ruleset
    assert (
        'iifname "msks-tap" ip saddr 172.31.0.1 '
        "ip daddr 172.31.0.2 udp dport 53 accept"
    ) in ruleset
    assert (
        'iifname "msks-tap" ip saddr 172.31.0.1 '
        "ct state established,related accept" in ruleset
    )
    assert "tcp dport 53" not in ruleset  # UDP-only resolver (#70 review)
    # Order is load-bearing in both chains: an accept after its drop
    # is dead code, and a dead established accept is exactly the
    # bug the forward dial once died of (#110's smoke). Scoped per
    # chain — both chains carry iifname drops.
    egress = ruleset[
        ruleset.index("chain egress") : ruleset.index("chain ingress")
    ]
    ingress = ruleset[ruleset.index("chain ingress") :]
    assert egress.index(
        'oifname "msks-tap" ct state established,related accept'
    ) < egress.index('oifname "msks-tap" drop')
    assert ingress.index(
        'iifname "msks-tap" ip saddr 172.31.0.1 '
        "ct state established,related accept"
    ) < ingress.index('iifname "msks-tap" drop')


async def test_apply_base_and_install_vm(tools, monkeypatch) -> None:
    settings, log = tools
    await nft.apply_base(settings)
    # The stub answers success for everything by default, so the
    # probe must be told the table is absent for a fresh install.
    monkeypatch.setenv(NFT_FAIL_AT, "list table")
    await nft.install_vm(
        settings, "ws-a", "msks-tap", "172.31.0.1", "172.31.0.2"
    )
    assert log_lines(log) == [
        "-f -",
        f"list table inet {table_name('ws-a')}",
        "-f -",
    ]
    # A fresh install applies the ruleset alone.
    stdin = Path(str(log) + ".stdin").read_text()
    assert f"table inet {table_name('ws-a')} {{" in stdin
    assert "delete table" not in stdin


async def test_install_vm_swaps_atomically(tools) -> None:
    """A table that exists is replaced in ONE nft transaction
    (#199): the delete and the re-add ride the same ``-f`` file, so
    no guest SYN slips between them past the redirect."""
    settings, log = tools
    await nft.install_vm(
        settings, "ws-a", "msks-tap", "172.31.0.1", "172.31.0.2"
    )
    assert log_lines(log) == [
        f"list table inet {table_name('ws-a')}",
        "-f -",
    ]
    stdin = Path(str(log) + ".stdin").read_text()
    applies = [
        block
        for block in stdin.split("--- -f -\n")
        if block.startswith("delete table")
    ]
    assert len(applies) == 1
    assert applies[0].startswith(
        f"delete table inet {table_name('ws-a')}\n"
        f"table inet {table_name('ws-a')} {{"
    )


async def test_delete_vm_table_tolerates_absence(tools, monkeypatch) -> None:
    settings, log = tools
    monkeypatch.setenv(NFT_FAIL_AT, "delete table")
    monkeypatch.setenv(
        NFT_STDERR,
        "netlink: Error: cache initialization failed: "
        "No such file or directory",
    )
    await nft.delete_vm_table(settings, "ws-a")
    assert log_lines(log) == [f"delete table inet {table_name('ws-a')}"]


async def test_nft_run_surfaces_real_failures(tools, monkeypatch) -> None:
    settings, _log = tools
    monkeypatch.setenv(NFT_FAIL_AT, "-f -")
    monkeypatch.setenv(NFT_STDERR, "syntax error")
    with pytest.raises(MicrovmError, match="nft base ruleset apply failed"):
        await nft.apply_base(settings)


async def test_nft_run_names_a_missing_tool(tmp_path: Path) -> None:
    settings = Settings(net=NetSettings(nft_tool=str(tmp_path / "not-there")))
    with pytest.raises(MicrovmError, match="tool not found"):
        await nft.apply_base(settings)


async def test_install_vm_pins_the_workspace_ruleset(tools) -> None:
    """The ruleset that reaches nft names THIS workspace's tap and
    guest address (#70 review) — chain↔workspace scoping, end to end
    through the stub's captured stdin."""
    settings, log = tools
    await nft.install_vm(
        settings, "ws-pin", "msks-pinned", "172.31.0.1", "172.31.0.2"
    )
    applied = log.with_name(log.name + ".stdin").read_text()
    assert 'iifname "msks-pinned" ip saddr 172.31.0.1' in applied
    assert "msks-e-" in applied


# --- consent chain shapes and flow elements (#69) ----------------------------


def test_armed_ruleset_redirects_web_egress() -> None:
    """The armed shape (#199): a prerouting redirect of the guest's
    TCP 80/443 to the per-tap listener, the input chain widened to
    that listener ahead of the tap's drop, and the guest's QUIC
    dead so nothing routes around the redirect."""
    ruleset = nft.vm_ruleset(
        "ws-a",
        "msks-tap",
        "172.31.0.1",
        "172.31.0.2",
        "eth0",
        interceptor_port=8643,
    )
    prerouting = ruleset[
        ruleset.index("chain intercept") : ruleset.index("chain egress")
    ]
    assert (
        "type nat hook prerouting priority dstnat; policy accept;"
        in prerouting
    )
    assert (
        'iifname "msks-tap" tcp dport { 80, 443 } redirect to :8643'
        in prerouting
    )
    egress = ruleset[
        ruleset.index("chain egress") : ruleset.index("chain ingress")
    ]
    assert 'iifname "msks-tap" udp dport 443 drop' in egress
    assert egress.index("udp dport 443 drop") < egress.index(
        'oifname "eth0" accept'
    )
    ingress = ruleset[ruleset.index("chain ingress") :]
    assert (
        'iifname "msks-tap" ip saddr 172.31.0.1 ip daddr 172.31.0.2 '
        "tcp dport 8643 accept" in ingress
    )
    assert ingress.index("tcp dport 8643 accept") < ingress.index(
        'iifname "msks-tap" drop'
    )


def test_disarmed_ruleset_carries_no_interception() -> None:
    """The default shape keeps #52's posture: no redirect, no input
    widening, no QUIC drop."""
    ruleset = nft.vm_ruleset(
        "ws-a", "msks-tap", "172.31.0.1", "172.31.0.2", "eth0"
    )
    assert "redirect" not in ruleset
    assert "8643" not in ruleset
    assert "udp dport 443" not in ruleset


def policy(mode: str, specs=()):
    from msks.consent.specs import EgressPolicy

    return EgressPolicy("ws-a", mode, tuple(specs))


def test_allow_mode_keeps_the_52_posture_plus_lockout() -> None:
    ruleset = nft.vm_ruleset(
        "ws-a",
        "msks-tap",
        "172.31.0.1",
        "172.31.0.2",
        "eth0",
        policy=policy("allow"),
    )
    assert "queue num" not in ruleset
    assert "set " not in ruleset
    # The DNS lockout precedes the uplink accept.
    egress = ruleset[
        ruleset.index("chain egress") : ruleset.index("chain ingress")
    ]
    assert egress.index("tcp dport { 53, 853 } drop") < egress.index(
        'oifname "eth0" accept'
    )
    assert egress.index("udp dport { 53, 853 } drop") < egress.index(
        'oifname "eth0" accept'
    )


def test_static_mode_drops_unmatched_new_traffic() -> None:

    ruleset = nft.vm_ruleset(
        "ws-a",
        "msks-tap",
        "172.31.0.1",
        "172.31.0.2",
        "eth0",
        policy=policy("static", ("10.0.0.0/8", "203.0.113.7:5432")),
    )
    assert "queue num" not in ruleset
    assert 'iifname "msks-tap" ip daddr 10.0.0.0/8 accept' in ruleset
    assert (
        'iifname "msks-tap" ip daddr 203.0.113.7/32 tcp dport 5432'
        " accept" in ruleset
    )
    # The allow-set matches must render in the static shape too, and
    # ahead of the terminal drop: a name-spec allowlist entry pins
    # through them (the KVM smoke caught their absence once — the
    # resolver pinned into sets nothing matched).
    assert "ip daddr @allows_any accept" in ruleset
    assert "ip daddr . tcp dport @allows_port accept" in ruleset
    assert ruleset.index("ip daddr @allows_any accept") < ruleset.index(
        'oifname "eth0" drop'
    )
    # The default for unmatched new traffic is the drop.
    assert 'oifname "eth0" drop' in ruleset
    assert 'oifname "eth0" accept' not in ruleset


def test_interactive_mode_queues_new_flows() -> None:
    ruleset = nft.vm_ruleset(
        "ws-a",
        "msks-tap",
        "172.31.0.1",
        "172.31.0.2",
        "eth0",
        policy=policy("interactive"),
        queue_num=1107,
    )
    egress = ruleset[
        ruleset.index("chain egress") : ruleset.index("chain ingress")
    ]
    # Established beats the gates; the allow matches beat the
    # reject match (an allow pin outranks a lingering reject — the
    # supersede rule); everything beats the queue and the final
    # drop.
    assert egress.index("ct state established,related accept") < egress.index(
        "@allows_any accept"
    )
    assert egress.index("@allows_any accept") < egress.index(
        "@allows_port accept"
    )
    assert egress.index("@allows_port accept") < egress.index("@rejects")
    assert egress.index("@rejects reject with tcp reset") < egress.index(
        "queue num"
    )
    assert egress.index("queue num 1107") < egress.index('oifname "eth0" drop')
    # Only NEW flows queue: an established flow's later packets
    # never re-enter consent (once per flow, not per cache window).
    assert "ct state new queue num 1107" in ruleset
    # No bypass flag: an unbound or full queue drops (fail-closed).
    assert "queue bypass" not in ruleset
    # The timeout-bearing sets exist.
    assert "type ipv4_addr; flags timeout;" in ruleset
    assert "type ipv4_addr . inet_service; flags timeout;" in ruleset


async def test_flow_elements_round_trip(tools) -> None:
    settings, log = tools
    await nft.allow_element(settings, "ws-el", "10.1.2.3", None, 300)
    await nft.allow_element(settings, "ws-el", "10.1.2.3", 443, 90)
    await nft.reject_element(settings, "ws-el", "10.1.2.3", 443, 10)
    await nft.clear_elements(settings, "ws-el", "10.1.2.3", 443)
    lines = log_lines(log)
    assert (
        f"add element inet {table_name('ws-el')} allows_any "
        "{ 10.1.2.3 timeout 300s }" in lines
    )
    assert (
        f"add element inet {table_name('ws-el')} allows_port "
        "{ 10.1.2.3 . 443 timeout 90s }" in lines
    )
    assert (
        f"add element inet {table_name('ws-el')} rejects "
        "{ 10.1.2.3 . 443 timeout 10s }" in lines
    )
    assert (
        f"delete element inet {table_name('ws-el')} allows_any "
        "{ 10.1.2.3 }" in lines
    )
    assert (
        f"delete element inet {table_name('ws-el')} allows_port "
        "{ 10.1.2.3 . 443 }" in lines
    )


async def test_clear_elements_tolerates_absence(tools, monkeypatch) -> None:
    settings, _log = tools
    monkeypatch.setenv(NFT_FAIL_AT, "delete element")
    monkeypatch.setenv(NFT_STDERR, "netlink: Error: No such file or directory")
    await nft.clear_elements(settings, "ws-el", "10.1.2.3", None)


async def test_install_vm_passes_the_policy_shape(tools) -> None:
    settings, log = tools
    await nft.install_vm(
        settings,
        "ws-gated",
        "msks-tap",
        "172.31.0.1",
        "172.31.0.2",
        policy=policy("interactive"),
        queue_num=4242,
    )
    applied = log.with_name(log.name + ".stdin").read_text()
    assert "queue num 4242" in applied
    assert "allows_any" in applied


async def test_table_exists_tolerates_a_missing_tool(tmp_path) -> None:
    """The probe reports no table when the tool is absent, so the
    apply below is the call that names the missing binary."""
    settings = Settings(net=NetSettings(nft_tool=str(tmp_path / "absent")))
    assert await nft.table_exists(settings, "ws-a") is False


# --- consent elements across a swap (#260 review) ---------------------------


def json_set(name: str, elems: list) -> bytes:
    import json as json_mod

    return json_mod.dumps(
        {"nftables": [{"set": {"name": name, "elem": elems}}]}
    )


def test_element_scopes_reads_both_set_kinds() -> None:
    payload = json_set(
        "allows_port",
        [
            {"elem": {"val": ["10.1.2.3", 443], "timeout": 2987}},
            {"elem": {"val": "10.9.9.9"}},
        ],
    )
    assert nft.element_scopes(payload) == [
        ("10.1.2.3 . 443", 2987),
        ("10.9.9.9", None),
    ]


def test_element_scopes_tolerates_garbage() -> None:
    assert nft.element_scopes(b"not json") == []
    assert nft.element_scopes(b"{}") == []
    assert nft.element_scopes(json_set("x", [{"elem": {"timeout": 5}}])) == []


def test_element_statements_render_the_restore_file() -> None:
    body = nft.element_statements(
        "msks-e-x",
        {
            "allows_any": [("10.1.2.3", 45)],
            "rejects": [("10.2.3.4 . 25", None)],
            "allows_port": [],
        },
    )
    assert (
        body
        == "add element inet msks-e-x allows_any { 10.1.2.3 timeout 45s }\n"
        "add element inet msks-e-x rejects { 10.2.3.4 . 25 }\n"
    )


async def test_dump_reads_and_restore_writes(tools) -> None:
    settings, log = tools
    dumped = await nft.dump_consent_elements(settings, "ws-a")
    assert dumped == {}  # the stub answers nothing for list set
    assert [line.split()[4] for line in log_lines(log)] == [
        "allows_any",
        "allows_port",
        "rejects",
    ]
    log.write_text("")
    stdin = Path(str(log) + ".stdin")
    stdin.write_text("")
    await nft.restore_consent_elements(
        settings, "ws-a", {"allows_any": [("10.1.2.3", 45)]}
    )
    assert "add element" in stdin.read_text()
    # An empty dump restores nothing.
    log.write_text("")
    await nft.restore_consent_elements(settings, "ws-a", {})
    assert log_lines(log) == []


async def test_dump_reads_a_real_listing(tools, monkeypatch) -> None:
    """The dump path end to end against a JSON listing: the elements
    parse into the snapshot restore consumes."""
    settings, _log = tools

    async def fake_json(settings, args):
        assert args[:4] == ["list", "set", "inet", table_name("ws-a")]
        if args[4] == "allows_any":
            return json_set(
                "allows_any", [{"elem": {"val": "10.1.2.3", "timeout": 60}}]
            )
        if args[4] == "allows_port":
            # A set that exists but holds nothing: the dump skips it.
            return json_set("allows_port", [])
        return None  # the third set is absent

    monkeypatch.setattr(nft, "nft_json", fake_json)
    assert await nft.dump_consent_elements(settings, "ws-a") == {
        "allows_any": [("10.1.2.3", 60)]
    }


async def test_dump_tolerates_a_missing_tool(tmp_path) -> None:
    settings = Settings(net=NetSettings(nft_tool=str(tmp_path / "absent")))
    assert await nft.dump_consent_elements(settings, "ws-a") == {}
