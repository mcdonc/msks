"""The macOS menu bar (#1007, phase 6 of #924): the standard
roles every app on the platform carries — About and Quit — in
the global bar the desktop draws at the top of the screen. The
bar belongs to macOS alone: a Linux or Windows window keeps
the shape it has, and no bar is built there. The roles'
shortcuts live on the bar's own actions — Quit carries the
platform's standard Cmd+Q — so the windows carry no shortcut
of their own beside the zoom's.

This module imports PySide6 at module scope, the screens' own
contract (#928): nothing imports it at CLI parse time, and the
window path loads it after its guards.
"""

import sys

from PySide6 import QtGui, QtWidgets

from ...spec.version import __version__
from .forms import about_box
from .rows import WINDOW_TITLE

#: The About box's text (#1007): the client's name beside its
#: version word — the same word the version-skew notice names
#: this client by (#706).
ABOUT_TEXT = f"{WINDOW_TITLE} {__version__}"


def show_about(window) -> None:
    """The About role's ask (#1007): the client's name and
    version in the house dialog shape — a box built over the
    window and centered on it (#1014), under its own modal
    loop."""
    about_box(window, f"About {WINDOW_TITLE}", ABOUT_TEXT).exec()


def install_bar(window: QtWidgets.QMainWindow) -> None:
    """Stand the macOS menu bar on the window (#1007): one menu
    under the client's name carrying the two standard roles —
    the roles Qt's platform integration seats in the
    application menu the desktop draws. Quit carries the
    standard binding (Cmd+Q) and answers through the window's
    own close path, the one ``aboutToQuit`` path that closes
    every shell (#954). A window on any other platform keeps
    the shape it has — the gate builds nothing there, and the
    offscreen suite drives the build on every host with the
    platform patched to darwin.
    """
    if sys.platform != "darwin":
        return
    bar = window.menuBar()
    menu = bar.addMenu(WINDOW_TITLE)
    # The actions take the menu as their parent: a parentless
    # QAction stays Python-owned, and ``addAction`` alone does
    # not take ownership — the object would leave with the
    # frame that built it.
    about = QtGui.QAction(f"About {WINDOW_TITLE}", menu)
    about.setMenuRole(QtGui.QAction.MenuRole.AboutRole)
    about.triggered.connect(lambda: show_about(window))
    menu.addAction(about)
    menu.addSeparator()
    quit_action = QtGui.QAction(f"Quit {WINDOW_TITLE}", menu)
    quit_action.setMenuRole(QtGui.QAction.MenuRole.QuitRole)
    quit_action.setShortcut(QtGui.QKeySequence.StandardKey.Quit)
    quit_action.triggered.connect(window.close)
    menu.addAction(quit_action)
