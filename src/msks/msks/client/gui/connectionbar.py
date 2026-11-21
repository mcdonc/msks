"""The connection picker's button (phase 5 of #924, #1009): the
tool button the workspace list's status row carries — the
standing connection's name beside a menu of the presenter's
presets, the standing one checked. The button is a row's child,
never a pane of its own: its menu drops below the status row it
rides in. The widget stays thin — it renders
:class:`msks.client.gui.connection.ConnectionPreset` rows and
forwards picks as ``picked(preset)``; the application and the
re-dial belong to the presenter and the window path.

This module imports PySide6 at module scope — the screens' own
contract: nothing imports it at CLI parse time, and the
workspace list (itself lazily imported on the window path) owns
the import.
"""

from PySide6 import QtCore, QtWidgets

from .connection import ConnectionPreset

#: The button's plain label (#1009): no preset marking itself
#: reads as this — the dial still stands, the menu still offers
#: every preset.
NO_STANDING = "connection"

#: The connection glyph the button draws (#1009): the style's
#: own network-drive emblem, the stock icon every platform
#: ships for a link.
LINK_ICON = QtWidgets.QStyle.StandardPixmap.SP_DriveNetIcon


class ConnectionButton(QtWidgets.QToolButton):
    """The standing connection's button (#1009): the name beside
    a menu of the presenter's presets, a pick crossing as
    ``picked(preset)`` — the button itself holds no connection
    state."""

    picked = QtCore.Signal(object)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setText(NO_STANDING)
        self.setIcon(self.style().standardIcon(LINK_ICON))
        self.setToolButtonStyle(
            QtCore.Qt.ToolButtonStyle.ToolButtonTextBesideIcon
        )
        self.setPopupMode(
            QtWidgets.QToolButton.ToolButtonPopupMode.InstantPopup
        )
        self.setToolTip("The daemon this window talks to")

    def host(self, presets: list[ConnectionPreset]) -> None:
        """The menu from the presenter's rows (#1009): one action
        per preset, its text the name beside the address and a
        check on the standing one, the button's own label naming
        the standing connection."""
        menu = QtWidgets.QMenu(self)
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
        self.setMenu(menu)
        self.setText(standing)
