"""The main window's tab strip (#962, phase 4.6 of #924): the
top-level pages the window opens on. "Workspaces" is first — the
list and the detail pages over the bridge (#939, #945) — and the
Images and Secrets tabs of the plan (#963–#966) land beside it
through :meth:`MainTabs.addTab` — Images since #964, the
published channel and the catalog under the second tab. The bar
crosses by arrow keys (the spatial-navigation rule carried to the
GUI): Left and Right move between the tabs, Down leaves the bar
for the page below, and the bar never holds the keyboard without
a way out (no focus trap).
"""

from PySide6 import QtCore, QtWidgets

#: The first tab's label (#962): the workspace screens' page.
WORKSPACES_LABEL = "Workspaces"

#: The second tab's label (#964): the images page — the catalog
#: and the published channel, the TUI image page's counterpart.
IMAGES_LABEL = "Images"


class MainTabs(QtWidgets.QTabWidget):
    """The window's top-level tabs (#962): the workspaces page
    under the first tab, the later phase 4.6 pages beside it.
    The Workspaces tab carries the create opener — a plus
    button embedded in the tab — whose click the window path
    wires to the shell's create dialog."""

    create_requested = QtCore.Signal()

    def __init__(self, workspaces: QtWidgets.QWidget) -> None:
        super().__init__()
        self.addTab(workspaces, WORKSPACES_LABEL)
        # The create opener (#962): a plus button on the tab
        # itself, the shell strip's own "+" shape (#969) — a
        # click asks for a create dialog, the shell that owns
        # the bridge answers it.
        plus = QtWidgets.QToolButton()
        plus.setText("+")
        plus.setAutoRaise(True)
        plus.setToolTip("Create a workspace")
        plus.clicked.connect(self.create_requested)
        self.tabBar().setTabButton(
            0, QtWidgets.QTabBar.ButtonPosition.RightSide, plus
        )
        # The bar joins the tab chain (#962): its platform default
        # takes no focus, so the keyboard could never reach the
        # arrows below. TabFocus — not Strong — keeps the pointer
        # out of it: a click picks a tab and leaves the focus
        # where it stood.
        self.tabBar().setFocusPolicy(QtCore.Qt.FocusPolicy.TabFocus)
        self.tabBar().installEventFilter(self)

    def eventFilter(self, obj, event) -> bool:
        """Down leaves the bar (#962): the page below takes the
        focus, at its first focusable child — the same stop Tab
        reaches — so the bar answers no key the page below should
        hear (Left and Right, the tabs' own moves, still do)."""
        if (
            event.type() == QtCore.QEvent.Type.KeyPress
            and event.key() == QtCore.Qt.Key.Key_Down
        ):
            self.enter_page()
            return True
        return super().eventFilter(obj, event)

    def enter_page(self) -> None:
        """Focus the current page's first tab stop (#962): the
        focus chain's next focusable widget under the page, the
        page itself when nothing under it takes the keyboard (a
        stub, a blank page) — explicit focus stands even where
        the page's own policy would refuse a Tab."""
        page = self.currentWidget()
        target = self.first_tab_stop(page) or page
        # OtherFocusReason (#962): explicit focus, which stands
        # even where the page's own policy would refuse a Tab —
        # the arrow is not a tab stop, it is a jump.
        target.setFocus(QtCore.Qt.FocusReason.OtherFocusReason)

    def first_tab_stop(self, page: QtWidgets.QWidget):
        """The page's first tab stop, or None (#962): the focus
        chain walked from the page — depth-first, the page's own
        descendants before anything else — until a widget the
        Tab key would stop on. The walk stops at the page's
        edge: a stop outside the page (the strip itself, a
        later tab's content) is not the page's own, and the
        chain's wrap back to the page answers None."""
        seen = page.nextInFocusChain()
        while self.inside_walk(page, seen):
            if self.is_tab_stop(page, seen):
                return seen
            seen = seen.nextInFocusChain()
        return None

    def inside_walk(self, page, seen) -> bool:
        """Whether the walk continues at this widget: a real
        widget, inside the page's subtree and not the page
        itself — the page is the chain's own wrap point, so the
        cycle back to it ends the walk (a page the chain can
        walk forever — a window with nothing focusable under
        it — hangs nothing)."""
        return (
            seen is not None and seen is not page and page.isAncestorOf(seen)
        )

    def is_tab_stop(self, page, seen) -> bool:
        """Whether the walk's current widget is the page's own
        tab stop: inside the subtree, not the page itself, and
        carrying the Tab key's focus bit."""
        return seen is not page and (
            seen.focusPolicy() & QtCore.Qt.FocusPolicy.TabFocus
        )
