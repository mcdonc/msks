"""The images tab (phase 4.6 of #924, #964): the thin widget
that renders the presenter's row models
(:mod:`msks.client.gui.imagerows`) as the TUI's image page's
counterpart — two labeled groups, *In the catalog* (the
daemon's rows, the default marker, each import's date) and
*Published* (the channel manifest's rows with their
descriptions, sizes, build dates, and membership markers). The
screen holds no business logic: the tab's one refresh walks the
bridge to the gathered read (:meth:`msks.client.gui.data.
GuiData.images_page`), and the answers rebuild the groups from
the plain models. An import on a published row holds the wait
behind a progress dialog — the daemon downloads, verifies, and
floor-checks, and the catalog refreshes when the import lands;
a catalog row opens its detail (the shared ``image info``
renderer's own lines); remove asks for confirmation, the same
DELETE ``image rm`` makes; the default-image choice posts the
designation. Refusals and failures flash the status line, the
listing's own contract (#904).

This module imports PySide6 at module scope — the same contract
as the screens' modules: the package's init imports none of it
at parse time, and ``.app``'s window path loads it after its
guards.
"""

import time

from PySide6 import QtCore, QtGui, QtWidgets

from ...data import RestFailure
from ...images import info_lines
from ..bridge import Bridge
from ..dialogs import Centered, center_on_parent
from ..forms import confirm
from ..imagerows import (
    IMPORTED_MARKER,
    CatalogRow,
    PublishedRow,
    catalog_rows,
    published_rows,
)
from ..listtable import ListTable, bold_header, resized_viewport
from . import layout
from .cells import (
    CATALOG_COLUMNS,
    CATALOG_GROUP,
    PUBLISHED_COLUMNS,
    PUBLISHED_GROUP,
    ROW_INDEX_ROLE,
    ROW_KEY_ROLE,
    built_rank,
    counts_text,
    default_cell,
    imported_rank,
    imported_text,
    importing_label,
    published_cells,
    ranked_item,
    read_only_item,
    size_rank,
)

#: The tab's gathered refresh key (#964): the open's first read
#: and the manual refresh's — one flight, both groups.
PAGE_KEY = "images-page"

#: The catalog-only refresh key (#964): the read an action's
#: landing takes — the channel's rows did not change, the
#: TUI's own rule (#565).
CATALOG_KEY = "images-catalog"

#: The import flight's key (#964).
IMPORT_KEY = "images-import"

#: The remove and default flights' keys (#964).
REMOVE_KEY = "images-remove"
DEFAULT_KEY = "images-default"

#: How long a flashed refusal stays on the status line,
#: milliseconds — the listing's own span (#939). An outage (a
#: fetch that answered nothing) never times out: the line holds
#: its message until an answer resolves it (#904).
FLASH_MS = 4000

#: The import dialog's elapsed tick (#964), milliseconds — the
#: TUI importing modal's own cadence: the wait shows it moving.
IMPORT_TICK_MS = 1000


def action_reply_text(key: str, label: str) -> str:
    """The action's own flash (#964): the remove's plain past
    tense beside the image's ref, the default's own designation
    line — the workspaces screen's outcome shape."""
    if key == REMOVE_KEY:
        return f"deleted {label}"
    return f"designated {label} the default image"


