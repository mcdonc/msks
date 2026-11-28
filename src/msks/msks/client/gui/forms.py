"""The GUI tabs' shared chrome (#987, #988, #1014): the
read-only table and the save/cancel row the mounts and proxies
surfaces build the same way — one definition, the screens' own
rule against drift — beside the centered popups every screen
opens (#1014): the yes/no question, the item picker, the text
prompt, the masked passphrase prompt (#1147), and the about box,
built over their asking window so the placement holds across
window managers. This module imports PySide6 at module scope, the
screens' own contract: nothing imports it at CLI parse time.
"""

from PySide6 import QtCore, QtGui, QtWidgets

from .dialogs import center_on_parent
from .listtable import ListTable
from .tintedlabel import TintedLabel


class ModePicker(QtWidgets.QComboBox):
    """A combo that commits only on a deliberate pick (#1049
    review): the wheel passes through — a scroll across the pane
    never changes a live choice (an egress posture, an audit
    filter) — while popup picks and typed selection commit
    through ``activated`` as every deliberate pick does. The
    Egress tab's switcher and its audit filter share it."""

    def wheelEvent(self, event: QtGui.QWheelEvent) -> None:
        """One wheel event over the picker ignores itself: the
        parent takes the scroll, the choice stands."""
        event.ignore()


def muted_note(text: str) -> QtWidgets.QLabel:
    """A muted section note: the empty state's honest stand-in,
    colored the palette's own shadow (#945's own shape, shared
    by the page's sections and the tabs' stand-ins)."""
    label = QtWidgets.QLabel(text)
    shadow = label.palette().color(QtGui.QPalette.ColorRole.Shadow)
    label.setStyleSheet(f"color: {shadow.name()};")
    return label


class ElidedNote(TintedLabel):
    """A note that keeps one line (#1154 review): the text elides
    at the label's right edge, the whole text stays on ``text()``
    and on the tooltip, and the layout reads no width from the
    label — a refusal wider than the pane opens nothing and holds
    no window to its width. The label is its row's flexible item:
    it expands into the free room, so the row carries no stretch
    beside it. The Egress trail's footer row carries its refusal
    on one. A tinted label (#1160): the trail's refusal wears the
    error tint."""

    def __init__(self, text: str = "") -> None:
        super().__init__()
        self.setSizePolicy(
            QtWidgets.QSizePolicy.Policy.Expanding,
            QtWidgets.QSizePolicy.Policy.Preferred,
        )
        self.full = ""
        self.line = ""
        self.setText(text)

    def minimumSizeHint(self) -> QtCore.QSize:
        """One line tall and no width: the row's minimum never
        reads the note's words."""
        return QtCore.QSize(0, super().minimumSizeHint().height())

    def setText(self, text: str) -> None:
        """The whole text stands on ``text()``; the label shows the
        line that fits its width."""
        self.full = text
        self.elide()

    def text(self) -> str:
        return self.full

    def resizeEvent(self, event: QtGui.QResizeEvent) -> None:
        super().resizeEvent(event)
        self.elide()

    def elide(self) -> None:
        """The line for the label's width: the whole text when it
        fits, an elided tail when it does not — the whole text
        then on the tooltip."""
        self.line = self.fontMetrics().elidedText(
            self.full, QtCore.Qt.TextElideMode.ElideRight, self.width()
        )
        QtWidgets.QLabel.setText(self, self.line)
        self.setToolTip("" if self.line == self.full else self.full)


def fit_columns(
    table: QtWidgets.QTableWidget, content_columns: tuple = ()
) -> None:
    """The table's width rule (#1025): the columns share the
    pane's whole horizontal width, the named content columns
    hugging their text instead — widening the window widens
    the sharing columns, never the pane's blank tail."""
    header = table.horizontalHeader()
    for column in range(table.columnCount()):
        mode = (
            QtWidgets.QHeaderView.ResizeMode.ResizeToContents
            if column in content_columns
            else QtWidgets.QHeaderView.ResizeMode.Stretch
        )
        header.setSectionResizeMode(column, mode)


def read_only_table(columns: tuple, content_columns: tuple = ()) -> ListTable:
    """A table that renders but never edits (#987, #988): one
    row per record, single-row selection, the columns sharing
    the pane's whole width (#1025) with the content columns
    hugging their text. The table is the listing's own class
    (#1177), so its headers read like every other page's —
    bold and left over their cells — and its keys follow the
    spatial-navigation rule. The caller owns the rows and the
    selection's wiring."""
    table = ListTable(0, len(columns))
    table.setHorizontalHeaderLabels(columns)
    table.verticalHeader().setVisible(False)
    table.setSelectionBehavior(
        QtWidgets.QAbstractItemView.SelectionBehavior.SelectRows
    )
    table.setSelectionMode(
        QtWidgets.QAbstractItemView.SelectionMode.SingleSelection
    )
    table.setEditTriggers(
        QtWidgets.QAbstractItemView.EditTrigger.NoEditTriggers
    )
    table.setShowGrid(False)
    fit_columns(table, content_columns)
    return table


