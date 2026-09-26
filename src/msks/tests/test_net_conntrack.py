"""Conntrack eviction for consent revocation (#69)."""

import asyncio

import pytest
from msks.microvm.errors import MicrovmError
from msks.net import conntrack


async def test_delete_flows_runs_the_tool(tmp_path) -> None:
    log = tmp_path / "ct.log"
    tool = tmp_path / "conntrack"
    tool.write_text(
        "#!/bin/sh\nprintf '%s\\n' \"$*\" >> " + str(log) + "\nexit 0\n"
    )
    tool.chmod(0o755)
    await conntrack.delete_flows(str(tool), "172.31.0.1", "10.0.0.9")
    assert log.read_text().strip() == "-D -s 172.31.0.1 -d 10.0.0.9"


async def test_delete_flows_names_a_missing_tool(tmp_path) -> None:
    with pytest.raises(MicrovmError, match="tool not found"):
        await conntrack.delete_flows(
            str(tmp_path / "absent"), "172.31.0.1", "10.0.0.9"
        )


async def test_delete_flows_kills_a_hung_tool(tmp_path) -> None:
    """A conntrack that never exits is killed at the deadline — the
    revoke is not held hostage by a wedged subprocess."""
    tool = tmp_path / "hung-conntrack"
    tool.write_text("#!/bin/sh\nsleep 60\n")
    tool.chmod(0o755)
    await asyncio.wait_for(
        conntrack.delete_flows(
            str(tool), "172.31.0.1", "10.0.0.9", deadline_s=0.2
        ),
        15.0,
    )
