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
from .detail import DetailSections, detail_sections, frame_names
from .rows import WINDOW_TITLE, detail_text, workspace_row
from .shells import ShellHub, ShellTabs
from .termsession import shell_argv
from .workspaces import (
    FLASH_MS,
    REFRESH_MS,
    WorkspaceList,
    read_only_item,
    workspace_costs,
)

#: The row read's key (#945): the page answers only its own
#: key, whatever else shares the bridge.
ROW_KEY = "detail-row"

#: The health read's key (#945).
HEALTH_KEY = "detail-health"

#: The capacity report's key (#971): the footer strip's storage
#: fact, the list's own strip's read.
STORAGE_KEY = "detail-storage"

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
    """One workspace's page (#945, #954, #971): the tabbed body
    — the Info tab (the presenter's three sections: the mounts
    table with its next-start note, the health facts, the
    events list) with one tab per active shell beside it
    (#954) — over the footer strip, the same one-line read the
    listing's own bottom strip gives (#971). The workspace's
    name lives in the window title while the page shows. A
    failed refresh flashes the status line while the sections
    keep their content; a listing that can no longer see the
    workspace emits ``removed`` — the TUI page's own close
    (#309's close_removed shape) — and the back action returns
    to the list, closing no shell (#954)."""

    back = QtCore.Signal()
    removed = QtCore.Signal(str)
    shell_wanted = QtCore.Signal(str)

    def __init__(self, bridge: Bridge, hub: ShellHub) -> None:
        super().__init__()
        self.bridge = bridge
        self.workspace_id = ""
        self.row: dict | None = None
        self.health_doc: dict | None = None
        self.frames: list[dict] = []
        self.rendered: DetailSections | None = None
        self.row_pending = False
        self.health_pending = False
        self.storage_pending = False
        self.storage_costs: dict[str, int] = {}
        self.flash_seq = 0
        self.flash_outage = False
        # The timer first, the chrome right behind it: see the
        # note at build_chrome below for the mid-build events.
        self.timer = QtCore.QTimer(self)
        self.timer.setInterval(REFRESH_MS)
        self.timer.timeout.connect(self.refresh)
        bridge.succeeded.connect(self.on_succeeded)
        bridge.failed.connect(self.on_failed)
        # The chrome before the sections (#945): a show or hide
        # event Qt delivers during the build — a stack raising a
        # half-built child among them — meets whole chrome (the
        # timer above, the Back button's focus anchor here), not
        # a page still missing a piece its event handlers touch.
        self.build_chrome()
        # The footer strip (#971): the same one-line read the
        # listing's own bottom strip gives — it clips in place
        # rather than wrapping or widening the window, the
        # listing strip's own rule.
        self.footer = QtWidgets.QLabel("", self)
        self.footer.setTextInteractionFlags(
            QtCore.Qt.TextInteractionFlag.TextSelectableByMouse
        )
        self.footer.setSizePolicy(
            QtWidgets.QSizePolicy.Policy.Ignored,
            QtWidgets.QSizePolicy.Policy.Preferred,
        )
        sections = self.build_sections()
        scroll = QtWidgets.QScrollArea(self)
        scroll.setWidgetResizable(True)
        scroll.setWidget(sections)
        # The tabbed body (#954): the sections scroll is the
        # Info tab, and the workspace's shells host beside it —
        # the strip re-hosts on every open, and the hub keeps
        # the sessions themselves alive across visits.
        self.tabs = ShellTabs(hub, scroll)
        self.tabs.new_shell.connect(self.want_shell)
        layout = QtWidgets.QVBoxLayout(self)
        layout.addLayout(self.chrome)  # the top row, the list's own
        layout.addWidget(self.tabs, stretch=1)
        layout.addWidget(self.footer)
        self.setLayout(layout)
        escape = QtGui.QShortcut(
            QtGui.QKeySequence(QtCore.Qt.Key.Key_Escape), self
        )
        escape.activated.connect(self.go_back)

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
        """The page's controls at the window's top (#945, #971):
        back to the list — the theme's own left-pointing arrow,
        drawn large, its tooltip and accessible name carrying
        the word — and the status line a failed refresh flashes,
        the same top row the listing's own actions sit in. The
        page refreshes on its own timer (#971: no manual button);
        its new-shell action is the tab strip's "+" (#969)."""
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
        self.status = QtWidgets.QLabel("", self)
        self.chrome = QtWidgets.QHBoxLayout()
        self.chrome.addWidget(back_button)
        self.chrome.addWidget(self.status, stretch=1)

    @QtCore.Slot()
    def want_shell(self) -> None:
        """The page's shell action (#954, #969): one more ssh
        connection on the workspace the page shows, through the
        window shell's own open path — the tab strip's "+"
        asks for it and the strip takes the new tab."""
        if self.workspace_id:
            self.shell_wanted.emit(self.workspace_id)

    def open(self, workspace_id: str) -> None:
        """The page takes a workspace (#945, #954, #971): the
        state resets — fresh row, doc, frame buffer, rendered
        sections — the shell tabs re-host for this workspace,
        and the first refresh goes out. The page carries no
        header of its own; the window's title names the
        workspace, the shell's list label standing in until the
        row's own answer lands."""
        self.workspace_id = workspace_id
        self.row = None
        self.health_doc = None
        self.frames = []
        self.rendered = None
        self.footer.setText("")
        self.clear_sections()
        self.tabs.host(workspace_id)
        self.refresh()

    def clear_sections(self) -> None:
        """A workspace's opening blank (#945): the sections blank
        until the first answers land."""
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
        """One page refresh through the bridge (#945, #971) —
        the tick and the page's own open share it. A flight
        already out is not stacked, the listing's own guard
        (#470); the sections stand until the answers replace
        them."""
        if not self.row_pending:
            self.row_pending = True
            self.bridge.call(ROW_KEY, "workspaces")
        if not self.health_pending:
            self.health_pending = True
            self.bridge.call(HEALTH_KEY, "health")
        if not self.storage_pending:
            self.storage_pending = True
            self.bridge.call(STORAGE_KEY, "storage")

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
        elif key == STORAGE_KEY:
            self.storage_landed(result)

    def row_landed(self, rows: object) -> None:
        """The row read's answer (#945): this workspace's row
        out of the listing the read carries, the TUI page's own
        find. A listing without it emits ``removed``; a standing
        outage resolves with it."""
        self.row_pending = False
        self.resolve_outage()
        fresh = find_row(rows, self.workspace_id)
        if fresh is None:
            # The TUI's own-current guard (#309's close_removed):
            # an answer landing after Back hands the operator the
            # list already — the notice would name a close that
            # already happened.
            if not self.isHidden():
                self.removed.emit(self.workspace_id)
            return
        self.row = fresh
        self.rebuild()

    def health_landed(self, doc: object) -> None:
        """The health read's answer (#945): a doc lands as the
        section's standing content and resolves an outage.
        ``None`` is the read's best-effort silence (#717 —
        :func:`fetch_health` answers every failure with it), so
        under an outage the same tick that fails the row read
        answers the health read with it: both the standing doc
        and any outage flash keep."""
        self.health_pending = False
        if doc is None:
            return
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
        if key not in (ROW_KEY, HEALTH_KEY, STORAGE_KEY):
            return
        if key == ROW_KEY:
            self.row_pending = False
        elif key == HEALTH_KEY:
            self.health_pending = False
        else:
            self.storage_pending = False
        self.flash(str(failure), outage=failure.reach)

    def storage_landed(self, report: object) -> None:
        """The capacity report into the footer's cache (#971):
        the per-workspace cost, the list's own strip's read. A
        report that lands changes the footer's storage fact at
        the next render."""
        self.storage_pending = False
        self.storage_costs = workspace_costs(report or {})
        self.rebuild()

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
        # The footer reads every answer (#945, #971): the row's
        # own facts — the created age above all — move on a clock
        # the section models carry no part of, so the sections'
        # skip must not freeze the footer with them.
        self.render_footer(model)
        # The window's title reads every answer too (#971), but
        # only while the page owns the window: the shell feeds
        # the hidden page deliberately (frames keep landing after
        # Back), and a hidden page's answer must not re-name the
        # window the list holds — the same own-current guard the
        # removed-emit takes. A revisit names from the list
        # again, and the row's own answer refines it here.
        if not self.isHidden():
            self.window().setWindowTitle(f"{WINDOW_TITLE} — {model.label}")
        if model == self.rendered:
            return
        self.rendered = model
        self.render_sections(model)

    def render_sections(self, model: DetailSections) -> None:
        """The sections as the model reads (#945): each section
        its own widgets — the footer renders on every answer,
        in :meth:`rebuild`, outside this skip-guarded path."""
        self.render_mounts(model)
        self.render_health(model)
        self.render_events(model)

    def render_footer(self, model: DetailSections) -> None:
        """The footer strip (#971): the same one-line read the
        listing's own bottom strip gives — the identity, the
        posture and costs, the storage the capacity report
        charges, an honest dash until one lands."""
        self.footer.setText(
            detail_text(
                workspace_row(self.row),
                self.storage_costs.get(self.workspace_id),
            )
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
        self.mounts.resizeColumnsToContents()
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
        feed): the buffer keeps only the frames that name this
        workspace — the feed is a daemon-wide subscriber, and
        busy siblings would otherwise evict this page's history
        out of the cap — and the section re-renders the moment
        one lands."""
        data = frame.get("data") or {}
        if not frame_names(data, self.workspace_id):
            return
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
        # The page is the timeout's receiver (#959): the
        # pending callback is severed when the widget's C++
        # object goes, instead of firing later on a wrapper
        # whose widgets are already destroyed.
        QtCore.QTimer.singleShot(FLASH_MS, self, lambda: self.clear_stale(seq))

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
        Escape shortcut. A wrapper without its Back button
        does none of the three (#959): Qt can deliver events to
        a page whose construction never finished, and to the
        empty wrapper PySide6 raises at teardown for a C++
        object that outlived its Python one."""
        if getattr(self, "back_button", None) is not None:
            self.back_button.setFocus()
            self.refresh()
            self.timer.start()
        super().showEvent(event)

    def hideEvent(self, event: QtGui.QHideEvent) -> None:
        """The tick stops with the page (#945): a hidden page's
        tick must not touch widgets. A wrapper without its
        ``timer`` (#959) has no tick to stop."""
        timer = getattr(self, "timer", None)
        if timer is not None:
            timer.stop()
        super().hideEvent(event)


