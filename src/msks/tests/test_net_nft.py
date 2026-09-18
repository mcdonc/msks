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
    # nothing else in the appliance — and the replies to connections
    # the appliance itself opened into the guest (the forward's
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


async def test_apply_base_and_install_vm(tools) -> None:
    settings, log = tools
    await nft.apply_base(settings)
    await nft.install_vm(
        settings, "ws-a", "msks-tap", "172.31.0.1", "172.31.0.2"
    )
    # install converges: the old table drops before the fresh one.
    assert log_lines(log) == [
        "-f -",
        f"delete table inet {table_name('ws-a')}",
        "-f -",
    ]


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
