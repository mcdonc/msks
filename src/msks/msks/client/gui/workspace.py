"""The workspace detail screen (phase 4 of #924, #945): the
page the workspace list opens on one row — the presenter's
sections as widgets (mounts with their derived state, the
health facts, the recent events), refreshed on the listing's
own cadence through the bridge, the same reads the TUI's
workspace page makes: the row out of the workspaces listing,
the doc out of the health read.

This module imports PySide6 at module scope — the same
contract as the list screen's module
(:mod:`msks.client.gui.workspaces`): nothing imports it at CLI
parse time, and ``.app``'s window path imports it lazily after
its guards (#927), the reachability the gate counts.
"""

from PySide6 import QtCore, QtGui, QtWidgets

from ..data import RestFailure
from .bridge import Bridge
from .detail import DetailSections, detail_sections
from .rows import workspace_row
from .workspaces import (
    FLASH_MS,
    REFRESH_MS,
    WorkspaceList,
    read_only_item,
)

#: The row read's key (#945): the page answers only its own
#: key, whatever else shares the bridge.
ROW_KEY = "detail-row"

#: The health read's key (#945).
HEALTH_KEY = "detail-health"

#: How many frames the page buffers (#945): the pushed frames
#: (#946's feed) land here — the presenter keeps the last
#: twenty for the section, and the buffer holds a wider history
#: between renders without growing without bound.
FRAMES_BUFFER = 100

#: The back arrow's size, pixels (#945): the theme's own
#: left-pointing arrow drawn large against the row's text —
#: the window's fonts run one and a half times the platform
#: default (#939), and a small glyph reads as decoration, not
#: a control.
BACK_ICON_PX = 24

#: The mounts table's columns (#945), in display order — the
#: entry's own fields, the state beside the reason the gate
#: named.
MOUNT_COLUMNS = ("HOST", "GUEST", "MODE", "STATE", "REASON")


def find_row(rows: list[dict] | None, workspace_id: str) -> dict | None:
    """This workspace's row out of a listing (#945): the same
    find the TUI's page makes over the listing the read
    carries."""
    return next(
        (row for row in rows or [] if row.get("id") == workspace_id),
        None,
    )


def muted_note(text: str) -> QtWidgets.QLabel:
    """A muted section note (#945): the empty section's honest
    stand-in, colored the palette's own shadow."""
    label = QtWidgets.QLabel(text)
    shadow = label.palette().color(QtGui.QPalette.ColorRole.Shadow)
    label.setStyleSheet(f"color: {shadow.name()};")
    return label


