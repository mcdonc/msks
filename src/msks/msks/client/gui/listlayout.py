"""The workspace listing's pane arithmetic (#939, #972): the
column layout the listing's table takes against its pane's
width — every column at least as wide as its longest value (the
header's own label included), the spare beyond that split
evenly across the columns, and a pane narrower than the
columns' total giving ground on the image column alone, its
text ellipsized around the middle. The action column keeps its
buttons' own width: it neither gives ground on a narrow pane
nor takes spare on a wide one, so the row's actions stay whole
whatever the pane's width.

Plain widget arithmetic, no screen state: the listing
(:mod:`msks.client.gui.workspaces`) hands over the table, this
module hands back the layout — the images panes' own shape
(:mod:`msks.client.gui.images.layout`).

This module imports PySide6 at module scope — the screens' own
contract: nothing imports it at CLI parse time, and the
listing's module owns the import (#927).
"""

from PySide6 import QtCore

from .listcells import (
    ACTION_COLUMN,
    CELL_MARGIN,
    COLUMNS,
    IMAGE_COLUMN,
    IMAGE_ROLE,
)
from .listtable import ListTable


def size_columns(table: ListTable) -> None:
    """The columns' widths (#939): every column at least as
    wide as its longest value — the header's own label
    included — and the spare beyond that split evenly across
    the columns, an odd cell to the earlier ones, so the
    table fills the pane's whole width. The TUI listing's
    own arithmetic (#347), over the widget's columns. A pane
    narrower than the columns' total gives ground on the
    image column alone — the one whose values run long — its
    text ellipsized around the middle; every other column
    keeps its whole values, and the image reads whole again
    the moment the pane has the room."""
    fit_images(table, None)  # whole values first: minimums measure them
    table.resizeColumnsToContents()
    size_action_column(table)
    spare = (
        table.viewport().width()
        - sum(table.columnWidth(column) for column in range(len(COLUMNS)))
        - table.columnWidth(ACTION_COLUMN)
    )
    if spare <= 0:
        metrics = table.fontMetrics()
        floor = metrics.horizontalAdvance(COLUMNS[IMAGE_COLUMN]) + CELL_MARGIN
        capped = max(table.columnWidth(IMAGE_COLUMN) + spare, floor)
        table.setColumnWidth(IMAGE_COLUMN, capped)
        fit_images(table, capped - CELL_MARGIN)
        return
    share, extra = divmod(spare, len(COLUMNS))
    for column in range(len(COLUMNS)):
        table.setColumnWidth(
            column,
            table.columnWidth(column) + share + (1 if column < extra else 0),
        )


def size_action_column(table: ListTable) -> None:
    """The action column's width (#972): its buttons' own
    hint — the column neither gives ground on a narrow pane
    nor takes spare on a wide one, so the row's actions stay
    whole whatever the pane's width. A row the rebuild has
    not filled yet measures nothing; an empty table sets no
    width at all."""
    cells = (
        table.cellWidget(row, ACTION_COLUMN) for row in range(table.rowCount())
    )
    width = max(
        (cell.sizeHint().width() for cell in cells if cell is not None),
        default=0,
    )
    if width:
        table.setColumnWidth(ACTION_COLUMN, width + CELL_MARGIN)


def fit_images(table: ListTable, budget: int | None) -> None:
    """The image cells' display (#939): the whole value when
    the budget is open, else the value ellipsized around the
    middle — the version half of a reference kept, the TUI
    clip's own shape. A row the rebuild has not filled yet
    stands as it is, the action column's own rule."""
    metrics = table.fontMetrics()
    for row in range(table.rowCount()):
        item = table.item(row, IMAGE_COLUMN)
        if item is None:
            continue
        whole = item.data(IMAGE_ROLE)
        if budget is None:
            item.setText(whole)
            continue
        item.setText(
            metrics.elidedText(
                whole,
                QtCore.Qt.TextElideMode.ElideMiddle,
                budget,
            )
        )
