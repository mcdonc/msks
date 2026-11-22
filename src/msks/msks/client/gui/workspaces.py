"""The workspace list screen (phase 3 of #924, #939): the thin
widget that renders the presenter's row models in the main
window — the TUI listing's five fields (NAME, STATUS with the
unresolved-mounts and pending-changes markers, ID, IMAGE,
CREATED) and the row-actions column beside them. The screen
holds no business logic: daemon rows arrive as row models
(:mod:`msks.client.gui.rows`), the bridge
(:mod:`msks.client.gui.bridge`) carries every call, and a
refresh failure flashes the status line while the rows already
on screen stand. The row's menu (#972) opens the editor
(:mod:`msks.client.gui.edit`, #986) beside its other actions,
and the page's bottom button row (#1011) — Add, Edit, Clone,
Remove, the detail pages' Mounts and Proxies pattern — carries
the create ask (the shell owns the dialog), the selected row's
edit, clone (#1063, the name prompt the daemon's route needs),
and delete.

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
from .connectionbar import ConnectionButton
from .edit import EditDialog
from .forms import confirm, prompt_text
from .listcells import (
    ACTION_COLUMN,
    COLUMNS,
    CREATED_COLUMN,
    ID_COLUMN,
    IMAGE_COLUMN,
    IMAGE_ROLE,
    NAME_COLUMN,
    ROW_ID_ROLE,
    STATUS_COLUMN,
    TOGGLE_ICONS,
    CreatedItem,
    ImageItem,
    StateItem,
    action_reply_text,
    clone_reply_text,
    read_only_item,
    state_text,
)
from .listlayout import size_columns
from .listtable import ListTable
from .rows import (
    WorkspaceRow,
    detail_text,
    workspace_costs,
    workspace_rows,
)

#: The bridge call's key for a listing refresh (#939): the
#: screen answers only its own key, whatever else shares the
#: bridge.
LIST_KEY = "workspaces-list"

#: The action calls' keys (#940, #972, #1063): start, stop,
#: remove, and clone on a row, each its own flight.
START_KEY = "workspaces-start"
STOP_KEY = "workspaces-stop"
DELETE_KEY = "workspaces-delete"
CLONE_KEY = "workspaces-clone"

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


def action_reply(key: str, label: str, reply: object) -> str:
    """The action's flash text (#940, #1063): the clone names the
    new row it made — a different workspace than the one the
    operator clicked — while every other action names its own
    workspace beside the status the daemon's answer carries."""
    if key == CLONE_KEY:
        return clone_reply_text(reply)
    return action_reply_text(label, reply)


