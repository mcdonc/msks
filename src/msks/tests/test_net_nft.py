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
    ruleset = nft.vm_ruleset("ws-a", "msks-tap", "172.31.0.1", "eth0")
    assert f"table inet {table_name('ws-a')}" in ruleset
    assert "type filter hook forward priority filter" in ruleset
    assert 'iifname "msks-tap" ip saddr 172.31.0.1 oifname "eth0" accept' in ruleset
    assert 'iifname "msks-tap" drop' in ruleset


async def test_apply_base_and_install_vm(tools) -> None:
    settings, log = tools
    await nft.apply_base(settings)
    await nft.install_vm(settings, "ws-a", "msks-tap", "172.31.0.1")
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
        "netlink: Error: cache initialization failed: No such file or directory",
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
