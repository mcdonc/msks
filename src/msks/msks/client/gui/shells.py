"""The workspace shell tabs (phase 4.5 of #924, #954, #974):
the hub that owns a workspace's terminal sessions across
detail-page visits, and the tab strip that hosts them inside
the page's Shells tab.

The hub (:class:`ShellHub`) owns the :class:`TerminalWidget`
instances — one per active ssh connection — and each session's
number and custom title. The shells outlive the detail page
(#954): the hub belongs to the window's shell, and a page that
closes (Back to the list) closes nothing — only a tab's own
close ends its session, through the widget's own teardown
contract (close before ``deleteLater``).

The strip (:class:`ShellTabs`) is the Shells tab's own tab
widget (#974): one tab per session, titled with the session's
number until an in-place rename stands or the program inside
sets its title (#1163), and the "+" as the
last tab — directly right of the rightmost shell tab — opening
one more shell (#969, #971). The strip carries the actions the
shell keys drive (#1163, :mod:`.shellkeys`): closing the current
shell, stepping to the next or the previous one, and selecting
the Nth. Hosting is per workspace —
opening another workspace's page unhooks this one's widgets
(they keep running hidden) and hooks the new workspace's.

The module imports PySide6 at module scope, the same contract
as the screen modules (:mod:`msks.client.gui.workspace`):
nothing imports it at CLI parse time.
"""

from functools import partial

from PySide6 import QtCore, QtWidgets

from . import terminal
from .consent import ConsentPresenter
from .consentpanel import ConsentPanel
from .termsession import shell_argv

#: A shell tab's minimum text width, characters (#969): a tab
#: titled with a one-digit session number stays wide enough to
#: read and grab — three digits of the tab's own font beside
#: the tab's close button.
MIN_TAB_CHARS = 3

#: A shell tab's maximum text width, characters (#1163): a
#: program's title runs as long as a path or a command line,
#: and an uncapped tab would push the other shells and the "+"
#: off the strip. A title past the ceiling elides at the tab's
#: right edge, and the tab's tooltip carries it whole.
MAX_TAB_CHARS = 24


class ShellSession:
    """One active ssh connection's record (#954): the session's
    number per workspace, its custom title once renamed, the
    title the program inside set last (#1163), and the terminal
    widget running it. The number never repeats inside
    a workspace — a closed session's number is not reused — so
    a title names one session for its whole life."""

    def __init__(self, no: int, widget) -> None:
        self.no = no
        self.title: str | None = None
        #: The program's own title (#1163): what the program
        #: inside set last through OSC 0/2 — the empty string
        #: until it sets one, and again once it clears it.
        self.program_title = ""
        self.widget = widget

    @property
    def label(self) -> str:
        """The tab's title text (#954, #1163): the custom name
        once one stands, else the program's own title while one
        stands, else the session's number."""
        if self.title is not None:
            return self.title
        return self.program_title or str(self.no)


class ShellBar(QtWidgets.QTabBar):
    """The strip's own bar (#969, #1163): a shell tab never
    shrinks under three characters of text beside its close
    button — the default hint squeezes a one-digit title into a
    sliver the pointer cannot grab — and never grows past the
    title ceiling."""

    #: The strip's "+" tab's index (#971): the one tab that is
    #: not a shell and takes its natural width. The strip
    #: recomputes it on every structural change; -1 holds only
    #: between an unhost and the next host.
    plus_index = -1

    def tabSizeHint(self, index):  # noqa: N802
        """The platform's own hint, floored and ceiled for the
        shell tabs — every tab but the "+" itself."""
        size = super().tabSizeHint(index)
        if index != self.plus_index:
            size.setWidth(
                min(max(size.width(), self.min_tab_px()), self.max_tab_px())
            )
        return size

    def tab_px(self, chars: int) -> int:
        """A tab's width for ``chars`` digits of the bar's font
        beside the tab's close button, both sides' padding
        included."""
        digits = self.fontMetrics().horizontalAdvance("0" * chars)
        button = self.style().pixelMetric(
            QtWidgets.QStyle.PixelMetric.PM_SmallIconSize, None, self
        )
        padding = 2 * self.style().pixelMetric(
            QtWidgets.QStyle.PixelMetric.PM_TabBarTabHSpace, None, self
        )
        return digits + button + padding

    def min_tab_px(self) -> int:
        """The floor (#969): three digits of the bar's font beside
        the tab's close button, both sides' padding included."""
        return self.tab_px(MIN_TAB_CHARS)

    def max_tab_px(self) -> int:
        """The ceiling (#1163): the floor's own measure at
        :data:`MAX_TAB_CHARS` — the bar elides a title past it
        at paint."""
        return self.tab_px(MAX_TAB_CHARS)


