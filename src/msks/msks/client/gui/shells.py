"""The workspace shell tabs (phase 4.5 of #924, #954): the hub
that owns a workspace's terminal sessions across detail-page
visits, and the tab strip that hosts them beside the page's
Info tab.

The hub (:class:`ShellHub`) owns the :class:`TerminalWidget`
instances — one per active ssh connection — and each session's
number and custom title. The shells outlive the detail page
(#954): the hub belongs to the window's shell, and a page that
closes (Back to the list) closes nothing — only a tab's own
close ends its session, through the widget's own teardown
contract (close before ``deleteLater``).

The strip (:class:`ShellTabs`) is the page's tab widget: the
Info tab first, then one tab per session, titled with the
session's number until an in-place rename stands. Hosting is
per workspace — opening another workspace's page unhooks this
one's widgets (they keep running hidden) and hooks the new
workspace's.

The module imports PySide6 at module scope, the same contract
as the screen modules (:mod:`msks.client.gui.workspace`):
nothing imports it at CLI parse time.
"""

from PySide6 import QtCore, QtWidgets

from . import terminal
from .termsession import shell_argv

#: The Info tab's label (#954): the detail page's own content —
#: mounts, health, events — the first tab, with the shells
#: beside it.
INFO_LABEL = "Info"

#: The inline title editor's minimum width, pixels (#954): the
#: tab's own text width, but never too narrow to type in.
EDITOR_MIN_PX = 80


class ShellSession:
    """One active ssh connection's record (#954): the session's
    number per workspace, its custom title once renamed, and the
    terminal widget running it. The number never repeats inside
    a workspace — a closed session's number is not reused — so
    a title names one session for its whole life."""

    def __init__(self, no: int, widget) -> None:
        self.no = no
        self.title: str | None = None
        self.widget = widget

    @property
    def label(self) -> str:
        """The tab's title text (#954): the custom name once one
        stands, the session's number until then."""
        return self.title if self.title is not None else str(self.no)


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
        self.shell_opened.emit(workspace_id, no)
        return session

    def shells(self, workspace_id: str) -> list[ShellSession]:
        """This workspace's sessions in open order (#954)."""
        return list(self.sessions.get(workspace_id, ()))

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
    """The detail page's tab strip (#954): the Info tab first,
    then one tab per shell of the hosted workspace. Hosting is
    exclusive — ``host`` unhooks the standing shell widgets
    (they keep running, owned by the hub) and hooks the new
    workspace's — and the hub's signals keep the strip in step
    while its workspace shows."""

    def __init__(self, hub: ShellHub, info: QtWidgets.QWidget) -> None:
        super().__init__()
        self.hub = hub
        self.hosted = ""
        self.addTab(info, INFO_LABEL)
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
        self.editor: QtWidgets.QLineEdit | None = None

    # --- hosting ------------------------------------------------------

    def host(self, workspace_id: str) -> None:
        """Show one workspace's shells (#954): the standing tabs
        come off first — their widgets keep running under the
        hub — then one tab per session, titled as its record
        reads, the newest shell current."""
        self.unhost()
        self.hosted = workspace_id
        for session in self.hub.shells(workspace_id):
            self.add_session(session)
        last = self.count() - 1
        if last > 0:
            self.setCurrentIndex(last)

    def unhost(self) -> None:
        """Take the shell tabs off (#954): each widget unhooks
        from the strip and keeps running — the hub owns it, and
        a page's close ends nothing."""
        self.cancel_rename()
        while self.count() > 1:
            widget = self.widget(1)
            self.removeTab(1)
            widget.setParent(None)
        self.hosted = ""
        self.tab_widgets.clear()
        self.drop_buttons()

    def add_session(self, session: ShellSession) -> None:
        """One session's tab (#954): titled as its record reads,
        with its own close button — the Info tab carries none."""
        index = self.addTab(session.widget, session.label)
        self.tab_widgets[session.no] = session.widget
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

    def drop_button(self, no: int) -> None:
        """One close's button down (#954) — the same leak the
        unhost path clears wholesale."""
        button = self.tab_buttons.pop(no, None)
        if button is not None:
            button.deleteLater()

    @QtCore.Slot(str, int)
    def on_renamed(self, workspace_id: str, no: int) -> None:
        """A rename stood (#954): the tab's text takes it."""
        if workspace_id != self.hosted:
            return
        for index in range(1, self.count()):
            session = self.session_at(index)
            if session is not None and session.no == no:
                self.setTabText(index, session.label)
                return

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
        place — the Info tab renames nothing — and the open
        editor's Escape closes it as a cancel."""
        if obj is self.editor:
            if self.editor_escape(obj, event):
                self.cancel_rename()
                return True
            return super().eventFilter(obj, event)
        if self.tab_double_click(event):
            return True
        return super().eventFilter(obj, event)

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
        if index <= 0:
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
        rect = bar.tabRect(index)
        if rect.width() < EDITOR_MIN_PX:
            rect.setWidth(EDITOR_MIN_PX)
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
