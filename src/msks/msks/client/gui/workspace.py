"""The workspace detail screen (phase 4 of #924, #945): the
page the workspace list opens on one row — the tab strip's
surfaces (the shells, the editable mounts, the proxies, the
eegress posture, the events window) refreshed on the listing's
own cadence through the bridge: the row out of the workspaces
listing, the footer's storage fact out of the capacity report.
The Info
pane the page once carried beside the shells is gone (#1017):
its mounts table read the same facts the Mounts tab edits
(#987), its events window lives in the Events tab (#989), and
its health facts — the daemon's own, not the workspace's —
went with it by named decision: the machine fact already
reads through the images tab's own gathered health fetch, and
the page's own reads tell the daemon's reachability (a
refused read flashes, an outage stands for an answer), so the
page asks the daemon for no health doc at all.

This module imports PySide6 at module scope — the same
contract as the list screen's module
(:mod:`msks.client.gui.workspaces`): nothing imports it at CLI
parse time, and ``.app``'s window path imports it lazily after
its guards (#927), the reachability the gate counts.
"""

from PySide6 import QtCore, QtGui, QtWidgets

from ..bridge import GAVE_UP, became_running, bridge_serving, holder_lost
from ..data import RestFailure
from ..frames import listing_frame, row_frame
from .bridge import Bridge
from .consent import ConsentPresenter
from .create import CreateDialog
from .detail import frame_names
from .egress import EgressTab
from .events import EventsTab
from .mounts import MountsTab
from .proxies import ProxiesTab
from .rows import (
    WINDOW_TITLE,
    WorkspaceRow,
    detail_text,
    workspace_costs,
    workspace_row,
)
from .shells import ShellHub, ShellsTab
from .tabstyle import mark_active
from .termsession import shell_argv
from .workspaces import FLASH_MS, WorkspaceList

#: The row read's key (#945): the page answers only its own
#: key, whatever else shares the bridge.
ROW_KEY = "detail-row"

#: The capacity report's key (#971): the footer strip's storage
#: fact, the list's own strip's read.
STORAGE_KEY = "detail-storage"

#: The back arrow's size, pixels (#945): the theme's own
#: left-pointing arrow drawn large against the row's text —
#: the window's fonts run one and a half times the platform
#: default (#939), and a small glyph reads as decoration, not
#: a control.
BACK_ICON_PX = 24

#: The Shells tab's label (#974): the page's default tab, the
#: workspace's terminal sessions as subtabs.
SHELLS_LABEL = "Shells"

#: The Mounts tab's label (#987): the workspace's editable
#: mounts surface.
MOUNTS_LABEL = "Mounts"

#: The Proxies tab's label (#988): the workspace's forward and
#: remote proxies with their live status.
PROXIES_LABEL = "Proxies"

#: The Events tab's label (#989): the page's recent-frames
#: window.
EVENTS_LABEL = "Events"

#: The re-hold rows read's key prefix (#1127): the shell's
#: flights on the bridge, one workspace per key — the answer
#: feeds the policy's spawn gate.
REHOLD_KEY = "rehold:"

#: The re-hold deferrals' poll, milliseconds (#1127): the
#: failure frames are edge-triggered, so a signal the window
#: swallowed waits as a mark — the shell's timer re-rules each
#: one whose window closed, exactly as a fresh signal would.
#: The TUI's own 1s tick drives its drain; the GUI carries this
#: timer because nothing else in it polls.
REHOLD_POLL_MS = 5000

#: The Egress tab's label (#1042): the workspace's egress
#: posture and the in-place mode switch (#1049).
EGRESS_LABEL = "Egress"

#: The page's recovery flash (#904, #1129 — the TUI listing's
#: words for its own screen): the first answer after an outage
#: names the recovery once, and the flash clears with its span.
RECOVERY_TEXT = "daemon back — page resumed"


def find_row(rows: list[dict] | None, workspace_id: str) -> dict | None:
    """This workspace's row out of a listing (#945): the same
    find the TUI's page makes over the listing the read
    carries."""
    return next(
        (row for row in rows or [] if row.get("id") == workspace_id),
        None,
    )


