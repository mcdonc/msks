"""The workspace page's terminal-only mode (#1152): the page's
toggle hides the window's chrome and leaves the shell tabs and
the terminal in the layout of a tabbed terminal app — the tab
bar on top, the terminal filling the rest. A corner over the
shell tabs holds the way back: the workspace's name, painted
muted, and the restore button. The desktop's own title bar and
the macOS menu bar stay.

The mode belongs to the page. It ends at every hide the page
takes (the listing and the other top-level tabs must never show
under a hidden strip), it closes no shell, and the window's own
top-level strip hides through :attr:`TerminalOnly.chrome_hidden`
— the page forwards it, and the window's composition wires it to
the strip's bar. The page's status line keeps flashing in the
mode: the flash writes to whichever label the page calls its
``status``, and the mode swaps that label for the corner's own
while it stands, carrying the standing text across.

This module imports PySide6 at module scope, the screens' own
contract: nothing imports it at CLI parse time.
"""

from PySide6 import QtCore, QtGui, QtWidgets

#: The toggle's tooltip (#1152): the words its glyph alone
#: cannot carry — the maximize glyph stands for the terminal
#: taking the window.
TERMINAL_ONLY_TIP = "Show only the shell tabs and the terminal"

#: The restore button's tooltip (#1152): the glyph is the
#: platform's own "restore down" one.
RESTORE_TIP = "Show the workspace page's controls again"

#: The workspace name's longest paint, pixels (#1152): a long
#: name elides in the middle rather than widening the corner
#: past the tab bar, and the whole name then reads in the
#: label's tooltip.
NAME_MAX_PX = 240


def elide_name(label: QtWidgets.QLabel, name: str) -> None:
    """Stand the workspace's name on the corner's label
    (#1152): elided in the middle when longer than the corner
    allows, the whole name in the tooltip only when the paint
    cut it — a tooltip repeating the visible text is noise."""
    paint = label.fontMetrics().elidedText(
        name, QtCore.Qt.TextElideMode.ElideMiddle, NAME_MAX_PX
    )
    label.setText(paint)
    label.setToolTip(name if paint != name else "")


class TerminalOnly(QtCore.QObject):
    """The terminal-only mode over one workspace page (#1152):
    the toggle on the page's chrome row, the corner over the
    shell tabs (the muted name, the status line's seat, the
    restore button), and the switch itself. The page hands in
    its own controls at construction — the chrome row's
    widgets, the footer, the Escape shortcut — and the mode
    shows and hides them whole."""

    #: True while the mode stands (#1152): the window's own
    #: top-level strip hides through it.
    chrome_hidden = QtCore.Signal(bool)

    def __init__(self, page) -> None:
        super().__init__(page)
        self.page = page
        self.on = False
        #: The chrome row's status label, the one the flash
        #: writes while the mode is off: :attr:`status` stands
        #: in its place while the mode is on.
        self.chrome_status = page.status
        self.corner = QtWidgets.QWidget(page.shells)
        self.name = QtWidgets.QLabel("", self.corner)
        # Muted (#1152): the placeholder's gray, the role both
        # the light and the dark palette set — the name reads
        # at a glance beside the restore button without
        # competing with the tabs.
        self.name.setForegroundRole(QtGui.QPalette.ColorRole.PlaceholderText)
        self.status = QtWidgets.QLabel("", self.corner)
        self.restore = QtWidgets.QToolButton(self.corner)
        self.restore.setIcon(
            page.style().standardIcon(
                QtWidgets.QStyle.StandardPixmap.SP_TitleBarNormalButton
            )
        )
        self.restore.setToolTip(RESTORE_TIP)
        self.restore.setAccessibleName("Show the page controls")
        self.restore.clicked.connect(self.leave)
        corner_row = QtWidgets.QHBoxLayout(self.corner)
        corner_row.setContentsMargins(0, 0, 0, 0)
        corner_row.addWidget(self.name)
        corner_row.addWidget(self.status)
        corner_row.addWidget(self.restore)
        # The corner takes the tab row's right end (#1152) —
        # where a tabbed terminal app keeps its own controls —
        # and stands hidden until the mode asks for it: a
        # hidden corner leaves the bar the whole row.
        page.shells.setCornerWidget(
            self.corner, QtCore.Qt.Corner.TopRightCorner
        )
        self.corner.hide()
        self.toggle = QtWidgets.QToolButton(page)
        self.toggle.setIcon(
            page.style().standardIcon(
                QtWidgets.QStyle.StandardPixmap.SP_TitleBarMaxButton
            )
        )
        self.toggle.setToolTip(TERMINAL_ONLY_TIP)
        self.toggle.setAccessibleName("Terminal only")
        self.toggle.clicked.connect(self.enter)
        # The toggle before the external-terminal button
        # (#1152): the rightmost control keeps its corner. The
        # toggle hides with the row too — the corner's restore
        # button stands in its place — so the row leaves no
        # gap above the terminals.
        page.chrome.insertWidget(page.chrome.count() - 1, self.toggle)
        #: The chrome row's widgets and the toggle: what the
        #: mode hides and shows, whole.
        self.row = (
            page.back_button,
            page.bar,
            self.chrome_status,
            page.external_button,
            self.toggle,
        )

    def set_name(self, name: str) -> None:
        """The workspace's name on the corner's label (#1152):
        the page's own label, standing in from the list until
        the row's own answer refines it."""
        elide_name(self.name, name)

    @QtCore.Slot()
    def enter(self) -> None:
        """The toggle's action (#1152): the Shells tab shows —
        the mode has room for the terminals alone — then the
        chrome row, the footer, and the Back shortcut hide, the
        corner stands up with the status line's seat, the
        terminal takes the keyboard, and the window's strip
        hears the ask. A second ask while the mode stands
        changes nothing."""
        if self.on:
            return
        self.on = True
        page = self.page
        page.bar.setCurrentIndex(page.stack.indexOf(page.shells_tab))
        for widget in self.row:
            widget.hide()
        page.footer.hide()
        page.escape.setEnabled(False)
        # The status line's seat moves (#1152): the standing
        # text — a flash mid-span, an outage — comes along, and
        # the flash's own timeout clears whichever label stands
        # when it fires.
        self.status.setText(self.chrome_status.text())
        page.status = self.status
        self.corner.show()
        page.shells_tab.focus_shell()
        self.chrome_hidden.emit(True)

    @QtCore.Slot()
    def leave(self) -> None:
        """The restore button's action, and the page's hide's
        (#1152): the corner goes, every hidden control shows
        again, the status text returns to the chrome row's
        label, the Back shortcut arms, and the window's strip
        hears the mode ended. The current tabs — the page's,
        the shell's — and every session stand as they were. An
        ask while the mode is off changes nothing."""
        if not self.on:
            return
        self.on = False
        page = self.page
        self.chrome_status.setText(self.status.text())
        page.status = self.chrome_status
        self.corner.hide()
        for widget in self.row:
            widget.show()
        page.footer.show()
        page.escape.setEnabled(True)
        self.chrome_hidden.emit(False)
