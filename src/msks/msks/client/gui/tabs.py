"""The main window's tab strip (#962, phase 4.6 of #924): the
top-level pages the window opens on. "Workspaces" is first — the
list and the detail pages over the bridge (#939, #945) — and the
Images and Secrets tabs of the plan (#963–#966) land beside it
through :meth:`MainTabs.addTab` — Images since #964, the
published channel and the catalog under the second tab, and
Secrets since #966, the placeholder listing and the audit
group full width under the third (#997). The bar
crosses by arrow keys (the spatial-navigation rule carried to the
GUI): Left and Right move between the tabs, Down leaves the bar
for the page below, and the bar never holds the keyboard without
a way out (no focus trap). The strip itself stays plain (#1011):
the Workspaces tab carried a plus badge for the create action
until the action moved to the page's own button row, and no tab
carries an embedded button now. The workspace page's
terminal-only mode (#1152) hides the whole bar through
:meth:`MainTabs.set_bar_hidden`, and the pages fill the strip
while it stands.
"""

from PySide6 import QtCore, QtWidgets

from .arrival import Settle, stagger_on_change
from .tabstyle import mark_active

#: The first tab's label (#962): the workspace screens' page.
WORKSPACES_LABEL = "Workspaces"

#: The second tab's label (#964): the images page — the catalog
#: and the published channel, the TUI image page's counterpart.
IMAGES_LABEL = "Images"

#: The third tab's label (#966): the secrets page — the
#: placeholder listing and the audit group, full width
#: (#997), the TUI secrets page's counterpart.
SECRETS_LABEL = "Secrets"

#: The fourth tab's label (#1214): the settings page — the
#: dictate pill's settings and the speech model's download.
SETTINGS_LABEL = "Settings"


class MainTabs(QtWidgets.QTabWidget):
    """The window's top-level tabs (#962): the workspaces page
    under the first tab, the later phase 4.6 pages beside it.
    The bar carries no embedded buttons (#1011): the create
    action the Workspaces tab's plus badge once opened lives on
    the listing's own button row, the page's bottom bar the
    detail pages' Mounts and Proxies tabs set.

    The bar joins the tab chain (#962): its platform default
    takes no focus, so the keyboard could never reach the
    arrows below. TabFocus — not Strong — keeps the pointer
    out of it: a click picks a tab and leaves the focus
    where it stood.
    """

    def __init__(self, workspaces: QtWidgets.QWidget, bridge=None) -> None:
        super().__init__()
        self.bridge = bridge
        self.addTab(workspaces, WORKSPACES_LABEL)
        self.tabBar().setFocusPolicy(QtCore.Qt.FocusPolicy.TabFocus)
        # The active tab's mark (#1012): the same accent tint
        # the workspace detail strip carries, so both tab
        # levels read the same way.
        mark_active(self.tabBar())
        self.tabBar().installEventFilter(self)
        # The page settle on every tab change (#1177): the page
        # shown clears, stands in its final shape behind the
        # gate — the bridge's refresh landed, the layout quiet
        # — then shows at once, its visible tables' rows rising
        # from the first paint. The opening's own settle again,
        # one gate per page, so a page returned to mid-hold
        # starts its hold over.
        self.gates: dict[QtWidgets.QWidget, Settle] = {}
        self.currentChanged.connect(self.settle)
        # The rows of the page's visible tables rise on the same
        # change (#1177), a loop iteration after the show.
        stagger_on_change(self, self.widget)

    def settle(self, index: int) -> None:
        """A tab change's settle: the page the strip shows
        clears and shows once its refresh has landed. A change
        naming no page — the strip's last tab leaving — asks
        nothing."""
        page = self.widget(index)
        if page is None:
            return
        gate = self.gates.get(page)
        if gate is None:
            gate = self.gates[page] = Settle(page, self.bridge)
        gate.hold()

    def set_bar_hidden(self, hidden: bool) -> None:
        """Hide the strip's own bar (#1152): the workspace page's
        terminal-only mode asks for it — the pages fill the
        strip while it stands, the current tab keeps its place,
        and a hidden bar takes no key and no focus. The page's
        hide brings the bar back through the same ask."""
        self.tabBar().setVisible(not hidden)

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
