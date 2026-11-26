"""The secrets tab (phase 4.6 of #924, #966, #997, #1060): the
thin widget that renders the presenter's row models
(:mod:`msks.client.gui.secretrows`) as the TUI secrets page's
counterpart — the placeholder listing (coverage, name,
destinations, a live TTL countdown, the created label, the lock
state) with the audit group beneath, each table's columns
sharing all the horizontal space the window gives it
(:func:`msks.client.gui.listtable.fill_columns`, re-flowed on
every window and pane resize, a scrollbar's appearance
included). The tab holds no business logic: its reads walk
the bridge to the gathered read
(:meth:`msks.client.gui.data.GuiData.secrets_page`), and the
answers rebuild the groups from the plain models. The reads run
at every show, behind every action's landing, and on the
pushed secret family (#1060): the frames the routes, the
watcher, and the interceptor publish (mint, revoke, unlock,
expiry — rare — and swap, sighting, one per intercepted
request) coalesce behind a short window into one re-read while
the tab owns the window, so the page holds no Refresh button.
The TTL column counts down from the deadline epoch the presenter lands
— a per-second repaint over the cached models, no daemon call
per tick. Mint opens the form (:mod:`.mint`), Unlock hands
the ask to the window's own flow (:mod:`msks.client.gui.unlock`,
#1147 — the prompt and the flights are the flow's, the outcome
lands on this tab's status line), and Renew and Revoke sit on
the bar beside them, enabled on a held placeholder row and dark
otherwise (#1165), the right-click menu carrying the same two
flights — renew opens the TTL picker, and revoke asks first —
each action landing through the bridge with the listing
refreshed behind it. The
sentinel pane is gone (#997): the page renders the two tables
alone, the sentinel stays the CLI's and the TUI's own surface,
and activating a row sends nothing. Refusals and failures
flash the status line, the listing's own contract (#904).

This module imports PySide6 at module scope — the same contract
as the screens' modules: the package's init imports none of it
at parse time, and ``.app``'s window path loads it after its
guards.
"""

from PySide6 import QtCore, QtWidgets

from ...data import RestFailure
from ...frames import secrets_frame
from ..feedpage import FeedPage
from ..forms import confirm, pick_item
from ..listtable import (
    ListTable,
    bold_header,
    fill_columns,
    resized_viewport,
)
from ..secretrows import (
    SECRET_TTL_SECONDS,
    SECRET_TTLS,
    coverage_words,
    deadline_stamp,
    expires_text,
    expiry_kind,
    state_kind,
    ttl_default,
)
from ..secretrows import (
    audit_rows as build_audit_rows,
)
from ..secretrows import (
    secret_rows as build_secret_rows,
)
from ..statusflash import StatusFlash
from ..tints import SEMANTIC_ROLE, TintedLabel
from . import cells
from .mint import MintDialog


def table_edge(key: int, current: int, last: int) -> bool | None:
    """The listing's edge keys (#966, #997): Right on the last
    column and Left on the first hand the keyboard out of the
    table — None keeps the walk inside the cells. The chain's
    own order carries the keyboard onward (the audit group
    below, the strip above, the action bar at the bottom), the
    spatial-navigation rule in the page's reading order."""
    if key == QtCore.Qt.Key.Key_Right and current >= last:
        return True
    if key == QtCore.Qt.Key.Key_Left and current <= 0:
        return False
    return None


class RowTable(ListTable):
    """The listing's own table (#966, #997): the Up bridge the
    list owns, beside the Left/Right edges' own
    (:func:`table_edge`) — Right on the last column hands the
    keyboard down to the audit group, Left on the first hands
    it back up the page, the reading order with the sentinel
    pane gone."""

    def keyPressEvent(self, event) -> None:
        hand = table_edge(
            event.key(), self.currentColumn(), self.columnCount() - 1
        )
        if hand is None:
            super().keyPressEvent(event)
            return
        QtWidgets.QWidget.focusNextPrevChild(self, hand)


#: The tab's gathered refresh key (#966): every read it takes
#: — the show, the serve poke, an action's landing, the frame
#: window's fire — one flight, both groups.
PAGE_KEY = "secrets-page"

