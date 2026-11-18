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
from .rows import WorkspaceRow, detail_text, workspace_rows

#: The standing refresh cadence (#939), milliseconds — the TUI's
#: own listing interval (#470 L2, five seconds): the list reloads
#: on this tick while the window owns it, so a workspace another
#: surface moved appears without a keypress.
REFRESH_MS = 5000

#: The bridge call's key for a listing refresh (#939): the
#: screen answers only its own key, whatever else shares the
#: bridge.
LIST_KEY = "workspaces-list"

#: The action calls' keys (#940, #972): start, stop, and
#: remove on a row, each its own flight.
START_KEY = "workspaces-start"
STOP_KEY = "workspaces-stop"
DELETE_KEY = "workspaces-delete"

#: The capacity report's key (#849, the GUI's read): the detail
#: strip's storage figure, fetched beside the rows and cached
#: between listings.
STORAGE_KEY = "workspaces-storage"

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

#: The toggle's icon per action (#972): the VCR pair — the play
#: gesture for a start, the stop sign for a stop.
TOGGLE_ICONS = {
    "start": QtWidgets.QStyle.StandardPixmap.SP_MediaPlay,
    "stop": QtWidgets.QStyle.StandardPixmap.SP_MediaStop,
}

#: The table's data columns (#939), in display order — the TUI
#: listing's own five, the fields the row models carry.
COLUMNS = ("NAME", "STATUS", "ID", "IMAGE", "CREATED")

NAME_COLUMN = 0
STATUS_COLUMN = 1
ID_COLUMN = 2
IMAGE_COLUMN = 3
CREATED_COLUMN = 4

#: The row-actions column (#972): last, with no header label and
#: a header that sorts nothing — one cell of buttons per row,
#: the row's own actions beside its facts.
ACTION_COLUMN = len(COLUMNS)


def read_only_item(
    item: QtWidgets.QTableWidgetItem,
) -> QtWidgets.QTableWidgetItem:
    """One cell that renders but never edits (#939): the
    listing is a view over the daemon's state, and an edit would
    paint a value only the display holds — the next refresh
    throws it away. The cell stays selectable and enabled."""
    item.setFlags(item.flags() & ~QtCore.Qt.ItemFlag.ItemIsEditable)
    return item


def action_reply_text(label: str, reply: object) -> str:
    """The action's own flash (#940, the TUI's #776 mask): the
    workspace's label beside the status the daemon's answer
    carries — the outcome shows the moment the action lands,
    whatever the next listing flight does. An answer without a
    readable status still names its workspace."""
    status = reply.get("status") if isinstance(reply, dict) else None
    return f"{label} {status}" if status else label


def cost_of(ws: dict) -> int | None:
    """One report entry's root-plus-home cost (#849), or None
    for an entry that predates the per-row fields."""
    if "root_bytes" not in ws or "home_bytes" not in ws:
        return None
    return ws["root_bytes"] + ws["home_bytes"]


