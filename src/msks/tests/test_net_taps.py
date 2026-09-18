"""Tap lifecycle against a recording `ip` stub (#52)."""

from pathlib import Path

import pytest
from msks.microvm.errors import MicrovmError
from msks.net import taps
from msks.settings import NetSettings, Settings
from netstubs import IP_FAIL_AT, IP_STDERR, log_lines, stub_ip


@pytest.fixture
def tools(tmp_path: Path):
    log = tmp_path / "ip.log"
    settings = Settings(net=NetSettings(ip_tool=str(stub_ip(tmp_path, log))))
    return settings, log


async def test_create_tap_runs_the_three_commands(tools) -> None:
    settings, log = tools
    await taps.create_tap("msks-abc", "172.31.0.2/30", settings)
    assert log_lines(log) == [
        # The sweep first (#70 review): a tap left by an unclean death
        # converges instead of wedging the recovery boot.
        "link del dev msks-abc",
        "tuntap add dev msks-abc mode tap",
        "addr add 172.31.0.2/30 dev msks-abc",
        "link set dev msks-abc up",
    ]


async def test_remove_tap_tolerates_an_absent_device(
    tools, monkeypatch
) -> None:
    settings, log = tools
    monkeypatch.setenv(IP_FAIL_AT, "link del")
    monkeypatch.setenv(IP_STDERR, 'Cannot find device "msks-abc"')
    await taps.remove_tap("msks-abc", settings)
    assert log_lines(log) == ["link del dev msks-abc"]


async def test_remove_tap_surfaces_real_failures(tools, monkeypatch) -> None:
    settings, _log = tools
    monkeypatch.setenv(IP_FAIL_AT, "link del")
    monkeypatch.setenv(IP_STDERR, "something else broke")
    with pytest.raises(MicrovmError, match="tap remove msks-abc failed"):
        await taps.remove_tap("msks-abc", settings)


async def test_ip_cmd_names_a_missing_tool(tmp_path: Path) -> None:
    settings = Settings(net=NetSettings(ip_tool=str(tmp_path / "not-there")))
    with pytest.raises(MicrovmError, match="tool not found"):
        await taps.create_tap("msks-abc", "172.31.0.2/30", settings)