class ShellHub(QtCore.QObject):
    """The window's shell sessions (#954): one registry per
    workspace, owned by the window's shell so the terminals
    outlive any one detail-page visit. ``argv_factory`` builds
    each child's argv — production spawns the ssh argv the TUI
    builds (:func:`msks.client.gui.termsession.shell_argv`); the
    suite its own sleepers."""

    shell_opened = QtCore.Signal(str, int)
    shell_closed = QtCore.Signal(str, int)
    shell_renamed = QtCore.Signal(str, int)
    #: The program inside set or cleared its shell's title
    #: (#1163): the record took the text, an emptied one
    #: included.
    shell_titled = QtCore.Signal(str, int)

    def __init__(self, argv_factory=shell_argv) -> None:
        super().__init__()
        self.argv_factory = argv_factory
        self.sessions: dict[str, list[ShellSession]] = {}
        self.counters: dict[str, int] = {}

    def open_shell(self, workspace_id: str) -> ShellSession:
        """Open one more ssh connection (#954): the terminal
        widget over a fresh child, its number assigned per
        workspace in open order and never reused, announced for
        a hosting tab strip to add beside the standing ones."""
        widget = terminal.TerminalWidget(self.argv_factory(workspace_id))
        no = self.counters.get(workspace_id, 1)
        self.counters[workspace_id] = no + 1
        session = ShellSession(no, widget)
        self.sessions.setdefault(workspace_id, []).append(session)
        widget.title_changed.connect(
            partial(self.program_titled, workspace_id, no)
        )
        self.shell_opened.emit(workspace_id, no)
        return session

    def program_titled(self, workspace_id: str, no: int, text: str) -> None:
        """The program inside set or cleared its title (#1163):
        the record keeps the latest text — the empty string is
        the clear, and the tab reads the number again — and the
        hosting strip hears. A session that already closed
        hears nothing: the lookup finds no record."""
        session = self.find(workspace_id, no)
        if session is None:
            return
        session.program_title = text
        self.shell_titled.emit(workspace_id, no)

    def shells(self, workspace_id: str) -> list[ShellSession]:
        """This workspace's sessions in open order (#954)."""
        return list(self.sessions.get(workspace_id, ()))

    def terminals(self) -> list:
        """Every live terminal across the workspaces (#976): the
        whole-client zoom steps them together — the hosted one
        and the ones running on behind their tabs."""
        return [
            session.widget
            for sessions in self.sessions.values()
            for session in sessions
        ]

    def find(self, workspace_id: str, no: int) -> ShellSession | None:
        """One session by its number, when it still runs."""
        for session in self.sessions.get(workspace_id, ()):
            if session.no == no:
                return session
        return None

    def rename(self, workspace_id: str, no: int, title: str) -> None:
        """Stand a custom title on one session (#954): the title
        names the tab for the session's life; an empty title
        changes nothing (the editor's empty commit reads as its
        cancel)."""
        session = self.find(workspace_id, no)
        if session is None or not title.strip():
            return
        session.title = title.strip()
        self.shell_renamed.emit(workspace_id, no)

    def close_shell(self, workspace_id: str, no: int) -> None:
        """End one session (#954): the widget's own close runs
        the teardown contract — the child hung up inside a bound
        grace, the library handles freed, the registry unseated
        — and the widget goes after it."""
        session = self.find(workspace_id, no)
        if session is None:
            return
        self.sessions[workspace_id].remove(session)
        session.widget.close()
        session.widget.deleteLater()
        self.shell_closed.emit(workspace_id, no)

    def shutdown(self) -> None:
        """Close every session in every workspace (#954): the
        window's quit path — a child left running would outlive
        a GUI whose event loop no longer reads its master."""
        for workspace_id in list(self.sessions):
            for session in self.shells(workspace_id):
                self.close_shell(workspace_id, session.no)