class WorkspaceShell(QtWidgets.QStackedWidget):
    """The window's two screens (#939, #945, #954): the list
    and the detail page over one bridge — the list's opened row
    swaps to the page, the page's back returns — the stack's own
    hide and show starting and stopping each screen's tick. The
    shell hub (#954) lives here: the terminals outlive the
    detail page, and the quit path closes every session."""

    def __init__(
        self,
        bridge: Bridge,
        terminal_cmd: list[str] | None = None,
        hub: ShellHub | None = None,
    ) -> None:
        super().__init__()
        self.bridge = bridge
        self.terminal_cmd = terminal_cmd
        self.hub = hub if hub is not None else ShellHub()
        self.listing = WorkspaceList(bridge)
        self.listing.opened.connect(self.open_detail)
        self.listing.shell_requested.connect(self.open_shell)
        self.listing.external_requested.connect(self.open_external)
        self.detail = WorkspaceDetail(bridge, self.hub)
        self.detail.back.connect(self.show_listing)
        self.detail.removed.connect(self.listing_removed)
        self.detail.shell_wanted.connect(self.open_shell)
        bridge.landed.connect(self.frame_landed)
        bridge.feed_dropped.connect(self.feed_dropped)
        bridge.feed_reconnected.connect(self.feed_back)
        # The quit path closes every shell (#954): the window's
        # children never see a close event of their own, and a
        # session left open would outlive the loop that reads
        # its master.
        app = QtWidgets.QApplication.instance()
        if app is not None:
            app.aboutToQuit.connect(self.hub.shutdown)
        self.addWidget(self.listing)
        self.addWidget(self.detail)

    @QtCore.Slot(str)
    def open_shell(self, workspace_id: str) -> None:
        """A shell action named a workspace (#954): the page
        takes the workspace if it has not already, then one more
        ssh connection opens — the strip beside the page's Info
        tab takes the new tab and the focus."""
        if self.detail.workspace_id != workspace_id or (
            self.currentWidget() is not self.detail
        ):
            self.open_detail(workspace_id)
        self.hub.open_shell(workspace_id)

    @QtCore.Slot(str)
    def open_external(self, workspace_id: str) -> None:
        """The external-terminal action (#954): the same shell
        the TUI's windowed shell action opens — the configured
        launcher with the ssh argv appended — detached, beside
        the running window. Without a launcher the notice names
        the setting (``terminal_open_cmd``); the GUI has no
        terminal of its own to hand the session."""
        if not self.terminal_cmd:
            self.listing.flash(
                "set terminal_open_cmd to open external terminals"
            )
            return
        argv = [*self.terminal_cmd, *shell_argv(workspace_id)]
        ok = QtCore.QProcess.startDetached(argv[0], argv[1:])
        label = self.detail_label(workspace_id)
        if ok:
            self.listing.flash(f"opened an external terminal — {label}")
        else:
            self.listing.flash(f"the terminal launcher failed — {label}")

    def detail_label(self, workspace_id: str) -> str:
        """The workspace's label for a flash, the page's own
        stand-in when the list holds none."""
        model = self.listing.rows_by_id.get(workspace_id)
        return model.label if model else workspace_id

    @QtCore.Slot(str)
    def open_detail(self, workspace_id: str) -> None:
        """The list opened a row (#945, #971): the page takes the
        workspace — its name in the window title, the label the
        list holds standing in until the row's own answer — and
        the page carries no header of its own."""
        model = self.listing.rows_by_id.get(workspace_id)
        label = model.label if model else workspace_id
        self.detail.open(workspace_id)
        self.name_window(label)  # the row's own answer refines it
        self.setCurrentWidget(self.detail)

    def name_window(self, label: str) -> None:
        """The window names the workspace on show (#971): the
        page's header is gone, and the window's own title bar is
        where the operator reads which workspace stands."""
        self.window().setWindowTitle(f"{WINDOW_TITLE} — {label}")

    def unname_window(self) -> None:
        """The window back to the plain title (#971): the list
        owns the window again."""
        self.window().setWindowTitle(WINDOW_TITLE)

    @QtCore.Slot()
    def show_listing(self) -> None:
        """The page returned (#945, #971): the list owns the
        window again, its title with it."""
        self.unname_window()
        self.setCurrentWidget(self.listing)

    @QtCore.Slot(object)
    def frame_landed(self, frame: dict) -> None:
        """One pushed frame (#946): the page buffers it whatever
        screen owns the window — its events section is the
        frame's own record — and the screen the operator sees
        refreshes at once, through the same path its timer
        drives; the hidden screen refreshes at its next show.
        The tick stays the backstop the feed makes immediate."""
        self.detail.push_frame(frame)
        self.currentWidget().refresh()

    @QtCore.Slot(str)
    def feed_dropped(self, reason: str) -> None:
        """The events feed's drop (#946): the screen the operator
        sees flashes it, each retry re-telling — the outage's own
        heartbeat while it lasts. The flash runs its own span,
        not the screens' outage flag: that flag belongs to their
        refresh answers (#904), and a REST answer arriving over
        a still-down feed would otherwise read as the feed's
        recovery."""
        screen = self.currentWidget()
        screen.flash(f"the events feed dropped — {reason}")

    @QtCore.Slot()
    def feed_back(self) -> None:
        """The feed's recovery (#946): one flash says the frames
        flow again; the refresh that follows is the feed's
        own."""
        screen = self.currentWidget()
        screen.flash("the events feed reconnected")

    @QtCore.Slot(str)
    def listing_removed(self, workspace_id: str) -> None:
        """The page's workspace left the listing (#945, the
        TUI's close_removed shape): the list returns with the
        notice on its status line."""
        model = self.listing.rows_by_id.get(workspace_id)
        label = model.label if model else workspace_id
        self.listing.flash(f"{label} removed — closing its page")
        self.unname_window()
        self.setCurrentWidget(self.listing)
