"""The bridge holder's spawn half (#1102): the detached client
child that serves a workspace's forward proxy rows.

Stdlib-only on purpose: the boot helper in :mod:`msks.client.rest`
reaches this from deep inside every session pre-flight (an ssh,
console, or forward child that boots a stopped workspace), and it
must not pull the client's REST or ssh layers with it — those
import each other. ``holder_due`` decides whether a boot owes the
workspace a holder; ``spawn_holder`` leaves one behind.
"""

import subprocess
import sys


def holder_due(rows: list[dict]) -> bool:
    """Whether a start owes the workspace a fresh holder: it owns
    forward rows, and none of the forward rows already reads
    ``listening`` (a holder stands — another start must not stack
    a standby beside it; a listening remote row is daemon-served
    and says nothing about the bridge, #1102 review round 3)."""
    forward = [row for row in rows if row["direction"] != "remote"]
    if not forward:
        return False
    return not any(row.get("status") == "listening" for row in forward)


def spawn_holder(workspace_id: str) -> bool:
    """Leave a bridge holder behind: a detached child of this
    client that serves the workspace's forward rows until the
    workspace stops. It runs the same interpreter with the same
    environment, in its own session so the caller's exit leaves it
    standing. The CLI, the TUI, the GUI, and every session child
    that boots a workspace reach this through one definition."""
    argv = [
        sys.executable,
        "-m",
        "msks.client.cli",
        "proxy",
        "hold",
        workspace_id,
    ]
    try:
        subprocess.Popen(
            argv,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    except OSError:
        return False
    return True
