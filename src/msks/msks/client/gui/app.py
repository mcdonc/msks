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
# own error convention.
PENDING_MSG = (
    "the msks GUI window lands with #928; phase 1 of #924 wires the seam"
)


def run() -> int:
    """Answer ``msks gui`` until #928 ships the window shell."""
    print(PENDING_MSG, file=sys.stderr)
    return 1
