"""The GUI tabs' shared chrome (#987, #988, #1014): the
read-only table and the save/cancel row the mounts and proxies
surfaces build the same way — one definition, the screens' own
rule against drift — beside the centered popups every screen
opens (#1014): the yes/no question, the item picker, and the
about box, built over their asking window so the placement
holds across window managers. This module imports PySide6 at
module scope, the screens' own contract: nothing imports it at
CLI parse time.
"""

from PySide6 import QtGui, QtWidgets

from .dialogs import center_on_parent


def muted_note(text: str) -> QtWidgets.QLabel:
    """A muted section note: the empty state's honest stand-in,
    colored the palette's own shadow (#945's own shape, shared
    by the page's sections and the tabs' stand-ins)."""
    label = QtWidgets.QLabel(text)
    shadow = label.palette().color(QtGui.QPalette.ColorRole.Shadow)
    label.setStyleSheet(f"color: {shadow.name()};")
    return label


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


def read_only_table(
    columns: tuple, content_columns: tuple = ()
) -> QtWidgets.QTableWidget:
    """A table that renders but never edits (#987, #988): one
    row per record, single-row selection, the columns sharing
    the pane's whole width (#1025) with the content columns
    hugging their text. The caller owns the rows and the
    selection's wiring."""
    table = QtWidgets.QTableWidget(0, len(columns))
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