class WorkspaceDetail(QtWidgets.QWidget):
    """One workspace's page (#945, #954, #971, #974): the tab
    row beside the back arrow — Shells, the default (#974),
    with the workspace's terminal sessions as subtabs (#954),
    Mounts (#987), Proxies (#988), Egress (#1042, the posture
    surface with its in-place mode switch, #1049), and Events
    (#989), the Info pane gone with its duplicated sections
    (#1017), over the
    footer strip, the same one-line read the listing's own
    bottom strip gives (#971). The page registers as the
    workspace's egress decider while it shows (#999): the
    bridge's link claims the workspace's holds at the open,
    the registration retiring at every hide (a page nobody
    sees holds flows nobody answers) and re-claiming at the
    next show. The consent presenter (#1000) stands behind the
    registration: the link's frames land on the presenter's
    controller, the seam the consent panel the phase's next
    work docks renders. The workspace's name lives in
    the window title while the page shows. A failed refresh
    flashes the status line while the tabs keep their content;
    a listing that can no longer see the workspace emits
    ``removed`` — the TUI page's own close (#309's
    close_removed shape) — and the back action returns to the
    list, closing no shell (#954)."""

    back = QtCore.Signal()
    removed = QtCore.Signal(str)
    shell_wanted = QtCore.Signal(str)
    external_wanted = QtCore.Signal(str)

    def __init__(self, bridge: Bridge, hub: ShellHub) -> None:
        super().__init__()
        self.bridge = bridge
        self.hub = hub
        self.workspace_id = ""
        # The consent presenter (#1000): built at every open,
        # the decider's state and verdict seam — the panel the
        # phase's next work docks renders its rows. Set before
        # the chrome: a mid-build event meets it standing.
        self.consent: ConsentPresenter | None = None
        self.row: dict | None = None
        self.row_pending = False
        self.storage_pending = False
        self.refresh_due = False
        self.storage_costs: dict[str, int] = {}
        self.flash_seq = 0
        self.flash_outage = False
        bridge.succeeded.connect(self.on_succeeded)
        bridge.failed.connect(self.on_failed)
        # The chrome before the rest (#945): a show or hide
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
        # The tab row beside the back arrow (#974, #987): Shells
        # — the default, the workspace's terminal sessions as
        # subtabs — Mounts — the editable mounts surface —
        # Proxies, Egress — the workspace's egress posture
        # (#1042) — and Events. The bar sits on the chrome's own
        # row after the arrow and drives the stack below it. The
        # strip re-hosts on every open, and the hub keeps the
        # sessions themselves alive across visits (#954).
        self.shells_tab = ShellsTab(hub, bridge)
        self.shells = self.shells_tab.shells
        self.shells.new_shell.connect(self.want_shell)
        self.mounts_tab = MountsTab(bridge)
        self.mounts_tab.row_updated.connect(self.mounts_landed)
        self.proxies_tab = ProxiesTab(bridge)
        self.events_tab = EventsTab()
        self.egress_tab = EgressTab(bridge)
        self.bar = QtWidgets.QTabBar(self)
        # The active tab's mark (#1012): the strip paints its
        # current tab with the accent tint the main strip
        # carries too, so the page the operator stands on reads
        # at a glance on both levels.
        mark_active(self.bar)
        self.bar.addTab(SHELLS_LABEL)
        self.bar.addTab(MOUNTS_LABEL)
        self.bar.addTab(PROXIES_LABEL)
        self.bar.addTab(EGRESS_LABEL)
        self.bar.addTab(EVENTS_LABEL)
        self.stack = QtWidgets.QStackedWidget(self)
        self.stack.addWidget(self.shells_tab)
        self.stack.addWidget(self.mounts_tab)
        self.stack.addWidget(self.proxies_tab)
        self.stack.addWidget(self.egress_tab)
        self.stack.addWidget(self.events_tab)
        self.bar.currentChanged.connect(self.stack.setCurrentIndex)
        self.chrome.insertWidget(1, self.bar)
        layout = QtWidgets.QVBoxLayout(self)
        layout.addLayout(self.chrome)  # the arrow's own row
        layout.addWidget(self.stack, stretch=1)
        layout.addWidget(self.footer)
        self.setLayout(layout)
        escape = QtGui.QShortcut(
            QtGui.QKeySequence(QtCore.Qt.Key.Key_Escape), self
        )
        escape.activated.connect(self.go_back)

    def build_chrome(self) -> None:
        """The page's controls at the window's top (#945, #971,
        #974): back to the list — the theme's own left-pointing
        arrow, drawn large, its tooltip and accessible name
        carrying the word — then the tab strip, and the status
        line a failed refresh flashes filling the row's rest.
        The page refreshes on its open and on the row-affecting
        frames the events feed pushes (#971's manual button
        stays gone; #1019 retired the standing tick); its
        new-shell action is the tab strip's "+"
        (#969)."""
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
        # The external-terminal action's home on the page (#972,
        # #954): the same shell the tab strip runs, opened in
        # the configured launcher outside the window.
        external = QtWidgets.QToolButton(self)
        external.setIcon(
            self.style().standardIcon(
                QtWidgets.QStyle.StandardPixmap.SP_ComputerIcon
            )
        )
        external.setToolTip(
            "Open a shell in an external terminal (terminal_open_cmd)"
        )
        external.clicked.connect(self.want_external)
        self.external_button = external
        self.chrome = QtWidgets.QHBoxLayout()
        self.chrome.addWidget(back_button)
        self.chrome.addWidget(self.status, stretch=1)
        self.chrome.addWidget(external)

    @QtCore.Slot()
    def want_shell(self) -> None:
        """The page's shell action (#954, #969): one more ssh
        connection on the workspace the page shows, through the
        window shell's own open path — the tab strip's "+"
        asks for it and the strip takes the new tab."""
        if self.workspace_id:
            self.shell_wanted.emit(self.workspace_id)

    @QtCore.Slot()
    def want_external(self) -> None:
        """The external-terminal action (#954, #972): the page's
        workspace opens its shell in the configured launcher
        outside the window."""
        if self.workspace_id:
            self.external_wanted.emit(self.workspace_id)

    def open(self, workspace_id: str) -> None:
        """The page takes a workspace (#945, #954, #971): the
        state resets — fresh row, footer, and tabs — the
        shell tabs re-host for this workspace, and the first
        refresh goes out. The page carries no header of its
        own; the window's title names the workspace, the
        shell's list label standing in until the row's own
        answer lands.

        The open takes a first shell when none stands (#974):
        the Shells tab opens with a terminal to type in, and
        its subtab takes the focus."""
        self.workspace_id = workspace_id
        self.row = None
        self.footer.setText("")
        # The row resets with every open (#974): Shells is the
        # page's default tab, and a prior visit's tab must not
        # hand the next workspace a hidden terminal holding the
        # keyboard.
        self.bar.setCurrentIndex(0)
        # Host before registration: even an immediate frame must reach the
        # panel's current presenter. Terminals remain owned by the hub.
        self.consent = ConsentPresenter(workspace_id, self.bridge)
        self.shells_tab.host(workspace_id, self.consent)
        if not self.hub.shells(workspace_id):
            self.hub.open_shell(workspace_id)
        self.bridge.start_decider(workspace_id, self.consent.controller)
        # The Mounts tab blanks with the page's own reset (#987):
        # a flight the prior workspace still has out cancels, and
        # the first row answer fills the tab. The Proxies tab
        # takes the switch the same way (#988).
        self.mounts_tab.host(workspace_id, {})
        self.proxies_tab.host(workspace_id)
        self.egress_tab.host(workspace_id)
        self.events_tab.clear()
        self.refresh_standing()

    @QtCore.Slot()
    def refresh(self) -> None:
        """One page refresh through the bridge (#945, #971,
        #1019) — the row-affecting pushed frames and the feed's
        serve poke share it. A read already out carries the ask
        (#470's one-flight rule, #1129's carry): the landing
        takes the read the guard refused, so a frame or a poke
        landing mid-flight still gets its read — the TUI
        listing's own arm (#1019). The tabs stand until the
        answers replace them."""
        if self.row_pending or self.storage_pending:
            self.refresh_due = True
        self.read_page()

    def refresh_standing(self) -> None:
        """The open's and the show's own read (#945, #1019): a
        read already out stands for it. The row read is a whole
        listing, so a flight issued before the open lands on the
        opened workspace the same, and the open and the show a
        stacked widget's switch puts back to back add no second
        pair of reads. Only a frame's or a poke's refresh
        carries (#1129): those re-read for a change an older
        flight cannot hold. The tabs take their pokes either
        way: at a show before the open their host was still
        empty, and their own guards skip a read already out."""
        if self.row_pending or self.storage_pending:
            self.poke_tabs()
            return
        self.read_page()

    def read_page(self) -> None:
        """The page's reads themselves: each goes out unless its
        own flight stands (#470), and the Proxies and Egress
        tabs ride the page's refresh (#988, #1042)."""
        if not self.row_pending:
            self.row_pending = True
            self.bridge.call(ROW_KEY, "workspaces")
        if not self.storage_pending:
            self.storage_pending = True
            self.bridge.call(STORAGE_KEY, "storage")
        self.poke_tabs()

    def poke_tabs(self) -> None:
        """The Proxies and Egress tabs' reads (#988, #1042):
        their listings are their own, refreshed beside the row,
        and each skips a read of its own already out."""
        self.proxies_tab.poke()
        self.egress_tab.poke()

    def serve_due(self) -> None:
        """The refresh a standing read deferred (#1129): served
        the moment no read stands — one more read, the TUI
        listing's own arm (#1019), so a serve poke or a frame
        landing mid-flight still gets its read."""
        if not self.refresh_due or self.row_pending or self.storage_pending:
            return
        self.refresh_due = False
        self.refresh()

    @QtCore.Slot(str, object)
    def on_succeeded(self, key: str, result: object) -> None:
        """One answered call (#945): the row read finds this
        workspace's row — a listing that cannot see it emits
        ``removed`` — and the capacity report keeps the footer's
        storage fact. Another key's answer is not this page's
        to take."""
        if key == ROW_KEY:
            self.row_landed(result)
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
            # already happened. The page leaves with it: a
            # deferred refresh has nothing left to read.
            self.refresh_due = False
            if not self.isHidden():
                self.removed.emit(self.workspace_id)
            return
        self.row = fresh
        self.rebuild()
        self.serve_due()

    @QtCore.Slot(str, object)
    def on_failed(self, key: str, failure: RestFailure) -> None:
        """One refused call (#945): the failure flashes — an
        outage (the daemon answered nothing) stands until an
        answer resolves it — and the tabs keep the content they
        have. Another key's failure is not this page's to
        show."""
        if key not in (ROW_KEY, STORAGE_KEY):
            return
        if key == ROW_KEY:
            self.row_pending = False
        else:
            self.storage_pending = False
        self.flash(str(failure), outage=failure.reach)
        self.serve_due()

    def storage_landed(self, report: object) -> None:
        """The capacity report into the footer's cache (#971):
        the per-workspace cost, the list's own strip's read. A
        report that lands changes the footer's storage fact at
        the next render and resolves a standing outage — an
        answer from the daemon proves the reachability the
        flash held, the contract the health read carried before
        the read went with the Info pane (#1017)."""
        self.storage_pending = False
        self.resolve_outage()
        self.storage_costs = workspace_costs(report or {})
        self.rebuild()
        self.serve_due()

    def resolve_outage(self) -> None:
        """A standing outage's resolve (#945, #904's contract):
        an answer landed, and the recovery flashes once in the
        outage's place (#1129) — the tabs keep the content they
        have."""
        if self.flash_outage:
            self.flash(RECOVERY_TEXT)

    def rebuild(self) -> None:
        """The page from its standing facts (#945, #1017): the
        footer strip, the window's title, and the Mounts tab's
        hosting over the row — every answer renders them, none
        behind a skip guard, the sections the guard once held
        being gone with the Info pane. A row that has not
        landed renders nothing: there is nothing to render
        against yet."""
        if self.row is None:
            return
        model = workspace_row(self.row)
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
        # The Mounts tab takes the row with every render (#987):
        # the page's own read is the tab's standing truth between
        # changes.
        self.mounts_tab.host(self.workspace_id, self.row)

    def render_footer(self, model: WorkspaceRow) -> None:
        """The footer strip (#971): the same one-line read the
        listing's own bottom strip gives — the identity, the
        posture and costs, the storage the capacity report
        charges, an honest dash until one lands."""
        self.footer.setText(
            detail_text(
                model,
                self.storage_costs.get(self.workspace_id),
            )
        )

    @QtCore.Slot(object)
    def push_frame(self, frame: dict) -> None:
        """One pushed frame to the page's surfaces (#945's
        seam, #946's feed, #1019's refresh). A row-affecting
        frame re-reads the row — the standing tick the frames
        replace — under the listing's own match: the lifecycle
        and volume families naming this workspace, or a
        catalog move (``image.changed``), which names none and
        moves every row's image cell. A frame that names this
        workspace reaches the page's tabs — the feed is a
        daemon-wide subscriber, and a busy sibling's frames
        would otherwise land on this page's tabs — where a
        proxy transition repaints the Proxies tab (#988), the
        frame appends to the Events tab (#989), and a rules
        frame repaints the Egress tab (#1042)."""
        if row_frame(frame, self.workspace_id):
            self.refresh()
        data = frame.get("data") or {}
        if not frame_names(data, self.workspace_id):
            return
        self.proxies_tab.frame(frame)
        self.events_tab.push(frame)
        self.egress_tab.push(frame)
        self.rebuild()

    @QtCore.Slot(object)
    def mounts_landed(self, row: dict) -> None:
        """A landed mounts save (#987): the tab's answer is a
        fresh daemon row — the page takes it whole and
        re-renders, the footer reading the same facts the tab
        painted."""
        self.row = row
        self.rebuild()

    @QtCore.Slot()
    def go_back(self) -> None:
        """The back action (#945): the page hands the window
        back to the list."""
        self.back.emit()

    def anchor_focus(self) -> None:
        """Where a showing page's keyboard lands (#974): the
        current shell's terminal when the Shells tab shows one,
        the Back button otherwise — the Escape anchor the
        page's own shortcut needs. The strip's own focus paths
        (a new shell, a rename) take their own anchors."""
        if self.stack.currentWidget() is self.shells_tab:
            widget = self.shells.currentWidget()
            if widget is not None and widget is not self.shells.plus_page:
                widget.setFocus()
                return
        self.back_button.setFocus()

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

    def showEvent(self, event: QtGui.QShowEvent) -> None:
        """The page's first refresh at open (#945, #1019: the
        standing tick is gone — the row re-reads on the
        row-affecting frames the events feed pushes and on the
        feed's serve poke) and the keyboard's anchor: the Back
        button takes the focus — a key event needs a focused
        child to reach the page's Escape shortcut. A wrapper
        without its Back button does none of it (#959): Qt can
        deliver events to a page whose construction never
        finished, and to the empty wrapper PySide6 raises at
        teardown for a C++ object that outlived its Python one.
        The decider registration returns with the show (#999):
        the hide retired it, and the workspace's holds wait for
        a decider only while one watches — the page showing
        again is the watch resuming, whatever hid it (Back's
        return, a tab switch back to Workspaces)."""
        if getattr(self, "back_button", None) is not None:
            self.anchor_focus()
            self.refresh_standing()
            if self.workspace_id and self.consent is not None:
                self.bridge.start_decider(
                    self.workspace_id, self.consent.controller
                )
        super().showEvent(event)

    def hideEvent(self, event: QtGui.QHideEvent) -> None:
        """The decider registration retires with the page
        (#999): the daemon holds flows only while a decider
        watches, and a page nobody sees must not hold them —
        the bridge's cancel closes the link's socket, the
        daemon's own retire. The stack's own addWidget hides a
        page that never opened — no claim stands to retire —
        so the retire asks only while a workspace does."""
        if self.workspace_id:
            self.bridge.stop_decider()
        super().hideEvent(event)