#: The renew and revoke flights' keys (#966).
RENEW_KEY = "secrets-renew"
REVOKE_KEY = "secrets-revoke"

#: The recovery's flash (#904, #1132 — the TUI listing's own
#: words): the first answered read after an outage names the
#: recovery once, and the flash clears with its span.
RECOVERY_TEXT = "daemon back — secrets resumed"

#: The countdown repaint's cadence (#966), milliseconds — the
#: TUI page's own per-second tick, paint-time math over the
#: cached models.
TICK_MS = 1000

#: The frame-driven re-read's coalescing window (#1060),
#: milliseconds: the secret family's frames batch into one
#: flight — the window arms at a frame and fires once, frames
#: landing inside it absorbed into the read its end takes, and
#: a sustained burst re-arms it flight by flight, one read per
#: window. The interceptor's chatty kinds (``secret.swap``, one
#: frame per intercepted request) are the load the window
#: holds back; the rare kinds wait it out too — half a second
#: under a mint no operator reads as delay.
FRAME_WINDOW_MS = 500


class SecretsTab(FeedPage, StatusFlash, QtWidgets.QWidget):
    """The Secrets tab of the main window's strip (#966, #997,
    #1060): the placeholder listing and the audit group stacked
    full width — refreshed whole at every show, behind every
    action's landing, and on the pushed secret family behind
    its coalescing window while the tab owns the window. Every
    daemon exchange walks the bridge; the answers arrive on the
    GUI thread through this widget's slots, and a failure
    flashes the status line while the groups already on screen
    stand."""

    #: The Unlock button's ask (#1147): the window's flow takes
    #: it from there — the passphrase prompt, the flights, and
    #: the outcome on this tab's status line.
    unlock_asked = QtCore.Signal()

    def __init__(self, bridge) -> None:
        super().__init__()
        self.bridge = bridge
        self.pending = False
        self.refresh_due = False
        self.action_pending = False
        self.action_model = None
        self.action_choice: str | None = None
        self.flash_seq = 0
        self.daemon_down = False  # the mixin's state (#1132)
        self.outage_text = ""
        # The serial at which the line took the fetch's own text
        # (#1132): the landing clears that text and only it — a
        # flash in its span keeps the line (#776's mask).
        self.fetch_seq = 0
        self.secret_raw: list[dict] = []
        self.audit_raw: list[dict] = []
        self.secret_models: list = []
        self.audit_models: list = []
        bridge.succeeded.connect(self.on_succeeded)
        bridge.failed.connect(self.on_failed)
        # The pushed frames and the feed's state (#1060): the
        # secret family re-reads the page behind its coalescing
        # window while it owns the window, the serve poke
        # re-reads it whole, and a drop flashes — the pushed
        # frames are the page's freshness now that no button
        # stands.
        bridge.landed.connect(self.frame_landed)
        bridge.feed_served.connect(self.feed_served)
        bridge.feed_dropped.connect(self.feed_dropped)
        self.table = RowTable(0, len(cells.LIST_COLUMNS), self)
        self.table.setHorizontalHeaderLabels(cells.LIST_COLUMNS)
        cells.coverage_tip(self.table)  # the mint form's own words (#1165)
        self.build_table(self.table)
        self.table.setContextMenuPolicy(
            QtCore.Qt.ContextMenuPolicy.CustomContextMenu
        )
        self.table.customContextMenuRequested.connect(self.menu_requested)
        # The held row drives the bar's Renew and Revoke (#1165):
        # every move of the hold — a pick, a rebuild's carry, the
        # restore's clear — lands on the selection model's
        # current row, and that change is the one source for the
        # buttons; the gate re-reads the hold by hand at its own
        # turns.
        self.table.selectionModel().currentChanged.connect(
            lambda *_: self.sync_buttons()
        )
        self.list_empty = QtWidgets.QLabel(cells.list_empty_text(), self)
        # The listing's empty state wraps (#966): its advice
        # line must not price the tab's minimum past the
        # window's own default width.
        self.list_empty.setWordWrap(True)
        # The plain list table, not the crossing one (#966): the
        # audit group spans the page with no sibling column to
        # cross, its Up bridge alone the edges it needs.
        self.audit_table = ListTable(0, len(cells.AUDIT_COLUMNS), self)
        self.audit_table.setHorizontalHeaderLabels(cells.AUDIT_COLUMNS)
        self.build_table(self.audit_table, sortable=False)
        self.audit_empty = QtWidgets.QLabel(cells.audit_empty_text(), self)
        # The audit empty state wraps too (#966), for the same
        # reason.
        self.audit_empty.setWordWrap(True)
        self.status = TintedLabel("", self)
        self.mint_button = QtWidgets.QPushButton("Mint…", self)
        self.mint_button.clicked.connect(self.open_mint)
        self.mint_button.installEventFilter(self)
        self.unlock_button = QtWidgets.QPushButton("Unlock…", self)
        self.unlock_button.clicked.connect(self.unlock_asked)
        self.unlock_button.installEventFilter(self)
        # The row's two actions on the bar (#1165): dark until a
        # placeholder row is held, the same flights the menu
        # items run.
        self.renew_button = QtWidgets.QPushButton("Renew…", self)
        self.renew_button.clicked.connect(self.renew_selected)
        self.renew_button.setEnabled(False)
        self.renew_button.installEventFilter(self)
        self.revoke_button = QtWidgets.QPushButton("Revoke…", self)
        self.revoke_button.clicked.connect(self.revoke_selected)
        self.revoke_button.setEnabled(False)
        self.revoke_button.installEventFilter(self)
        self.bar_buttons = (
            self.mint_button,
            self.unlock_button,
            self.renew_button,
            self.revoke_button,
        )
        self.counts = QtWidgets.QLabel("", self)
        self.counts.setTextInteractionFlags(
            QtCore.Qt.TextInteractionFlag.TextSelectableByMouse
        )
        self.tick_timer = QtCore.QTimer(self)
        self.tick_timer.setInterval(TICK_MS)
        self.tick_timer.timeout.connect(self.on_tick)
        # The frame window's own timer (#1060): single-shot, one
        # flight at its fire — armed by the family's frames
        # while the tab owns the window, stopped when it hides
        # (the next show re-reads whole whatever moved).
        self.frame_timer = QtCore.QTimer(self)
        self.frame_timer.setSingleShot(True)
        self.frame_timer.setInterval(FRAME_WINDOW_MS)
        self.frame_timer.timeout.connect(self.frame_due)
        self.layout_widgets()

    def layout_widgets(self) -> None:
        """The tab's panes (#997, #1011, #1060, #1165): the
        placeholder listing and the audit group stacked full
        width — each spanning the window's whole width — the
        Mint, Unlock, Renew, and Revoke buttons and the flash
        line in a row at the bottom of the page, the buttons in
        the reading order the focus chain follows, and the
        standing counts on the bottom line."""
        bar = QtWidgets.QHBoxLayout()
        bar.addWidget(self.mint_button)
        bar.addWidget(self.unlock_button)
        bar.addWidget(self.renew_button)
        bar.addWidget(self.revoke_button)
        bar.addWidget(self.status, stretch=1)
        listing = QtWidgets.QGroupBox(cells.LIST_GROUP, self)
        listing_rows = QtWidgets.QVBoxLayout(listing)
        listing_rows.addWidget(self.table)
        listing_rows.addWidget(self.list_empty)
        audit = QtWidgets.QGroupBox(cells.AUDIT_GROUP, self)
        audit_rows = QtWidgets.QVBoxLayout(audit)
        audit_rows.addWidget(self.audit_table)
        audit_rows.addWidget(self.audit_empty)
        columns = QtWidgets.QVBoxLayout(self)
        columns.addWidget(listing, stretch=1)
        columns.addWidget(audit, stretch=1)
        columns.addLayout(bar)
        columns.addWidget(self.counts)

    def build_table(self, table: ListTable, sortable: bool = True) -> None:
        """A group table's standing shape (#966, #997):
        whole-row selection, no cell edits, no vertical grid,
        and columns sized by :func:`fill_columns` — the contents
        first, the spare shared after, the images tab's own
        read. The listing sorts by its header clicks; the audit
        table keeps the presenter's newest-first order, its own
        contract."""
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
        table.setStyleSheet(
            "QTableView::item { border-bottom: 1px solid rgba(128, 128, "
            "128, 90); }"
        )
        table.verticalHeader().setVisible(False)
        bold_header(table)
        # The pane's own resizes re-flow the columns (#997): a
        # vertical scrollbar's appearance shrinks the viewport
        # after a fill sized against the wider one, and a
        # window resized while the tab stood hidden reaches the
        # pane at its next show.
        table.viewport().installEventFilter(self)
        # No standing sort (#966): the indicator clears before
        # the sorting enables — the listing keeps the daemon's
        # own order until a header click asks for one, the TUI
        # page's rule, and the audit table's beside it.
        table.horizontalHeader().setSortIndicator(
            -1, QtCore.Qt.SortOrder.AscendingOrder
        )
        table.setSortingEnabled(sortable)

    @QtCore.Slot()
    def refresh(self) -> None:
        """The tab's whole refresh (#966): the read every show
        takes and the serve poke's (#1060) — the status line
        saying the fetch is out, the serial bump retiring any
        pending flash timeout so none clears it mid-flight. A
        read already out carries the ask instead
        (:meth:`take_flight`)."""
        if self.pending:
            self.refresh_due = True
            return
        self.flash_seq += 1
        self.fetch_seq = self.flash_seq
        self.status.setText("fetching the placeholders and the audit rows…")
        self.status.set_kind(None)
        self.take_flight()

    def take_flight(self) -> None:
        """The gathered flight itself (#966), the status line
        untouched — the refresh an action's landing takes keeps
        the flash that named the outcome standing while it
        flies, for its own span (#776's mask, #1132: the serial
        used to retire that timeout here). A flight already out
        carries the need instead: its landing takes the flight
        the gate refused, so an action's refresh is never
        dropped."""
        if self.pending:
            self.refresh_due = True
            return
        self.pending = True
        self.bridge.call(PAGE_KEY, "secrets_page")

    # -- the pushed frames and the feed (#1060) -----------------------------

    @QtCore.Slot(object)
    def frame_landed(self, frame: dict) -> None:
        """One pushed frame (#1060): the secret family — the
        routes' mint, revoke, and unlock, the watcher's expiry,
        the interceptor's swap and sighting, whatever client
        made the move — arms the coalescing window while the tab
        owns the window; every other family is another surface's
        own, and a hidden tab re-reads whole at its next show.
        The window arms once and fires once: frames landing
        inside it absorb into the one read its end takes."""
        if not secrets_frame(frame) or not self.isVisible():
            return
        if not self.frame_timer.isActive():
            self.frame_timer.start()

    @QtCore.Slot()
    def frame_due(self) -> None:
        """The window's fire (#1060): the coalesced re-read
        flies, its status line untouched — the page's own flash
        stands while it reads, the action landings' own rule.
        A flight already out carries the need instead
        (:meth:`take_flight`'s guard), one re-flight per
        landing."""
        self.take_flight()

    @QtCore.Slot(str, object)
    def on_succeeded(self, key: str, result: object) -> None:
        """One answered call (#966): the gathered read rebuilds
        both groups, and an action's landing flashes its outcome
        and refreshes. Another key's answer is not this tab's
        to take."""
        if key == PAGE_KEY:
            self.page_landed(result)
        elif key in (RENEW_KEY, REVOKE_KEY):
            self.action_arrived(key, result)

    def page_landed(self, result: object) -> None:
        """The gathered answer (#966): both groups rebuild from
        the presenter's models, the standing counts beside them
        — and a refresh the gate carried while this flight was
        out flies now, the heal an action's landing asked
        for."""
        self.pending = False
        page = result if isinstance(result, dict) else {}
        self.secret_raw = list(page.get("secrets") or [])
        self.audit_raw = list(page.get("audit") or [])
        self.rebuild()
        self.standing()
        if self.refresh_due:
            self.refresh_due = False
            self.take_flight()

    def rebuild(self) -> None:
        """Both groups from the presenter (#966): the listing in
        the daemon's own order, the audit rows newest first —
        the whole page rebuilt from plain data, the selections
        carried by their rows' own keys across the swap."""
        self.fill_list(build_secret_rows(self.secret_raw))
        self.fill_audit(build_audit_rows(self.audit_raw))

    def size_columns(self) -> None:
        """Both tables' columns at their panes' whole width
        (#997): the fills land here, and the window's and the
        panes' own resizes re-flow the spare."""
        fill_columns(self.table)
        fill_columns(self.audit_table)

    def fill_list(self, models: list) -> None:
        """The listing's rows (#966, #1165): one per model, the
        selection carried by the row's id across the rebuild,
        the empty state named beside an empty table, the columns
        filling the pane's whole width (#997) — a bounded row's
        EXPIRES cell carrying its deadline's full date and time
        as the tooltip. The bar's Renew and Revoke answer the
        hold the rebuild leaves through the selection model's
        current-row signal alone."""
        held = self.held_first(self.table)
        was_empty = self.table.rowCount() == 0
        self.table.setSortingEnabled(False)
        self.table.setRowCount(len(models))
        for index, model in enumerate(models):
            rendered = cells.list_cells(model)
            first = cells.ranked_item(*rendered[0])
            first.setData(cells.ROW_INDEX_ROLE, index)
            first.setData(cells.ROW_KEY_ROLE, model.id)
            self.table.setItem(index, 0, first)
            for column, (cell, rank) in enumerate(rendered[1:], start=1):
                item = cells.ranked_item(cell, rank)
                if column == cells.EXPIRES_COLUMN and (
                    model.expires_ts is not None
                ):
                    item.setToolTip(deadline_stamp(model.expires_ts))
                self.table.setItem(index, column, item)
            self.tint_row(index, model)
        self.secret_models = models
        self.list_empty.setVisible(not models)
        self.table.setSortingEnabled(True)
        self.settle_hold(self.table, held, was_empty)
        fill_columns(self.table)

    def fill_audit(self, models: list) -> None:
        """The audit group's rows (#966): one per model, newest
        first — the presenter's own order, no header sort — the
        selection carried by the audit id where the row carried
        one, the columns filling the pane's whole width
        (#997)."""
        held = self.held_first(self.audit_table)
        was_empty = self.audit_table.rowCount() == 0
        self.audit_table.setRowCount(len(models))
        for index, model in enumerate(models):
            texts = cells.audit_cells(model)
            for column, cell in enumerate(texts):
                item = cells.ranked_item(cell, None)
                if column == 0:
                    item.setData(cells.ROW_INDEX_ROLE, index)
                    item.setData(cells.ROW_KEY_ROLE, model.audit_id)
                self.audit_table.setItem(index, column, item)
        self.audit_models = models
        self.audit_empty.setVisible(not models)
        self.settle_hold(self.audit_table, held, was_empty)
        fill_columns(self.audit_table)

    def held_first(self, table: ListTable) -> object:
        """The current row's identity (#966): the carry key its
        first cell holds — the placeholder's id, the audit row's
        — or None with no current row."""
        row = table.currentRow()
        if row < 0:
            return None
        item = table.item(row, 0)
        return item.data(cells.ROW_KEY_ROLE) if item is not None else None

    def settle_hold(
        self, table: ListTable, held: object, was_empty: bool
    ) -> None:
        """The hold after a rebuild (#966, the listing's own
        rule): the first rows a group ever shows hold the first
        row, and a held row follows its own key wherever the
        rebuild lands it."""
        if held is None and was_empty and table.rowCount() > 0:
            table.selectRow(0)
            return
        self.restore_first(table, held)

    def restore_first(self, table: ListTable, held: object) -> None:
        """The held row back on the rebuilt table (#966): the
        row whose own key still reads the held one, whole and
        current — a row the rebuild dropped simply loses its
        hold."""
        if held is None:
            return
        for row in range(table.rowCount()):
            item = table.item(row, 0)
            if item is not None and item.data(cells.ROW_KEY_ROLE) == held:
                table.selectRow(row)
                table.setCurrentItem(item)
                return
        table.clearSelection()
        table.selectionModel().clearCurrentIndex()

    @QtCore.Slot(str, object)
    def on_failed(self, key: str, failure: RestFailure) -> None:
        """One refused call (#966): the failure flashes on the
        status line — an outage stands until an answer resolves
        it — and the groups already on screen stand. Another
        key's failure is not this tab's to show."""
        if key == PAGE_KEY:
            self.pending = False
            # The carried ask dies with the flight it waited on
            # (#966, review round two): the outage flash already
            # names the retry, and a later landing must not fire
            # a refresh nobody asked for.
            self.refresh_due = False
            self.refuse(str(failure), reach=failure.reach)
        elif key in (RENEW_KEY, REVOKE_KEY):
            self.action_landed()
            self.refuse(str(failure), reach=failure.reach)

    # -- the countdown -----------------------------------------------------

    @QtCore.Slot()
    def on_tick(self) -> None:
        """The per-second repaint (#966, #1165): the EXPIRES cells'
        live countdown beside its deadline's clock, paint-time
        math over the cached models — the daemon is asked
        nothing. A row without a deadline epoch keeps its frozen
        cell (``never``, the unparsable dash)."""
        for row in range(self.table.rowCount()):
            item = self.table.item(row, cells.EXPIRES_COLUMN)
            model = self.row_model(row)
            if item is None or model is None or model.expires_ts is None:
                continue
            item.setText(expires_text(model.expires_ts))
            item.setData(SEMANTIC_ROLE, expiry_kind(model.expires_ts))

    def tint_row(self, index: int, model) -> None:
        """The row's tints (#1160): the state word by its lock,
        the expiry cell warn under the renew floor. The kinds
        ride the items; the delegate paints them."""
        state = self.table.item(index, cells.STATE_COLUMN)
        expires = self.table.item(index, cells.EXPIRES_COLUMN)
        if state is not None:
            state.setData(SEMANTIC_ROLE, state_kind(model))
        if expires is not None:
            expires.setData(SEMANTIC_ROLE, expiry_kind(model.expires_ts))

    # -- the focused row into actions ---------------------------------------

    def row_model(self, row: int):
        """The model a table row carries (#966), or None when the
        row holds no first cell or the cell carries no model
        index (a row the table counts but a fill has not
        reached)."""
        item = self.table.item(row, 0)
        if item is None:
            return None
        index = item.data(cells.ROW_INDEX_ROLE)
        return self.secret_models[index] if index is not None else None

    def selected_model(self):
        """The held placeholder row's model (#1165): the row the
        bar's Renew and Revoke act on — None with no row held."""
        return self.row_model(self.table.currentRow())

    @QtCore.Slot()
    def sync_buttons(self) -> None:
        """The bar's row buttons (#1165, the mounts tab's own
        rule): Renew and Revoke answer the held placeholder row
        — the table's current row — beside the one-flight gate: a
        pick lights them, a cleared or vanished hold darkens them,
        a flight out darkens both until its landing."""
        live = self.selected_model() is not None and not self.action_pending
        self.renew_button.setEnabled(live)
        self.revoke_button.setEnabled(live)

    @QtCore.Slot()
    def renew_selected(self) -> None:
        """The Renew button (#1165): the held row's renew, the
        menu item's own flight; a press with no row held — a
        dark button's click — sends nothing."""
        model = self.selected_model()
        if model is not None:
            self.renew_row(model)

    @QtCore.Slot()
    def revoke_selected(self) -> None:
        """The Revoke button (#1165): the held row's revoke behind
        its confirmation, the menu item's own flight; a press
        with no row held sends nothing."""
        model = self.selected_model()
        if model is not None:
            self.revoke_row(model)

    def gate(self, model, choice: str | None) -> None:
        """The action gate closes (#966, #1165): the flight's row
        and choice remembered, and the bar's Renew and Revoke
        dark until the landing opens the gate — one action at a
        time, on the row the operator picked."""
        self.action_pending = True
        self.action_model = model
        self.action_choice = choice
        self.sync_buttons()

    def raw_row(self, model) -> dict | None:
        """The raw daemon row behind a listing model (#966):
        the renew picker's default reads the row's own
        deadline."""
        return next(
            (row for row in self.secret_raw if row["id"] == model.id), None
        )

    def renew_row(self, model) -> None:
        """The renew action (#966): the TTL picker over the
        presenter's span, its opening choice the nearest one to
        the row's remaining lifetime; the pick extends the row's
        lifetime in place behind the one-flight gate."""
        if self.action_pending:
            return
        choice = self.pick_ttl(model)
        if choice is None:
            return
        self.gate(model, choice)
        self.bridge.call(
            RENEW_KEY, "renew_secret", model.id, SECRET_TTL_SECONDS[choice]
        )

    def pick_ttl(self, model) -> str | None:
        """The renew picker (#966, #1014): the presenter's
        choices with its own default — the row's raw deadline
        naming the opening one — centered on the main window. A
        cancelled picker decides nothing."""
        raw = self.raw_row(model)
        default = ttl_default(raw) if raw is not None else SECRET_TTLS[-1]
        return pick_item(
            self,
            "Renew",
            f"renew {coverage_words(model.coverage)}/{model.name} for",
            list(SECRET_TTLS),
            default,
        )

    def revoke_row(self, model) -> None:
        """The revoke action (#966): the row retires everywhere
        at once — the question names it — and a yes posts the
        DELETE behind the one-flight gate; a no decides
        nothing."""
        if self.action_pending:
            return
        if not self.confirm_revoke(model):
            return
        self.gate(model, None)
        self.bridge.call(REVOKE_KEY, "revoke_secret", model.id)

    def confirm_revoke(self, model) -> bool:
        """Revoke's confirmation (#966, #1014): No the default
        and both centered on the main window — the retirement
        is the operator's call, not the pointer's."""
        return confirm(self, "Revoke", cells.revoke_question(model))

    def action_arrived(self, key: str, result: object) -> None:
        """An action's landing (#966): the flight's remembered
        row flashes its outcome — the renewed lifetime read off
        the daemon's own reply, the countdown's vocabulary, the
        revoked row's label — and the listing refreshes behind
        it. The gate set the row when the flight left; a landing
        reads it back."""
        model = self.action_model
        self.action_landed()
        if key == RENEW_KEY:
            reply = result if isinstance(result, dict) else {}
            self.outcome(cells.renewed_text(model, reply))
        else:
            self.outcome(cells.revoked_text(model))
        self.take_flight()

    def action_landed(self) -> None:
        """An action flight's end (#966): the gate opens, the
        flight's row clears, and the bar's Renew and Revoke
        answer the hold again (#1165)."""
        self.action_pending = False
        self.action_model = None
        self.action_choice = None
        self.sync_buttons()

    @QtCore.Slot()
    def open_mint(self) -> None:
        """The Mint action (#966, #997): the form over the
        bridge — a landed mint refreshes the listing and the
        flash names the row, the page carries no sentinel
        display; a cancel decides nothing."""
        dialog = MintDialog(self.bridge, self)
        if dialog.exec() and dialog.reply:
            self.take_flight()
            self.outcome(cells.minted_text(dialog.reply))

    @QtCore.Slot(QtCore.QPoint)
    def menu_requested(self, pos: QtCore.QPoint) -> None:
        """A right-click on a placeholder row opens its menu
        (#966, #1165): the clicked row takes the selection first
        — the Mounts tab's own rule (#1098) — so the entries,
        which read the bar's state, answer the row under the
        pointer even from an empty hold, and the renew and the
        revoke behind its confirmation follow. A press on no row
        opens nothing."""
        row = self.table.rowAt(pos.y())
        if row < 0:
            return
        self.table.selectRow(row)
        menu = self.row_menu(row)
        if menu is None:
            return
        menu.popup(self.table.viewport().mapToGlobal(pos))

    def row_menu(self, row: int) -> QtWidgets.QMenu | None:
        """The placeholder row's menu (#966, #997, #1165) — ``None``
        for a row the table does not hold. Its entries read the
        bar's own state: a flight already out darkens both here
        as it does on the bar, the mounts tab's own rule
        (#1098)."""
        model = self.row_model(row)
        if model is None:
            return None
        menu = QtWidgets.QMenu(self.table)
        renew = menu.addAction("Renew…", lambda: self.renew_row(model))
        revoke = menu.addAction("Revoke…", lambda: self.revoke_row(model))
        renew.setEnabled(self.renew_button.isEnabled())
        revoke.setEnabled(self.revoke_button.isEnabled())
        return menu

    # -- the status line ----------------------------------------------------

    def standing(self) -> None:
        """The standing counts (#966): a refresh's tally lands
        on the bottom line, and the status line's own fetch text
        clears — only it: a flash in its span keeps the line for
        its span (#776's mask, #1132: an action's outcome used to
        last one round trip), and a standing outage resolves with
        the recovery's one flash, the mixin's daemon-down state
        deciding."""
        self.counts.setText(
            cells.counts_text(self.secret_models, self.audit_models)
        )
        if self.flash_seq == self.fetch_seq:
            self.status.clear()
        self.recover(RECOVERY_TEXT)

    # -- the widget's own events --------------------------------------------

    def showEvent(self, event) -> None:
        """The tab's refresh at every show (#966, #1060): the
        groups fill when the tab is shown, not when the window
        builds its strip, and the countdown's tick starts with
        them — a return to the page re-reads whatever moved
        while it stood hidden. A wrapper without its ``bridge``
        (#959) fetches nothing either."""
        if getattr(self, "bridge", None) is not None:
            self.refresh()
            self.tick_timer.start()
        super().showEvent(event)

    def hideEvent(self, event) -> None:
        """A hidden tab stops its tick (#966): a background
        page repaints nothing — the countdown resumes with the
        tab, whole, from the epochs the models carry. The frame
        window stops with it (#1060): a page the operator
        cannot see re-reads at its next show, not at a fire the
        hide outraced."""
        self.tick_timer.stop()
        self.frame_timer.stop()
        super().hideEvent(event)

    def resizeEvent(self, event) -> None:
        """A resized window re-flows both tables' columns
        (#997): the spare the new width leaves beyond the
        columns' contents shares out again, each table filling
        all the horizontal space the window gives it. A wrapper
        without its tables (#959) sizes nothing."""
        if getattr(self, "audit_table", None) is not None:
            self.size_columns()
        super().resizeEvent(event)

    def eventFilter(
        self, watched: QtCore.QObject, event: QtCore.QEvent
    ) -> bool:
        """The bar buttons' arrow bridges (#962's rule, every bar
        button — Mint, Unlock, Renew, and Revoke, #1165) beside a
        pane's own resize (#997): Up from a bottom button climbs
        the reading order — the audit group above it — and Down
        hands the keyboard out of the page, onward down the chain
        (the strip the wrap reaches), so the arrows cross the
        page in reading order without a Tab; a group pane's
        resize re-flows its columns.
        """
        self.pane_resized(watched, event)
        if watched not in self.bar_buttons or (
            event.type() != QtCore.QEvent.Type.KeyPress
        ):
            return super().eventFilter(watched, event)
        if event.key() == QtCore.Qt.Key.Key_Up:
            QtWidgets.QWidget.focusNextPrevChild(watched, False)
            return True
        if event.key() == QtCore.Qt.Key.Key_Down:
            QtWidgets.QWidget.focusNextPrevChild(watched, True)
            return True
        return super().eventFilter(watched, event)

    def pane_resized(self, watched: QtCore.QObject, event) -> None:
        """A group pane's own resize (#997): a vertical scrollbar
        appearing shrinks the viewport after a fill sized
        against the wider one, and a window resized while the
        tab stood hidden reaches the pane at its next show — a
        pane never owes the operator a horizontal bar for it,
        the images tab's own rule (#964) over the shared
        :func:`resized_viewport`."""
        if (
            resized_viewport((self.table, self.audit_table), watched, event)
            is not None
        ):
            self.size_columns()
