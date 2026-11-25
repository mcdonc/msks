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

The state tints live here too (#1160): the three semantic colors
a state word wears beside its text, one set per scheme, and the
two painters that apply them — the tables' delegate, which reads
the palette in hand at paint time, and the tinted label, which
follows a scheme switch through its own ``PaletteChange``.
"""

from PySide6 import QtCore, QtGui, QtWidgets

from . import ghostty as gt
from .kinds import ERROR, OK, WARN
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


# -- the state tints (#1160) ---------------------------------------------

#: The dark chrome's tints: each passes 4.5:1 against both
#: BASE_GRAY and ALTERNATE_GRAY (the dark error is the tight
#: one at 4.78 on the alternate gray).
_DARK_SEMANTIC = {
    OK: (111, 207, 122),
    WARN: (242, 193, 78),
    ERROR: (255, 150, 140),
}

#: The light chrome's tints: each passes 4.5:1 against the
#: white base and the (247, 247, 247) alternate base.
_LIGHT_SEMANTIC = {
    OK: (30, 127, 60),
    WARN: (154, 98, 0),
    ERROR: (198, 40, 40),
}

#: The item role a state cell's kind rides on: the delegate reads
#: it at paint time, and no item stores a color. The offset clears
#: every per-table role the cell layers define.
SEMANTIC_ROLE = QtCore.Qt.ItemDataRole.UserRole + 32

#: The roles a tinted label's palette carries: the text a QLabel
#: draws with, and the text the chrome's own text reads as.
TEXT_ROLES = (
    QtGui.QPalette.ColorRole.Text,
    QtGui.QPalette.ColorRole.WindowText,
)

#: The resolve mask for TEXT_ROLES: the one bit per role Qt's
#: palette resolution reads (``1 << role``).
_TEXT_MASK = sum(1 << role.value for role in TEXT_ROLES)


def luminance(color: QtGui.QColor) -> float:
    """A color's relative luminance (WCAG): the contrast
    ratio's input, each channel linearized."""
    channels = [color.redF(), color.greenF(), color.blueF()]
    linear = [
        value / 12.92 if value <= 0.03928 else ((value + 0.055) / 1.055) ** 2.4
        for value in channels
    ]
    red, green, blue = linear
    return 0.2126 * red + 0.7152 * green + 0.0722 * blue


def contrast(one: QtGui.QColor, two: QtGui.QColor) -> float:
    """The WCAG contrast ratio of two colors: 1 for a pair that
    matches, 21 for black on white. 4.5 is the floor text reads
    by."""
    lighter, darker = sorted((luminance(one), luminance(two)), reverse=True)
    return (lighter + 0.05) / (darker + 0.05)


def is_dark(palette: QtGui.QPalette) -> bool:
    """The scheme a palette paints in, read off its base: a base
    darker than mid-gray is the dark chrome. The two constant
    palettes differ by far more than the threshold."""
    base = palette.color(QtGui.QPalette.ColorRole.Base)
    return luminance(base) < 0.5


def semantic(kind, palette: QtGui.QPalette) -> QtGui.QColor | None:
    """One kind's color on the base ``palette`` paints: the
    dark set on a dark base, the light set otherwise. The
    palette is the one in hand at paint time, so a scheme
    switch repaints every tint with the chrome. None for no
    kind (an untagged cell, a label back in the chrome's own
    text)."""
    if kind not in _DARK_SEMANTIC:
        return None
    table = _DARK_SEMANTIC if is_dark(palette) else _LIGHT_SEMANTIC
    return QtGui.QColor(*table[kind])


def tinted(palette: QtGui.QPalette, kind, base: QtGui.QPalette) -> None:
    """Write ``kind``'s colors for ``base`` into ``palette``'s
    text roles. A palette with the text roles' mask alone leaves
    every other role to the application's palette."""
    color = semantic(kind, base)
    if color is None:
        return
    brush = QtGui.QBrush(color)
    for role in TEXT_ROLES:
        palette.setBrush(role, brush)


class SemanticDelegate(QtWidgets.QStyledItemDelegate):
    """The state cells' painter (#1160): a cell tagged with a
    kind under :data:`SEMANTIC_ROLE` paints its text in that
    kind's color on the palette the option carries, read at
    paint time. A selected cell keeps the highlight's own text:
    Qt draws ``HighlightedText`` on ``Highlight``, and a tint
    over that could sink into the highlight. An untagged cell
    paints as the style would."""

    def initStyleOption(self, option, index) -> None:
        super().initStyleOption(option, index)
        kind = index.data(SEMANTIC_ROLE)
        selected = option.state & QtWidgets.QStyle.StateFlag.State_Selected
        if kind is None or selected:
            return
        tinted(option.palette, kind, option.palette)


class TintedLabel(QtWidgets.QLabel):
    """A label whose text wears a kind (#1160): the status line
    under a refusal or an outage, the consent row's last
    seconds. The palette carries the text roles alone, so the
    label keeps following the chrome's base and font; a scheme
    switch arrives as ``PaletteChange`` and the label takes its
    kind's color again on the new base."""

    def __init__(self, text: str = "", parent=None) -> None:
        super().__init__(text, parent)
        self.kind = None
        self.retinting = False

    def set_kind(self, kind) -> None:
        """Wear ``kind``; None wears the chrome's own text color."""
        self.kind = kind
        self.retint()

    def retint(self) -> None:
        """The palette rewritten for the kind on the base the
        label now sits on. A palette whose mask names the text
        roles alone resolves every other role from the
        application's; one with no mask at all is the label that
        never had a palette of its own."""
        palette = QtGui.QPalette()
        palette.setResolveMask(_TEXT_MASK if self.kind else 0)
        tinted(palette, self.kind, self.palette())
        self.retinting = True
        try:
            self.setPalette(palette)
        finally:
            self.retinting = False

    def event(self, event) -> bool:
        if (
            event.type() == QtCore.QEvent.Type.PaletteChange
            and not self.retinting
        ):
            self.retint()
        return super().event(event)


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