class ImageDetailDialog(Centered, QtWidgets.QDialog):
    """One catalog row's full record (#964): the shared ``image
    info`` renderer's own lines — the boot facts, the baked
    identity, the seed and cmdline, the designation — in a
    read-only block the operator can select and copy. Enter or
    a double-click on a catalog row opens it; the Close button
    and Escape dismiss it. The opener's widget lands as the
    parent (#1014): the record opens centered on the main
    window."""

    def __init__(self, row: dict, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle(f"Image {row['name']}:{row['version']}")
        text = QtWidgets.QLabel("\n".join(info_lines(row)), self)
        text.setTextInteractionFlags(
            QtCore.Qt.TextInteractionFlag.TextSelectableByMouse
        )
        close = QtWidgets.QPushButton("Close", self)
        close.setDefault(True)
        close.clicked.connect(self.accept)
        buttons = QtWidgets.QHBoxLayout()
        buttons.addStretch(1)
        buttons.addWidget(close)
        layout = QtWidgets.QVBoxLayout(self)
        layout.addWidget(text)
        layout.addLayout(buttons)


class ImagesTab(QtWidgets.QWidget):
    """The Images tab of the main window's strip (#964): the two
    labeled groups the presenter's models fill, refreshed whole
    when the tab opens and through the manual action — the
    channel's rows do not move on their own, so no standing tick
    runs. Every daemon exchange walks the bridge; the answers
    arrive on the GUI thread through this widget's slots, and a
    failure flashes the status line while the groups already on
    screen stand.
    """

    def __init__(self, bridge: Bridge, manifest_url: str) -> None:
        super().__init__()
        self.bridge = bridge
        self.url = manifest_url
        self.pending = False
        self.catalog_pending = False
        self.flash_seq = 0
        self.flash_outage = False
        self.action_pending = False
        self.action_flight: str | None = None
        self.progress: QtWidgets.QProgressDialog | None = None
        self.progress_ref = ""
        self.import_started = 0.0
        self.catalog_raw: list[dict] = []
        self.published_raw: list[dict] = []
        self.catalog_models: list[CatalogRow] = []
        self.published_models: list[PublishedRow] = []
        # The catalog's reader sequence (#964): every flight
        # that reads the catalog — the gathered page, the
        # catalog-only refresh — takes the next number at its
        # issue, and an answer applies its catalog half only
        # while its own number is still the last one out: a
        # flight that predates a later reader keeps the fresher
        # rows, whichever kind of flight answered first.
        self.read_seq = 0
        self.page_seq = 0
        self.catalog_seq = 0
        bridge.succeeded.connect(self.on_succeeded)
        bridge.failed.connect(self.on_failed)
        self.catalog_table = ListTable(0, len(CATALOG_COLUMNS), self)
        self.catalog_table.setHorizontalHeaderLabels(CATALOG_COLUMNS)
        self.build_table(self.catalog_table)
        self.catalog_table.activated.connect(self.open_detail_at)
        self.catalog_table.setContextMenuPolicy(
            QtCore.Qt.ContextMenuPolicy.CustomContextMenu
        )
        self.catalog_table.customContextMenuRequested.connect(
            self.menu_requested
        )
        self.published_table = ListTable(0, len(PUBLISHED_COLUMNS), self)
        self.published_table.setHorizontalHeaderLabels(PUBLISHED_COLUMNS)
        self.build_table(self.published_table)
        self.published_table.activated.connect(self.import_at)
        self.published_table.setContextMenuPolicy(
            QtCore.Qt.ContextMenuPolicy.CustomContextMenu
        )
        self.published_table.customContextMenuRequested.connect(
            self.published_menu_requested
        )
        self.status = QtWidgets.QLabel("", self)
        self.refresh_button = QtWidgets.QPushButton("Refresh", self)
        self.refresh_button.clicked.connect(self.refresh)
        # The button's arrow bridges (#962's rule, the page's
        # own): Up from the page's bottom bar enters the groups
        # above, Down hands the keyboard out of the page.
        self.refresh_button.installEventFilter(self)
        self.import_timer = QtCore.QTimer(self)
        self.import_timer.setInterval(IMPORT_TICK_MS)
        self.import_timer.timeout.connect(self.import_tick)
        # The counts line (#964, the operator's ask): the two
        # groups' standing tally beneath the published pane, the
        # workspace list's own bottom strip's style — one
        # `·`-separated line the operator can select and copy
        # whole. A long line clips in place: the strip's width
        # follows the window's, never the text's.
        self.counts = QtWidgets.QLabel("", self)
        self.counts.setTextInteractionFlags(
            QtCore.Qt.TextInteractionFlag.TextSelectableByMouse
        )
        self.counts.setSizePolicy(
            QtWidgets.QSizePolicy.Policy.Ignored,
            QtWidgets.QSizePolicy.Policy.Preferred,
        )
        # The refresh action leads the bar (#964, the operator's
        # ask, the bar at the page's bottom since #1011 — the
        # Mounts and Proxies pattern): the button on the left,
        # the flash line taking the rest.
        bar = QtWidgets.QHBoxLayout()
        bar.addWidget(self.refresh_button)
        bar.addWidget(self.status, stretch=1)
        layout = QtWidgets.QVBoxLayout(self)
        layout.addWidget(self.group(CATALOG_GROUP, self.catalog_table))
        layout.addWidget(self.group(PUBLISHED_GROUP, self.published_table))
        layout.addLayout(bar)
        layout.addWidget(self.counts)
        self.setLayout(layout)

    def group(self, title: str, table: ListTable) -> QtWidgets.QGroupBox:
        """One labeled group's box (#565, the GUI's read): the
        TUI page's own two headings, each over its table."""
        box = QtWidgets.QGroupBox(title, self)
        rows = QtWidgets.QVBoxLayout(box)
        rows.addWidget(table)
        return box

    def build_table(self, table: ListTable) -> None:
        """A group table's standing shape (#964): whole-row
        selection, no cell edits, no vertical grid, and columns
        that size to their contents with the spare stretching
        the last one — the listing's own reads, minus the
        sorting the page's fixed order owns."""
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
        rule = self.palette().color(QtGui.QPalette.ColorRole.Mid).name()
        table.setStyleSheet(
            f"QTableView::item {{ border-bottom: 1px solid {rule}; }}"
        )
        table.verticalHeader().setVisible(False)
        bold_header(table)
        # The pane's own resizes re-flow the columns (#964): a
        # vertical scrollbar's appearance shrinks the viewport
        # after a fill sized against the wider one.
        table.viewport().installEventFilter(self)
        # The header's presses belong to the tab (#964): the
        # sort a click means — a new column ascending, the same
        # column toggled — is the tab's own rule, applied with
        # its arrow; Qt's own first-click order reads against
        # its arrow and is set aside.
        table.horizontalHeader().viewport().installEventFilter(self)
        # The columns sort (#964): a header click orders the
        # rows — the rank cells by their carried ranks — and the
        # sorted-by column carries the arrow. The REF column
        # holds the standing ascending sort, the presenter's own
        # reading order; a fill pauses the sorting for its swap
        # and re-applies whatever the indicator holds.

        table.setSortingEnabled(True)
        table.horizontalHeader().setSortIndicator(
            0, QtCore.Qt.SortOrder.AscendingOrder
        )

    @QtCore.Slot()
    def refresh(self) -> None:
        """The tab's whole refresh (#964): the open's first read
        and the manual action's — both groups from one flight,
        the status line saying the fetch is out. A refresh
        already in flight is skipped, the listing's own guard
        (#470)."""
        if self.pending:
            return
        self.pending = True
        self.flash_seq += 1  # retire any pending flash timeout
        self.status.setText("fetching the catalog and the published manifest…")
        self.read_seq += 1
        self.page_seq = self.read_seq
        self.bridge.call(PAGE_KEY, "images_page", self.url)

    def refresh_catalog(self) -> None:
        """The catalog-only refresh (#964): the read an action's
        landing takes — the channel's rows did not change, and
        the published group's markers move against the new
        catalog alone. The reader sequence moves here, so a page
        flight already out — its catalog read possibly older
        than this one's — cannot land its half over the fresher
        rows, and this one's answer stands down before a later
        page flight's."""
        if self.catalog_pending:
            return
        self.read_seq += 1
        self.catalog_seq = self.read_seq
        self.catalog_pending = True
        self.bridge.call(CATALOG_KEY, "images")

    @QtCore.Slot(str, object)
    def on_succeeded(self, key: str, result: object) -> None:
        """One answered call (#964): the gathered read rebuilds
        both groups, a catalog read rebuilds the catalog group
        and the markers, an import lands its flight behind the
        progress dialog, and the remove and default choices land
        theirs — each with its flash and the catalog refresh
        that shows what moved. Another key's answer is not this
        tab's to take."""
        if key == PAGE_KEY:
            self.page_landed(result)
        elif key == CATALOG_KEY:
            self.catalog_landed(result)
        elif key == IMPORT_KEY:
            self.import_landed(result)
        elif key in (REMOVE_KEY, DEFAULT_KEY):
            self.action_arrived(key)

    def action_arrived(self, key: str) -> None:
        """The remove or default choice's landing (#964): the
        flight's remembered ref flashes its outcome, and the
        catalog refresh shows what moved — the marker that
        traveled, the row that left."""
        label = self.action_flight or ""
        self.action_landed()
        self.flash(action_reply_text(key, label))
        self.refresh_catalog()

    def page_landed(self, result: object) -> None:
        """The gathered answer (#964): both groups rebuild from
        the presenter's models — the published half always, the
        catalog half only when no fresher catalog read landed
        under the flight — the raw rows cached for the detail
        view and the after-action markers."""
        self.pending = False
        page = result or {}
        self.published_raw = list(page.get("published") or [])
        if self.page_seq == self.read_seq:
            self.catalog_raw = list(page.get("catalog") or [])
        self.rebuild()
        self.standing(counts_text(self.catalog_raw, self.published_raw))

    def catalog_landed(self, result: object) -> None:
        """The catalog answer (#964): the catalog group and the
        published markers rebuild against the new rows, and the
        standing counts read the new state — unless a later
        reader went out after this flight, whose fresher rows
        this one must not wipe."""
        self.catalog_pending = False
        if self.catalog_seq == self.read_seq:
            self.catalog_raw = list(result or [])
            self.rebuild()
            self.standing(counts_text(self.catalog_raw, self.published_raw))
            return
        self.rebuild()
        self.standing(counts_text(self.catalog_raw, self.published_raw))

    def rebuild(self) -> None:
        """Both groups from the presenter (#964): the catalog in
        its reading order, the published rows beside it, their
        markers read against the catalog as it stands — the
        whole page rebuilt from plain data, the selection
        carried by row content across the swap, both panes sized
        once after the groups stand (the panes' standing sorts
        order the rows over whatever order the fills wrote)."""
        self.fill_catalog(catalog_rows(self.catalog_raw))
        self.fill_published(
            published_rows(self.published_raw, self.catalog_raw)
        )
        self.size_columns()

    def fill_catalog(self, models: list[CatalogRow]) -> None:
        """The catalog group's rows (#964): one per model, the
        selection carried by the row's own hash across the
        rebuild — a hold follows its build wherever the reading
        order lands it, and a rebuilt archive's twin under the
        same ref never takes another build's hold."""
        held = self.held_first(self.catalog_table)
        was_empty = self.catalog_table.rowCount() == 0
        table = self.catalog_table
        table.setSortingEnabled(False)
        table.setRowCount(len(models))
        for index, model in enumerate(models):
            ref = read_only_item(QtWidgets.QTableWidgetItem(model.ref))
            ref.setData(ROW_INDEX_ROLE, index)
            ref.setData(ROW_KEY_ROLE, model.hash)
            table.setItem(index, 0, ref)
            cells = (
                (default_cell(model), 1 if model.default else 0),
                (model.hash_prefix, None),
                (model.imported, imported_rank(model.imported)),
            )
            for column, (cell, rank) in enumerate(cells, start=1):
                table.setItem(index, column, ranked_item(cell, rank))
        self.catalog_models = models
        table.setSortingEnabled(True)
        self.settle_hold(table, held, was_empty)
        self.size_columns()

    def fill_published(self, models: list[PublishedRow]) -> None:
        """The published group's rows (#964): one per model, the
        selection carried by the row's url — the same settle;
        the pane's standing sort orders the rows, the REF column
        ascending, over whatever order the fill wrote."""
        held = self.held_first(self.published_table)
        was_empty = self.published_table.rowCount() == 0
        table = self.published_table
        table.setSortingEnabled(False)
        table.setRowCount(len(models))
        for index, model in enumerate(models):
            ranks = (
                None,
                None,
                size_rank(model.size),
                None,
                built_rank(model.built),
                None,
            )
            for column, cell in enumerate(published_cells(model)):
                item = ranked_item(cell, ranks[column])
                if column == 0:
                    item.setData(ROW_INDEX_ROLE, index)
                    item.setData(ROW_KEY_ROLE, model.url)
                table.setItem(index, column, item)
        self.published_models = models
        table.setSortingEnabled(True)
        self.settle_hold(table, held, was_empty)
        self.size_columns()

    def size_columns(self) -> None:
        """Both panes' columns against their widths (#964): the
        layout module's own arithmetic — the catalog pane
        sharing the spare, the published pane taking its
        character budget in the giving order, the sorting
        paused around the text rewrites."""
        layout.size_columns(
            self.catalog_table, self.published_table, self.published_models
        )

    def held_first(self, table: ListTable) -> object:
        """The current row's identity (#964): the carry key its
        first cell holds — the hash, the url — or None with no
        current row."""
        row = table.currentRow()
        if row < 0:
            return None
        item = table.item(row, 0)
        return item.data(ROW_KEY_ROLE) if item is not None else None

    def settle_hold(
        self, table: ListTable, held: object, was_empty: bool
    ) -> None:
        """The hold after a rebuild (#964, the listing's own
        rule): the first rows a group ever shows hold the first
        row — the keyboard and the eye start somewhere — and a
        held row follows its own key wherever the rebuild lands
        it."""
        if held is None and was_empty and table.rowCount() > 0:
            table.selectRow(0)
            return
        self.restore_first(table, held)

    def restore_first(self, table: ListTable, held: object) -> None:
        """The held row back on the rebuilt table (#964): the
        row whose own key still reads the held one, whole and
        current — a row the rebuild dropped simply loses its
        hold."""
        if held is None:
            return
        for row in range(table.rowCount()):
            item = table.item(row, 0)
            if item is not None and item.data(ROW_KEY_ROLE) == held:
                table.selectRow(row)
                table.setCurrentItem(item)
                return
        table.clearSelection()
        table.selectionModel().clearCurrentIndex()

    @QtCore.Slot(str, object)
    def on_failed(self, key: str, failure: RestFailure) -> None:
        """One refused call (#964): the failure flashes on the
        status line — an outage stands until an answer resolves
        it — and the groups already on screen stand. Another
        key's failure is not this tab's to show."""
        if key == PAGE_KEY:
            self.pending = False
            self.flash(str(failure), outage=failure.reach)
        elif key == CATALOG_KEY:
            self.catalog_pending = False
            self.flash(str(failure), outage=failure.reach)
        elif key == IMPORT_KEY:
            self.land_import()
            self.flash(str(failure), outage=failure.reach)
        elif key in (REMOVE_KEY, DEFAULT_KEY):
            self.action_landed()
            self.flash(str(failure), outage=failure.reach)
        # A stranger's key answers nothing: the tab took no such
        # flight, so its failure belongs to whoever did.

    @QtCore.Slot(QtCore.QModelIndex)
    def open_detail_at(self, index: QtCore.QModelIndex) -> None:
        """One activated catalog row (#964): the model its row
        carries opens the detail dialog."""
        item = self.catalog_table.item(index.row(), 0)
        if item is not None:
            self.open_detail_row(
                self.catalog_models[item.data(ROW_INDEX_ROLE)]
            )

    def open_detail_row(self, model: CatalogRow) -> None:
        """The detail dialog over the row's full record (#964):
        the raw daemon row the model's hash finds, rendered by
        the shared ``image info`` lines."""
        row = self.catalog_record(model)
        if row is not None:
            ImageDetailDialog(row, self).exec()

    def catalog_record(self, model: CatalogRow) -> dict | None:
        """The raw daemon row behind a catalog model (#964): the
        detail view's own fields — the boot facts the row models
        do not carry — found by the hash the actions share."""
        return next(
            (row for row in self.catalog_raw if row["hash"] == model.hash),
            None,
        )

    @QtCore.Slot(QtCore.QModelIndex)
    def import_at(self, index: QtCore.QModelIndex) -> None:
        """One activated published row (#964): the model its row
        carries starts the import."""
        item = self.published_table.item(index.row(), 0)
        if item is not None:
            self.start_import(self.published_models[item.data(ROW_INDEX_ROLE)])

    def start_import(self, model: PublishedRow) -> None:
        """The import action (#964): the exact build already in
        the catalog imports nothing — the daemon would download
        the archive again to verify bytes it holds, and the
        flash names the ref so the skip reads as intentional.
        Any other row posts its URL behind the progress dialog,
        modal over the window and one flight at a time, and the
        dialog holds the wait until the daemon answers."""
        if self.progress is not None:
            return  # one import flight at a time, the actions' gate
        if model.marker == IMPORTED_MARKER:
            self.flash(f"{model.ref} is already in the catalog")
            return
        self.progress_ref = model.ref
        self.import_started = time.monotonic()
        self.progress = QtWidgets.QProgressDialog(
            importing_label(model.ref, 0), None, 0, 0, self
        )
        self.progress.setWindowTitle("Import image")
        self.progress.setWindowModality(QtCore.Qt.WindowModality.WindowModal)
        # The one popup that shows without a modal loop (#1014):
        # the progress dialog opens through ``show`` and re-sizes
        # itself as it shows, so the centering the dialogs'
        # ``exec`` path carries lands here by hand, after the
        # show — the wait sits on the main window's center like
        # every other popup.
        self.progress.show()
        center_on_parent(self.progress)
        self.import_timer.start()
        self.bridge.call(IMPORT_KEY, "import_image", model.url)

    def import_landed(self, result: object) -> None:
        """The import's answer (#964): the dialog closes, the
        flash carries the registered reference, and the catalog
        refresh shows the new row with its marker."""
        self.land_import()
        record = result if isinstance(result, dict) else {}
        if "ref" in record and "hash" in record:
            self.flash(imported_text(record))
        self.refresh_catalog()

    def land_import(self) -> None:
        """The import flight's end (#964): the dialog closes and
        the tick stops — every path out of the wait takes this,
        so the tab leaves nothing running."""
        self.import_timer.stop()
        dialog, self.progress = self.progress, None
        if dialog is not None:
            dialog.close()
            dialog.deleteLater()

    @QtCore.Slot()
    def import_tick(self) -> None:
        """The wait's own tick (#964): the dialog's elapsed
        second, the TUI importing modal's own cadence — the
        archive is hundreds of MiB and the operator sees it
        moving."""
        if self.progress is not None:
            elapsed = int(time.monotonic() - self.import_started)
            self.progress.setLabelText(
                importing_label(self.progress_ref, elapsed)
            )

    def default_row(self, model: CatalogRow) -> None:
        """The default action (#964): the designated row flashes
        so; any other posts the designation by its full hash —
        the same exchange the CLI's ``image default`` makes —
        behind the one-flight gate."""
        if model.default:
            self.flash(f"{model.ref} is already the default")
            return
        if self.action_pending:
            return
        self.action_pending = True
        self.action_flight = model.ref
        self.bridge.call(DEFAULT_KEY, "set_default_image", model.hash)

    def remove_row(self, model: CatalogRow) -> None:
        """The remove action (#964): the image leaves the
        catalog after the operator confirms — a refusal sends
        nothing to the daemon — behind the one-flight gate."""
        if self.action_pending:
            return
        if not self.confirm_remove(model.ref):
            return
        self.action_pending = True
        self.action_flight = model.ref
        self.bridge.call(REMOVE_KEY, "remove_image", model.hash)

    def confirm_remove(self, ref: str) -> bool:
        """Remove's confirmation (#964, #1014): the question
        names the image — No the default, both centered on the
        main window — a removal is the operator's call, not the
        pointer's."""
        return confirm(self, "Remove image", f"Remove {ref} from the catalog?")

    def action_landed(self) -> None:
        """An action flight's end (#964): the gate opens and the
        flight's target clears."""
        self.action_pending = False
        self.action_flight = None

    @QtCore.Slot(QtCore.QPoint)
    def menu_requested(self, pos: QtCore.QPoint) -> None:
        """A right-click on a catalog row opens its menu (#964):
        the detail, the default designation, and the remove
        behind its confirmation. A press on no row opens
        nothing."""
        menu = self.catalog_menu(self.catalog_table.rowAt(pos.y()))
        if menu is not None:
            menu.popup(self.catalog_table.viewport().mapToGlobal(pos))

    @QtCore.Slot(QtCore.QPoint)
    def published_menu_requested(self, pos: QtCore.QPoint) -> None:
        """A right-click on a published row opens its menu
        (#964): the import, for a row the catalog does not hold
        as this build. A press on no row opens nothing."""
        menu = self.published_menu(self.published_table.rowAt(pos.y()))
        if menu is not None:
            menu.popup(self.published_table.viewport().mapToGlobal(pos))

    def published_menu(self, row: int) -> QtWidgets.QMenu | None:
        """The published row's menu (#964) — ``None`` for a row
        the table does not hold, and for the exact build
        already in the catalog, whose activation flashes the
        skip the import path names."""
        item = self.published_table.item(row, 0)
        if item is None:
            return None
        model = self.published_models[item.data(ROW_INDEX_ROLE)]
        menu = QtWidgets.QMenu(self.published_table)
        menu.addAction("Import…", lambda: self.start_import(model))
        return menu

    def catalog_menu(self, row: int) -> QtWidgets.QMenu | None:
        """The catalog row's menu (#964) — ``None`` for a row
        the table does not hold."""
        item = self.catalog_table.item(row, 0)
        if item is None:
            return None
        model = self.catalog_models[item.data(ROW_INDEX_ROLE)]
        menu = QtWidgets.QMenu(self.catalog_table)
        menu.addAction("Open the detail", lambda: self.open_detail_row(model))
        menu.addAction("Set as the default", lambda: self.default_row(model))
        menu.addAction("Remove…", lambda: self.remove_row(model))
        return menu

    def flash(self, message: str, *, outage: bool = False) -> None:
        """The status line's flash (#939's machinery, the tab's
        own): the message shows; a refusal clears itself after
        the flash span, an outage stands for the next answer to
        resolve. Each flash carries its own serial, so a later
        flash's message is not erased by an earlier flash's
        timeout."""
        self.flash_outage = outage
        self.flash_seq += 1
        self.status.setText(message)
        if outage:
            return
        seq = self.flash_seq
        # The tab is the timeout's receiver (#959): the pending
        # callback is severed when the widget's C++ object goes.
        QtCore.QTimer.singleShot(FLASH_MS, self, lambda: self.clear_stale(seq))

    def clear_stale(self, seq: int) -> None:
        """The flash timeout's slot: the line clears only when
        this timeout's flash is still the one on it."""
        if seq == self.flash_seq:
            self.status.clear()

    def standing(self, text: str) -> None:
        """The standing tally (#964): a refresh's counts land on
        the bottom line beneath the published pane, and the
        status line's flash resolves — the serial bump retires
        any timeout still pending and any outage the flash
        named — standing clear until something else says its
        piece."""
        self.flash_seq += 1
        self.flash_outage = False
        self.counts.setText(text)
        self.status.clear()

    def showEvent(self, event: QtGui.QShowEvent) -> None:
        """The tab's first refresh at open (#964): the groups
        fill when the tab is shown, not when the window builds
        its strip — a background tab fetches nothing. A wrapper
        without its ``bridge`` (#959) fetches nothing either: Qt
        can deliver events to a widget whose construction never
        finished."""
        if getattr(self, "bridge", None) is not None:
            self.refresh()
        super().showEvent(event)

    def resizeEvent(self, event: QtGui.QResizeEvent) -> None:
        """A resized pane re-flows both groups' columns (#964):
        the catalog's spare shares out again, and the published
        pane's description and hash take the new width's
        arithmetic — never a horizontal scrollbar. A wrapper
        without its tables (#959) sizes nothing."""
        if getattr(self, "published_table", None) is not None:
            self.size_columns()
        super().resizeEvent(event)

    def eventFilter(
        self, watched: QtCore.QObject, event: QtCore.QEvent
    ) -> bool:
        """The tab's filtered gestures (#962's rule, #964's
        own): the refresh button's arrows — Up handing the
        keyboard to whatever stands above the tab, Down entering
        the groups below — and a group header's press, the sort
        the click means applied by the tab's own rule."""
        if self.button_key(watched, event):
            return True
        if self.header_sorted(watched, event):
            return True
        self.pane_resized(watched, event)
        return super().eventFilter(watched, event)

    def pane_resized(self, watched: QtCore.QObject, event) -> None:
        """A group pane's own resize (#964): the columns re-flow
        — a vertical scrollbar appearing shrinks the viewport
        after a fill sized against the wider one, and a pane
        never owes the operator a horizontal bar for it — the
        shared leaf's :func:`resized_viewport` (#997), the
        secrets tab's beside it."""
        if (
            resized_viewport(
                (self.catalog_table, self.published_table), watched, event
            )
            is not None
        ):
            self.size_columns()

    def button_key(self, watched: QtCore.QObject, event) -> bool:
        """The refresh button's arrow bridges (#962's rule): Up
        from the page's bottom bar enters the published group
        above it, and Down hands the keyboard out of the page —
        onward down the chain, the strip the wrap reaches — so
        the arrows cross the page in reading order without a
        Tab. The button's own chain walk, not the tab's: the
        move starts from where the operator stands."""
        if watched is not self.refresh_button or (
            event.type() != QtCore.QEvent.Type.KeyPress
        ):
            return False
        if event.key() == QtCore.Qt.Key.Key_Up:
            QtWidgets.QWidget.focusNextPrevChild(self.refresh_button, False)
            return True
        if event.key() == QtCore.Qt.Key.Key_Down:
            QtWidgets.QWidget.focusNextPrevChild(self.refresh_button, True)
            return True
        return False

    def header_sorted(self, watched: QtCore.QObject, event) -> bool:
        """A press on a group's header (#964): the sort the
        click means — a new column ascending, the same column
        toggled — applied through the table's own sort with its
        arrow on the column, the press swallowed so Qt's own
        first-click order never reads against its arrow."""
        for table in (self.catalog_table, self.published_table):
            header = table.horizontalHeader()
            if watched is header.viewport() and self.header_click(event):
                if event.type() == QtCore.QEvent.Type.MouseButtonRelease:
                    return True  # the press sorted; Qt's own
                    # click path never lands on top of it
                return self.sort_pressed(table, header, event)
        return False

    def header_click(self, event) -> bool:
        """Whether this event is a header's own click gesture
        (#964): a press or its matching release, the left button
        — the gesture the tab's sort rule answers, every other
        press reaching Qt's own handling."""
        return (
            event.type()
            in (
                QtCore.QEvent.Type.MouseButtonPress,
                QtCore.QEvent.Type.MouseButtonRelease,
            )
            and event.button() == QtCore.Qt.MouseButton.LeftButton
        )

    def sort_pressed(self, table: ListTable, header, event) -> bool:
        """The pressed header's own sort (#964): the section the
        press names, ordered by the tab's rule — a press on no
        section (the blank stretch, a grip) sorts nothing and
        reaches Qt's own handling."""
        section = header.logicalIndexAt(event.position().toPoint())
        if section < 0:
            return False
        if header.sortIndicatorSection() != section:
            order = QtCore.Qt.SortOrder.AscendingOrder
        elif header.sortIndicatorOrder() == QtCore.Qt.SortOrder.AscendingOrder:
            order = QtCore.Qt.SortOrder.DescendingOrder
        else:
            order = QtCore.Qt.SortOrder.AscendingOrder
        table.sortByColumn(section, order)
        return True