class ShellTabs(QtWidgets.QTabWidget):
    """The Shells tab's own strip (#954, #974): one tab per
    shell of the hosted workspace, the "+" last. Hosting is
    exclusive — ``host`` unhooks the standing shell widgets
    (they keep running, owned by the hub) and hooks the new
    workspace's — and the hub's signals keep the strip in step
    while its workspace shows."""

    new_shell = QtCore.Signal()

    def __init__(self, hub: ShellHub) -> None:
        super().__init__()
        self.hub = hub
        self.hosted = ""
        # The bar first (#969): the strip's own floor-width hint
        # has to stand before the first tab sizes against it.
        self.setTabBar(ShellBar(self))
        # A title past the ceiling elides (#1163): the bar's hint
        # caps the shell tabs, and the elide mode paints a capped
        # tab's text with an ellipsis instead of clipping it.
        self.tabBar().setElideMode(QtCore.Qt.TextElideMode.ElideRight)
        # The "+" (#969, #971): a tab of its own, always the
        # last — directly right of the rightmost shell tab, and
        # the strip's whole content when no shell stands. A
        # click on it opens one more shell; visiting it by
        # keyboard or a tab shift opens nothing.
        self.plus_page = QtWidgets.QWidget(self)
        self.addTab(self.plus_page, "+")
        self.tabBar().setTabToolTip(0, "Open a shell tab")
        self.tabBar().tabBarClicked.connect(self.bar_clicked)
        self.sync_plus_index()
        self.tabBar().installEventFilter(self)
        # The shell tabs' own maps, number to widget and number
        # to the tab's close button (#954): a close takes the
        # hub's record down before the strip hears it, and a
        # removed tab's button is NOT deleted by Qt (it hides as
        # the bar's child) — the strip owns both lifetimes.
        self.tab_widgets: dict[int, QtWidgets.QWidget] = {}
        self.tab_buttons: dict[int, QtWidgets.QToolButton] = {}
        hub.shell_opened.connect(self.on_opened)
        hub.shell_closed.connect(self.on_closed)
        hub.shell_renamed.connect(self.on_renamed)
        hub.shell_titled.connect(self.on_titled)
        self.editor: QtWidgets.QLineEdit | None = None

    # --- hosting ------------------------------------------------------

    def host(self, workspace_id: str) -> None:
        """Show one workspace's shells (#954, #974): the strip
        starts empty, then one tab per session, titled as its
        record reads, the newest shell current, the "+" last."""
        self.unhost()
        self.hosted = workspace_id
        self.addTab(self.plus_page, "+")
        self.tabBar().setTabToolTip(
            self.indexOf(self.plus_page), "Open a shell tab"
        )
        self.sync_plus_index()
        for session in self.hub.shells(workspace_id):
            self.add_session(session)
        last = self.count() - 2
        if last >= 0:
            self.setCurrentIndex(last)

    def unhost(self) -> None:
        """Take the shell tabs off (#954): each widget unhooks
        from the strip and keeps running — the hub owns it, and
        a page's close ends nothing. The "+" comes off with
        them; the strip re-adds it at its next host."""
        self.cancel_rename()
        while self.count() > 0:
            widget = self.widget(0)
            self.removeTab(0)
            if widget is not self.plus_page:
                widget.setParent(None)
        self.hosted = ""
        self.sync_plus_index()
        self.tab_widgets.clear()
        self.drop_buttons()

    def add_session(self, session: ShellSession) -> None:
        """One session's tab (#954): titled as its record reads,
        with its own close button — inserted before the "+" so
        the strip's last tab stays the strip's own."""
        index = self.insertTab(self.count() - 1, session.widget, session.label)
        self.sync_plus_index()
        self.tab_widgets[session.no] = session.widget
        self.title_tab(index, session)
        button = QtWidgets.QToolButton(self.tabBar())
        button.setIcon(
            self.style().standardIcon(
                QtWidgets.QStyle.StandardPixmap.SP_TabCloseButton
            )
        )
        button.setToolTip("Close this shell")
        button.clicked.connect(
            lambda: self.hub.close_shell(self.hosted, session.no)
        )
        self.tab_buttons[session.no] = button
        self.tabBar().setTabButton(
            index, QtWidgets.QTabBar.ButtonPosition.RightSide, button
        )

    def drop_buttons(self) -> None:
        """Delete the tracked close buttons (#954): a removed
        tab's button hides as the bar's child rather than dying,
        and its click holds its session's record alive."""
        for button in self.tab_buttons.values():
            button.deleteLater()
        self.tab_buttons.clear()

    def sync_plus_index(self) -> None:
        """The "+" tab's index back into the bar (#971): the bar
        floors every shell tab and skips the + by this index,
        which every structural change can move."""
        self.tabBar().plus_index = self.indexOf(self.plus_page)

    @QtCore.Slot(int)
    def bar_clicked(self, index: int) -> None:
        """One click on the bar (#971): the "+" opens a shell;
        every other tab is a tab, and current moving onto the +
        without a click (a close shifting tabs) opens nothing."""
        if self.widget(index) is self.plus_page:
            self.new_shell.emit()

    # --- the hub's signals ---------------------------------------------

    @QtCore.Slot(str, int)
    def on_opened(self, workspace_id: str, no: int) -> None:
        """A shell opened (#954): a tab beside the standing ones
        when the strip hosts the workspace — the newest shell
        takes the window and the focus."""
        if workspace_id != self.hosted:
            return
        session = self.hub.find(workspace_id, no)
        if session is None:
            return
        self.add_session(session)
        self.setCurrentWidget(session.widget)
        session.widget.setFocus()

    @QtCore.Slot(str, int)
    def on_closed(self, workspace_id: str, no: int) -> None:
        """A shell closed (#954): its tab comes off through the
        strip's own map — the hub's record is already gone."""
        if workspace_id != self.hosted:
            return
        self.close_editor_for(no)
        self.drop_button(no)
        widget = self.tab_widgets.pop(no, None)
        if widget is None:
            return
        index = self.indexOf(widget)
        if index >= 0:
            self.removeTab(index)
        self.sync_plus_index()
        self.leave_plus()

    def leave_plus(self) -> None:
        """Take current off the blank "+" (#971): a close that
        removed the tab current stood on lands Qt's default on
        the +, a body with nothing in it — the remaining shell
        before it takes the window back, and the + alone when
        nothing stands (the page's next open creates a
        shell)."""
        if self.currentWidget() is not self.plus_page:
            return
        self.setCurrentIndex(max(0, self.currentIndex() - 1))

    def drop_button(self, no: int) -> None:
        """One close's button down (#954) — the same leak the
        unhost path clears wholesale."""
        button = self.tab_buttons.pop(no, None)
        if button is not None:
            button.deleteLater()

    @QtCore.Slot(str, int)
    def on_renamed(self, workspace_id: str, no: int) -> None:
        """A rename stood (#954): the tab's text takes it, over
        whatever the program inside set (#1163)."""
        if workspace_id != self.hosted:
            return
        self.retitle(no)

    @QtCore.Slot(str, int)
    def on_titled(self, workspace_id: str, no: int) -> None:
        """The program inside set or cleared its title (#1163):
        the tab follows the record."""
        if workspace_id != self.hosted:
            return
        self.retitle(no)

    def retitle(self, no: int) -> None:
        """One session's tab back to its record (#954, #1163): the
        label the record reads — the rename winning over the
        program's title, the number once the program clears
        its own."""
        for index in range(self.count()):
            session = self.session_at(index)
            if session is not None and session.no == no:
                self.title_tab(index, session)
                return

    def title_tab(self, index: int, session: ShellSession) -> None:
        """One shell tab's text and tooltip from its record
        (#1163): the label, and the program's whole title under
        the pointer while one stands and no rename does — a
        title past the ceiling elides on the tab, and the
        tooltip is where it reads whole."""
        self.setTabText(index, session.label)
        program = session.program_title if session.title is None else ""
        self.setTabToolTip(index, program)

    # --- the strip's own actions (#1163) --------------------------------
    # The shell keys (:mod:`.shellkeys`) drive these, the same
    # steps the pointer takes through the tab and the close
    # button.

    def shell_count(self) -> int:
        """The shell tabs standing: the "+" sits directly after
        the last one (#971), so its index is the count — and 0
        before any host, where the index is -1."""
        return max(0, self.indexOf(self.plus_page))

    def current_session(self) -> ShellSession | None:
        """The session whose tab is current (#1163): None when
        the "+" or no tab stands there."""
        return self.session_at(self.currentIndex())

    def close_current(self) -> None:
        """The close button's action on the current shell
        (#1163): the hub's record goes down and the tab with it,
        exactly as the tab's own button does — and a terminal that
        held the keyboard hands it to the shell the strip lands
        on, as every terminal app's close does. The pointer's
        own close leaves the focus where it stood (#969's
        rule). Nothing standing current — the "+" alone —
        closes nothing."""
        session = self.current_session()
        if session is None:
            return
        held = self.keyboard_in(session)
        self.hub.close_shell(self.hosted, session.no)
        if held:
            self.focus_landed()

    def keyboard_in(self, session: ShellSession) -> bool:
        """Whether the session's terminal, or a child of it
        holding the keyboard in its stead (the find bar's edit),
        has the focus (#1163): a close ends that shell all the
        same, and the keyboard follows as from the terminal
        itself. Without the hand-off Qt leaves no focus widget
        standing there, and every page-scoped key goes dead
        until a click."""
        focus = QtWidgets.QApplication.focusWidget()
        return focus is not None and (
            focus is session.widget or session.widget.isAncestorOf(focus)
        )

    def focus_landed(self) -> None:
        """The keyboard onto the shell the strip landed on
        (#1163): a removed tab hides its page without Qt's own
        focus move (a stack's page switch makes one; a removal
        does not), so the closed terminal would hold the
        keyboard until its deleteLater — the landed shell's
        terminal takes it, or the bar when the "+" lands."""
        landed = self.currentWidget()
        target = self.tabBar() if landed is self.plus_page else landed
        target.setFocus()

    def step_shell(self, delta: int) -> None:
        """The next or the previous shell (#1163): the current
        tab moves by one among the shell tabs, wrapping at both
        ends — the "+" is never a stop, and a current standing
        on it (a close can land it there, #971) starts from the
        first or the last shell. The chosen shell's terminal
        takes the keyboard."""
        count = self.shell_count()
        if count == 0:
            return
        current = self.currentIndex()
        if current >= count:
            target = 0 if delta > 0 else count - 1
        else:
            target = (current + delta) % count
        self.setCurrentIndex(target)
        self.widget(target).setFocus()

    def select_shell(self, position: int) -> None:
        """The Nth shell (#1163), counting from one: its tab
        current and its terminal focused; a position past the
        standing shells changes nothing."""
        index = position - 1
        if 0 <= index < self.shell_count():
            self.setCurrentIndex(index)
            self.widget(index).setFocus()

    def session_at(self, index: int) -> ShellSession | None:
        """The session a shell tab hosts, by widget identity —
        the strip's own order is the record."""
        for session in self.hub.shells(self.hosted):
            if self.widget(index) is session.widget:
                return session
        return None

    # --- the in-place rename -------------------------------------------

    def eventFilter(self, obj, event) -> bool:
        """The strip's two filtered gestures (#954): a
        double-click on a shell tab opens the title editor in
        place — only a shell tab renames — and the open
        editor's Escape closes it as a cancel."""
        if obj is self.editor:
            if self.editor_override(event):
                event.accept()
                return True
            if self.editor_escape(obj, event):
                self.cancel_rename()
                return True
            return super().eventFilter(obj, event)
        if self.tab_double_click(event):
            return True
        return super().eventFilter(obj, event)

    def editor_override(self, event) -> bool:
        """Whether this event is the question that hands the
        open editor's Escape to a matching shortcut (#1099):
        the page stands one on the bare key — its Back — and
        the dispatch consumes the press before the filter's
        cancel arm below reads it. The accept yields the
        shortcut and keeps the press for the cancel, the same
        claim the terminal's widget makes for its program
        inside. The override arrives only for a press the
        shortcut matched, so the bare key names it."""
        return (
            event.type() == QtCore.QEvent.Type.ShortcutOverride
            and event.key() == QtCore.Qt.Key.Key_Escape
        )

    def editor_escape(self, obj, event) -> bool:
        """Whether this event is the open editor's Escape."""
        return (
            obj is self.editor
            and event.type() == QtCore.QEvent.Type.KeyPress
            and event.key() == QtCore.Qt.Key.Key_Escape
        )

    def tab_double_click(self, event) -> bool:
        """Whether this event is a left double-click on a shell
        tab; the editor opens over it when it is."""
        if (
            event.type() != QtCore.QEvent.Type.MouseButtonDblClick
            or event.button() != QtCore.Qt.MouseButton.LeftButton
        ):
            return False
        index = self.tabBar().tabAt(event.position().toPoint())
        if index < 0 or self.widget(index) is self.plus_page:
            return False
        self.begin_rename(index)
        return True

    def begin_rename(self, index: int) -> None:
        """The title editor over one tab (#954): the tab's own
        rect, its standing text selected — Return stands the
        name, Escape cancels, and a second editor never stacks
        on a first."""
        self.cancel_rename()
        bar = self.tabBar()
        # The tab's own rect carries a floor already (#969): the
        # bar's hint keeps every shell tab at least three
        # characters wide beside its close button.
        rect = bar.tabRect(index)
        editor = QtWidgets.QLineEdit(self)
        corner = bar.mapTo(self, rect.topLeft())
        editor.setGeometry(corner.x(), corner.y(), rect.width(), rect.height())
        session = self.session_at(index)
        editor.setProperty("session_no", session.no if session else 0)
        editor.setText(session.label if session else "")
        editor.selectAll()
        editor.returnPressed.connect(self.commit_rename)
        editor.installEventFilter(self)
        editor.show()
        editor.setFocus()
        self.editor = editor

    def close_editor_for(self, no: int) -> None:
        """Take the editor down when its session closes (#954):
        a standing edit names a session that is gone."""
        if (
            self.editor is not None
            and self.editor.property("session_no") == no
        ):
            self.cancel_rename()

    def commit_rename(self) -> None:
        """Stand the edited name (#954): the hub owns the
        record; an empty edit cancels, changing nothing."""
        if self.editor is None:
            return
        no = self.editor.property("session_no")
        text = self.editor.text()
        self.cancel_rename()
        # The edit keys on the session's number, not the tab's
        # position: tabs close and shift under an open editor,
        # and a positional key would land the name on another
        # session.
        session = self.hub.find(self.hosted, no)
        if session is not None:
            self.hub.rename(self.hosted, session.no, text)

    def cancel_rename(self) -> None:
        """Take the editor down (#954) — a cancel's edit changes
        nothing."""
        if self.editor is not None:
            self.editor.deleteLater()
            self.editor = None


