"""The GUI entry: what the ``msks gui`` command runs.

The Qt import lives inside :func:`run` — the CLI imports this
module at load time (``wscommands.py``), so a module-scope
PySide6 import would break every command on a base install. The
window shell is the phase-1 surface (#928): a main window over
the daemon's REST API, whose screens arrive with phases 3 and 4
of the plan (#924).
"""

import sys
from types import ModuleType

# What a base install hears when it asks for the window (#928):
# one stderr line, the client's error convention, naming the
# install that brings the Qt binding.
HINT = "msks: the desktop GUI needs the gui extra: pip install 'msks[gui]'"

# The shell window's title, until the screens give it a
# workspace to name (#924 phases 3 and 4).
WINDOW_TITLE = "msks"


def run() -> int:
    """Open the GUI window; the lazy probe keeps the parse path
    Qt-free, and a base install hears the hint."""
    try:
        # allow-deferred-import — the optional gui extra; the
        # CLI loads this module at parse time on every command,
        # so the binding imports only when the window is asked
        # for (the ImportError arm prints the install hint).
        from PySide6 import QtWidgets
    except ImportError:
        print(HINT, file=sys.stderr)
        return 1
    return open_window(QtWidgets)


def open_window(QtWidgets: ModuleType) -> int:
    """Show the main-window shell and run the event loop.

    The instance check reuses an existing ``QApplication`` — the
    suite's offscreen tests create one before the window opens,
    and Qt wants one per process.
    """
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication(
        sys.argv[:1]
    )
    window = QtWidgets.QMainWindow()
    window.setWindowTitle(WINDOW_TITLE)
    window.show()
    return app.exec()
