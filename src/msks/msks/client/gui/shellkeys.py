"""The Shells tab's keys (#1163): Ctrl+Shift+T opens a shell,
Ctrl+Shift+W closes the current one, Ctrl+Tab and Ctrl+Shift+Tab
move to the next and the previous shell, Ctrl+1 through Ctrl+9
jump to the Nth shell, and Alt+1 through Alt+5 jump to the page's
tabs — the set Konsole and GNOME Terminal share.

The shortcuts stand on the workspace page with the
``WidgetWithChildren`` context, the terminal-only toggle's own
shape (#1152): Qt asks the shortcut map before the focused widget
sees a press, so the keys answer from a terminal — whose every
other key stays with the program inside (#1066) — and from
anywhere else in the page, and nowhere else. The listing, the
Images and Secrets pages, and the window's own strip hear them as
ordinary keys. A shell action first brings the Shells tab forward
with the keyboard on the current shell: the keys answer from the
Mounts or Events tab too, and a shell opened or closed behind
another tab would be a change the operator cannot see. The
actions themselves are the strip's (:class:`.shells.ShellTabs`),
the same steps the pointer takes.

The module imports PySide6 at module scope, the screen modules'
contract: nothing imports it at CLI parse time.
"""

from functools import partial

from PySide6 import QtCore, QtGui

#: The shell positions the number keys reach (#1163): Ctrl+N
#: jumps to the Nth shell, the first nine — a tenth shell
#: switches by Ctrl+Tab or the pointer.
SHELL_KEYS = 9


class ShellKeys(QtCore.QObject):
    """The page's shell keys (#1163), built once with the page
    and gone with it: every shortcut a child of the page, active
    while the focus stands inside it. A held key answers once —
    the keyboard's repeat opens no second shell."""

    def __init__(self, page) -> None:
        super().__init__(page)
        self.page = page
        self.shortcuts: list[QtGui.QShortcut] = []
        for spellings, slot in (
            (("Ctrl+Shift+T",), self.open_shell),
            (("Ctrl+Shift+W",), self.close_shell),
            (("Ctrl+Tab",), self.next_shell),
            # Ctrl+Shift+Tab arrives as Backtab with the control
            # held on most platforms — Qt's own name for the
            # shifted Tab (#1066) — and as a shifted Tab
            # elsewhere: both spellings stand, one action.
            (("Ctrl+Shift+Tab", "Ctrl+Backtab"), self.previous_shell),
            *(
                ((f"Ctrl+{n}",), partial(self.jump_shell, n))
                for n in range(1, SHELL_KEYS + 1)
            ),
            # The page's tabs in bar order: Alt+N is the Nth, as
            # many as the bar carries.
            *(
                ((f"Alt+{n}",), partial(self.jump_page_tab, n))
                for n in range(1, page.bar.count() + 1)
            ),
        ):
            self.stand(spellings, slot)

    def stand(self, spellings: tuple[str, ...], slot) -> None:
        """One action's shortcut on the page (#1163): every
        spelling of its key, the page's scope, one answer per
        press."""
        shortcut = QtGui.QShortcut(self.page)
        shortcut.setKeys(
            [QtGui.QKeySequence(spelling) for spelling in spellings]
        )
        shortcut.setContext(
            QtCore.Qt.ShortcutContext.WidgetWithChildrenShortcut
        )
        # A held key must not open a shell per repeat (#1152's
        # rule for its toggle): one press, one action.
        shortcut.setAutoRepeat(False)
        shortcut.activated.connect(slot)
        self.shortcuts.append(shortcut)

    @QtCore.Slot()
    def open_shell(self) -> None:
        """Ctrl+Shift+T: the Shells tab forward, then the "+"
        tab's action through the page's own path (#954, #969)."""
        self.show_shells()
        self.page.want_shell()

    @QtCore.Slot()
    def close_shell(self) -> None:
        """Ctrl+Shift+W: the Shells tab forward, then the current
        tab's close button."""
        self.show_shells()
        self.page.shells.close_current()

    @QtCore.Slot()
    def next_shell(self) -> None:
        """Ctrl+Tab: the next shell, wrapping at the last."""
        self.show_shells()
        self.page.shells.step_shell(1)

    @QtCore.Slot()
    def previous_shell(self) -> None:
        """Ctrl+Shift+Tab: the previous shell, wrapping at the
        first."""
        self.show_shells()
        self.page.shells.step_shell(-1)

    def jump_shell(self, position: int) -> None:
        """Ctrl+N: the Nth shell current, its terminal focused;
        a position past the standing shells changes nothing."""
        self.show_shells()
        self.page.shells.select_shell(position)

    def jump_page_tab(self, position: int) -> None:
        """Alt+N: the page's Nth tab current (#1163). Shells —
        the first — is the shell actions' own showing; another
        tab leaves the move to Qt, which hands the keyboard from
        the hidden terminal to the new tab's first stop."""
        if position == 1:
            self.show_shells()
            return
        self.page.bar.setCurrentIndex(position - 1)

    def show_shells(self) -> None:
        """The Shells tab in front with the keyboard on the
        current shell, before a shell action (#1163): the keys
        answer from every tab of the page, and a shell opened,
        closed, or chosen behind the Mounts or Events tab would
        be a change the operator cannot see — and a chosen
        shell's terminal would take the keyboard while hidden.
        The page's own anchor lands the keyboard on the current
        shell, so the action starts from a terminal that holds
        it: a close then hands the keyboard on to the landed
        shell."""
        self.page.bar.setCurrentIndex(0)
        self.page.anchor_focus()
