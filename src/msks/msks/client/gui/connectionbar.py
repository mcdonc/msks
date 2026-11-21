"""The window's connection strip (phase 5 of #924, #1009): the
standing connection's name beside a menu of the presenter's
presets, over the swappable content a pick re-dials. The strip
is the window's own chrome — it survives the reconnect its pick
causes, while the content below it (the shell, the tabs, the
bridge behind them) rebuilds whole over the picked values.

The widget stays thin: it renders
:class:`msks.client.gui.connection.ConnectionPreset` rows and
forwards picks as ``picked(preset)``; the application and the
reconnect belong to the presenter and the window path. This
module imports PySide6 at module scope — the screens' own
contract: nothing imports it at CLI parse time, and the window
path (``gui/app.py``'s ``open_window``) owns the import.
"""

from PySide6 import QtCore, QtWidgets

from .connection import ConnectionPreset

#: The strip's label when no preset marks itself the standing
#: connection (#1009): an address the file's globals named, or
#: the local socket default — the menu still offers every
#: preset, and the label names the dial honestly.
NO_STANDING = "connection"

#: The connection glyph the strip's button draws (#1009): the
#: style's own network emblem, the same stock icon every
#: platform ships for a link.
LINK_ICON = QtWidgets.QStyle.StandardPixmap.SP_ComputerIcon


class ConnectionStrip(QtWidgets.QWidget):
    """The standing connection's row and the content it fronts
    (#1009): a tool button whose menu lists the presets (the
    standing one checked), over a layout the window path swaps
    the whole content tree into at every connect. A menu pick
    crosses as ``picked(preset)`` — the window path applies it
    and re-dials; the strip itself holds no connection
    state."""

    picked = QtCore.Signal(object)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.button = QtWidgets.QToolButton(self)
        self.button.setText(NO_STANDING)
        self.button.setIcon(self.style().standardIcon(LINK_ICON))
        self.button.setToolButtonStyle(
            QtCore.Qt.ToolButtonStyle.ToolButtonTextBesideIcon
        )
        self.button.setPopupMode(
            QtWidgets.QToolButton.ToolButtonPopupMode.InstantPopup
        )
        self.button.setToolTip("The daemon this window talks to")
        row = QtWidgets.QHBoxLayout(self)
        row.setContentsMargins(0, 0, 0, 0)
        row.addWidget(self.button)
        row.addStretch(1)
        #: The content below the strip (#1009): one widget at a
        #: time, swapped whole at every connect.
        self.body = QtWidgets.QVBoxLayout()
        self.layout().addLayout(self.body)

    def host(self, presets: list[ConnectionPreset]) -> None:
        """The menu from the presenter's presets (#1009): one
        action per preset, its text the name beside the address
        and a check on the standing one; the button's own label
        names the standing connection."""
        menu = QtWidgets.QMenu(self.button)
        standing = NO_STANDING
        for preset in presets:
            action = menu.addAction(f"{preset.name} — {preset.address}")
            action.setObjectName(f"preset-{preset.kind}-{preset.name}")
            action.setCheckable(True)
            action.setChecked(preset.active)
            action.triggered.connect(
                lambda _=False, row=preset: self.picked.emit(row)
            )
            if preset.active:
                standing = preset.name
        self.button.setMenu(menu)
        self.button.setText(standing)

    def setContent(self, widget: QtWidgets.QWidget) -> None:
        """Swap the content tree (#1009): the standing widget
        leaves the layout and deletes itself — its bridge, its
        shell hub, and its terminals went with the connection it
        belonged to — and the fresh tree takes the slot."""
        while self.body.count():
            item = self.body.takeAt(0)
            gone = item.widget()
            if gone is not None:
                gone.deleteLater()
        self.body.addWidget(widget)
