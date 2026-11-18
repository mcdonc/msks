"""The listing's table widget (#962): the row set the list
screen's models fill, carrying the one key bridge the table
owns — the Up on the top row handing the keyboard upward.
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
