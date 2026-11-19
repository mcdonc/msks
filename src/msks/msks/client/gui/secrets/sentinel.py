"""The sentinel panel (phase 4.6 of #924, #966): the masked
display beside the placeholder listing — the TUI's reveal modal
as a standing panel. A row's activation fetches the row with
its sentinel and fills the panel masked (the prefix names the
reach, the body renders one bullet per character); the Show
action reveals the real sentinel behind a confirmation, Hide
masks it again, and Copy writes the real value to the
clipboard — masked or revealed, the copy lands the token. The
panel holds the raw row for the reveal and the copy; the model
it renders (:func:`msks.client.gui.secretrows.sentinel_model`)
puts no bearer power on screen. Clearing empties every line:
the sentinel leaves the widget tree with the row that carried
it.

This module imports PySide6 at module scope — the package's
init imports none of it at parse time (#927).
"""

from PySide6 import QtCore, QtWidgets

from ..secretrows import sentinel_model
from . import cells


class SentinelPanel(QtWidgets.QWidget):
    """The listing's right-hand panel (#966): the sentinel of
    the row the operator activated — masked on arrival, the
    reveal behind a confirm, the copy to the clipboard."""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.row: dict | None = None
        self.revealed = False
        self.label = QtWidgets.QLabel("", self)
        self.sentinel = QtWidgets.QLabel("", self)
        # The masked body and the revealed token alike stay
        # selectable: an operator copies a value off a label
        # with the mouse too, the detail pages' own read.
        self.sentinel.setTextInteractionFlags(
            QtCore.Qt.TextInteractionFlag.TextSelectableByMouse
        )
        self.reach = QtWidgets.QLabel("", self)
        self.note = QtWidgets.QLabel(cells.PANEL_IDLE, self)
        for line in (self.label, self.sentinel, self.reach, self.note):
            # Every line wraps (#966): a long label or reach line
            # must not price the pane's minimum past the
            # window's own default width.
            line.setWordWrap(True)
        self.show_button = QtWidgets.QPushButton("Show", self)
        self.show_button.clicked.connect(self.toggle_reveal)
        self.copy_button = QtWidgets.QPushButton("Copy", self)
        self.copy_button.clicked.connect(self.copy)
        buttons = QtWidgets.QHBoxLayout()
        buttons.addWidget(self.show_button)
        buttons.addWidget(self.copy_button)
        buttons.addStretch(1)
        rows = QtWidgets.QVBoxLayout(self)
        rows.setContentsMargins(0, 0, 0, 0)
        rows.addWidget(self.label)
        rows.addWidget(QtWidgets.QLabel("sentinel:", self))
        rows.addWidget(self.sentinel)
        rows.addWidget(self.reach)
        rows.addWidget(self.note)
        rows.addLayout(buttons)
        rows.addStretch(1)
        self.sync_enabled()

    def sync_enabled(self) -> None:
        """The actions follow the panel's hold (#966): with no
        row on it, neither button answers."""
        empty = self.row is None
        self.show_button.setEnabled(not empty)
        self.copy_button.setEnabled(not empty)

    def show_row(self, row: dict) -> None:
        """One row's fresh or standing sentinel onto the panel
        (#966, over #440): masked on arrival, the previous row's
        state left behind."""
        self.row = row
        self.revealed = False
        self.paint(sentinel_model(row))
        self.sync_enabled()

    def clear(self) -> None:
        """Empty the panel (#966): every line clears — the
        sentinel leaves the widget tree with the row that
        carried it, the note back to its idle line."""
        self.row = None
        self.revealed = False
        self.label.clear()
        self.sentinel.clear()
        self.reach.clear()
        self.note.setText(cells.PANEL_IDLE)
        self.sync_enabled()

    def paint(self, model) -> None:
        """The panel's lines from its model (#966): the row's
        label, the masked sentinel — the real one while revealed
        — and the reach line the prefix decodes."""
        self.label.setText(model.label)
        value = (
            self.row.get("sentinel") or "" if self.revealed else model.masked
        )
        self.sentinel.setText(value)
        self.reach.setText(f"reach: {model.reach}")
        self.show_button.setText("Hide" if self.revealed else "Show")

    def toggle_reveal(self) -> None:
        """Show asks first (#966): the confirmation names what
        revealing means — the token on screen until the panel
        masks it again. Hide masks back with no ask; the token
        leaves the display the moment the operator turns it
        off."""
        if self.row is None:
            return
        if self.revealed:
            self.revealed = False
            self.paint(sentinel_model(self.row))
            return
        if self.confirm_reveal():
            self.revealed = True
            self.paint(sentinel_model(self.row))

    def confirm_reveal(self) -> bool:
        """The reveal's gate (#966): No is the default — the
        token reaches the screen only on the operator's own
        yes."""
        answer = QtWidgets.QMessageBox.question(
            self,
            cells.REVEAL_TITLE,
            cells.REVEAL_QUESTION,
            (
                QtWidgets.QMessageBox.StandardButton.Yes
                | QtWidgets.QMessageBox.StandardButton.No
            ),
            QtWidgets.QMessageBox.StandardButton.No,
        )
        return answer == QtWidgets.QMessageBox.StandardButton.Yes

    def copy(self) -> None:
        """The copy (#966): the real sentinel to the clipboard —
        masked or revealed, the copy lands the token — and the
        note says what happened."""
        if self.row is None:
            return
        QtWidgets.QApplication.clipboard().setText(
            self.row.get("sentinel") or ""
        )
        self.note.setText("copied to the clipboard")
