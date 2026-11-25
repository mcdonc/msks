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

_ROLES = QtGui.QPalette.ColorRole

#: The dark chrome's grays (#1006): the Fusion dark formula
#: the Qt Creator dark-theme wizard ships — a 53-gray window
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

_WHITE, _BLACK, _RED = (255, 255, 255), (0, 0, 0), (255, 0, 0)

#: The dark chrome's roles (#1006): every reading role white on
#: the grays, the disabled roles dimmed, the placeholder text
#: at the dim gray (Qt's own derivation dims it from the text
#: color), and the accent at the formula's blue.
_DARK_ACTIVE = {
    _ROLES.Window: WINDOW_GRAY,
    _ROLES.WindowText: _WHITE,
    _ROLES.Base: BASE_GRAY,
    _ROLES.AlternateBase: ALTERNATE_GRAY,
    _ROLES.ToolTipBase: _WHITE,
    _ROLES.ToolTipText: WINDOW_GRAY,
    _ROLES.Text: _WHITE,
    _ROLES.Dark: DARK_EDGE,
    _ROLES.Shadow: SHADOW,
    _ROLES.Button: WINDOW_GRAY,
    _ROLES.ButtonText: _WHITE,
    _ROLES.BrightText: _RED,
    _ROLES.Link: HIGHLIGHT,
    _ROLES.Highlight: HIGHLIGHT,
    _ROLES.HighlightedText: _WHITE,
    _ROLES.PlaceholderText: DIM_GRAY,
    _ROLES.Accent: HIGHLIGHT,
}
_DARK_DISABLED = {
    _ROLES.WindowText: DIM_GRAY,
    _ROLES.Text: DIM_GRAY,
    _ROLES.ButtonText: DIM_GRAY,
    _ROLES.Highlight: HIGHLIGHT_OFF,
    _ROLES.HighlightedText: DIM_GRAY,
    _ROLES.Accent: HIGHLIGHT_OFF,
}

#: The light chrome's roles (#1006): Fusion's own standard
#: palette, pinned as constants. The style's live
#: ``standardPalette()`` answer is scheme-aware since Qt 6.5 (a
#: dark-aware platform hands a dark palette there), so the
#: light arm takes the values, not the read — a dark desktop at
#: apply time cannot tint it, and the two arms stay symmetric.
_LIGHT_ACTIVE = {
    _ROLES.Window: (239, 239, 239),
    _ROLES.WindowText: _BLACK,
    _ROLES.Base: _WHITE,
    _ROLES.AlternateBase: (247, 247, 247),
    _ROLES.ToolTipBase: (255, 255, 220),
    _ROLES.ToolTipText: _BLACK,
    _ROLES.Text: _BLACK,
    _ROLES.Light: _WHITE,
    _ROLES.Midlight: (202, 202, 202),
    _ROLES.Dark: (159, 159, 159),
    _ROLES.Mid: (184, 184, 184),
    _ROLES.Shadow: (118, 118, 118),
    _ROLES.Button: (239, 239, 239),
    _ROLES.ButtonText: _BLACK,
    _ROLES.BrightText: _WHITE,
    _ROLES.Link: (0, 0, 255),
    _ROLES.Highlight: (48, 140, 198),
    _ROLES.HighlightedText: _WHITE,
    _ROLES.PlaceholderText: (0, 0, 0, 128),
    _ROLES.Accent: (48, 140, 198),
}
_LIGHT_DISABLED = {
    _ROLES.WindowText: (190, 190, 190),
    _ROLES.Text: (190, 190, 190),
    _ROLES.ButtonText: (190, 190, 190),
    _ROLES.Base: (239, 239, 239),
    _ROLES.Dark: (190, 190, 190),
    _ROLES.Shadow: (177, 177, 177),
    _ROLES.Highlight: (145, 145, 145),
    _ROLES.Accent: (145, 145, 145),
}


def palette_from(active: dict, disabled: dict) -> QtGui.QPalette:
    """One chrome set from its role tables: the active roles
    written across every group, the disabled ones dimmed over
    them. A four-value entry carries its alpha — the light
    placeholder's own half-transparent black, the one value
    the standard palette ships with alpha."""
    pal = QtGui.QPalette()
    for role, values in active.items():
        pal.setColor(role, QtGui.QColor(*values))
    group = QtGui.QPalette.ColorGroup.Disabled
    for role, values in disabled.items():
        pal.setColor(group, role, QtGui.QColor(*values))
    return pal


def light_palette() -> QtGui.QPalette:
    """The Fusion chrome's light set (#1006): the standard
    palette's own values as constants — deterministic wherever
    the window opens, whatever the platform theme hands."""
    return palette_from(_LIGHT_ACTIVE, _LIGHT_DISABLED)


def dark_palette() -> QtGui.QPalette:
    """The Fusion chrome's dark set (#1006): the wizard's dark
    formula — every reading role white on the grays, the
    disabled roles dimmed, the placeholder and accent roles Qt's
    own derivation adds."""
    return palette_from(_DARK_ACTIVE, _DARK_DISABLED)


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
        """One scheme's chrome palette (#1006): the constant
        light set, or the dark formula beside it."""
        if scheme == QtCore.Qt.ColorScheme.Dark:
            return dark_palette()
        return light_palette()

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
        own fallback until the scheme reaches it — the walk
        takes only the terminals the standing set has not
        reached, the same guard the zoom's join keeps."""
        if self.standing is None:
            return
        standing = grid(self.standing)
        for widget in self.hub.terminals():
            if widget.color_scheme != standing:
                colors.apply(widget, standing)
