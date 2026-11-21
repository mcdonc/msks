"""The workspace detail screen (phase 4 of #924, #945): the
page the workspace list opens on one row — the tab strip's
surfaces (the shells, the editable mounts, the proxies, the
events window) refreshed on the listing's own cadence through
the bridge: the row out of the workspaces listing, the
footer's storage fact out of the capacity report. The Info
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

from ..data import RestFailure
from .bridge import Bridge
from .consent import ConsentPresenter
from .create import CreateDialog
from .detail import frame_names
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
from .workspaces import FLASH_MS, REFRESH_MS, WorkspaceList

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
    Mounts (#987), Proxies (#988), and Events (#989), the Info
    pane gone with its duplicated sections (#1017) — over the
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
        # Proxies, and Events. The bar sits on the chrome's own
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
        self.bar = QtWidgets.QTabBar(self)
        # The active tab's mark (#1012): the strip paints its
        # current tab with the accent tint the main strip
        # carries too, so the page the operator stands on reads
        # at a glance on both levels.
        mark_active(self.bar)
        self.bar.addTab(SHELLS_LABEL)
        self.bar.addTab(MOUNTS_LABEL)
        self.bar.addTab(PROXIES_LABEL)
        self.bar.addTab(EVENTS_LABEL)
        self.stack = QtWidgets.QStackedWidget(self)
        self.stack.addWidget(self.shells_tab)
        self.stack.addWidget(self.mounts_tab)
        self.stack.addWidget(self.proxies_tab)
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
        The page refreshes on its own timer (#971: no manual
        button); its new-shell action is the tab strip's "+"
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
        state resets — fresh row and footer — the
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
        self.events_tab.clear()
        self.refresh()

    @QtCore.Slot()
    def refresh(self) -> None:
        """One page refresh through the bridge (#945, #971) —
        the tick and the page's own open share it. A flight
        already out is not stacked, the listing's own guard
        (#470); the tabs stand until the answers replace
        them."""
        if not self.row_pending:
            self.row_pending = True
            self.bridge.call(ROW_KEY, "workspaces")
        if not self.storage_pending:
            self.storage_pending = True
            self.bridge.call(STORAGE_KEY, "storage")
        # The Proxies tab rides the page's own tick (#988): its
        # listing is its own read, refreshed beside the row.
        self.proxies_tab.poke()

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
            # already happened.
            if not self.isHidden():
                self.removed.emit(self.workspace_id)
            return
        self.row = fresh
        self.rebuild()

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

    def resolve_outage(self) -> None:
        """A standing outage's resolve (#945, #904's contract):
        an answer landed, and the flash it held clears — the
        tabs keep the content they have."""
        if self.flash_outage:
            self.clear_flash()

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
        """One pushed frame to the tabs that read it (#945's
        seam, #946's feed): a frame reaches the page's tabs
        only when it names this workspace — the feed is a
        daemon-wide subscriber, and a busy sibling's frames
        would otherwise land on this page's tabs."""
        data = frame.get("data") or {}
        if not frame_names(data, self.workspace_id):
            return
        # A proxy transition repaints the Proxies tab (#988) —
        # the feed's frame is the tab's own refresh trigger, the
        # same path the page's tick drives — and every frame
        # appends to the Events tab (#989).
        self.proxies_tab.frame(frame)
        self.events_tab.push(frame)
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
        object that outlived its Python one. The decider
        registration returns with the show (#999): the hide
        retired it, and the workspace's holds wait for a
        decider only while one watches — the page showing again
        is the watch resuming, whatever hid it (Back's return,
        a tab switch back to Workspaces)."""
        if getattr(self, "back_button", None) is not None:
            self.anchor_focus()
            self.refresh()
            self.timer.start()
            if self.workspace_id and self.consent is not None:
                self.bridge.start_decider(
                    self.workspace_id, self.consent.controller
                )
        super().showEvent(event)

    def hideEvent(self, event: QtGui.QHideEvent) -> None:
        """The tick stops with the page (#945), and the decider
        registration retires with it (#999): the daemon holds
        flows only while a decider watches, and a page nobody
        sees must not hold them — the bridge's cancel closes
        the link's socket, the daemon's own retire. A wrapper
        without its ``timer`` (#959) has no tick to stop; its
        bridge stands from the constructor's first line, so the
        retire needs no guard of its own."""
        timer = getattr(self, "timer", None)
        if timer is not None:
            timer.stop()
        # The stack's own addWidget hides a page that never
        # opened — no claim stands to retire — so the retire
        # asks only while a workspace does.
        if self.workspace_id:
            self.bridge.stop_decider()
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
        bridge.feed_dropped.connect(self.feed_dropped)
        bridge.feed_reconnected.connect(self.feed_back)
        bridge.decider_rejected.connect(self.decider_refused)
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
        dialog = CreateDialog(self.bridge)
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
        """One pushed frame (#946): the page's tabs take it
        whatever screen owns the window — its events tab is the
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
