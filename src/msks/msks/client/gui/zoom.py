"""The whole-client zoom (#976, phase 6 of #924): Ctrl+Plus,
Ctrl+Equal, and Ctrl+Minus step both font sets at once, and
Ctrl+0 sets both back — the keys answer wherever the focus
stands, on the terminal or on any screen's chrome. The UI
fonts move on the application font every screen inherits; the
terminal faces move on every live hub terminal, and the window
resizes around the visible one so its cell grid holds at the
new size.

The module imports PySide6 at module scope, the same contract
as the screen modules: the window path (``app.open_window``)
loads it, nothing at CLI parse time (#927).
"""

from PySide6 import QtCore, QtGui

from .listtable import rebold_headers
from .terminal import fonts

#: The UI fonts' own clamp (#976), points: the terminal window
#: is 7–28 (``terminal/fonts.py``); the chrome stays readable
#: above 8 and stops short of dwarfing the widgets at 28.
UI_MIN_PT, UI_MAX_PT = 8.0, 28.0

#: One keypress's step, points — both font sets take it
#: together (#976).
STEP_PT = 1


class Zoom(QtCore.QObject):
    """Both font sets' zoom state (#976): the application font
    behind the screens' chrome, and the terminal face every
    live shell runs. The shortcuts sit on the window with
    application scope, so a focused list, editor, or terminal
    swallows none of them; the visible terminal's window
    resizes around its grid, keeping its columns and rows."""

    def __init__(self, hub, window) -> None:
        super().__init__(window)
        self.hub = hub
        self.term_pt = fonts.DEFAULT_PT
        app = QtGui.QGuiApplication.instance()
        self.ui_pt = app.font().pointSizeF()
        self.ui_default = self.ui_pt
        self.hub.shell_opened.connect(self.shell_joined)
        self.install(window)

    def retarget(self, hub) -> None:
        """Point the zoom at a reconnected window's terminals
        (#1009): the hub the old shell owned closed with its
        connection, and the new hub's shells join the standing
        zoom — the steps the operator set survive the daemon
        switch instead of resetting to the defaults. A re-ask
        for the same hub changes nothing."""
        if hub is self.hub:
            return
        self.hub.shell_opened.disconnect(self.shell_joined)
        self.hub = hub
        hub.shell_opened.connect(self.shell_joined)

    def install(self, window) -> None:
        """Stand the zoom keys on the window (#976): one
        shortcut per action, application scope — the focus
        never gates them. Three spellings grow: the numpad's
        plus (``Ctrl++`` matches the keypad key), the bare
        equal (``Ctrl+=``, the same key without the shift a
        plus asks), and the shifted equal (``Ctrl+Shift++`` —
        a sequence match wants the shift named, and a
        keyboard with no numpad has no other plus)."""
        for keys, slot in (
            (("Ctrl++", "Ctrl+=", "Ctrl+Shift++"), self.grow),
            (("Ctrl+-",), self.shrink),
            (("Ctrl+0",), self.reset),
        ):
            shortcut = QtGui.QShortcut(window)
            shortcut.setKeys([QtGui.QKeySequence(k) for k in keys])
            shortcut.setContext(QtCore.Qt.ShortcutContext.ApplicationShortcut)
            shortcut.activated.connect(slot)

    @QtCore.Slot()
    def grow(self) -> None:
        """Ctrl+Plus and Ctrl+Equal: one step up both sets."""
        self.scale(STEP_PT)

    @QtCore.Slot()
    def shrink(self) -> None:
        """Ctrl+Minus: one step down both sets."""
        self.scale(-STEP_PT)

    def scale(self, direction: int) -> None:
        """One keypress on both font sets (#976): each clamps
        on its own — the terminal holds ``MIN_PT``–``MAX_PT``
        while the chrome holds its own range, and one set still
        moves while the other stands at its clamp."""
        term = fonts.stepped(self.term_pt, direction)
        ui = min(max(self.ui_pt + direction, UI_MIN_PT), UI_MAX_PT)
        if term == self.term_pt and ui == self.ui_pt:
            return  # both sets stand at their clamp
        self.term_pt = term
        self.set_ui(ui)
        self.apply_terminals()

    def set_ui(self, size: float) -> None:
        """Write the UI font on the application (#976): every
        screen that inherits the application font follows — the
        rows, the pages, the feed, and the detail chrome — and
        the after-write walk re-seats the tables' bold headers
        on the fresh size: their explicit bold face is the one
        font the application write does not move."""
        self.ui_pt = size
        app = QtGui.QGuiApplication.instance()
        font = app.font()
        font.setPointSizeF(size)
        app.setFont(font)
        rebold_headers()

    def apply_terminals(self) -> None:
        """The stepped face on every live terminal (#976): the
        visible one resizes the window around its held grid; a
        hidden one re-faces and re-derives its grid when its
        tab next shows it."""
        for widget in self.hub.terminals():
            if widget.font.pointSize() != self.term_pt:
                widget.apply_font_size(self.term_pt)

    @QtCore.Slot()
    def reset(self) -> None:
        """Ctrl+0: both font sets back to their defaults — the
        terminal face to its own default, the chrome to the
        scaled size the window opened with."""
        if self.term_pt == fonts.DEFAULT_PT and self.ui_pt == self.ui_default:
            return
        self.term_pt = fonts.DEFAULT_PT
        self.set_ui(self.ui_default)
        self.apply_terminals()

    @QtCore.Slot(str, int)
    def shell_joined(self, workspace_id: str, no: int) -> None:
        """A shell opened after a zoom joins it (#976): the
        hub's fresh terminal carries the default, and a
        default-sized one beside the zoomed chrome would read
        against it. The walk takes any live terminal the face
        has not reached — the fresh shell, wherever the hub's
        own signal order seats its tab — and the window stays
        put: the new shell's own layout sizes it."""
        if self.term_pt == fonts.DEFAULT_PT:
            return
        for widget in self.hub.terminals():
            if widget.font.pointSize() != self.term_pt:
                widget.set_face(self.term_pt)