def workspace_costs(report: dict) -> dict[str, int]:
    """The capacity report's per-workspace costs (#849): the
    root-plus-home bytes, keyed by id. An entry that predates
    the per-row fields reads as nothing, so the standing figures
    keep until a fuller report lands."""
    costs: dict[str, int] = {}
    for ws in report.get("workspaces") or []:
        cost = cost_of(ws)
        if cost is not None and "id" in ws:
            costs[ws["id"]] = cost
    return costs


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

    opened = QtCore.Signal(str)
    shell_requested = QtCore.Signal(str)

    def __init__(self, bridge: Bridge) -> None:
        super().__init__()
        self.bridge = bridge
        self.pending = False
        self.flash_seq = 0
        bridge.succeeded.connect(self.on_succeeded)
        bridge.failed.connect(self.on_failed)
        self.table = QtWidgets.QTableWidget(0, len(COLUMNS) + 1, self)
        self.table.setHorizontalHeaderLabels((*COLUMNS, ""))
        self.table.verticalHeader().setVisible(False)
        # The standing sort (#939): CREATED newest-first, the
        # TUI's own default (#854, #470 L1) — the order the
        # window opens in, the first arrow on the header, and
        # the state a new session starts in. A header click
        # moves it; the fill's re-enable re-applies whatever the
        # indicator holds across refreshes.
        self.table.horizontalHeader().setSortIndicator(
            CREATED_COLUMN, QtCore.Qt.SortOrder.DescendingOrder
        )
        self.table.setSortingEnabled(True)
        # A click takes the whole row (#939): no cell edits, so
        # the unit the operator selects is the workspace — the
        # row highlight a listing of records reads by.
        self.table.setSelectionBehavior(
            QtWidgets.QAbstractItemView.SelectionBehavior.SelectRows
        )
        # No vertical dividers between cells (#939): the columns
        # read by their headers and alignment alone, and the row
        # — the record — reads by its own bottom rule.
        # ``showGrid`` draws both directions, so it goes off and
        # the rule below draws just the horizontal one, in the
        # palette's own mid tone.
        self.table.setShowGrid(False)
        rule = self.palette().color(QtGui.QPalette.ColorRole.Mid).name()
        self.table.setStyleSheet(
            f"QTableView::item {{ border-bottom: 1px solid {rule}; }}"
        )
        # The hold keeps its color when the grid loses the focus
        # (#939): Qt paints a selected row in the palette's
        # inactive highlight while another widget holds the
        # focus — a washed-out row reads as dehighlighted — so
        # the table's inactive colors copy its active ones.
        palette = self.table.palette()
        active = QtGui.QPalette.ColorGroup.Active
        inactive = QtGui.QPalette.ColorGroup.Inactive
        for role in (
            QtGui.QPalette.ColorRole.Highlight,
            QtGui.QPalette.ColorRole.HighlightedText,
        ):
            palette.setColor(inactive, role, palette.color(active, role))
        self.table.setPalette(palette)
        # A press on the grid's blank space keeps the hold too
        # (#939): Qt's own view clears a selection when a press
        # lands on no row, and the blank space below the rows is
        # chrome, not a select-nothing gesture.
        self.table.viewport().installEventFilter(self)
        # The action header sorts nothing (#972): a press on the
        # row-actions column's header never reaches the view,
        # so its click moves no ordering — the sortable columns
        # stand as they are.
        self.table.horizontalHeader().installEventFilter(self)
        # The row actions (#972): the actions a row carries in
        # its own cell — the toggle and the shell button, one
        # flight at a time across them. The flight remembers its
        # target — the flash names it whatever the listing does
        # meanwhile.
        self.action_pending = False
        self.action_flight: tuple[str, str] | None = None
        self.row_actions: dict[str, QtWidgets.QToolButton] = {}
        # A right-click on a row reaches the same actions (#972)
        # and Delete behind its own confirmation.
        self.table.setContextMenuPolicy(
            QtCore.Qt.ContextMenuPolicy.CustomContextMenu
        )
        self.table.customContextMenuRequested.connect(self.menu_requested)
        self.table.selectionModel().selectionChanged.connect(self.sync_actions)
        # A row's activation opens the workspace (#945): the
        # keypress or double-click the platform serves lands on
        # the row the index names.
        self.table.activated.connect(self.open_current)
        self.status = QtWidgets.QLabel("", self)
        self.flash_outage = False  # the standing flash's kind
        # The detail strip (#849's shape, #940's GUI read): the
        # held row's quick facts on one line at the listing's
        # bottom. Every fact but storage reads the row models
        # the fill keeps; storage reads the cached capacity
        # report, fetched beside the rows.
        self.rows_by_id: dict[str, WorkspaceRow] = {}
        self.storage_used: dict[str, int] = {}
        self.storage_pending = False
        self.detail = QtWidgets.QLabel("", self)
        self.detail.setTextInteractionFlags(
            QtCore.Qt.TextInteractionFlag.TextSelectableByMouse
        )
        # A long row clips in place: the strip's width follows
        # the window's, never the text's — a wide digest cannot
        # widen the window under the listing.
        self.detail.setSizePolicy(
            QtWidgets.QSizePolicy.Policy.Ignored,
            QtWidgets.QSizePolicy.Policy.Preferred,
        )
        self.sync_actions()
        self.timer = QtCore.QTimer(self)
        self.timer.setInterval(REFRESH_MS)
        self.timer.timeout.connect(self.refresh)
        # The status line alone rides the top (#972): the row
        # cells carry the actions now, and the rows reclaim the
        # vertical space the toolbar held.
        top = QtWidgets.QHBoxLayout()
        top.addWidget(self.status, stretch=1)
        layout = QtWidgets.QVBoxLayout(self)
        layout.addLayout(top)
        layout.addWidget(self.table)
        layout.addWidget(self.detail)
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
        follows its row wherever the order lands it; the first
        rows a listing ever shows hold the first row. Sorting
        pauses for the fill and resumes after, so the rebuild
        cannot re-order rows mid-swap."""
        # The model map and the strip refresh on every answer
        # (#940): the unchanged-listing skip spares the table's
        # rebuild, not the strip's facts — a change the five
        # displayed cells cannot show (a sizes edit's pending
        # count, a cpu change) still reads on the next line.
        self.rows_by_id = {model.id: model for model in models}
        self.update_detail()
        if self.rows_unchanged(models):
            return
        was_empty = self.table.rowCount() == 0
        held = self.held_rows()
        self.table.setSortingEnabled(False)
        self.table.setRowCount(len(models))
        self.row_actions.clear()
        for index, model in enumerate(models):
            self.fill_row(index, model)
        self.table.setSortingEnabled(True)
        self.size_columns()
        self.settle_hold(was_empty, held)

    def settle_hold(
        self, was_empty: bool, held: tuple[set[str], tuple | None]
    ) -> None:
        """The hold after a rebuild (#939): the first rows a
        listing ever shows hold the first row — the standing
        sort puts the newest workspace there, the one a create
        just made, so the keyboard and the eye start somewhere.
        Later answers carry the operator's hold by workspace id
        instead, moving nothing already held."""
        if was_empty and self.table.rowCount() > 0:
            first = self.table.item(0, NAME_COLUMN).data(ROW_ID_ROLE)
            self.restore_rows(({first}, (first, NAME_COLUMN)))
        else:
            self.restore_rows(held)

    def fill_row(self, index: int, model: WorkspaceRow) -> None:
        """One row's five cells (#939, #940): read-only items,
        the workspace's id on the name cell for the hold, the
        status and created cells carrying their sort data."""
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
            index, CREATED_COLUMN, read_only_item(CreatedItem(model))
        )
        self.fill_actions(index, model)

    def fill_actions(self, index: int, model: WorkspaceRow) -> None:
        """One row's action cell (#972): the start/stop toggle
        reading the model's own action — the play gesture while
        the workspace stands stopped or only created, the stop
        sign otherwise — with the shell button beside it. The
        clicks act on this row's workspace, not the selection.
        """
        toggle = QtWidgets.QToolButton(self.table)
        toggle.setIcon(self.style().standardIcon(TOGGLE_ICONS[model.action]))
        toggle.setToolTip(model.action.capitalize())
        toggle.clicked.connect(
            lambda: self.toggle_workspace((model.id, model.label))
        )
        shell = QtWidgets.QToolButton(self.table)
        shell.setIcon(
            self.style().standardIcon(
                QtWidgets.QStyle.StandardPixmap.SP_ComputerIcon
            )
        )
        shell.setToolTip("Open a shell")
        shell.clicked.connect(lambda: self.shell_requested.emit(model.id))
        cell = QtWidgets.QWidget(self.table)
        row = QtWidgets.QHBoxLayout(cell)
        row.setContentsMargins(0, 0, 0, 0)
        row.addWidget(toggle)
        row.addWidget(shell)
        self.table.setCellWidget(index, ACTION_COLUMN, cell)
        self.row_actions[model.id] = toggle

    def rows_unchanged(self, models: list[WorkspaceRow]) -> bool:
        """Whether the table already reads exactly as the models
        do (#939): same rows, whatever order each side sits in —
        the standing sort orders one set of rows the one
        deterministic way, so equal sets need no rebuild — with
        the image read by its whole value, an elided display and
        all. An unchanged answer this way rebuilds nothing."""
        if self.table.rowCount() != len(models):
            return False
        shown = self.displayed_texts()
        return shown is not None and shown == self.model_texts(models)

    def displayed_texts(self) -> list[tuple[str, ...]] | None:
        """The table's rows as comparable tuples (#939) — the
        image by its whole value — sorted, or ``None`` when any
        cell stands missing."""
        texts = []
        for row in range(self.table.rowCount()):
            items = [
                self.table.item(row, column) for column in range(len(COLUMNS))
            ]
            if any(item is None for item in items):
                return None
            texts.append(
                (
                    items[NAME_COLUMN].text(),
                    items[STATUS_COLUMN].text(),
                    items[ID_COLUMN].text(),
                    items[IMAGE_COLUMN].data(IMAGE_ROLE),
                    items[CREATED_COLUMN].text(),
                )
            )
        return sorted(texts)

    def model_texts(self, models: list[WorkspaceRow]) -> list:
        """The models as the same comparable tuples (#939),
        sorted the same way."""
        return sorted(
            (
                model.label,
                state_text(model),
                model.id,
                model.image,
                model.created,
            )
            for model in models
        )

    def held_rows(self) -> tuple[set[str], tuple[str, int] | None]:
        """What the operator holds on the table (#939): the rows
        selected — as their workspaces' ids — and the current
        cell, the form that survives a rebuild. A row with no
        name item holds nothing."""
        held: set[str] = set()
        for index in self.table.selectedIndexes():
            name = self.table.item(index.row(), NAME_COLUMN)
            if name is not None:
                held.add(name.data(ROW_ID_ROLE))
        current = self.table.currentIndex()
        now = None
        if current.isValid():
            name = self.table.item(current.row(), NAME_COLUMN)
            if name is not None:
                now = (name.data(ROW_ID_ROLE), current.column())
        return held, now

    def restore_rows(
        self, held: tuple[set[str], tuple[str, int] | None]
    ) -> None:
        """The held rows back on the rebuilt table (#939): each
        whole row on its workspace's new place, the current cell
        with it. A workspace the new listing dropped simply loses
        its hold."""
        held_rows, now = held
        rows = self.row_ids()
        self.restore_selection(held_rows, rows)
        self.restore_current(now, rows)

    def row_ids(self) -> dict[str, int]:
        """The workspace id per table row (#939): where each
        hold lands after a rebuild."""
        return {
            self.table.item(row, NAME_COLUMN).data(ROW_ID_ROLE): row
            for row in range(self.table.rowCount())
        }

    def restore_selection(self, held: set[str], rows: dict[str, int]) -> None:
        """The held rows back (#939): the whole set at once, each
        row every cell of it, so a hold the new listing dropped
        clears rather than lingers on whatever row landed under
        its slot."""
        selection = QtCore.QItemSelection()
        for workspace in held:
            row = rows.get(workspace)
            if row is not None:
                selection.select(
                    self.table.model().index(row, 0),
                    self.table.model().index(row, ACTION_COLUMN),
                )
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
            return  # pragma: no cover — no current cell to hold:
            # the first-row hold sets one from the first listing
            # that shows rows, and an empty answer skips the
            # rebuild whole, so only a Qt-internal clear (none
            # our UI performs) reaches this arm.
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
        self.size_action_column()
        spare = (
            self.table.viewport().width()
            - sum(
                self.table.columnWidth(column)
                for column in range(len(COLUMNS))
            )
            - self.table.columnWidth(ACTION_COLUMN)
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

    def size_action_column(self) -> None:
        """The action column's width (#972): its buttons' own
        hint — the column neither gives ground on a narrow pane
        nor takes spare on a wide one, so the row's actions stay
        whole whatever the pane's width. A row the rebuild has
        not filled yet measures nothing; an empty table sets no
        width at all."""
        cells = (
            self.table.cellWidget(row, ACTION_COLUMN)
            for row in range(self.table.rowCount())
        )
        width = max(
            (cell.sizeHint().width() for cell in cells if cell is not None),
            default=0,
        )
        if width:
            self.table.setColumnWidth(ACTION_COLUMN, width + CELL_MARGIN)

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
        """One answered call (#939, #940): a listing rebuilds the
        table — the presenter's plain data in — with the status
        line clearing (the flash names a failure the next answer
        resolved); an action's answer lands the flight and
        refreshes the listing, which shows what the action moved.
        Another key's answer is not this screen's to take."""
        if key in (START_KEY, STOP_KEY, DELETE_KEY):
            label = self.action_flight[1] if self.action_flight else ""
            self.action_landed()
            self.flash(action_reply_text(label, result))
            self.refresh()
            return
        if key == STORAGE_KEY:
            self.storage_landed(result)
            return
        if key != LIST_KEY:
            return
        self.listing_landed(result)

    def listing_landed(self, result: object) -> None:
        """One answered listing (#939, #940): the table rebuilds
        from the answer, and a standing outage resolves — the
        daemon answered. A refusal or an action's outcome flash
        runs its own span (#776's mask): a listing answer landing
        inside it cannot wipe the outcome the operator just
        read."""
        self.pending = False
        self.set_rows(workspace_rows(result))
        if self.flash_outage:
            self.clear_flash()
        self.load_storage()

    @QtCore.Slot(str, object)
    def on_failed(self, key: str, failure: RestFailure) -> None:
        """One refused call (#939, #940): the failure flashes on
        the status line — an outage (the daemon answered nothing)
        stands until an answer resolves it — and the rows already
        on screen stand: the operator keeps the list the last
        answer built, and a failed action takes nothing away
        either. Another key's failure is not this screen's to
        show."""
        if key in (START_KEY, STOP_KEY, DELETE_KEY):
            self.action_landed()
            self.flash(str(failure), outage=failure.reach)
            return
        if key == STORAGE_KEY:
            # The TUI strip's own silence (#849): the strip is a
            # detail beside a listing that reports its failures —
            # the standing figures stay, an honest dash until one
            # lands.
            self.storage_pending = False
            return
        if key != LIST_KEY:
            return
        self.pending = False
        self.flash(str(failure), outage=failure.reach)

    @QtCore.Slot(QtCore.QModelIndex)
    def open_current(self, index: QtCore.QModelIndex) -> None:
        """One activated row opened (#945): the workspace the
        index names, by the id its row carries — the page the
        shell swaps to."""
        name = self.table.item(index.row(), NAME_COLUMN)
        if name is not None:
            self.opened.emit(name.data(ROW_ID_ROLE))

    def held_workspace(self) -> tuple[str, str] | None:
        """The workspace the actions sit on (#940): the current
        row's — else the first selected row's — id and label."""
        rows = sorted({index.row() for index in self.table.selectedIndexes()})
        if not rows:
            return None
        row = self.table.currentIndex().row()
        if row not in rows:
            row = rows[0]
        name = self.table.item(row, NAME_COLUMN)
        if name is None:
            return None
        return name.data(ROW_ID_ROLE), name.text()

    @QtCore.Slot()
    def sync_actions(self) -> None:
        """The row toggles' state (#972): one action flight at a
        time — every row's toggle stands disabled while one is
        out. The strip rides the same signal: the hold moved, the
        detail follows."""
        for toggle in self.row_actions.values():
            toggle.setEnabled(not self.action_pending)
        self.update_detail()

    def update_detail(self) -> None:
        """The detail strip's content (#849, the GUI's read):
        the held row's one line — nothing held, the strip
        blanks."""
        held = self.held_workspace()
        model = self.rows_by_id.get(held[0]) if held else None
        if model is None:
            self.detail.setText("")
            return
        self.detail.setText(
            detail_text(model, self.storage_used.get(model.id))
        )

    def load_storage(self) -> None:
        """The strip's storage cache refresh (#849): one flight
        out beside the rows, cached between listings — the
        listing's repaint never waits on the disk walk behind
        the capacity report."""
        if self.storage_pending:
            return
        self.storage_pending = True
        self.bridge.call(STORAGE_KEY, "storage")

    def storage_landed(self, report: object) -> None:
        """The capacity report into the strip's cache (#849):
        the per-workspace root-plus-home cost, keyed by id. A
        report that predates the per-row fields keeps the
        standing figures."""
        self.storage_pending = False
        self.storage_used = workspace_costs(report or {})
        self.update_detail()

    def action_landed(self) -> None:
        """An action flight's end (#940): the buttons take clicks
        again and the flight's target clears."""
        self.action_pending = False
        self.action_flight = None
        self.sync_actions()

    def toggle_workspace(self, workspace: tuple[str, str]) -> None:
        """One row's toggle click (#972): the model's own action
        picks the verb — start reads no confirmation, stop asks
        first — and the flight guards the whole table, exactly
        the selection actions' rule (#940)."""
        model = self.rows_by_id.get(workspace[0])
        if model is None or self.action_pending:
            return
        if model.action == "start":
            self.start_workspace(workspace)
        else:
            self.stop_workspace(workspace)

    def start_workspace(self, workspace: tuple[str, str]) -> None:
        """The start action (#940, #972): the workspace starts
        through the bridge, and the list refreshes when the
        daemon answers."""
        if self.action_pending:
            return
        self.action_pending = True
        self.action_flight = workspace
        self.sync_actions()
        self.bridge.call(START_KEY, "start", workspace[0])

    def stop_workspace(self, workspace: tuple[str, str]) -> None:
        """The stop action (#940, #972): the workspace stops
        after the operator confirms — a refusal sends nothing to
        the daemon."""
        if self.action_pending:
            return
        if not self.confirm_stop(workspace[1]):
            return
        self.action_pending = True
        self.action_flight = workspace
        self.sync_actions()
        self.bridge.call(STOP_KEY, "stop", workspace[0])

    def delete_workspace(self, workspace: tuple[str, str]) -> None:
        """The Delete action (#972): the workspace removes after
        the operator confirms, its data with it — a refusal sends
        nothing to the daemon."""
        if self.action_pending:
            return
        if not self.confirm_delete(workspace[1]):
            return
        self.action_pending = True
        self.action_flight = workspace
        self.sync_actions()
        self.bridge.call(DELETE_KEY, "remove", workspace[0])

    def confirm_stop(self, label: str) -> bool:
        """Stop's confirmation (#940): the question names the
        workspace, and No is the default — a stop is the
        operator's call, not the pointer's."""
        answer = QtWidgets.QMessageBox.question(
            self,
            "Stop workspace",
            f"Stop {label}?",
            (
                QtWidgets.QMessageBox.StandardButton.Yes
                | QtWidgets.QMessageBox.StandardButton.No
            ),
            QtWidgets.QMessageBox.StandardButton.No,
        )
        return answer == QtWidgets.QMessageBox.StandardButton.Yes

    def confirm_delete(self, label: str) -> bool:
        """Delete's confirmation (#972): the question names the
        workspace and its cost — the data goes with it — and No
        is the default, the stop question's own rule."""
        answer = QtWidgets.QMessageBox.question(
            self,
            "Delete workspace",
            f"Delete {label}? Its data goes with it.",
            (
                QtWidgets.QMessageBox.StandardButton.Yes
                | QtWidgets.QMessageBox.StandardButton.No
            ),
            QtWidgets.QMessageBox.StandardButton.No,
        )
        return answer == QtWidgets.QMessageBox.StandardButton.Yes

    @QtCore.Slot(QtCore.QPoint)
    def menu_requested(self, pos: QtCore.QPoint) -> None:
        """A right-click on a row opens its menu (#972): the same
        actions the row's buttons carry — start or stop per the
        model's own action, a shell — and Delete behind its own
        confirmation. A press on no row opens nothing."""
        menu = self.row_menu(self.table.rowAt(pos.y()))
        if menu is not None:
            menu.popup(self.table.viewport().mapToGlobal(pos))

    def row_menu(self, row: int) -> QtWidgets.QMenu | None:
        """The row's menu (#972) — ``None`` for a row the table
        does not hold."""
        name = self.table.item(row, NAME_COLUMN)
        if name is None:
            return None
        workspace = (name.data(ROW_ID_ROLE), name.text())
        model = self.rows_by_id.get(workspace[0])
        verb = model.action if model is not None else "start"
        menu = QtWidgets.QMenu(self.table)
        menu.addAction(
            verb.capitalize(),
            lambda: self.toggle_workspace(workspace),
        )
        menu.addAction(
            "Open a shell",
            lambda: self.shell_requested.emit(workspace[0]),
        )
        menu.addAction("Delete", lambda: self.delete_workspace(workspace))
        return menu

    def flash(self, message: str, *, outage: bool = False) -> None:
        """The status line's flash (#939, #904): the message
        shows; a refusal clears itself after the flash span, an
        outage stands for the next answer to resolve. Each flash
        carries its own serial, so a later flash's message is not
        erased by an earlier flash's timeout."""
        self.flash_outage = outage
        self.flash_seq += 1
        self.status.setText(message)
        if outage:
            return
        seq = self.flash_seq
        # The screen is the timeout's receiver (#959): the
        # pending callback is severed when the widget's C++
        # object goes, instead of firing later on a wrapper
        # whose widgets are already destroyed.
        QtCore.QTimer.singleShot(FLASH_MS, self, lambda: self.clear_stale(seq))

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
        self.flash_outage = False
        self.status.clear()

    def showEvent(self, event: QtGui.QShowEvent) -> None:
        """The screen's first refresh at open (#939), and the
        tick's resume on a re-show: the screen refreshes while it
        owns the window. A wrapper without its ``timer`` does
        neither (#959): Qt can deliver events to a screen whose
        construction never finished, and to the empty wrapper
        PySide6 raises at teardown for a C++ object that
        outlived its Python one — neither owns the tick the
        open starts."""
        timer = getattr(self, "timer", None)
        if timer is not None:
            self.refresh()
            timer.start()
        super().showEvent(event)

    def hideEvent(self, event: QtGui.QHideEvent) -> None:
        """The tick stops with the screen (#939): a closed
        window hides its children, and a timer tick after close
        must not touch widgets. A wrapper without its ``timer``
        (#959) has no tick to stop — showEvent names the two
        shapes that arrive here half-built."""
        timer = getattr(self, "timer", None)
        if timer is not None:
            timer.stop()
        super().hideEvent(event)

    def eventFilter(
        self, watched: QtCore.QObject, event: QtCore.QEvent
    ) -> bool:
        """The screen's two filtered gestures (#939, #972): a
        press on the grid's blank space below the rows, and a
        press on the action column's header. A wrapper without
        its ``table`` (#959) filters nothing: the event goes to
        Qt's own handling."""
        table = getattr(self, "table", None)
        if table is None:
            return super().eventFilter(watched, event)
        if self.header_press(watched, event):
            return True
        if self.blank_press(watched, event):
            return True
        return super().eventFilter(watched, event)

    def blank_press(self, watched: QtCore.QObject, event) -> bool:
        """Whether this event is a press on the grid's blank
        space (#939): the hold stands until a row click, a
        refresh's rebuild, or the workspace leaving the listing
        moves it."""
        return (
            watched is self.table.viewport()
            and event.type() == QtCore.QEvent.Type.MouseButtonPress
            and not self.table.indexAt(event.position().toPoint()).isValid()
        )

    def header_press(self, watched: QtCore.QObject, event) -> bool:
        """Whether this event is a press on the action column's
        header (#972): the one header a click must not sort — the
        press never reaches the view, so the standing sort stands.
        """
        header = self.table.horizontalHeader()
        return (
            watched is header
            and event.type() == QtCore.QEvent.Type.MouseButtonPress
            and header.logicalIndexAt(event.position().toPoint())
            == ACTION_COLUMN
        )

    def resizeEvent(self, event: QtGui.QResizeEvent) -> None:
        """A resized pane re-flows the columns (#939): the
        minimums follow the contents whatever the pane's width,
        and the spare fills the pane's new width. A wrapper
        without its ``table`` (#959) sizes nothing."""
        if getattr(self, "table", None) is not None:
            self.size_columns()
        super().resizeEvent(event)
