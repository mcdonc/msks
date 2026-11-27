"""A window torn off the strip (#1203): the top-level window that
opens when a left-drag drops a shell tab off every strip. The
session never restarts: the terminal widget moves from one strip's
stack into the other, the pty child running through both.

The window is a plain top-level window in the desktop's own frame
around a strip of the workspace's shells (:class:`.shells.ShellTabs`,
``holder`` naming this window): the shell the drag carried, and
later any tab dragged into it or opened by its "+". The frame is
the desktop's own on purpose. A custom title bar dragged by Qt's
own move would leave the window stuck on Wayland, where the
compositor places windows and a client's ``move`` is ignored; the
native frame moves everywhere, and the tab strip is the drag
handle for the gestures that need one (:mod:`.shelldrag`): a tab
dragged out of this window tears off or lands on another strip
the same way a tab of the page's strip does. The window's title
carries the current shell's label — the program's, or the renamed
name, the text the tab wore.

The window answers the shell keys (#1163, :mod:`.shellkeys`) as a
scope of its own: Ctrl+Tab and Ctrl+Shift+Tab step among its tabs,
Ctrl+1 through Ctrl+9 jump to the Nth, Ctrl+Shift+W closes the
current shell, and Ctrl+Shift+T opens one more shell in this
window. It carries no page tabs, so the Alt+N keys stand nowhere
in it. The window goes down with its last shell: the tab's close,
a drop onto another strip, and the window's own close — which ends
every shell it holds, the main window's own rule — all take it
down, a shell lost under a drag once the drag ends.

This module imports PySide6 at module scope, the screens' own
contract: nothing imports it at CLI parse time.
"""

from PySide6 import QtCore, QtWidgets

from .shellkeys import ShellKeys


class NoPageTabs:
    """The page-tab stand-in a torn-off window gives the shell
    keys (#1203): no tabs, so no Alt+N stands and a showing of
    the first tab asks nothing."""

    def count(self) -> int:
        return 0

    def setCurrentIndex(self, index: int) -> None:  # noqa: N802
        return None


class ShellWindow(QtWidgets.QMainWindow):
    """One torn-off window (#1203): a native frame around a strip
    of the workspace's shells, built by the strip a tab leaves
    (:meth:`msks.client.gui.shells.ShellTabs.tear_off`) and gone
    with its last shell. The shell keys (:class:`.shellkeys.ShellKeys`)
    take the window as their scope — it stands in for a page: the
    strip as ``shells``, the tab stand-in as ``bar``, the current
    shell's terminal as the keyboard's anchor."""

    def __init__(self, hub, workspace_id: str) -> None:
        super().__init__(None, QtCore.Qt.WindowType.Window)
        self.hub = hub
        self.workspace_id = workspace_id
        self.setAttribute(QtCore.Qt.WidgetAttribute.WA_DeleteOnClose)
        self.strip = None
        # The page-tab stand-in (#1203): the shell keys read a
        # page's bar for the Alt+N set; this window has none.
        self.bar = NoPageTabs()
        #: True while the window's own close runs (#1203): the
        #: last shell's end would otherwise ask for the close
        #: again from inside it.
        self.closing = False
        self.keys = None

    def fill(self, strip) -> None:
        """The window's strip joins (#1203): it hosts the
        workspace, its "+" opens a shell in this window, its
        current tab titles the window, and the shell keys stand on
        the window over it. The strip's own hub handlers connected
        at its construction, so the window's connect after them
        and hear the news with the strip already settled."""
        self.strip = strip
        self.shells = strip
        strip.host(self.workspace_id)
        strip.new_shell.connect(self.open_shell)
        strip.currentChanged.connect(self.retitle)
        self.setCentralWidget(strip)
        self.keys = ShellKeys(self)
        self.hub.shell_closed.connect(self.shell_gone)
        # The dock news after the record moved (#1203): the strip
        # losing the shell unhooked it on the undock news before,
        # so the count reads settled here.
        self.hub.shell_docked.connect(self.shell_gone)
        self.hub.shell_renamed.connect(self.heard)
        self.hub.shell_titled.connect(self.heard)
        self.retitle()

    def size_to(self, size: QtCore.QSize) -> None:
        """The window opens around the terminal's standing size
        (#1203) under its strip's bar."""
        bar = self.strip.tabBar().sizeHint().height()
        self.resize(size.width(), size.height() + bar)

    # --- the title ------------------------------------------------------

    def retitle(self, _index: int = 0) -> None:
        """The window's title is the current shell's label
        (#1203) — the tab's own reading — with the client's name
        beside it for the taskbar; a window between shells
        carries the workspace's id."""
        session = self.strip.current_session()
        label = session.label if session is not None else self.workspace_id
        self.setWindowTitle(f"{label} — msks")

    def heard(self, workspace_id: str, no: int) -> None:
        """A rename or a program title on the hub (#1163): the
        window follows when the news is the current shell's."""
        session = self.strip.current_session()
        if session is not None and (workspace_id, no) == (
            self.workspace_id,
            session.no,
        ):
            self.retitle()

    # --- the shell keys' scope -----------------------------------------

    def want_shell(self) -> None:
        """The "+" and Ctrl+Shift+T (#1203): one more shell
        seated in this window."""
        self.open_shell()

    def open_shell(self) -> None:
        """One more shell in this window (#1203): the hub opens
        it with this window as its holder, and the strip takes
        the tab."""
        self.hub.open_shell(self.workspace_id, window=self)

    def anchor_focus(self) -> None:
        """Where a shell action leaves the keyboard (#1163): the
        current shell's terminal, the bar when the "+" alone
        stands."""
        widget = self.strip.currentWidget()
        if widget is not None and widget is not self.strip.plus_page:
            widget.setFocus()
        else:
            self.strip.tabBar().setFocus()

    # --- the life -------------------------------------------------------

    def shell_gone(self, workspace_id: str, no: int) -> None:
        """A shell of this window closed or left for another
        strip (#1203): the window with nothing left to hold goes
        down — the strip took its tab off on the undock news
        before this one, so the count it keeps is settled — unless
        a drag runs: its bar may be the drag's source, and the
        window waits out the drag with the controller
        (:meth:`.shelldrag.ShellDrag.hold`)."""
        if (
            self.strip is not None
            and self.strip.shell_count() == 0
            and not self.closing
        ):
            if self.hub.drag.dragging:
                self.hub.drag.hold(self)
            else:
                self.close()

    def place(self, at: QtCore.QPoint) -> None:
        """Put the window's client area at ``at`` (#1203): the
        pointer's grab point sat in the tab, and the tab in the
        window's strip, so the frame goes above and left of it by
        the desktop's own margins. A desktop that places windows
        itself ignores the move."""
        frame = self.geometry().topLeft() - self.frameGeometry().topLeft()
        self.move(at - frame)

    def closeEvent(self, event) -> None:  # noqa: N802
        """The window's close ends every shell it holds (#1203),
        the main window's own rule scaled to one window: the hub
        takes the records down, and the news above would take the
        window down in turn — this close is already that. A close
        arriving with nothing held is the news's own."""
        self.closing = True
        if self.strip is not None:
            for session in self.strip.sessions():
                self.hub.close_shell(self.workspace_id, session.no)
        event.accept()
