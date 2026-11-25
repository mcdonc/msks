"""The state tints (#1160): the three semantic colors a state
word wears beside its text, one set per desktop scheme, and the
two painters that apply them. A table cell carries its kind
under :data:`SEMANTIC_ROLE` and :class:`SemanticDelegate` writes
the kind's color into the palette in hand at paint time, so no
widget stores a color and a scheme switch repaints every tint
with the chrome; a selected or disabled cell keeps the style's
own text. :class:`TintedLabel` wears a kind with a palette whose
resolve mask names the text roles alone, so it keeps following
the application palette for everything else and takes its
color again on its own ``PaletteChange``.

The kind words live in the Qt-free leaf :mod:`msks.client.gui.kinds`;
:mod:`msks.client.gui.scheme` is the desktop-scheme pairing the
tints read their base from.

This module imports PySide6 at module scope, the same contract
as the screen modules: nothing imports it at CLI parse time
(#927).
"""

from PySide6 import QtCore, QtGui, QtWidgets

from .kinds import ERROR, OK, WARN

#: The dark chrome's tints: each passes 4.5:1 against both
#: BASE_GRAY and ALTERNATE_GRAY (the dark error is the tight
#: one at 4.78 on the alternate gray).
_DARK_SEMANTIC = {
    OK: (111, 207, 122),
    WARN: (242, 193, 78),
    ERROR: (255, 150, 140),
}

#: The light chrome's tints: each passes 4.5:1 against the
#: white base, the (247, 247, 247) alternate base, and the
#: (239, 239, 239) window the tinted labels sit on (the ok is
#: the tight one at 4.81 on the window).
_LIGHT_SEMANTIC = {
    OK: (28, 120, 56),
    WARN: (145, 92, 0),
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
    over that could sink into the highlight. A disabled cell
    keeps its dimmed text too: the tint would read at full
    strength beside dimmed neighbours. An untagged cell paints
    as the style would."""

    def initStyleOption(self, option, index) -> None:
        super().initStyleOption(option, index)
        kind = index.data(SEMANTIC_ROLE)
        State = QtWidgets.QStyle.StateFlag
        state = option.state
        selected = bool(state & State.State_Selected)
        disabled = not state & State.State_Enabled
        if kind is None or selected or disabled:
            return
        tinted(option.palette, kind, option.palette)


class TintedLabel(QtWidgets.QLabel):
    """A label whose text wears a kind (#1160): the status line
    under a refusal or an outage, the consent row's last
    seconds. The palette carries the text roles alone, so the
    label keeps following the chrome's base and font; a scheme
    switch arrives as ``PaletteChange`` and the label takes its
    kind's color again on the new base."""

    kind = None
    retinting = False

    def __init__(self, text: str = "", parent=None) -> None:
        super().__init__(text, parent)

    def set_kind(self, kind) -> None:
        """Wear ``kind``; None wears the chrome's own text color."""
        self.kind = kind
        self.retint()

    def say(self, text: str, kind=None) -> None:
        """Stand ``text`` wearing ``kind``: the note lines' one
        write — a refusal the error tint, anything else the
        chrome's own text."""
        self.setText(text)
        self.set_kind(kind)

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
