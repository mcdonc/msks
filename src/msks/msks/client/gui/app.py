"""The GUI entry: what the ``msks gui`` command runs.

The Qt import lives inside :func:`run` — the CLI imports this
module at load time (``wscommands.py``), so a module-scope
PySide6 import would break every command on a base install. The
window shell is the phase-1 surface (#928): a main window over
the daemon's REST API, whose screens arrive with phases 3 and 4
of the plan (#924).
"""

import os
import sys
from importlib.util import find_spec
from types import ModuleType

# What a base install hears when it asks for the window (#928):
# one stderr line, the client's error convention, naming the
# install that brings the Qt binding.
HINT = "msks: the desktop GUI needs the gui extra: pip install 'msks[gui]'"

# The binding is installed but will not load (a broken wheel set
# or missing system libraries): the reinstall that fixes it.
BROKEN = (
    "msks: the PySide6 install does not load; reinstall it with "
    "pip install --force-reinstall 'msks[gui]'"
)

# A session with no display and no platform override: Qt's own
# failure here is an abort (qFatal, a multi-line dump and exit
# 134), past anything Python can catch — the pre-flight keeps the
# client's one-line convention instead.
NO_DISPLAY = (
    "msks: the GUI needs a display (set DISPLAY or "
    "WAYLAND_DISPLAY, or QT_QPA_PLATFORM=offscreen)"
)

# The shell window's title, until the screens give it a
# workspace to name (#924 phases 3 and 4).
WINDOW_TITLE = "msks"


def run() -> int:
    """Open the GUI window; the guards keep the parse path
    Qt-free and answer every failure with one stderr line."""
    if find_spec("PySide6") is None:
        print(HINT, file=sys.stderr)
        return 1
    try:
        # allow-deferred-import — the optional gui extra; the
        # CLI loads this module at parse time on every command,
        # so the binding imports only when the window is asked
        # for (the guard above already answered absence).
        from PySide6 import QtWidgets
    except ImportError:
        print(BROKEN, file=sys.stderr)
        return 1
    # The display gate sits between the import and the window:
    # the import needs no display, and Qt's own failure at the
    # QApplication constructor is an abort (qFatal, a multi-line
    # dump and exit 134) past anything Python can catch.
    if not any(
        os.environ.get(var)
        for var in ("DISPLAY", "WAYLAND_DISPLAY", "QT_QPA_PLATFORM")
    ):
        print(NO_DISPLAY, file=sys.stderr)
        return 1
    return open_window(QtWidgets)


def open_window(QtWidgets: ModuleType) -> int:
    """Show the main-window shell and run the event loop.

    The instance check reuses an existing ``QApplication`` — the
    offscreen window test enters through :func:`run` and makes
    the first one; the reuse arc is pinned by the stub test. Qt
    wants one application per process.
    """
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication(
        sys.argv[:1]
    )
    window = QtWidgets.QMainWindow()
    window.setWindowTitle(WINDOW_TITLE)
    window.show()
    return app.exec()