class ShellsTab(QtWidgets.QWidget):
    """Consent above persistent shells, refreshed while this tab is visible."""

    def __init__(self, hub: ShellHub, bridge) -> None:
        super().__init__()
        self.timer = QtCore.QTimer(self)
        self.timer.setInterval(1000)
        self.consent_panel = ConsentPanel(bridge, self)
        self.shells = ShellTabs(hub)
        self.consent_panel.focus_exhausted.connect(self.focus_shell)
        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addWidget(self.consent_panel)
        layout.addWidget(self.shells, stretch=1)
        self.timer.timeout.connect(self.consent_panel.refresh)
        # The receiver must be this tab, a QObject in the GUI thread
        # (#1001): the bridge emits decider_landed on its worker loop,
        # and a plain callable connected there runs on the emitting
        # thread — the panel would repaint off the GUI thread. The
        # QObject receiver takes the emit queued.
        bridge.decider_landed.connect(self.decider_frame)

    @QtCore.Slot(str, object)
    def decider_frame(self, outcome: str, payload: object) -> None:
        """One landed decider frame: the panel re-reads the
        presenter's rows on the GUI thread."""
        self.consent_panel.refresh()

    def host(self, workspace_id: str, presenter: ConsentPresenter) -> None:
        """Replace consent state and rehost the hub's persistent shells."""
        self.consent_panel.host(presenter)
        self.shells.host(workspace_id)

    def focus_shell(self) -> None:
        """Restore terminal focus after the final focused hold disappears."""
        widget = self.shells.currentWidget()
        if widget is not None and widget is not self.shells.plus_page:
            widget.setFocus()
        else:
            self.shells.tabBar().setFocus()

    def showEvent(self, event):
        if getattr(self, "consent_panel", None) is not None:
            self.consent_panel.refresh()
            self.timer.start()
        super().showEvent(event)

    def hideEvent(self, event):
        timer = getattr(self, "timer", None)
        if timer is not None:
            timer.stop()
        super().hideEvent(event)
