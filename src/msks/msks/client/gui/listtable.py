"""The listing's table widget (#962): the row set the list
screen's models fill, carrying the one key bridge the table
owns — the Up on the top row handing the keyboard upward —
and the column sizing every group table takes against its
pane's width (#997).
"""

from PySide6 import QtCore, QtWidgets


class ListTable(QtWidgets.QTableWidget):
    """The listing's own table (#962): the row set the
    presenter's models fill. Up on the top row — or an empty
    table's Up — hands the keyboard to whatever stands above
    the list in the focus chain (the window's tab strip): the
    arrows read top-to-bottom without a Tab, both directions,
    the spatial-navigation rule."""

    def keyPressEvent(self, event) -> None:
        if event.key() == QtCore.Qt.Key.Key_Up and self.currentRow() <= 0:
            # The base walk, not the view's own: the view's
            # override answers Shift-Tab's shape without moving
            # the focus, and the arrow needs the move. The
            # chain's own order carries the keyboard to the
            # strip above the list.
            QtWidgets.QWidget.focusNextPrevChild(self, False)
            return
        super().keyPressEvent(event)


def resized_viewport(
    tables: tuple, watched: QtCore.QObject, event: QtCore.QEvent
):
    """The table whose viewport a resize event names (#997, the
    images tab's own hook #964): a vertical scrollbar appearing
    shrinks the viewport after a fill sized against the wider
    one, and a window resized while the tab stood hidden
    reaches the pane at its next show. None when the event
    names no pane among ``tables``."""
    if event.type() != QtCore.QEvent.Type.Resize:
        return None
    for table in tables:
        if watched is table.viewport():
            return table
    return None


def fill_columns(table: QtWidgets.QTableWidget) -> None:
    """One table's columns at the pane's whole width (#997,
    the images catalog's own sizing #964 over #347): every
    column at least as wide as its longest value — the
    header's own label included — and the spare beyond that
    split evenly across the columns, an odd cell to the
    earlier ones. One definition for every group table, so
    each fills all the horizontal space its pane gives it."""
    table.resizeColumnsToContents()
    spare = table.viewport().width() - sum(
        table.columnWidth(column) for column in range(table.columnCount())
    )
    if spare <= 0:
        return
    share, extra = divmod(spare, table.columnCount())
    for column in range(table.columnCount()):
        table.setColumnWidth(
            column,
            table.columnWidth(column) + share + (1 if column < extra else 0),
        )