def save_cancel_row(
    submit_slot,
) -> tuple[
    QtWidgets.QPushButton, QtWidgets.QPushButton, QtWidgets.QHBoxLayout
]:
    """A dialog's save and cancel buttons with their row: save
    default and wired to the submit slot, cancel left for the
    dialog's own reject."""
    save = QtWidgets.QPushButton("Save")
    save.setDefault(True)
    save.clicked.connect(submit_slot)
    cancel = QtWidgets.QPushButton("Cancel")
    row = QtWidgets.QHBoxLayout()
    row.addWidget(save)
    row.addWidget(cancel)
    return save, cancel, row


def about_box(parent, title: str, text: str) -> QtWidgets.QMessageBox:
    """The about box in the house dialog shape (#1007, #1014):
    built over the asking window and centered on it before the
    modal loop, the placement every popup the client opens
    carries. The box stands unshown; the caller owns the ask.
    """
    box = QtWidgets.QMessageBox(parent)
    box.setWindowTitle(title)
    box.setText(text)
    box.setIcon(QtWidgets.QMessageBox.Icon.Information)
    center_on_parent(box)
    return box


def confirm_box(parent, title: str, text: str) -> QtWidgets.QMessageBox:
    """The yes/no question's own box (#1014): built over the
    asking widget, its No the default — the operator's call,
    not the pointer's — and centered on the asking window, the
    placement the static question call leaves to the window
    manager and a corner-placing manager answers off the app.
    The box stands unshown; the caller owns the ask."""
    box = QtWidgets.QMessageBox(parent)
    box.setWindowTitle(title)
    box.setText(text)
    box.setIcon(QtWidgets.QMessageBox.Icon.Question)
    box.setStandardButtons(
        QtWidgets.QMessageBox.StandardButton.Yes
        | QtWidgets.QMessageBox.StandardButton.No
    )
    box.setDefaultButton(QtWidgets.QMessageBox.StandardButton.No)
    center_on_parent(box)
    return box


def confirm(parent, title: str, text: str) -> bool:
    """The yes/no question asked centered (#1014): the box the
    seam above builds, shown under the modal loop — Yes answers
    True, anything else False."""
    return (
        confirm_box(parent, title, text).exec()
        == QtWidgets.QMessageBox.StandardButton.Yes
    )


def pick_item(
    parent, title: str, label: str, choices: list[str], current: str
) -> str | None:
    """The item picker asked centered (#1014): the choices in a
    closed combo over the asking window, ``current`` the opening
    one — a cancelled picker answers None, a pick the picked
    item. The static ``getItem`` call leaves the placement to
    the window manager; this build centers on the asking
    window first."""
    picker = QtWidgets.QInputDialog(parent)
    picker.setWindowTitle(title)
    picker.setLabelText(label)
    picker.setComboBoxItems(list(choices))
    picker.setTextValue(current)
    center_on_parent(picker)
    if picker.exec() != QtWidgets.QDialog.DialogCode.Accepted:
        return None
    return picker.textValue()


def prompt_text(
    parent, title: str, label: str, current: str = ""
) -> str | None:
    """The text prompt asked centered (#1063): one line of
    input over the asking window — a cancelled prompt answers
    None, a submit the entered text, stripped; *current* (#1226)
    opens the line with a standing value, the rename's own name.
    The mode is
    named the way the picker names its combo, not left to the
    constructor's default. The static ``getText`` call leaves
    the placement to the window manager; this build centers
    on the asking window first, the picker's own rule."""
    prompt = QtWidgets.QInputDialog(parent)
    prompt.setWindowTitle(title)
    prompt.setLabelText(label)
    prompt.setInputMode(QtWidgets.QInputDialog.InputMode.TextInput)
    prompt.setTextValue(current)
    center_on_parent(prompt)
    if prompt.exec() != QtWidgets.QDialog.DialogCode.Accepted:
        return None
    return prompt.textValue().strip()


def prompt_password(
    parent, title: str, label: str, accept: str = "OK"
) -> str | None:
    """The masked prompt asked centered (#1147, the unlock's
    passphrase ask): one password-mode line of input over the
    asking window — a cancelled prompt answers None, a submit the
    typed text as typed (a passphrase's spaces are its own, so
    nothing is stripped), and *accept* is the OK button's own
    word. The static ``getText`` call leaves the placement to the
    window manager; this build centers on the asking window
    first, the picker's own rule."""
    prompt = QtWidgets.QInputDialog(parent)
    prompt.setWindowTitle(title)
    prompt.setLabelText(label)
    prompt.setInputMode(QtWidgets.QInputDialog.InputMode.TextInput)
    prompt.setTextEchoMode(QtWidgets.QLineEdit.EchoMode.Password)
    prompt.setOkButtonText(accept)
    center_on_parent(prompt)
    if prompt.exec() != QtWidgets.QDialog.DialogCode.Accepted:
        return None
    return prompt.textValue()
