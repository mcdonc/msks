"""The GUI entry: what the ``msks gui`` command runs.

Phase 1 of the GUI client (#924) wires the command seam only.
The window shell — a PySide6 ``QMainWindow`` over the daemon's
REST API, imported lazily so a base install never needs Qt —
lands with #928, and the screens with phases 3 and 4 of the
plan.
"""

import sys

# What the seam answers until #928 ships the window: the command
# exists, the Qt surface does not. One stderr line, the client's
# own error convention — the `msks:` prefix every failure line
# carries (docs/cli.md, "Errors, exit codes, and timeouts").
PENDING_MSG = "msks: the desktop GUI is not in this build yet"


def run() -> int:
    """Answer ``msks gui`` until #928 ships the window shell."""
    print(PENDING_MSG, file=sys.stderr)
    return 1
