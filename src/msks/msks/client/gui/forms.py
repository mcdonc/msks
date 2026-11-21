"""The GUI tabs' shared chrome (#987, #988): the read-only
table and the save/cancel row the mounts and proxies surfaces
build the same way — one definition, the screens' own rule
against drift. This module imports PySide6 at module scope,
the screens' own contract: nothing imports it at CLI parse
time.
"""

from PySide6 import QtGui, QtWidgets


def muted_note(text: str) -> QtWidgets.QLabel:
    """A muted section note: the empty state's honest stand-in,
    colored the palette's own shadow (#945's own shape, shared
    by the page's sections and the tabs' stand-ins)."""
    label = QtWidgets.QLabel(text)
    shadow = label.palette().color(QtGui.QPalette.ColorRole.Shadow)
    label.setStyleSheet(f"color: {shadow.name()};")
    return label


def read_only_table(columns: tuple) -> QtWidgets.QTableWidget:
    """A table that renders but never edits (#987, #988): one
    row per record, single-row selection, the last column
    stretching to the pane's width. The caller owns the rows and
    the selection's wiring."""
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
    table.horizontalHeader().setStretchLastSection(True)
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
