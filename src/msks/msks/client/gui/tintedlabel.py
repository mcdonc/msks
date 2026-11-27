"""The label's tint painter (#1160, split from
:mod:`msks.client.gui.tints` at the fan-in ceiling in #1214):
:class:`TintedLabel`, the status line every screen flashes on,
wearing a kind with a palette whose resolve mask names the text
roles alone — so it keeps following the application palette for
everything else and takes its color again on its own
``PaletteChange``. The colors and :func:`msks.client.gui.tints.tinted`
stay in the tints module; the screens that flash a status line
import this one, the tables and cells import that.

This module imports PySide6 at module scope, the same contract
as the tints: nothing imports it at CLI parse time (#927).
"""

from PySide6 import QtCore, QtGui, QtWidgets

from .tints import TEXT_ROLES, tinted

#: The resolve mask for TEXT_ROLES: the one bit per role Qt's
#: palette resolution reads (``1 << role``).
_TEXT_MASK = sum(1 << role.value for role in TEXT_ROLES)


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