class WorkspaceList(QtWidgets.QWidget):
    """The main window's central screen (#939): the table of row
    models, the Add / Edit / Clone / Remove button row at the
    page's bottom (#1011, the Mounts and Proxies pattern), and the
    status line a failed refresh flashes. The status row carries the
    connection picker at its right end (#1009, phase 5): the
    standing daemon's name beside a menu of the client's
    presets, a pick crossing as ``connection_picked`` for the
    window path to re-dial. Every daemon exchange walks the
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
    connection_picked = QtCore.Signal(object)
    create_requested = QtCore.Signal()

    def __init__(self, bridge: Bridge) -> None:
        super().__init__()
        self.bridge = bridge
        self.pending = False
        self.flash_seq = 0
        bridge.succeeded.connect(self.on_succeeded)
        bridge.failed.connect(self.on_failed)
        self.table = ListTable(0, len(COLUMNS) + 1, self)
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
        # The action header sorts nothing (#972): a press on
        # the row-actions column's header never reaches the
        # view, so its click moves no ordering — the sortable
        # columns stand as they are. The filter watches the
        # header's viewport, the surface real clicks land on
        # (the header's own object filter never sees them).
        self.table.horizontalHeader().viewport().installEventFilter(self)
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
        self.daemon_rows: dict[str, dict] = {}
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
        # The page's action row (#1011): Add, Edit, Clone, and
        # Remove at the bottom of the page, the detail pages'
        # Mounts and Proxies pattern — the create ask crosses to
        # the shell that owns the dialog, Edit and Remove acting
        # on the selected row, Clone asking its name first
        # (#1063). Built before the first sync: the sync's slot
        # reads the buttons.
        self.add_button = QtWidgets.QPushButton("Add")
        self.add_button.clicked.connect(self.request_create)
        self.edit_button = QtWidgets.QPushButton("Edit")
        self.edit_button.clicked.connect(self.edit_held)
        self.edit_button.setEnabled(False)
        self.clone_button = QtWidgets.QPushButton("Clone")
        self.clone_button.clicked.connect(self.clone_held)
        self.clone_button.setEnabled(False)
        self.remove_button = QtWidgets.QPushButton("Remove")
        self.remove_button.clicked.connect(self.remove_held)
        self.remove_button.setEnabled(False)
        self.sync_actions()
        # The status line alone rides the top (#972): the row
        # cells carry the start/stop and shell actions, the
        # page's own actions the bottom row carries, and the
        # rows reclaim the vertical space a toolbar held.
        # The connection picker rides the status row's right
        # end (#1009, phase 5): the standing connection's name
        # beside a menu of the client's presets — the config
        # file's aliases, the environment, the dev daemon's
        # state dir — and a pick crosses as
        # ``connection_picked(preset)`` for the window path to
        # re-dial. The button is the row's own child, its menu
        # dropping below it.
        self.connection = ConnectionButton(self)
        self.connection.picked.connect(self.connection_picked)
        top = QtWidgets.QHBoxLayout()
        top.addWidget(self.status, stretch=1)
        top.addWidget(self.connection)
        actions = QtWidgets.QHBoxLayout()
        actions.addWidget(self.add_button)
        actions.addWidget(self.edit_button)
        actions.addWidget(self.clone_button)
        actions.addWidget(self.remove_button)
        actions.addStretch(1)
        layout = QtWidgets.QVBoxLayout(self)
        layout.addLayout(top)
        layout.addWidget(self.table, stretch=1)
        layout.addLayout(actions)
        layout.addWidget(self.detail)
        self.setLayout(layout)

    def host_presets(self, presets: list) -> None:
        """The picker's menu from the presenter's rows (#1009),
        delegated to the button the status row carries."""
        self.connection.host(presets)

    @QtCore.Slot()
    def refresh(self) -> None:
        """One listing refresh through the bridge (#939, #1019)
        — the screen's own open, a listing-affecting frame the
        events feed pushed, and the feed's serve poke share it.
        A refresh already in flight is skipped, exactly the TUI
        listing's own guard (#470): pokes that outlive a slow
        call would stack identical requests while the first
        answer has yet to land. The rows on screen stand until
        the answer replaces them."""
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
        size_columns(self.table)
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
        # A rebuild under a standing flight keeps the gate (#972):
        # the cells are new, the one-flight rule is not.
        toggle.setEnabled(not self.action_pending)
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

    @QtCore.Slot(str, object)
    def on_succeeded(self, key: str, result: object) -> None:
        """One answered call (#939, #940): a listing rebuilds the
        table — the presenter's plain data in — with the status
        line clearing (the flash names a failure the next answer
        resolved); an action's answer lands the flight and
        refreshes the listing, which shows what the action moved.
        Another key's answer is not this screen's to take."""
        if key in (START_KEY, STOP_KEY, DELETE_KEY, CLONE_KEY):
            label = self.action_flight[1] if self.action_flight else ""
            self.action_landed()
            self.flash(action_reply(key, label, result))
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
        # The daemon's own rows stand beside the models (#986):
        # the editor seeds from the row's whole facts — the
        # sizes the model carries no cell for — so the last
        # answer's rows stay keyed by id beside their models.
        self.daemon_rows = {row.get("id"): row for row in result or []}
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
        if key in (START_KEY, STOP_KEY, DELETE_KEY, CLONE_KEY):
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
        """The workspace the detail strip reads (#940, #972):
        the current row's — else the first selected row's — id
        and label."""
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
        detail follows. The page's own row (#1011) rides it too:
        Edit, Clone, and Remove take a held row besides the
        flight's gate."""
        for toggle in self.row_actions.values():
            toggle.setEnabled(not self.action_pending)
        held = self.held_workspace() is not None
        self.edit_button.setEnabled(held and not self.action_pending)
        self.clone_button.setEnabled(held and not self.action_pending)
        self.remove_button.setEnabled(held and not self.action_pending)
        self.update_detail()

    @QtCore.Slot()
    def request_create(self) -> None:
        """The Add action (#1011): the create dialog's ask crosses
        to the shell that owns the bridge — the same dialog the
        tab strip's plus badge once opened (#962), the dialog's
        own landed create refreshing this listing behind it."""
        self.create_requested.emit()

    @QtCore.Slot()
    def edit_held(self) -> None:
        """The page's Edit action (#1011): the held row opens the
        editor — the same path the row's context menu takes —
        with nothing held deciding nothing."""
        held = self.held_workspace()
        if held is not None:
            self.edit_workspace(held)

    @QtCore.Slot()
    def remove_held(self) -> None:
        """The page's Remove action (#1011): the held row removes
        behind the Delete confirmation the context menu keeps,
        with nothing held deciding nothing."""
        held = self.held_workspace()
        if held is not None:
            self.delete_workspace(held)

    @QtCore.Slot()
    def clone_held(self) -> None:
        """The page's Clone action (#1063): the held row opens the
        name prompt — the same path the row's context menu takes
        — with nothing held deciding nothing."""
        held = self.held_workspace()
        if held is not None:
            self.clone_workspace(held)

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

    def clone_workspace(self, workspace: tuple[str, str]) -> None:
        """The Clone action (#1063, the daemon's #773 route): the
        name prompt first — a clone needs a name the daemon's
        grammar takes, and the daemon owns the stopped-source
        rule — and a submitted name clones the workspace through
        the bridge, one action flight out like the others. A
        cancelled or empty prompt sends nothing."""
        if self.action_pending:
            return
        name = prompt_text(
            self, "Clone workspace", f"Clone {workspace[1]} into:"
        )
        if not name:
            return
        self.action_pending = True
        self.action_flight = workspace
        self.sync_actions()
        self.bridge.call(CLONE_KEY, "clone", workspace[0], name)

    def edit_workspace(self, workspace: tuple[str, str]) -> None:
        """The Edit action (#986): the editor opens over the list
        seeded from the row's own facts — the create dialog's
        shape, the sizes and topology editable — and a landed
        resize closes it and refreshes the listing behind it,
        which shows the pending marker the row grows. A row the
        last answer dropped names itself on the status line —
        the editor seeds from the row's whole facts or not at
        all."""
        row = self.daemon_rows.get(workspace[0])
        if row is None:
            self.flash(
                f"{workspace[1]} left the listing — refresh and retry the edit"
            )
            return
        dialog = EditDialog(self.bridge, row, self)
        dialog.accepted.connect(self.refresh)
        dialog.exec()

    def confirm_stop(self, label: str) -> bool:
        """Stop's confirmation (#940, #1014): the question names
        the workspace — No the default, both centered on the
        main window — a stop is the operator's call, not the
        pointer's."""
        return confirm(self, "Stop workspace", f"Stop {label}?")

    def confirm_delete(self, label: str) -> bool:
        """Delete's confirmation (#972, #1014): the question
        names the workspace and its cost — the data goes with
        it — No the default, both centered on the main window,
        the stop question's own rule."""
        return confirm(
            self, "Delete workspace", f"Delete {label}? Its data goes with it."
        )

    @QtCore.Slot(QtCore.QPoint)
    def menu_requested(self, pos: QtCore.QPoint) -> None:
        """A right-click on a row opens its menu (#972): the same
        actions the row's buttons carry — start or stop per the
        model's own action, a shell — beside Clone behind its
        name prompt (#1063) and Delete behind its own
        confirmation. A press on no row opens nothing."""
        menu = self.row_menu(self.table.rowAt(pos.y()))
        if menu is not None:
            menu.popup(self.table.viewport().mapToGlobal(pos))

    def row_menu(self, row: int) -> QtWidgets.QMenu | None:
        """The row's menu (#972, #986, #1063) — ``None`` for a row
        the table does not hold. Edit joins the row's own verb,
        the shell, Clone behind its name prompt, and Delete: the
        editor opens for the workspace's sizes and topology, the
        rename field waiting on the daemon's route."""
        name = self.table.item(row, NAME_COLUMN)
        if name is None:
            return None
        workspace = (name.data(ROW_ID_ROLE), name.text())
        model = self.rows_by_id.get(workspace[0])
        verb = model.action if model is not None else "start"
        menu = QtWidgets.QMenu(self.table)
        menu.addAction("Edit", lambda: self.edit_workspace(workspace))
        menu.addAction(
            verb.capitalize(),
            lambda: self.toggle_workspace(workspace),
        )
        menu.addAction(
            "Open a shell",
            lambda: self.shell_requested.emit(workspace[0]),
        )
        menu.addAction("Clone", lambda: self.clone_workspace(workspace))
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
        """The screen's refresh at open (#939, #1019): the
        standing tick is gone — the listing re-reads on the
        listing-affecting frames the events feed pushes
        (:meth:`refresh` and the feed's serve pokes below) —
        and a re-show refreshes, the screen's own answer for
        the frames that landed while it was hidden. A wrapper
        without its ``status`` (#959) refreshes nothing: Qt
        can deliver events to a screen whose construction
        never finished, and to the empty wrapper PySide6
        raises at teardown for a C++ object that outlived its
        Python one."""
        if getattr(self, "status", None) is not None:
            self.refresh()
        super().showEvent(event)

    def eventFilter(
        self, watched: QtCore.QObject, event: QtCore.QEvent
    ) -> bool:
        """The screen's three filtered surfaces (#939, #972,
        #964): a press on the grid's blank space below the rows,
        a press on the action column's header, and the pane's
        own viewport resize — the columns re-flowing against the
        width the pane actually took. A wrapper without its
        ``table`` (#959) filters nothing: the event goes to
        Qt's own handling."""
        table = getattr(self, "table", None)
        if table is None:
            return super().eventFilter(watched, event)
        self.pane_resized(watched, event)
        if self.header_press(watched, event):
            return True
        if self.blank_press(watched, event):
            return True
        return super().eventFilter(watched, event)

    def pane_resized(self, watched: QtCore.QObject, event) -> None:
        """The pane's own viewport resize (#964): the columns
        re-flow against the width the pane actually took — the
        screen's own resize event reads the viewport one layout
        pass behind, and every widen left its last delta as
        blank space beside the rows, a maximized window most of
        all. The event still reaches Qt: a viewport's resize is
        Qt's to finish."""
        if watched is self.table.viewport() and (
            event.type() == QtCore.QEvent.Type.Resize
        ):
            size_columns(self.table)

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
            watched is header.viewport()
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
            size_columns(self.table)
        super().resizeEvent(event)
