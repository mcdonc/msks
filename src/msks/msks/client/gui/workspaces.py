"""The workspace list screen (phase 3 of #924, #939): the thin
widget that renders the presenter's row models in the main
window — the TUI listing's five fields (NAME, STATUS with the
unresolved-mounts marker, ID, IMAGE, CREATED). The screen holds
no business logic: daemon rows arrive as row models
(:mod:`msks.client.gui.rows`), the bridge
(:mod:`msks.client.gui.bridge`) carries every call, and a
refresh failure flashes the status line while the rows already
on screen stand.

This module imports PySide6 at module scope. That is safe
because nothing imports it at CLI parse time: the package init
pins only the Qt-free ``.app``, ``.data``, and ``.rows``
(#927), and ``.app``'s window path imports this module lazily,
after its guards — the reachability the graph gate counts (the
bridge's own docstring states the same contract).
"""

from PySide6 import QtCore, QtGui, QtWidgets

from ..data import RestFailure
from .bridge import Bridge
from .rows import WorkspaceRow, workspace_rows

#: The standing refresh cadence (#939), milliseconds — the TUI's
#: own listing interval (#470 L2, five seconds): the list reloads
#: on this tick while the window owns it, so a workspace another
#: surface moved appears without a keypress.
REFRESH_MS = 5000

#: The bridge call's key for a listing refresh (#939): the
#: screen answers only its own key, whatever else shares the
#: bridge.
LIST_KEY = "workspaces-list"

#: How long a flashed refusal stays on the status line,
#: milliseconds — long enough to read, gone before it reads as
#: the screen's standing state. An outage (the daemon answered
#: nothing, :attr:`RestFailure.reach`) never times out: the line
#: holds its message until an answer resolves it, the #904
#: contract the TUI's own listing keeps.
FLASH_MS = 4000

#: The state cell's sort data (#939): the lifecycle rank the
#: presenter carries, so a column click sorts by the vocabulary's
#: own sequence rather than the alphabet.
RANK_ROLE = QtCore.Qt.ItemDataRole.UserRole

#: The image cell's whole value (#939), under its sort role:
#: the display text ellipsizes when the pane cannot fit every
#: column, and the whole value is what the column sorts by.
IMAGE_ROLE = QtCore.Qt.ItemDataRole.UserRole + 1

#: The created cell's sort data (#939): the parsed creation
#: moment — a header click orders by date, never by the
#: relative label's spelling.
CREATED_ROLE = QtCore.Qt.ItemDataRole.UserRole + 2

#: The workspace's id on every row's name item (#939): the
#: selection follows the workspace, not the row slot, across a
#: refresh's rebuild.
ROW_ID_ROLE = QtCore.Qt.ItemDataRole.UserRole + 3

#: The cell margin the contents sizing pays (#939): the budget
#: an ellipsized cell's text renders within.
CELL_MARGIN = 8

#: The table's columns (#939), in display order — the TUI
#: listing's own five, the fields the row models carry.
COLUMNS = ("NAME", "STATUS", "ID", "IMAGE", "CREATED")

NAME_COLUMN = 0
STATUS_COLUMN = 1
ID_COLUMN = 2
IMAGE_COLUMN = 3
CREATED_COLUMN = 4


def read_only_item(
    item: QtWidgets.QTableWidgetItem,
) -> QtWidgets.QTableWidgetItem:
    """One cell that renders but never edits (#939): the
    listing is a view over the daemon's state, and an edit would
    paint a value only the display holds — the next refresh
    throws it away. The cell stays selectable and enabled."""
    item.setFlags(item.flags() & ~QtCore.Qt.ItemFlag.ItemIsEditable)
    return item


def state_text(model: WorkspaceRow) -> str:
    """The status cell's text (#939): the unresolved-mounts
    marker ahead of the state word — the TUI status cell's shape
    (``✗1 stopped``) — the word alone on a healthy row."""
    if model.issue_marker is None:
        return model.state
    return f"{model.issue_marker} {model.state}"


class StateItem(QtWidgets.QTableWidgetItem):
    """The status cell as a sort item (#939): the marker beside
    the word, the lifecycle rank carried as the item's sort data
    — a column click orders the rows by the vocabulary's own
    sequence, a state outside it last."""

    def __init__(self, model: WorkspaceRow) -> None:
        super().__init__(state_text(model))
        self.setData(RANK_ROLE, model.state_rank)

    def __lt__(self, other: QtWidgets.QTableWidgetItem) -> bool:
        return self.data(RANK_ROLE) < other.data(RANK_ROLE)