class WorkspaceDetail(QtWidgets.QWidget):
    """One workspace's page (#945): the title lines over the
    presenter's three sections — the mounts table with its
    next-start note, the health facts, the events list —
    refreshed on the listing's own cadence plus the manual
    button, every exchange through the bridge. A failed refresh
    flashes the status line while the sections keep their
    content; a listing that can no longer see the workspace
    emits ``removed`` — the TUI page's own close (#309's
    close_removed shape) — and the back action returns to the
    list."""

    back = QtCore.Signal()
    removed = QtCore.Signal(str)

    def __init__(self, bridge: Bridge) -> None:
        super().__init__()
        self.bridge = bridge
        self.workspace_id = ""
        self.row: dict | None = None
        self.health_doc: dict | None = None
        self.frames: list[dict] = []
        self.rendered: DetailSections | None = None
        self.row_pending = False
        self.health_pending = False
        self.flash_seq = 0
        self.flash_outage = False
        bridge.succeeded.connect(self.on_succeeded)
        bridge.failed.connect(self.on_failed)
        self.title = QtWidgets.QLabel("", self)
        self.meta = QtWidgets.QLabel("", self)
        self.meta.setTextInteractionFlags(
            QtCore.Qt.TextInteractionFlag.TextSelectableByMouse
        )
        sections = self.build_sections()
        scroll = QtWidgets.QScrollArea(self)
        scroll.setWidgetResizable(True)
        scroll.setWidget(sections)
        self.build_chrome()
        layout = QtWidgets.QVBoxLayout(self)
        layout.addLayout(self.chrome)  # the top row, the list's own
        layout.addWidget(self.title)
        layout.addWidget(self.meta)
        layout.addWidget(scroll, stretch=1)
        self.setLayout(layout)
        escape = QtGui.QShortcut(
            QtGui.QKeySequence(QtCore.Qt.Key.Key_Escape), self
        )
        escape.activated.connect(self.go_back)
        self.timer = QtCore.QTimer(self)
        self.timer.setInterval(REFRESH_MS)
        self.timer.timeout.connect(self.refresh)

    def build_sections(self) -> QtWidgets.QWidget:
        """The three sections' column (#945): grouped boxes on
        one scrolling page — the mounts over the health over
        the events."""
        column = QtWidgets.QWidget(self)
        layout = QtWidgets.QVBoxLayout(column)
        layout.addWidget(self.build_mounts())
        layout.addWidget(self.build_health())
        layout.addWidget(self.build_events())
        return column

    def build_mounts(self) -> QtWidgets.QGroupBox:
        """The mounts section's chrome (#945): the table of
        entries — host, guest, mode, the derived state, the
        reason a gate named — with the next-start note under it
        and a muted stand-in for a workspace that mounts
        nothing."""
        self.mounts = QtWidgets.QTableWidget(0, len(MOUNT_COLUMNS), self)
        self.mounts.setHorizontalHeaderLabels(MOUNT_COLUMNS)
        self.mounts.verticalHeader().setVisible(False)
        self.mounts.setSelectionBehavior(
            QtWidgets.QAbstractItemView.SelectionBehavior.SelectRows
        )
        self.mounts.setShowGrid(False)
        rule = self.mounts.palette().color(QtGui.QPalette.ColorRole.Mid).name()
        self.mounts.setStyleSheet(
            f"QTableView::item {{ border-bottom: 1px solid {rule}; }}"
        )
        self.mounts.horizontalHeader().setStretchLastSection(True)
        self.pending_label = QtWidgets.QLabel("", self)
        self.pending_label.setVisible(False)
        self.empty_mounts = muted_note("none")
        box = QtWidgets.QGroupBox("Mounts", self)
        inner = QtWidgets.QVBoxLayout(box)
        inner.addWidget(self.mounts)
        inner.addWidget(self.pending_label)
        inner.addWidget(self.empty_mounts)
        return box

    def build_health(self) -> QtWidgets.QGroupBox:
        """The health section's chrome (#945): the daemon's
        facts, one pair a line, selectable as text — the doc the
        health read carries, with a muted stand-in until one
        lands."""
        self.health = QtWidgets.QLabel("", self)
        self.health.setTextInteractionFlags(
            QtCore.Qt.TextInteractionFlag.TextSelectableByMouse
        )
        self.empty_health = muted_note("none yet")
        box = QtWidgets.QGroupBox("Health", self)
        inner = QtWidgets.QVBoxLayout(box)
        inner.addWidget(self.health)
        inner.addWidget(self.empty_health)
        return box

    def build_events(self) -> QtWidgets.QGroupBox:
        """The events section's chrome (#945): the recent
        frames' lines, newest last, in a scrolling list — the
        pushed frames (#946) fill it, with a muted stand-in
        until one lands."""
        self.events = QtWidgets.QListWidget(self)
        self.empty_events = muted_note("none yet")
        box = QtWidgets.QGroupBox("Events", self)
        inner = QtWidgets.QVBoxLayout(box)
        inner.addWidget(self.events)
        inner.addWidget(self.empty_events)
        return box

    def build_chrome(self) -> None:
        """The page's controls at the window's top (#945): back
        to the list — the theme's own left-pointing arrow, drawn
        large, its tooltip and accessible name carrying the word
        — the manual refresh, and the status line a failed
        refresh flashes, the same top row the listing's own
        actions sit in."""
        back_button = QtWidgets.QPushButton(self)
        back_button.setIcon(
            self.style().standardIcon(
                QtWidgets.QStyle.StandardPixmap.SP_ArrowBack
            )
        )
        back_button.setIconSize(QtCore.QSize(BACK_ICON_PX, BACK_ICON_PX))
        back_button.setToolTip("Back to the workspace list")
        back_button.setAccessibleName("Back")
        back_button.clicked.connect(self.go_back)
        self.back_button = back_button
        refresh_button = QtWidgets.QPushButton("Refresh", self)
        refresh_button.clicked.connect(self.refresh)
        self.status = QtWidgets.QLabel("", self)
        self.chrome = QtWidgets.QHBoxLayout()
        self.chrome.addWidget(back_button)
        self.chrome.addWidget(refresh_button)
        self.chrome.addWidget(self.status, stretch=1)

    def open(self, workspace_id: str, label: str) -> None:
        """The page takes a workspace (#945): the state resets —
        fresh row, doc, frame buffer, rendered sections — and
        the first refresh goes out. The label the list holds
        stands in for the title until the row's own answer
        lands."""
        self.workspace_id = workspace_id
        self.row = None
        self.health_doc = None
        self.frames = []
        self.rendered = None
        self.title.setText(label)
        self.clear_sections()
        self.refresh()

    def clear_sections(self) -> None:
        """A workspace's opening blank (#945): the sections and
        the meta line clear until the first answers land."""
        self.meta.setText("")
        self.mounts.setRowCount(0)
        self.pending_label.setVisible(False)
        self.health.setText("")
        self.events.clear()
        self.empty_mounts.setVisible(True)
        self.mounts.setVisible(False)
        self.empty_health.setVisible(True)
        self.health.setVisible(False)
        self.empty_events.setVisible(True)
        self.events.setVisible(False)

    @QtCore.Slot()
    def refresh(self) -> None:
        """One page refresh through the bridge (#945) — the
        tick, the manual button, and the page's own open share
        it. A flight already out is not stacked, the listing's
        own guard (#470); the sections stand until the answers
        replace them."""
        if not self.row_pending:
            self.row_pending = True
            self.bridge.call(ROW_KEY, "workspaces")
        if not self.health_pending:
            self.health_pending = True
            self.bridge.call(HEALTH_KEY, "health")

    @QtCore.Slot(str, object)
    def on_succeeded(self, key: str, result: object) -> None:
        """One answered call (#945): the row read finds this
        workspace's row — a listing that cannot see it emits
        ``removed`` — and the health read keeps the doc, either
        answer re-rendering the sections. Another key's answer
        is not this page's to take."""
        if key == ROW_KEY:
            self.row_landed(result)
        elif key == HEALTH_KEY:
            self.health_landed(result)

    def row_landed(self, rows: object) -> None:
        """The row read's answer (#945): this workspace's row
        out of the listing the read carries, the TUI page's own
        find. A listing without it emits ``removed``; a standing
        outage resolves with it."""
        self.row_pending = False
        self.resolve_outage()
        fresh = find_row(rows, self.workspace_id)
        if fresh is None:
            self.removed.emit(self.workspace_id)
            return
        self.row = fresh
        self.rebuild()

    def health_landed(self, doc: object) -> None:
        """The health read's answer (#945): the doc the sections
        read, a standing outage resolving with it. A read the
        daemon cannot answer yet (``None``) keeps the sections'
        standing content."""
        self.health_pending = False
        self.resolve_outage()
        self.health_doc = doc
        self.rebuild()

    @QtCore.Slot(str, object)
    def on_failed(self, key: str, failure: RestFailure) -> None:
        """One refused call (#945): the failure flashes — an
        outage (the daemon answered nothing) stands until an
        answer resolves it — and the sections keep the content
        they have. Another key's failure is not this page's to
        show."""
        if key not in (ROW_KEY, HEALTH_KEY):
            return
        if key == ROW_KEY:
            self.row_pending = False
        else:
            self.health_pending = False
        self.flash(str(failure), outage=failure.reach)

    def resolve_outage(self) -> None:
        """A standing outage's resolve (#945, #904's contract):
        an answer landed, and the flash it held clears — the
        sections keep the content they have."""
        if self.flash_outage:
            self.clear_flash()

    def rebuild(self) -> None:
        """The sections from the page's standing facts (#945):
        the presenter's models over the row, the doc, and the
        frame buffer. An answer that changes nothing renders
        nothing — the listing's own rule (#470 L2), so a hold on
        the mounts table survives a refresh that moved
        nothing."""
        if self.row is None:
            return
        model = detail_sections(self.row, self.health_doc, self.frames)
        if model == self.rendered:
            return
        self.rendered = model
        self.render(model)

    def render(self, model: DetailSections) -> None:
        """The page as the model reads (#945): the title lines,
        then each section its own widgets."""
        self.render_title(model)
        self.render_mounts(model)
        self.render_health(model)
        self.render_events(model)

    def render_title(self, model: DetailSections) -> None:
        """The page's title lines (#945): the workspace's label,
        the row's own facts muted beneath — id, image, created,
        the fields the listing's columns carry."""
        facts = workspace_row(self.row or {})
        self.title.setText(model.label)
        self.meta.setText(
            f"id {facts.id}  ·  image {facts.image}"
            f"  ·  created {facts.created}"
        )

    def render_mounts(self, model: DetailSections) -> None:
        """The mounts section (#945): one row per entry — the
        spec the operator wrote, the state the daemon derived,
        the reason a gate named or the honest dash — with the
        next-start note while a change waits."""
        self.pending_label.setVisible(model.pending_note is not None)
        if model.pending_note is not None:
            self.pending_label.setText(model.pending_note)
        self.mounts.setRowCount(len(model.mounts))
        for row, entry in enumerate(model.mounts):
            self.fill_mount_row(row, entry)
        self.mounts.setVisible(bool(model.mounts))
        self.empty_mounts.setVisible(not model.mounts)

    def fill_mount_row(self, row: int, entry) -> None:
        """One mount's five cells (#945): read-only, the entry's
        own fields in the columns' order."""
        values = (
            entry.host,
            entry.guest,
            entry.mode,
            entry.state,
            entry.reason or "-",
        )
        for column, value in enumerate(values):
            self.mounts.setItem(
                row,
                column,
                read_only_item(QtWidgets.QTableWidgetItem(value)),
            )

    def render_health(self, model: DetailSections) -> None:
        """The health section (#945): the doc's facts, one pair
        a line — a doc that has not landed reads as the muted
        stand-in."""
        if model.health:
            self.health.setText(
                "\n".join(f"{key}  {value}" for key, value in model.health)
            )
            self.health.setVisible(True)
            self.empty_health.setVisible(False)
        else:
            self.health.setVisible(False)
            self.empty_health.setVisible(True)

    def render_events(self, model: DetailSections) -> None:
        """The events section (#945): the recent frames' lines,
        newest last, the tail scrolled into view."""
        self.events.clear()
        for entry in model.events:
            self.events.addItem(entry.line)
        if model.events:
            self.events.scrollToBottom()
        self.events.setVisible(bool(model.events))
        self.empty_events.setVisible(not model.events)

    @QtCore.Slot(object)
    def push_frame(self, frame: dict) -> None:
        """One pushed frame into the buffer (#945's seam, #946's
        feed): the section re-renders from the buffer the moment
        a frame lands — the presenter's filter keeps the frames
        that name this workspace."""
        self.frames.append(frame)
        if len(self.frames) > FRAMES_BUFFER:
            del self.frames[: len(self.frames) - FRAMES_BUFFER]
        self.rebuild()

    @QtCore.Slot()
    def go_back(self) -> None:
        """The back action (#945): the page hands the window
        back to the list."""
        self.back.emit()

    def flash(self, message: str, *, outage: bool = False) -> None:
        """The status line's flash (#945, #904's contract): the
        message shows; a refusal clears itself after the flash
        span, an outage stands for the next answer to resolve.
        Each flash carries its own serial, so a later flash's
        message is not erased by an earlier flash's
        timeout."""
        self.flash_outage = outage
        self.flash_seq += 1
        self.status.setText(message)
        if outage:
            return
        seq = self.flash_seq
        QtCore.QTimer.singleShot(FLASH_MS, lambda: self.clear_stale(seq))

    def clear_stale(self, seq: int) -> None:
        """The timeout's slot: the line clears only when its own
        flash still holds it."""
        if seq == self.flash_seq:
            self.status.clear()

    def clear_flash(self) -> None:
        """The whole clear — the path a landing answer takes
        when it resolves the outage the flash held."""
        self.flash_seq += 1
        self.flash_outage = False
        self.status.clear()

    def showEvent(self, event: QtGui.QShowEvent) -> None:
        """The page's first refresh at open (#945), the tick's
        resume while the page owns the window, and the
        keyboard's anchor: the Back button takes the focus — a
        key event needs a focused child to reach the page's
        Escape shortcut."""
        self.back_button.setFocus()
        self.refresh()
        self.timer.start()
        super().showEvent(event)

    def hideEvent(self, event: QtGui.QHideEvent) -> None:
        """The tick stops with the page (#945): a hidden page's
        tick must not touch widgets."""
        self.timer.stop()
        super().hideEvent(event)