class WorkspaceShell(QtWidgets.QStackedWidget):
    """The window's two screens (#939, #945, #954): the list
    and the detail page over one bridge — the list's opened row
    swaps to the page, the page's back returns. The shell hub
    (#954) lives here: the terminals outlive the detail page,
    and the quit path closes every session."""

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
        # The listing's Add button (#1011): the create ask lands
        # here — the shell owns the bridge, so the create dialog
        # is the shell's own surface.
        self.listing.create_requested.connect(self.open_create)
        self.detail = WorkspaceDetail(bridge, self.hub)
        self.detail.back.connect(self.show_listing)
        self.detail.removed.connect(self.listing_removed)
        self.detail.shell_wanted.connect(self.open_shell)
        self.detail.external_wanted.connect(self.open_external)
        bridge.landed.connect(self.frame_landed)
        bridge.succeeded.connect(self.rehold_rows_landed)
        bridge.feed_dropped.connect(self.feed_dropped)
        bridge.feed_reconnected.connect(self.feed_back)
        bridge.feed_served.connect(self.feed_served)
        bridge.decider_rejected.connect(self.decider_refused)
        # The re-hold deferrals' poll (#1127): parented to the
        # shell, so the widget's teardown stops it with it.
        self.rehold_timer = QtCore.QTimer(self)
        self.rehold_timer.timeout.connect(self.drain_rehold)
        self.rehold_timer.start(REHOLD_POLL_MS)
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
        """A shell action named a workspace (#954, #974): the
        page takes the workspace if it has not already, then
        one more ssh connection opens — the Shells tab's strip
        takes the new subtab and the focus. The page's own open
        creates the first shell of a workspace with none
        standing, so a fresh open's action answers itself."""
        shows = (
            self.detail.workspace_id == workspace_id
            and self.currentWidget() is self.detail
        )
        if shows:
            self.hub.open_shell(workspace_id)
        elif self.hub.shells(workspace_id):
            self.open_detail(workspace_id)
            self.hub.open_shell(workspace_id)
        else:
            self.open_detail(workspace_id)

    @QtCore.Slot(str)
    def open_external(self, workspace_id: str) -> None:
        """The external-terminal action (#954, #972): the same
        shell the TUI's windowed shell action opens — the
        configured launcher with the ssh argv appended —
        detached, beside the running window. The screen the
        operator sees carries the notice (#972): the action
        lives on the detail page now. Without a launcher the
        notice names the setting (``terminal_open_cmd``); the
        GUI has no terminal of its own to hand the session."""
        screen = self.currentWidget()
        if not self.terminal_cmd:
            screen.flash("set terminal_open_cmd to open external terminals")
            return
        argv = [*self.terminal_cmd, *shell_argv(workspace_id)]
        ok = QtCore.QProcess.startDetached(argv[0], argv[1:])
        label = self.detail_label(workspace_id)
        if ok:
            screen.flash(f"opened an external terminal — {label}")
        else:
            screen.flash(f"the terminal launcher failed — {label}")

    def detail_label(self, workspace_id: str) -> str:
        """The workspace's label for a flash, the page's own
        stand-in when the list holds none."""
        model = self.listing.rows_by_id.get(workspace_id)
        return model.label if model else workspace_id

    @QtCore.Slot()
    def open_create(self) -> None:
        """The listing's Add button (#1011, the plus badge's
        action until the badge left the tab strip, #962): the
        create dialog atop the workspaces — the TUI's create
        popup's counterpart (#309) — a landed create refreshing
        the listing behind it; a cancel or a refusal decides
        nothing."""
        dialog = CreateDialog(self.bridge, self)
        dialog.accepted.connect(self.listing.refresh)
        dialog.exec()

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
        """One pushed frame (#946, #1019): the page takes it
        whatever screen owns the window — its own row filter
        decides the re-read, and its events tab is the frame's
        own record — and a listing-affecting frame refreshes
        the listing while it owns the window: the listing
        re-reads on the frames the daemon publishes, not on a
        standing tick, the TUI listing's own rule. A hidden
        screen re-reads at its next show. A bridge-relevant
        frame crosses to the re-hold watch first (#1127)."""
        self.bridge_frame(frame)
        self.detail.push_frame(frame)
        if listing_frame(frame) and self.currentWidget() is self.listing:
            self.listing.refresh()

    # -- the bridge re-hold watch (#1127) --------------------------------

    def bridge_frame(self, frame: dict) -> None:
        """One bridge-relevant frame to the shell's re-hold watch
        (#1127): a listener that answers a spawn this window's
        session made says the recovery once — the toast lands on
        the screen the operator sees — and a failure signal (the
        holder died, or a running start left none) goes to the
        policy for its ruling: a weigh is one rows read over the
        bridge, and the give-up line names the remedy the row's
        cause already carries."""
        serving = bridge_serving(frame)
        if serving is not None:
            self.note_reheld(serving)
            return
        lost = holder_lost(frame) or became_running(frame)
        if lost is not None:
            self.weigh_bridge_signal(lost)

    def note_reheld(self, workspace_id: str) -> None:
        """A listener came up (#1127): the one that answers a
        spawn this session made says the recovery once, on the
        screen the operator sees."""
        if self.bridge.reholder.serving(workspace_id):
            self.currentWidget().flash(
                f"bridge re-held — {self.detail_label(workspace_id)}"
            )

    def weigh_bridge_signal(self, workspace_id: str) -> None:
        """One failure signal's ruling (#1127), landed: the
        shared path decides the give-up line or the rows read."""
        ruling = self.bridge.reholder.signal(workspace_id)
        self.land_ruling(workspace_id, ruling)

    def land_ruling(self, workspace_id: str, ruling: str | None) -> None:
        """One ruling's effect (#1127): the give-up line names
        the remedy the row's cause already carries, and a weigh
        is one rows read over the bridge."""
        if ruling is None:
            return
        if ruling == GAVE_UP:
            self.currentWidget().flash(
                f"bridge not re-held — {self.detail_label(workspace_id)}; "
                "the row's cause names 'msks proxy hold'"
            )
            return
        self.bridge.call(
            REHOLD_KEY + workspace_id, "workspace_proxies", workspace_id
        )

    @QtCore.Slot()
    def drain_rehold(self) -> None:
        """The re-hold deferrals' drain (#1127): the failure
        frames are edge-triggered, so a signal the window
        swallowed waits as a mark — the poll re-rules each one
        whose window closed, exactly as a fresh signal would."""
        for workspace_id, ruling in self.bridge.reholder.take_due():
            self.land_ruling(workspace_id, ruling)

    @QtCore.Slot(str, object)
    def rehold_rows_landed(self, key: str, result: object) -> None:
        """One re-hold rows read's answer (#1127): the refreshed
        rows gate the spawn — a listening row means a holder
        stands and a failed row with another cause means a
        holder cannot serve it. Every other key belongs to the
        screens' own flights and decides nothing here. A refusal
        never lands on this slot (the bridge's ``failed`` signal
        carries it): the window stands, the row's cause stays
        visible, and the next signal weighs again."""
        if not key.startswith(REHOLD_KEY):
            return
        self.bridge.reholder.rehold(key[len(REHOLD_KEY) :], result or [])

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
        flow again; the re-read is the serve poke's own — every
        connection that comes to serve refreshes the screen the
        operator sees, because frames published while no
        connection stood never landed."""
        screen = self.currentWidget()
        screen.flash("the events feed reconnected")

    @QtCore.Slot()
    def feed_served(self) -> None:
        """A feed connection serves (#1019): the screen the
        operator sees re-reads — first connection included, not
        only reconnects — because frames published before the
        connection stood never landed. The screens' own opening
        reads and this re-read bracket the startup race: a
        frame published between a REST answer and the
        server-side subscribe misses the socket but shows in
        the re-read, whose REST answer postdates the
        subscribe. The poke fires on the connection itself,
        before any frame (#1129): a window opened while the
        daemon was down gets its listing re-read when the
        feed's dial finally connects, with no frame to wait
        for on an idle daemon."""
        self.currentWidget().refresh()

    @QtCore.Slot(str)
    def decider_refused(self, reason: str) -> None:
        """The daemon refused the page's decider registration
        (#999): the reason flashes on the screen the operator
        sees — a workspace the daemon cannot resolve stays that
        way, so the link ended itself and one flash is the
        whole tell. The consent panel takes the surface over
        when the phase's later work docks it."""
        screen = self.currentWidget()
        screen.flash(f"the decider registration was refused — {reason}")

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
