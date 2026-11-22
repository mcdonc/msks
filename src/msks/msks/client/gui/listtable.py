"""The listing's table widget (#962): the row set the list
screen's models fill, carrying the one key bridge the table
owns — the Up on the top row handing the keyboard upward —
and the column sizing every group table takes against its
pane's width (#997).
"""

from PySide6 import QtCore, QtGui, QtWidgets

#: The bold-seat mark a bolded header carries (#976): the
#: zoom's after-write walk finds the headers by it — an
#: explicit font (the only way Qt holds a bold header) does
#: not follow the application font on its own.
BOLD_SEAT = "msks-bold-header"


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


def bold_header(table: QtWidgets.QTableWidget) -> None:
    """One group table's header text (#964, #976): the bold
    weight on the panes' headers — one definition, so every
    pane's column labels read as one table's chrome whatever
    the platform style would draw on its own. The header
    carries the bold-seat mark: the whole-client zoom's
    after-write walk (:func:`rebold_headers`) finds it there
    and moves the header with the rest of the chrome."""
    header = table.horizontalHeader()
    header.setProperty(BOLD_SEAT, True)
    header_font(header)


def header_font(header: QtWidgets.QHeaderView) -> None:
    """The header's bold face off the application font (#976):
    the size and the family stay the application's own."""
    font = QtGui.QGuiApplication.font()
    font.setBold(True)
    header.setFont(font)


def rebold_headers() -> None:
    """The zoom's after-write walk (#976): every bolded header
    still alive takes the fresh application font's bold face
    again. An explicit font is the only way Qt holds a bold
    header, and an explicit font freezes a widget against the
    application font's changes — the headers are the window's
    only explicitly-set fonts, so this walk is the whole gap
    between the zoom's write and the chrome following it."""
    app = QtWidgets.QApplication.instance()
    for widget in app.allWidgets():
        if widget.property(BOLD_SEAT):
            header_font(widget)


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
