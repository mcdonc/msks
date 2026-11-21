"""The desktop-scheme pairing (#1006, phase 6 of #924): the
window paints from the desktop's color scheme — the Fusion
chrome from one of two palettes and every live terminal grid
from the library's color defaults, both sets swapped together
when the desktop switches, live, with no restart.
``QStyleHints.colorScheme()`` names the desktop's scheme and
its ``colorSchemeChanged`` signal tells a switch; a desktop
that names no scheme keeps the palette the platform theme
handed the window (the startup path's own fallback), and the
grids keep the library's own defaults.

The module imports PySide6 at module scope, the same contract
as the screen modules: the window path
(:func:`msks.client.gui.app.open_window`) loads it, nothing at
CLI parse time (#927).
"""

from PySide6 import QtCore, QtGui

from . import ghostty as gt
from .terminal import colors

#: The dark chrome's grays (#1006): the documented Fusion dark
#: formula (Qt's own dark-theme derivation) — a 53-gray window
#: and buttons over a 42-gray base, white text, the blue
#: highlight, and the 127-gray the disabled roles dim to.
WINDOW_GRAY = (53, 53, 53)
BASE_GRAY = (42, 42, 42)
ALTERNATE_GRAY = (66, 66, 66)
DIM_GRAY = (127, 127, 127)
DARK_EDGE = (35, 35, 35)
SHADOW = (20, 20, 20)
HIGHLIGHT = (42, 130, 218)
HIGHLIGHT_OFF = (80, 80, 80)


def light_palette(app) -> QtGui.QPalette:
    """The Fusion chrome's light set (#1006): the pinned
    style's own standard palette — the light Fusion every
    platform shares, deterministic wherever the window opens."""
    return app.style().standardPalette()


def dark_palette() -> QtGui.QPalette:
    """The Fusion chrome's dark set (#1006): the documented
    Fusion dark formula over a fresh palette — every reading
    role white on the grays, the disabled roles dimmed, and
    the formula's own blue highlight."""
    pal = QtGui.QPalette()
    white, red = QtGui.QColor(255, 255, 255), QtGui.QColor(255, 0, 0)
    window = QtGui.QColor(*WINDOW_GRAY)
    dim = QtGui.QColor(*DIM_GRAY)
    highlight = QtGui.QColor(*HIGHLIGHT)
    disabled = QtGui.QPalette.ColorGroup.Disabled
    roles = QtGui.QPalette.ColorRole
    pal.setColor(roles.Window, window)
    pal.setColor(roles.WindowText, white)
    pal.setColor(disabled, roles.WindowText, dim)
    pal.setColor(roles.Base, QtGui.QColor(*BASE_GRAY))
    pal.setColor(roles.AlternateBase, QtGui.QColor(*ALTERNATE_GRAY))
    pal.setColor(roles.ToolTipBase, white)
    pal.setColor(roles.ToolTipText, window)
    pal.setColor(roles.Text, white)
    pal.setColor(disabled, roles.Text, dim)
    pal.setColor(roles.Dark, QtGui.QColor(*DARK_EDGE))
    pal.setColor(roles.Shadow, QtGui.QColor(*SHADOW))
    pal.setColor(roles.Button, window)
    pal.setColor(roles.ButtonText, white)
    pal.setColor(disabled, roles.ButtonText, dim)
    pal.setColor(roles.BrightText, red)
    pal.setColor(roles.Link, highlight)
    pal.setColor(roles.Highlight, highlight)
    pal.setColor(disabled, roles.Highlight, QtGui.QColor(*HIGHLIGHT_OFF))
    pal.setColor(roles.HighlightedText, white)
    pal.setColor(disabled, roles.HighlightedText, dim)
    return pal


def grid(scheme) -> int:
    """The terminal's own constant for a desktop scheme
    (#1006): the library's light/dark value the color set and
    the scheme query take."""
    if scheme == QtCore.Qt.ColorScheme.Dark:
        return gt.COLOR_SCHEME_DARK
    return gt.COLOR_SCHEME_LIGHT


class Scheme(QtCore.QObject):
    """The desktop scheme's pairing state (#1006): the
    application palette behind the chrome, and the color
    defaults every live terminal paints from. ``initial``
    overrides the desktop's own answer — the offscreen
    suite's seam, a platform that names no scheme — and the
    standing scheme survives a reconnect: a fresh tree's hub
    joins it as its shells open."""

    def __init__(self, hub, app, initial=None) -> None:
        super().__init__(app)
        self.hub = hub
        self.app = app
        self.standing = None
        hints = QtGui.QGuiApplication.styleHints()
        hints.colorSchemeChanged.connect(self.switched)
        hub.shell_opened.connect(self.shell_joined)
        scheme = initial if initial is not None else hints.colorScheme()
        if scheme in (
            QtCore.Qt.ColorScheme.Light,
            QtCore.Qt.ColorScheme.Dark,
        ):
            self.apply(scheme)

    def chrome(self, scheme) -> QtGui.QPalette:
        """One scheme's chrome palette (#1006): the style's own
        light standard, or the dark formula beside it."""
        if scheme == QtCore.Qt.ColorScheme.Dark:
            return dark_palette()
        return light_palette(self.app)

    def apply(self, scheme) -> None:
        """One scheme on the whole client (#1006): the chrome
        palette written on the application — every screen
        inherits it, and Qt repaints them all live — and the
        color set on every live terminal, the visible grids
        refreshing in the same step."""
        self.standing = scheme
        self.app.setPalette(self.chrome(scheme))
        for widget in self.hub.terminals():
            colors.apply(widget, grid(scheme))

    @QtCore.Slot(QtCore.Qt.ColorScheme)
    def switched(self, scheme) -> None:
        """``colorSchemeChanged``'s slot (#1006) — the seam a
        desktop's switch lands on: the pairing takes the named
        scheme at once. A withdrawal mid-session (the desktop
        naming no scheme where it named one) keeps the
        standing set: the window keeps painting what it
        painted."""
        if scheme in (
            QtCore.Qt.ColorScheme.Light,
            QtCore.Qt.ColorScheme.Dark,
        ):
            self.apply(scheme)

    def retarget(self, hub) -> None:
        """Point the pairing at a reconnected window's
        terminals (#1009): the hub the old shell owned closed
        with its connection, and the new hub's shells join the
        standing scheme as they open — the desktop's palette
        survives the daemon switch. A re-ask for the same hub
        changes nothing."""
        if hub is self.hub:
            return
        self.hub.shell_opened.disconnect(self.shell_joined)
        self.hub = hub
        hub.shell_opened.connect(self.shell_joined)

    @QtCore.Slot(str, int)
    def shell_joined(self, workspace_id: str, no: int) -> None:
        """A shell opened after the scheme stands joins it
        (#1006): the fresh terminal paints from the library's
        own fallback until the scheme reaches it, and the walk
        takes every live terminal — the same idempotent write
        the switch itself makes."""
        if self.standing is None:
            return
        for widget in self.hub.terminals():
            colors.apply(widget, grid(self.standing))