class ImageItem(QtWidgets.QTableWidgetItem):
    """The image cell (#939): the whole value under the sort
    role — an ellipsized display still sorts by the value it
    stands for."""

    def __init__(self, value: str) -> None:
        super().__init__(value)
        self.setData(IMAGE_ROLE, value)

    def __lt__(self, other: QtWidgets.QTableWidgetItem) -> bool:
        return self.data(IMAGE_ROLE) < other.data(IMAGE_ROLE)


class CreatedItem(QtWidgets.QTableWidgetItem):
    """The created cell (#939): the relative label reads, the
    parsed moment sorts — a header click orders by date, never
    by the label's spelling (``10mo ago`` sits a year away from
    ``1y ago`` in text order). A missing stamp reads as the
    oldest and sinks on the newest-first read."""

    def __init__(self, model: WorkspaceRow) -> None:
        super().__init__(model.created)
        self.setData(CREATED_ROLE, model.created_rank)

    def __lt__(self, other: QtWidgets.QTableWidgetItem) -> bool:
        return self.data(CREATED_ROLE) < other.data(CREATED_ROLE)


class WorkspaceList(QtWidgets.QWidget):
    """The main window's central screen (#939): the table of row
    models, a manual refresh button, and the status line a
    failed refresh flashes. Every daemon exchange walks the
    bridge — the screen never awaits the network — and the
    answers arrive on the GUI thread through this widget's
    slots, a receiver living in the thread the bridge's queued
    delivery needs.

    ``bridge`` is the window's own
    (:func:`msks.client.gui.app.open_window` builds both): the
    screen reads its two signals and its one verb, nothing
    else.
    """

    def __init__(self, bridge: Bridge) -> None:
        super().__init__()
        self.bridge = bridge
        self.pending = False
        self.flash_seq = 0
        bridge.succeeded.connect(self.on_succeeded)
        bridge.failed.connect(self.on_failed)
        self.table = QtWidgets.QTableWidget(0, len(COLUMNS), self)
        self.table.setHorizontalHeaderLabels(COLUMNS)
        self.table.verticalHeader().setVisible(False)
        # No standing sort (#939): the screen opens in the
        # daemon's order — the presenter preserves it — so the
        # indicator starts clear; a header click sets one, and
        # the fill's re-enable then re-applies the chosen sort
        # across refreshes.
        self.table.horizontalHeader().setSortIndicator(
            -1, QtCore.Qt.SortOrder.AscendingOrder
        )
        self.table.setSortingEnabled(True)
        self.refresh_button = QtWidgets.QPushButton("Refresh", self)
        self.refresh_button.clicked.connect(self.refresh)
        self.status = QtWidgets.QLabel("", self)
        self.timer = QtCore.QTimer(self)
        self.timer.setInterval(REFRESH_MS)
        self.timer.timeout.connect(self.refresh)
        row = QtWidgets.QHBoxLayout()
        row.addWidget(self.refresh_button)
        row.addWidget(self.status, stretch=1)
        layout = QtWidgets.QVBoxLayout(self)
        layout.addWidget(self.table)
        layout.addLayout(row)
        self.setLayout(layout)

    @QtCore.Slot()
    def refresh(self) -> None:
        """One listing refresh through the bridge (#939) — the
        timer's tick and the button's click share it, and the
        screen's own open does the first. A refresh already in
        flight is skipped, exactly the TUI listing's own guard
        (#470): ticks that outlive a slow call would stack
        identical requests while the first answer has yet to
        land. The rows on screen stand until the answer replaces
        them."""
        if self.pending:
            return
        self.pending = True
        self.bridge.call(LIST_KEY, "workspaces")

    def set_rows(self, models: list[WorkspaceRow]) -> None:
        """The table as the row models read (#939): one row per
        model in the order the daemon served, the label its own
        column, the marker beside the state word in the status
        one, and the id, image, and created cells beside them.
        An unchanged listing repaints nothing — the TUI
        listing's own rule (#470 L2) — because a rebuild under a
        sort moves the row slots while the selection holds its
        slot, and the highlight would wander off the workspace
        the operator clicked. A rebuild the listing does need
        carries the selection by workspace id, so the highlight
        follows its row wherever the order lands it. Sorting
        pauses for the fill and resumes after, so the rebuild
        cannot re-order rows mid-swap."""
        if self.rows_unchanged(models):
            return
        held = self.held_cells()
        self.table.setSortingEnabled(False)
        self.table.setRowCount(len(models))
        for index, model in enumerate(models):
            name = read_only_item(QtWidgets.QTableWidgetItem(model.label))
            name.setData(ROW_ID_ROLE, model.id)
            self.table.setItem(index, NAME_COLUMN, name)
            self.table.setItem(
                index, STATUS_COLUMN, read_only_item(StateItem(model))
            )
            self.table.setItem(
                index,
                ID_COLUMN,
                read_only_item(QtWidgets.QTableWidgetItem(model.id)),
            )
            self.table.setItem(
                index, IMAGE_COLUMN, read_only_item(ImageItem(model.image))
            )
            self.table.setItem(
                index,
                CREATED_COLUMN,
                read_only_item(CreatedItem(model)),
            )
        self.table.setSortingEnabled(True)
        self.size_columns()
        self.restore_cells(held)

    def rows_unchanged(self, models: list[WorkspaceRow]) -> bool:
        """Whether the table already reads exactly as the models
        do (#939): same rows, same order, same cells — the image
        by its whole value, an elided display and all. An
        unchanged answer this way rebuilds nothing."""
        if self.table.rowCount() != len(models):
            return False
        return all(
            self.row_unchanged(row, model) for row, model in enumerate(models)
        )

    def row_unchanged(self, row: int, model: WorkspaceRow) -> bool:
        """One row's cells against its model (#939): every text
        cell equal, the image equal by its whole value."""
        checks = (
            (NAME_COLUMN, model.label),
            (STATUS_COLUMN, state_text(model)),
            (ID_COLUMN, model.id),
            (CREATED_COLUMN, model.created),
        )
        for column, text in checks:
            item = self.table.item(row, column)
            if item is None or item.text() != text:
                return False
        image = self.table.item(row, IMAGE_COLUMN)
        return image is not None and image.data(IMAGE_ROLE) == model.image

    def held_cells(self) -> tuple[list[tuple[str, int]], tuple | None]:
        """What the operator holds on the table (#939): every
        selected cell and the current one, each as its
        workspace's id and its column — the form that survives a
        rebuild. A row with no name item holds nothing."""
        held = []
        for index in self.table.selectedIndexes():
            name = self.table.item(index.row(), NAME_COLUMN)
            if name is not None:
                held.append((name.data(ROW_ID_ROLE), index.column()))
        current = self.table.currentIndex()
        now = None
        if current.isValid():
            name = self.table.item(current.row(), NAME_COLUMN)
            if name is not None:
                now = (name.data(ROW_ID_ROLE), current.column())
        return held, now

    def restore_cells(
        self, held: tuple[list[tuple[str, int]], tuple | None]
    ) -> None:
        """The held cells back on the rebuilt table (#939): each
        on its workspace's row in its new place, the selection
        whole and the current cell with it. A workspace the new
        listing dropped simply loses its hold."""
        cells, now = held
        rows = self.row_ids()
        self.restore_selection(cells, rows)
        self.restore_current(now, rows)

    def row_ids(self) -> dict[str, int]:
        """The workspace id per table row (#939): where each
        hold lands after a rebuild."""
        return {
            self.table.item(row, NAME_COLUMN).data(ROW_ID_ROLE): row
            for row in range(self.table.rowCount())
        }

    def restore_selection(
        self, cells: list[tuple[str, int]], rows: dict[str, int]
    ) -> None:
        """The held selection back (#939): the whole set at once,
        so a hold the new listing dropped clears rather than
        lingers on whatever row landed under its slot."""
        selection = QtCore.QItemSelection()
        for workspace, column in cells:
            row = rows.get(workspace)
            if row is not None:
                index = self.table.model().index(row, column)
                selection.select(index, index)
        self.table.selectionModel().select(
            selection,
            QtCore.QItemSelectionModel.SelectionFlag.ClearAndSelect,
        )

    def restore_current(
        self, now: tuple[str, int] | None, rows: dict[str, int]
    ) -> None:
        """The held current cell back (#939): on its workspace's
        row when the listing still carries it, else gone — not
        onto whatever row landed under the old slot."""
        if now is None:
            return
        if now[0] in rows:
            self.table.setCurrentIndex(
                self.table.model().index(rows[now[0]], now[1])
            )
        else:
            self.table.selectionModel().clearCurrentIndex()

    def size_columns(self) -> None:
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
        self.fit_images(None)  # whole values first: minimums measure them
        self.table.resizeColumnsToContents()
        spare = self.table.viewport().width() - sum(
            self.table.columnWidth(column) for column in range(len(COLUMNS))
        )
        if spare <= 0:
            metrics = self.table.fontMetrics()
            floor = (
                metrics.horizontalAdvance(COLUMNS[IMAGE_COLUMN]) + CELL_MARGIN
            )
            capped = max(self.table.columnWidth(IMAGE_COLUMN) + spare, floor)
            self.table.setColumnWidth(IMAGE_COLUMN, capped)
            self.fit_images(capped - CELL_MARGIN)
            return
        share, extra = divmod(spare, len(COLUMNS))
        for column in range(len(COLUMNS)):
            self.table.setColumnWidth(
                column,
                self.table.columnWidth(column)
                + share
                + (1 if column < extra else 0),
            )

    def fit_images(self, budget: int | None) -> None:
        """The image cells' display (#939): the whole value when
        the budget is open, else the value ellipsized around the
        middle — the version half of a reference kept, the TUI
        clip's own shape."""
        metrics = self.table.fontMetrics()
        for row in range(self.table.rowCount()):
            item = self.table.item(row, IMAGE_COLUMN)
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

    @QtCore.Slot(str, object)
    def on_succeeded(self, key: str, result: object) -> None:
        """One answered refresh (#939): the table rebuilds from
        the daemon rows — the presenter's plain data in — and the
        status line clears: the flash names a failure the next
        answer resolved. Another key's answer is not this
        screen's to take."""
        if key != LIST_KEY:
            return
        self.pending = False
        self.set_rows(workspace_rows(result))
        self.clear_flash()

    @QtCore.Slot(str, object)
    def on_failed(self, key: str, failure: RestFailure) -> None:
        """One refused refresh (#939): the failure flashes on the
        status line — an outage (the daemon answered nothing)
        stands until an answer resolves it — and the rows already
        on screen stand: the operator keeps the list the last
        answer built. Another key's failure is not this screen's
        to show."""
        if key != LIST_KEY:
            return
        self.pending = False
        self.flash(str(failure), outage=failure.reach)

    def flash(self, message: str, *, outage: bool = False) -> None:
        """The status line's flash (#939, #904): the message
        shows; a refusal clears itself after the flash span, an
        outage stands for the next answer to resolve. Each flash
        carries its own serial, so a later flash's message is not
        erased by an earlier flash's timeout."""
        self.flash_seq += 1
        self.status.setText(message)
        if outage:
            return
        seq = self.flash_seq
        QtCore.QTimer.singleShot(FLASH_MS, lambda: self.clear_stale(seq))

    def clear_stale(self, seq: int) -> None:
        """The flash timeout's slot: the line clears only when
        this timeout's flash is still the one on it."""
        if seq == self.flash_seq:
            self.status.clear()

    def clear_flash(self) -> None:
        """The flash's whole clear — the path a successful
        refresh takes when it resolves the outage the flash
        named. The serial bump retires any timeout still
        pending."""
        self.flash_seq += 1
        self.status.clear()

    def showEvent(self, event: QtGui.QShowEvent) -> None:
        """The screen's first refresh at open (#939), and the
        tick's resume on a re-show: the screen refreshes while it
        owns the window."""
        self.refresh()
        self.timer.start()
        super().showEvent(event)

    def hideEvent(self, event: QtGui.QHideEvent) -> None:
        """The tick stops with the screen (#939): a closed
        window hides its children, and a timer tick after close
        must not touch widgets."""
        self.timer.stop()
        super().hideEvent(event)

    def resizeEvent(self, event: QtGui.QResizeEvent) -> None:
        """A resized pane re-flows the columns (#939): the
        minimums follow the contents whatever the pane's width,
        and the spare fills the pane's new width."""
        self.size_columns()
        super().resizeEvent(event)