class WorkspaceShell(QtWidgets.QStackedWidget):
    """The window's two screens (#939, #945): the list and the
    detail page over one bridge — the list's opened row swaps to
    the page, the page's back returns — the stack's own hide and
    show starting and stopping each screen's tick."""

    def __init__(self, bridge: Bridge) -> None:
        super().__init__()
        self.bridge = bridge
        self.listing = WorkspaceList(bridge)
        self.listing.opened.connect(self.open_detail)
        self.detail = WorkspaceDetail(bridge)
        self.detail.back.connect(self.show_listing)
        self.detail.removed.connect(self.listing_removed)
        self.addWidget(self.listing)
        self.addWidget(self.detail)

    @QtCore.Slot(str)
    def open_detail(self, workspace_id: str) -> None:
        """The list opened a row (#945): the page takes the
        workspace, titled with the label the list holds for it
        until its own answer lands."""
        model = self.listing.rows_by_id.get(workspace_id)
        label = model.label if model else workspace_id
        self.detail.open(workspace_id, label)
        self.setCurrentWidget(self.detail)

    @QtCore.Slot()
    def show_listing(self) -> None:
        """The page returned (#945): the list owns the window
        again."""
        self.setCurrentWidget(self.listing)

    @QtCore.Slot(str)
    def listing_removed(self, workspace_id: str) -> None:
        """The page's workspace left the listing (#945, the
        TUI's close_removed shape): the list returns with the
        notice on its status line."""
        model = self.listing.rows_by_id.get(workspace_id)
        label = model.label if model else workspace_id
        self.listing.flash(f"{label} removed — closing its page")
        self.setCurrentWidget(self.listing)
