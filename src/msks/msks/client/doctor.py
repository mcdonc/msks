"""``msks doctor``: the daemon-side tap-listener check (#533).

The host-side ``msksd doctor`` names the external tools before a
first run (#464); this command names what a running daemon is
missing: each running workspace's per-tap listeners — the
interceptor, the LLM proxy, the probe endpoint — and the binds
the daemon recorded as refused. A fault prints one line naming
the workspace, the port, the bind error, and the remedy; the
exit code is 1 when any fault stands, 0 when every bound listener
serves.
"""

import asyncio

from .context import call
from .tui.rows import fault_sentences, workspace_label


def workspace_doctor_lines(row: dict) -> list[str]:
    """One workspace's lines: a ✓ when every bound listener
    serves, one ✗ per refused bind."""
    sentences = fault_sentences(row)
    if not sentences:
        return [f"✓ {workspace_label(row)}: every tap listener serves"]
    return [f"✗ {sentence}" for sentence in sentences]


def fault_count(lines: list[str]) -> int:
    """The ✗ lines' count — the faults the exit code answers for."""
    return sum(1 for line in lines if line.startswith("✗"))


def doctor_lines(rows: list[dict]) -> tuple[list[str], int]:
    """(lines, fault count) over the daemon's rows: each running
    workspace's lines, and a standing line when no workspace runs
    at all. Only running workspaces carry faults — the daemon
    clears the record at detach — so the pass over the rows
    starts there."""
    running = [row for row in rows if row.get("status") == "running"]
    lines: list[str] = []
    for row in running:
        lines.extend(workspace_doctor_lines(row))
    if not running:
        lines.append("no running workspaces; nothing to check")
    return lines, fault_count(lines)


def cmd_doctor(transport=None) -> int:
    """``msks doctor``: name the daemon's refused tap-listener
    binds, one line each."""
    rows = asyncio.run(call("GET", "/api/v1/workspaces", transport=transport))
    lines, faults = doctor_lines(rows)
    for line in lines:
        print(line)
    return 1 if faults else 0
