"""The workspace list screen (phase 3 of #924, #939): the thin
widget that renders the presenter's row models in the main
window — name, state, and the unresolved-mounts marker, the row
set the TUI builds. The screen holds no business logic: daemon
rows arrive as row models (:mod:`msks.client.gui.rows`), the
bridge (:mod:`msks.client.gui.bridge`) carries every call, and a
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

#: How long a flashed failure stays on the status line,
#: milliseconds — long enough to read, gone before it reads as
#: the screen's standing state (the next refresh clears it
#: whatever the timer does).
FLASH_MS = 4000

#: The state cell's sort data (#939): the lifecycle rank the
#: presenter carries, so a column click sorts by the vocabulary's
#: own sequence rather than the alphabet.
RANK_ROLE = QtCore.Qt.ItemDataRole.UserRole

#: The table's columns (#939), in display order — the row set
#: the TUI's listing shows of the fields the row models carry.
COLUMNS = ("Name", "State")

NAME_COLUMN = 0
STATE_COLUMN = 1


def state_text(model: WorkspaceRow) -> str:
    """The state cell's text (#939): the unresolved-mounts
    marker ahead of the state word — the TUI status cell's shape
    (``✗1 stopped``) — the word alone on a healthy row."""
    if model.issue_marker is None:
        return model.state
    return f"{model.issue_marker} {model.state}"


class StateItem(QtWidgets.QTableWidgetItem):
    """The state cell as a sort item (#939): the marker beside
    the word, the lifecycle rank carried as the item's sort data
    — a column click orders the rows by the vocabulary's own
    sequence, a state outside it last."""

    def __init__(self, model: WorkspaceRow) -> None:
        super().__init__(state_text(model))
        self.setData(RANK_ROLE, model.state_rank)

    def __lt__(self, other: QtWidgets.QTableWidgetItem) -> bool:
        return self.data(RANK_ROLE) < other.data(RANK_ROLE)


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
        screen's own open does the first. The rows on screen
        stand until the answer replaces them."""
        self.bridge.call(LIST_KEY, "workspaces")

    def set_rows(self, models: list[WorkspaceRow]) -> None:
        """The table as the row models read (#939): one row per
        model in the order the daemon served, the label its own
        column, the marker beside the state word in the other.
        Sorting pauses for the fill and resumes after, so the
        rebuild cannot re-order rows mid-swap."""
        self.table.setSortingEnabled(False)
        self.table.setRowCount(len(models))
        for index, model in enumerate(models):
            self.table.setItem(
                index, NAME_COLUMN, QtWidgets.QTableWidgetItem(model.label)
            )
            self.table.setItem(index, STATE_COLUMN, StateItem(model))
        self.table.setSortingEnabled(True)

    @QtCore.Slot(str, object)
    def on_succeeded(self, key: str, result: object) -> None:
        """One answered refresh (#939): the table rebuilds from
        the daemon rows — the presenter's plain data in — and the
        status line clears: the flash names a failure the next
        answer resolved. Another key's answer is not this
        screen's to take."""
        if key != LIST_KEY:
            return
        self.set_rows(workspace_rows(result))
        self.clear_flash()

    @QtCore.Slot(str, object)
    def on_failed(self, key: str, failure: object) -> None:
        """One refused refresh (#939): the failure flashes on the
        status line, and the rows already on screen stand — the
        operator keeps the list the last answer built. Another
        key's failure is not this screen's to show."""
        if key != LIST_KEY:
            return
        self.flash(str(failure))

    def flash(self, message: str) -> None:
        """The status line's flash (#939): the message shows for
        the flash span, then the line clears itself."""
        self.status.setText(message)
        QtCore.QTimer.singleShot(FLASH_MS, self.clear_flash)

    def clear_flash(self) -> None:
        """The flash's clear — the timeout's slot, and the path
        a successful refresh takes when it resolves the outage
        the flash named."""
        self.status.clear()

    def showEvent(self, event: QtGui.QShowEvent) -> None:
        """The screen's first refresh at open (#939), and the
        tick's resume on a re-show: the screen refreshes while
        it owns the window."""
        self.refresh()
        self.timer.start()
        super().showEvent(event)

    def hideEvent(self, event: QtGui.QHideEvent) -> None:
        """The tick stops with the screen (#939): a closed
        window hides its children, and a timer tick after close
        must not touch widgets."""
        self.timer.stop()
        super().hideEvent(event)
