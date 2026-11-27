"""The shell tabs' drag gestures (#1203): a left-drag on a strip
carries a tab as a Qt drag-and-drop — along the strip, where the
others make room and the tab takes its dropped place and Ctrl+Tab
follows the new order; off every strip, where the tab tears off into
a window of its own that keeps the session running; and over another
strip of the workspace, where the bar shows the landing place and the
drop seats the shell there. The drag's loop is the desktop's own, so
the suite stands a scripted drag in for it (:class:`FakeDrag`): the
press and the move that start the drag are real events on the bar,
the script delivers the drag's enter, move, and drop events to the
bars as the desktop would, and the action the script leaves answers
the release. The torn-off window's own strip — its close button,
double-click rename, "+", and shell keys — the gestures in the
terminal-only mode, the main window's close reaching the torn-off
shells, and the shared title editor's Escape claim (#1099, moved
into the editor). All on the offscreen platform over real sleeper
children (#954's fixture shape)."""

import sys

import pytest
from test_gui_shells import (
    bare_titles,
    open_page,
    press,
    pressed_keys,
    strip_titles,
)
from test_gui_shells import (
    hub as hub,  # noqa: F401 — the fixtures, shared
)
from test_gui_shells import (
    qt as qt,
)
from test_gui_shells import (
    tabs as tabs,
)
from test_gui_shells import (
    window_shell as window_shell,
)
from test_gui_workspace import SLEEPER, StubBridge, daemon_row  # noqa: E402

# The hub's pty children spawn through ``fork()`` on Qt's own
# threads (termsession's pty design, #951); Python 3.14 warns on
# the multi-threaded fork by design of the spawn — the shells
# suite's own noise.
pytestmark = pytest.mark.filterwarnings(
    "ignore:This process.*multi-threaded.*:DeprecationWarning"
)


@pytest.fixture(autouse=True)
def linux_shaped_keys(monkeypatch: pytest.MonkeyPatch) -> None:
    """The shell keys are the Linux spelling (#1184), the shells
    suite's pin."""
    monkeypatch.setattr(sys, "platform", "linux")


# --- the drag's stand-in and its event shapes ---------------------------


class FakeDrag:
    """The suite's stand-in for :class:`QtGui.QDrag` (#1203): the
    drag's loop is the desktop's own and no offscreen pointer drives
    it, so ``exec`` runs the test's script — the drag's path,
    delivered to the bars as the desktop would deliver it — and
    answers the action the test set. Every drag made is kept."""

    script = None
    action = None
    made: list = []

    def __init__(self, source) -> None:
        self.source = source
        self.mime = None
        self.pixmap = None
        self.hot = None
        FakeDrag.made.append(self)

    def setMimeData(self, data) -> None:  # noqa: N802
        self.mime = data

    def setPixmap(self, pixmap) -> None:  # noqa: N802
        self.pixmap = pixmap

    def setHotSpot(self, hot) -> None:  # noqa: N802
        self.hot = hot

    def exec(self, actions):  # noqa: N802
        if FakeDrag.script is not None:
            FakeDrag.script(self)
        return FakeDrag.action


@pytest.fixture()
def dragging(qt, monkeypatch: pytest.MonkeyPatch) -> None:
    """The scripted drag stands in for the desktop's own."""
    from msks.client.gui import shelldrag

    monkeypatch.setattr(shelldrag, "make_drag", FakeDrag)
    FakeDrag.made = []
    FakeDrag.script = None
    FakeDrag.action = qt.QtCore.Qt.DropAction.IgnoreAction
    # the pointer parked off every strip until a test moves it: the
    # offscreen cursor would else sit wherever the last test left it
    qt.QtGui.QCursor.setPos(qt.QtCore.QPoint(900, 900))


def mouse(qt, kind, local, global_, button, buttons):
    return qt.QtGui.QMouseEvent(
        kind,
        qt.QtCore.QPointF(local),
        qt.QtCore.QPointF(global_),
        button,
        buttons,
        qt.QtCore.Qt.KeyboardModifier.NoModifier,
    )


def on_bar(qt, strip, kind, local, button, buttons):
    """A mouse event on the strip's bar at ``local`` (bar
    coordinates), its global position as the bar maps it."""
    bar = strip.tabBar()
    return qt.app.sendEvent(
        bar,
        mouse(qt, kind, local, bar.mapToGlobal(local), button, buttons),
    )


def left_press(qt, strip, local) -> None:
    Qt = qt.QtCore.Qt
    on_bar(
        qt,
        strip,
        qt.QtCore.QEvent.Type.MouseButtonPress,
        local,
        Qt.MouseButton.LeftButton,
        Qt.MouseButton.LeftButton,
    )


def left_move(qt, strip, local) -> bool:
    Qt = qt.QtCore.Qt
    return on_bar(
        qt,
        strip,
        qt.QtCore.QEvent.Type.MouseMove,
        local,
        Qt.MouseButton.NoButton,
        Qt.MouseButton.LeftButton,
    )


def left_release(qt, strip, local) -> None:
    Qt = qt.QtCore.Qt
    on_bar(
        qt,
        strip,
        qt.QtCore.QEvent.Type.MouseButtonRelease,
        local,
        Qt.MouseButton.LeftButton,
        Qt.MouseButton.NoButton,
    )


def payload(qt, workspace: str, no: int):
    """The drag's mime for one shell."""
    from msks.client.gui.shelldrag import shell_mime

    return shell_mime(workspace, no)


def drag_enter_event(qt, local, workspace: str, no: int):
    """A drag enter event at ``local`` (bar coordinates) carrying
    one shell, a move action proposed."""
    Qt = qt.QtCore.Qt
    data = payload(qt, workspace, no)
    event = qt.QtGui.QDragEnterEvent(
        qt.QtCore.QPointF(local),
        Qt.DropAction.MoveAction,
        data,
        Qt.MouseButton.NoButton,
        Qt.KeyboardModifier.NoModifier,
    )
    event.mime_kept = data  # the event owns no mime; this side does
    return event


#: The bar the drag last entered (#1203): the desktop enters a bar
#: before it moves over it and leaves it for the next, and Qt
#: answers a move or a drop for the bar that took the last enter
#: alone — so the helpers below enter first as the desktop would.
ENTERED = [None]


@pytest.fixture(autouse=True)
def no_bar_entered() -> None:
    ENTERED[0] = None


def drag_enter(qt, strip, local, workspace="ws-a", no=1) -> bool:
    ENTERED[0] = None
    event = drag_enter_event(qt, local, workspace, no)
    qt.app.sendEvent(strip.tabBar(), event)
    if event.isAccepted():
        ENTERED[0] = id(strip.tabBar())
    return event.isAccepted()


def entered(qt, strip, local, workspace, no) -> bool:
    """Whether the bar stands entered for a move or a drop: the
    drag enters it first when the last enter was another bar's."""
    if ENTERED[0] != id(strip.tabBar()):
        return drag_enter(qt, strip, local, workspace, no)
    return True


def drag_over(qt, strip, local, workspace="ws-a", no=1) -> bool:
    """A drag move over the bar; whether the bar took it."""
    if not entered(qt, strip, local, workspace, no):
        return False
    Qt = qt.QtCore.Qt
    data = payload(qt, workspace, no)
    event = qt.QtGui.QDragMoveEvent(
        qt.QtCore.QPointF(local),
        Qt.DropAction.MoveAction,
        data,
        Qt.MouseButton.NoButton,
        Qt.KeyboardModifier.NoModifier,
    )
    event.mime_kept = data  # the event owns no mime; this side does
    qt.app.sendEvent(strip.tabBar(), event)
    return event.isAccepted()


def drag_off(qt, strip) -> None:
    qt.app.sendEvent(strip.tabBar(), qt.QtGui.QDragLeaveEvent())
    ENTERED[0] = None


def drop_on(qt, strip, local, workspace="ws-a", no=1):
    """A drop on the bar at ``local``; the event, for its acceptance
    and its action."""
    entered(qt, strip, local, workspace, no)
    ENTERED[0] = None
    Qt = qt.QtCore.Qt
    data = payload(qt, workspace, no)
    event = qt.QtGui.QDropEvent(
        qt.QtCore.QPointF(local),
        Qt.DropAction.MoveAction,
        data,
        Qt.MouseButton.LeftButton,
        Qt.KeyboardModifier.NoModifier,
    )
    event.mime_kept = data  # the event owns no mime; this side does
    qt.app.sendEvent(strip.tabBar(), event)
    return event


def shown(qt, tabs, shells: int = 3, workspace: str = "ws-a") -> None:
    """The strip shown in a window-sized rect with ``shells``
    shells hosted: the bar laid out, its tab rects real."""
    for _ in range(shells):
        tabs.hub.open_shell(workspace)
    tabs.host(workspace)
    tabs.resize(900, 500)
    tabs.show()
    qt.app.processEvents()


def away(qt, strip) -> tuple:
    """A local point past the bar's edge, and its global
    position."""
    bar = strip.tabBar()
    local = qt.QtCore.QPoint(bar.width() + 200, bar.height() + 300)
    return local, bar.mapToGlobal(local)


def left_drag(qt, strip, index: int, script=None, action=None):
    """A left-drag of the tab at ``index`` of ``strip``: the press,
    the move past the drag distance that starts the drag, the
    script's path inside the drag's loop, and the action the drag
    answers. The drag made is returned."""
    bar = strip.tabBar()
    start = bar.tabRect(index).center()
    if action is not None:
        FakeDrag.action = action
    FakeDrag.script = script
    left_press(qt, strip, start)
    far = start + qt.QtCore.QPoint(qt.app.startDragDistance() + 1, 0)
    assert left_move(qt, strip, far) is True
    left_release(qt, strip, far)
    return FakeDrag.made[-1]


def tear_off(qt, strip, index: int, pointer=None):
    """A left-drag of the tab at ``index`` out of ``strip``,
    released off every strip — the pointer parked at ``pointer``,
    off every strip by default: the torn-off window, the one window
    the drag made."""
    hub = strip.hub
    before = {s.holder for s in hub.shells(strip.hosted)}

    def path(drag):
        # the pointer left the source's bar: the desktop sends the
        # leave, and the release lands on no strip
        drag_off(qt, strip)
        qt.QtGui.QCursor.setPos(
            pointer if pointer is not None else qt.QtCore.QPoint(900, 900)
        )

    left_drag(qt, strip, index, path, qt.QtCore.Qt.DropAction.IgnoreAction)
    made = [
        s.holder
        for s in hub.shells(strip.hosted)
        if s.holder is not None and s.holder not in before
    ]
    assert len(made) == 1
    return made[0]


def over(qt, strip, index: int, past_midpoint: bool = True):
    """A bar-local point over the tab at ``index`` of ``strip``:
    past its midpoint, or before."""
    rect = strip.tabBar().tabRect(index)
    x = rect.center().x() + (1 if past_midpoint else -1)
    return qt.QtCore.QPoint(x, rect.center().y())


def double_click(qt, widget, local) -> None:
    qt.app.sendEvent(
        widget,
        mouse(
            qt,
            qt.QtCore.QEvent.Type.MouseButtonDblClick,
            local,
            widget.mapToGlobal(local),
            qt.QtCore.Qt.MouseButton.LeftButton,
            qt.QtCore.Qt.MouseButton.LeftButton,
        ),
    )


# --- tear off -----------------------------------------------------------


def test_a_left_drag_out_of_the_strip_tears_the_tab_off(
    qt, hub, tabs, dragging
):
    """A left-drag released off every strip opens the terminal in a
    window of its own: the strip loses the tab, the session runs on
    without a restart — the same widget, the same child, the same
    record — and the window wears the tab's title over its own
    strip's tab."""
    shown(qt, tabs)
    session = hub.shells("ws-a")[0]
    widget = session.widget
    window = tear_off(qt, tabs, 0, pointer=qt.QtCore.QPoint(500, 400))
    qt.app.processEvents()
    assert window.isVisible()
    assert session.holder is window
    assert widget.window() is window
    assert widget.isVisible()
    assert not widget._closed
    assert hub.shells("ws-a")[0] is session
    assert bare_titles(tabs) == ["2", "3", "+"]
    assert bare_titles(window.strip) == ["1", "+"]
    assert window.windowTitle() == "1 — msks"
    # the window opened around the terminal's standing size, the
    # terminal under the strip's bar
    assert window.width() >= widget.width()
    bar = window.strip.tabBar()
    assert (
        widget.mapTo(window.strip, qt.QtCore.QPoint(0, 0)).y()
        > bar.geometry().bottom()
    )
    # the drag carried the shell's payload and the tab's own image
    drag = FakeDrag.made[-1]
    assert bytes(drag.mime.data("application/x-msks-shell")) == b"ws-a:1"
    assert drag.pixmap is not None and not drag.pixmap.isNull()
    assert drag.source is tabs.tabBar()


def test_the_torn_off_window_opens_under_the_pointer(qt, hub, tabs, dragging):
    """The window opens where the pointer let go, the pointer's place
    in the tab kept as its place in the window."""
    shown(qt, tabs)
    grab = (
        tabs.tabBar().tabRect(0).center() - tabs.tabBar().tabRect(0).topLeft()
    )
    window = tear_off(qt, tabs, 0, pointer=qt.QtCore.QPoint(500, 400))
    assert window.geometry().topLeft() == qt.QtCore.QPoint(500, 400) - grab
    assert FakeDrag.made[-1].hot == grab


def test_a_left_click_without_a_move_keeps_its_behavior(
    qt, window_shell, dragging
):
    """A left-click that does not move the pointer is the click it
    always was — the bar selects the tab — and a move under the drag
    distance starts no drag: nothing tears off and no drag is
    made."""
    shell, hub, _window = window_shell
    page = open_page(qt, shell, shells=2)
    tabs = page.shells
    bar = tabs.tabBar()
    start = bar.tabRect(0).center()
    left_press(qt, tabs, start)
    assert tabs.hub.drag.pressed is hub.shells("ws-a")[0]
    left_release(qt, tabs, start)
    assert tabs.hub.drag.pressed is None
    assert FakeDrag.made == []
    assert strip_titles(page) == ["1", "2", "+"]
    left_press(qt, tabs, start)
    left_move(
        qt,
        tabs,
        start + qt.QtCore.QPoint(qt.app.startDragDistance() - 1, 0),
    )
    assert FakeDrag.made == []
    left_release(qt, tabs, start)
    assert [s.holder for s in hub.shells("ws-a")] == [None, None]
    # the "+" is no shell: its press names nothing
    plus = bar.tabRect(2).center()
    left_press(qt, tabs, plus)
    assert tabs.hub.drag.pressed is None
    left_release(qt, tabs, plus)
    # a click on the "+" is the "+"'s own click: one more shell
    assert strip_titles(page) == ["1", "2", "3", "+"]


def test_a_right_press_is_no_drag(qt, hub, tabs, dragging):
    """Only the left button drags: a right press names no tab, and
    the moves after it start nothing."""
    shown(qt, tabs, shells=1)
    bar = tabs.tabBar()
    start = bar.tabRect(0).center()
    Qt = qt.QtCore.Qt
    on_bar(
        qt,
        tabs,
        qt.QtCore.QEvent.Type.MouseButtonPress,
        start,
        Qt.MouseButton.RightButton,
        Qt.MouseButton.RightButton,
    )
    assert tabs.hub.drag.pressed is None
    far = start + qt.QtCore.QPoint(qt.app.startDragDistance() + 1, 0)
    on_bar(
        qt,
        tabs,
        qt.QtCore.QEvent.Type.MouseMove,
        far,
        Qt.MouseButton.NoButton,
        Qt.MouseButton.RightButton,
    )
    assert FakeDrag.made == []
    assert tabs.hub.drag.dragging is False
    assert bare_titles(tabs) == ["1", "+"]


def test_a_press_off_every_tab_drags_nothing(qt, hub, tabs, dragging):
    """A left press on the bar's blank stretch past the "+" names no
    tab: the moves after it carry nothing and no drag is made."""
    shown(qt, tabs, shells=1)
    bar = tabs.tabBar()
    blank = qt.QtCore.QPoint(bar.width() + 1, bar.height() - 1)
    assert bar.tabAt(blank) < 0
    left_press(qt, tabs, blank)
    left_move(qt, tabs, blank + qt.QtCore.QPoint(200, 0))
    assert FakeDrag.made == []
    assert tabs.hub.drag.dragging is False
    assert bare_titles(tabs) == ["1", "+"]


def test_a_shell_closing_under_the_press_ends_the_drag(
    qt, hub, tabs, dragging
):
    """The pressed shell's tab closing before the drag begins — its
    program exited, the page's close — leaves the moves after it
    carrying nothing: a closed shell's widget is gone."""
    shown(qt, tabs, shells=2)
    bar = tabs.tabBar()
    left_press(qt, tabs, bar.tabRect(0).center())
    hub.close_shell("ws-a", 1)
    local, _global = away(qt, tabs)
    left_move(qt, tabs, local)
    assert tabs.hub.drag.pressed is None
    assert tabs.hub.drag.dragging is False
    assert FakeDrag.made == []
    left_release(qt, tabs, local)
    assert bare_titles(tabs) == ["2", "+"]


# --- reorder ------------------------------------------------------------


def test_a_drag_along_the_strip_moves_the_tab(qt, hub, tabs, dragging):
    """A drag along the strip moves the tab: the others make room as
    the pointer passes their midpoints, the tab takes the place at
    the drop, and the hub's order — what Ctrl+Tab cycles through —
    is the strip's."""
    shown(qt, tabs)
    bar = tabs.tabBar()
    assert tabs.currentIndex() == 2
    seen_before = []

    def path(drag):
        # past the first neighbor's midpoint: the tab takes the
        # pointer's place and becomes current
        assert drag_over(qt, tabs, over(qt, tabs, 1), no=1)
        assert bare_titles(tabs) == ["2", "1", "3", "+"]
        assert tabs.currentIndex() == 1
        assert [s.no for s in hub.shells("ws-a")] == [1, 2, 3]  # not yet
        assert bar.insertion is None  # the source bar marks nothing
        assert drag_over(qt, tabs, over(qt, tabs, 2), no=1)
        assert bare_titles(tabs) == ["2", "3", "1", "+"]
        seen_before.append(tabs.currentWidget())
        event = drop_on(qt, tabs, over(qt, tabs, 2), no=1)
        assert event.isAccepted()
        FakeDrag.action = qt.QtCore.Qt.DropAction.MoveAction

    left_drag(qt, tabs, 0, path)
    assert [s.no for s in hub.shells("ws-a")] == [2, 3, 1]
    assert tabs.currentIndex() == 2
    # the new order is the cycle the step keys drive (the keys
    # themselves land through the page's shell keys below)
    assert tabs.currentWidget() is hub.find("ws-a", 1).widget
    tabs.step_shell(1)
    assert tabs.currentWidget() is hub.find("ws-a", 2).widget
    tabs.step_shell(1)
    assert tabs.currentWidget() is hub.find("ws-a", 3).widget
    tabs.step_shell(-1)
    assert tabs.currentWidget() is hub.find("ws-a", 2).widget


def test_the_tab_moves_only_past_a_neighbors_midpoint(qt, hub, tabs, dragging):
    """Tabs of different widths trade places under a still pointer
    without the rule: the tab moves once the pointer passes the
    neighbor's midpoint, and not before."""
    shown(qt, tabs)
    bar = tabs.tabBar()
    wide = "a-title-wide-enough-to-widen-the-tab"
    hub.rename("ws-a", 2, wide)
    qt.app.processEvents()
    assert bar.tabRect(1).width() > bar.tabRect(0).width()

    def path(drag):
        assert drag_over(
            qt, tabs, over(qt, tabs, 1, past_midpoint=False), no=1
        )
        assert bare_titles(tabs) == ["1", wide, "3", "+"]
        assert drag_over(qt, tabs, over(qt, tabs, 1), no=1)
        assert bare_titles(tabs)[0] == wide
        drop_on(qt, tabs, over(qt, tabs, 1), no=1)
        FakeDrag.action = qt.QtCore.Qt.DropAction.MoveAction

    left_drag(qt, tabs, 0, path)
    assert [s.no for s in hub.shells("ws-a")] == [2, 1, 3]


def test_a_drag_that_never_leaves_its_own_half_changes_nothing(
    qt, hub, tabs, dragging
):
    """A drag dropped inside the dragged tab's own left half
    reorders nothing: the hub's order stands and the bar's."""
    shown(qt, tabs)
    bar = tabs.tabBar()
    inside = qt.QtCore.QPoint(bar.tabRect(0).left() + 30, 5)

    def path(drag):
        assert drag_over(qt, tabs, inside, no=1)
        drop_on(qt, tabs, inside, no=1)
        FakeDrag.action = qt.QtCore.Qt.DropAction.MoveAction

    left_drag(qt, tabs, 0, path)
    assert [s.no for s in hub.shells("ws-a")] == [1, 2, 3]
    assert bare_titles(tabs) == ["1", "2", "3", "+"]


def test_a_reorder_survives_the_unhost_and_the_rehost(qt, hub, tabs, dragging):
    """The order lives in the hub: another workspace's page visit
    and the return find the tabs where the drag left them."""
    shown(qt, tabs)

    def path(drag):
        drag_over(qt, tabs, over(qt, tabs, 2), no=1)
        drop_on(qt, tabs, over(qt, tabs, 2), no=1)
        FakeDrag.action = qt.QtCore.Qt.DropAction.MoveAction

    left_drag(qt, tabs, 0, path)
    tabs.host("ws-b")
    tabs.host("ws-a")
    assert bare_titles(tabs) == ["2", "3", "1", "+"]


def test_a_reorder_leaves_the_torn_off_shells_in_place(
    qt, hub, tabs, dragging
):
    """A torn-off shell keeps its relative place in the order while
    the page's tabs reorder among themselves, and a return docks at
    the place the strip names."""
    shown(qt, tabs)
    tear_off(qt, tabs, 1)  # shell 2 leaves; the strip holds 1, 3

    def path(drag):
        drag_over(qt, tabs, over(qt, tabs, 1), no=1)
        drop_on(qt, tabs, over(qt, tabs, 1), no=1)
        FakeDrag.action = qt.QtCore.Qt.DropAction.MoveAction

    left_drag(qt, tabs, 0, path)
    # the dragged shell lands at the end of the page's order — past
    # the one other docked shell — and the torn-off one keeps its
    # own place
    assert [s.no for s in hub.shells("ws-a")] == [2, 3, 1]
    assert bare_titles(tabs) == ["3", "1", "+"]
    tabs.dock(2, 0)
    assert [s.no for s in hub.shells("ws-a")] == [2, 3, 1]
    assert bare_titles(tabs) == ["2", "3", "1", "+"]
    # a dock at the end lands after the last docked shell
    tear_off(qt, tabs, 0)
    tabs.dock(2, 2)
    assert [s.no for s in hub.shells("ws-a")] == [3, 1, 2]
    assert bare_titles(tabs) == ["3", "1", "2", "+"]


def test_a_dock_naming_a_docked_shell_changes_nothing(qt, hub, tabs, dragging):
    """A dock for a shell already in the strip moves nothing, and
    one naming no standing session neither."""
    shown(qt, tabs, shells=1)
    tabs.dock(1, 0)
    tabs.dock(9, 0)
    assert bare_titles(tabs) == ["1", "+"]


def test_a_reorder_in_a_window_is_the_windows_own(qt, hub, tabs, dragging):
    """A drag along a torn-off window's strip reorders that window's
    tabs in the hub's order; the page's shells keep their places."""
    shown(qt, tabs, shells=2)
    window = tear_off(qt, tabs, 0)
    window.open_shell()  # shell 3 joins the window
    strip = window.strip
    assert bare_titles(strip) == ["1", "3", "+"]

    def path(drag):
        drag_over(qt, strip, over(qt, strip, 1), no=1)
        assert bare_titles(strip) == ["3", "1", "+"]
        drop_on(qt, strip, over(qt, strip, 1), no=1)
        FakeDrag.action = qt.QtCore.Qt.DropAction.MoveAction

    left_drag(qt, strip, 0, path)
    assert bare_titles(strip) == ["3", "1", "+"]
    # shell 1 moved past shell 3 — both the window's — in the hub's
    # order; the page's shell 2 kept its place before them
    assert [s.no for s in hub.shells("ws-a")] == [2, 3, 1]
    strip.host("ws-a")  # a rehost reads the hub's order
    assert bare_titles(strip) == ["3", "1", "+"]


def test_ctrl_tab_follows_the_dragged_order_by_key(qt, window_shell, dragging):
    """After a drag along the page's strip, Ctrl+Tab pressed on a
    terminal cycles the dragged order — the acceptance criterion,
    by the key itself."""
    shell, hub, _window = window_shell
    page = open_page(qt, shell, shells=3)
    tabs = page.shells

    def path(drag):
        drag_over(qt, tabs, over(qt, tabs, 2), no=1)
        drop_on(qt, tabs, over(qt, tabs, 2), no=1)
        FakeDrag.action = qt.QtCore.Qt.DropAction.MoveAction

    left_drag(qt, tabs, 0, path)
    assert strip_titles(page) == ["2", "3", "1", "+"]
    terminal = hub.find("ws-a", 1).widget
    assert qt.app.focusWidget() is terminal
    seen = pressed_keys(qt, terminal)
    press(qt, terminal, "Key_Tab", "ctrl")
    assert qt.app.focusWidget() is hub.find("ws-a", 2).widget
    press(qt, qt.app.focusWidget(), "Key_Tab", "ctrl")
    assert qt.app.focusWidget() is hub.find("ws-a", 3).widget
    press(qt, qt.app.focusWidget(), "Key_Tab", "ctrl")
    assert qt.app.focusWidget() is terminal
    assert qt.QtCore.Qt.Key.Key_Tab not in seen


def test_a_move_heard_from_the_hub_lands_the_tab(qt, hub, tabs, dragging):
    """A reorder the strip did not make itself — the hub's order
    moved by another hand — lands the tab at its new place among
    the strip's shells, the tab current."""
    shown(qt, tabs)
    hub.move("ws-a", 1, 2)
    assert bare_titles(tabs) == ["2", "3", "1", "+"]
    assert tabs.currentWidget() is hub.find("ws-a", 1).widget


# --- the tab dropped onto another strip --------------------------------


def test_a_window_tab_over_the_strip_shows_the_landing(
    qt, hub, tabs, dragging
):
    """A window's tab dragged over the page's strip shows where the
    shell will land — the marker at the insertion point's left
    edge, repainted as it moves — and the drop there seats the
    shell in the page at that place, the window down with its last
    shell."""
    shown(qt, tabs)
    bar = tabs.tabBar()
    session = hub.shells("ws-a")[0]
    window = tear_off(qt, tabs, 0)
    assert bare_titles(tabs) == ["2", "3", "+"]
    updates = []
    bar.update = lambda: updates.append(bar.insertion)

    def path(drag):
        assert drag_enter(qt, tabs, over(qt, tabs, 0), no=1)  # past "2"
        assert bar.insertion == 1
        assert bar.insertion_x() == bar.tabRect(1).left()
        assert drag_over(
            qt, tabs, over(qt, tabs, 1), no=1
        )  # past "3": the end
        assert bar.insertion == 2
        assert bar.insertion_x() == bar.tabRect(2).left()  # the "+"
        drag_over(qt, tabs, over(qt, tabs, 0), no=1)
        drop_on(qt, tabs, over(qt, tabs, 0), no=1)
        FakeDrag.action = qt.QtCore.Qt.DropAction.MoveAction

    left_drag(qt, window.strip, 0, path)
    assert updates == [1, 2, 1, None]
    assert session.holder is None
    assert not window.isVisible()
    assert bare_titles(tabs) == ["2", "1", "3", "+"]
    assert [s.no for s in hub.shells("ws-a")] == [2, 1, 3]
    assert tabs.currentWidget() is session.widget
    assert bar.insertion is None


def test_the_marker_paints(qt, hub, tabs):
    """A standing marker paints on the bar — the grab runs the
    paint — and a cleared one leaves no line in the pixels."""
    shown(qt, tabs, shells=1)
    bar = tabs.tabBar()
    plain = bar.grab().toImage()
    bar.insertion = 1
    marked = bar.grab().toImage()
    x = bar.insertion_x()
    column = [marked.pixel(xx, bar.height() // 2) for xx in (x, x + 1)]
    base = plain.pixel(x, bar.height() // 2)
    assert any(pixel != base for pixel in column)
    bar.insertion = None
    assert bar.grab().toImage() == plain


def test_a_leave_clears_the_marker(qt, hub, tabs, dragging):
    """The drag entering a strip stands the marker; its leave takes
    the marker off, and a later enter stands it again."""
    shown(qt, tabs, shells=2)
    bar = tabs.tabBar()
    window = tear_off(qt, tabs, 0)
    assert drag_enter(qt, tabs, over(qt, tabs, 0), no=1)
    assert bar.insertion == 1
    drag_off(qt, tabs)
    assert bar.insertion is None
    assert drag_enter(qt, tabs, over(qt, tabs, 0), no=1)
    assert bar.insertion == 1
    drag_off(qt, tabs)
    assert bar.insertion is None
    assert window.isVisible()


def test_the_marker_set_again_repaints_nothing(qt, hub, tabs, monkeypatch):
    """A marker set to the index it already stands at asks for no
    repaint: the drag's every move would otherwise repaint the bar
    under a still pointer."""
    shown(qt, tabs, shells=1)
    bar = tabs.tabBar()
    calls = []
    monkeypatch.setattr(bar, "update", lambda: calls.append(1))
    bar.insertion = 0
    bar.insertion = 0
    bar.insertion = 1
    assert calls == [1, 1]
    bar.insertion = None


def test_releasing_off_every_strip_leaves_the_new_window(
    qt, hub, tabs, dragging
):
    """A drag from a torn-off window released off every strip leaves
    the shell in the window the drag opened for it, and the window
    it came from goes down with nothing left."""
    shown(qt, tabs, shells=1)
    session = hub.shells("ws-a")[0]
    first = tear_off(qt, tabs, 0)
    strip = first.strip
    assert first.isVisible()

    def path(drag):
        drag_off(qt, strip)  # the pointer left the window's bar
        drag_off(qt, tabs)  # and passed over the page's, never entering
        assert tabs.tabBar().insertion is None
        qt.QtGui.QCursor.setPos(qt.QtCore.QPoint(700, 700))

    left_drag(qt, strip, 0, path, qt.QtCore.Qt.DropAction.IgnoreAction)
    assert session.holder is not first
    assert session.holder.isVisible()
    assert (
        session.holder.geometry().topLeft()
        == qt.QtCore.QPoint(700, 700) - FakeDrag.made[-1].hot
    )
    assert not first.isVisible()
    assert bare_titles(tabs) == ["+"]


def test_a_press_on_a_window_tab_without_a_move_drags_nothing(
    qt, hub, tabs, dragging
):
    """A press and a release on a window's tab with no move is the
    bar's own: the window stands and no drag is made."""
    shown(qt, tabs, shells=1)
    window = tear_off(qt, tabs, 0)
    strip = window.strip
    start = strip.tabBar().tabRect(0).center()
    left_press(qt, strip, start)
    assert tabs.hub.drag.source is strip
    left_release(qt, strip, start)
    assert tabs.hub.drag.pressed is None
    assert tabs.hub.drag.dragging is False
    assert len(FakeDrag.made) == 1  # the tear-off's alone
    assert window.isVisible()


def test_a_hidden_strip_is_no_landing(qt, hub, tabs, dragging):
    """A hidden bar — the page on another tab — takes no drop: the
    drag over its place is refused and marks nothing, and the shell
    tears off into a window of its own instead."""
    shown(qt, tabs, shells=2)
    window = tear_off(qt, tabs, 0)
    tabs.hide()

    def path(drag):
        assert drag_enter(qt, tabs, over(qt, tabs, 0), no=1) is False
        assert tabs.tabBar().insertion is None
        assert drop_on(qt, tabs, over(qt, tabs, 0), no=1).isAccepted() is False
        drag_off(qt, window.strip)
        qt.QtGui.QCursor.setPos(qt.QtCore.QPoint(700, 700))

    left_drag(qt, window.strip, 0, path, qt.QtCore.Qt.DropAction.IgnoreAction)
    assert bare_titles(tabs) == ["2", "+"]
    assert hub.shells("ws-a")[0].holder.isVisible()
    assert hub.shells("ws-a")[0].holder is not window


def test_a_strip_of_another_workspace_refuses_the_drag(
    qt, hub, tabs, dragging
):
    """The page showing another workspace is no landing for a shell
    of the first: its strip refuses the drag, and the shell tears
    off into a window of its own — a shell of the workspace the
    page does not show, kept whole in the first workspace's
    records."""
    shown(qt, tabs, shells=2)
    window = tear_off(qt, tabs, 0)
    tabs.host("ws-b")

    def path(drag):
        assert drag_enter(qt, tabs, over(qt, tabs, 0), no=1) is False
        assert drop_on(qt, tabs, over(qt, tabs, 0), no=1).isAccepted() is False
        drag_off(qt, window.strip)
        qt.QtGui.QCursor.setPos(qt.QtCore.QPoint(700, 700))

    left_drag(qt, window.strip, 0, path, qt.QtCore.Qt.DropAction.IgnoreAction)
    assert [s.no for s in hub.shells("ws-a")] == [1, 2]
    assert [s.no for s in hub.shells("ws-b")] == []


def test_a_drag_of_another_payload_is_refused(qt, hub, tabs, dragging):
    """A drag carrying no shell of the hub — another application's,
    or a malformed payload — is refused: nothing is marked, nothing
    lands."""
    shown(qt, tabs, shells=1)
    bar = tabs.tabBar()
    Qt = qt.QtCore.Qt
    other = qt.QtCore.QMimeData()
    other.setText("not a shell")
    event = qt.QtGui.QDragEnterEvent(
        qt.QtCore.QPointF(over(qt, tabs, 0)),
        Qt.DropAction.MoveAction,
        other,
        Qt.MouseButton.NoButton,
        Qt.KeyboardModifier.NoModifier,
    )
    event.mime_kept = other  # the event owns no mime; this side does
    qt.app.sendEvent(bar, event)
    assert not event.isAccepted()
    assert bar.insertion is None
    malformed = qt.QtCore.QMimeData()
    malformed.setData("application/x-msks-shell", b"ws-a:")
    event = qt.QtGui.QDragEnterEvent(
        qt.QtCore.QPointF(over(qt, tabs, 0)),
        Qt.DropAction.MoveAction,
        malformed,
        Qt.MouseButton.NoButton,
        Qt.KeyboardModifier.NoModifier,
    )
    event.mime_kept = malformed  # the event owns no mime; this side does
    qt.app.sendEvent(bar, event)
    assert not event.isAccepted()
    assert drag_enter(qt, tabs, over(qt, tabs, 0), no=9) is False  # unknown
    # a drag proposing a copy is not a move of a shell: refused
    copy = qt.QtCore.QMimeData()
    copy.setData("application/x-msks-shell", b"ws-a:1")
    event = qt.QtGui.QDragEnterEvent(
        qt.QtCore.QPointF(over(qt, tabs, 0)),
        Qt.DropAction.CopyAction,
        copy,
        Qt.MouseButton.NoButton,
        Qt.KeyboardModifier.NoModifier,
    )
    event.mime_kept = copy  # the event owns no mime; this side does
    qt.app.sendEvent(bar, event)
    assert not event.isAccepted()
    assert bar.insertion is None


def test_a_tab_drops_from_the_page_into_a_window(qt, hub, tabs, dragging):
    """The page's tab dropped over a torn-off window's strip lands
    there at the marked place — before the tab the pointer sits
    before, after the one it sits past — and the page's strip loses
    it."""
    shown(qt, tabs)
    window = tear_off(qt, tabs, 2)  # shell 3
    assert bare_titles(window.strip) == ["3", "+"]
    first = tear_off(qt, tabs, 0)  # shell 1, its own window
    assert bare_titles(window.strip) == ["3", "+"]

    def path(drag):
        # past "3": after it
        assert drag_enter(qt, window.strip, over(qt, window.strip, 0), no=2)
        assert window.strip.tabBar().insertion == 1
        drop_on(qt, window.strip, over(qt, window.strip, 0), no=2)
        FakeDrag.action = qt.QtCore.Qt.DropAction.MoveAction

    left_drag(qt, tabs, 0, path)  # shell 2, now the page's first tab
    assert bare_titles(window.strip) == ["3", "2", "+"]
    assert bare_titles(tabs) == ["+"]
    assert [s.no for s in hub.shells("ws-a")] == [1, 3, 2]
    assert window.strip.currentWidget() is hub.find("ws-a", 2).widget

    # and before "3" from the other window
    def path2(drag):
        assert drag_enter(
            qt,
            window.strip,
            over(qt, window.strip, 0, past_midpoint=False),
            no=1,
        )
        assert window.strip.tabBar().insertion == 0
        drop_on(
            qt,
            window.strip,
            over(qt, window.strip, 0, past_midpoint=False),
            no=1,
        )
        FakeDrag.action = qt.QtCore.Qt.DropAction.MoveAction

    left_drag(qt, first.strip, 0, path2)
    assert bare_titles(window.strip) == ["1", "3", "2", "+"]
    assert [s.no for s in hub.shells("ws-a")] == [1, 3, 2]
    assert not first.isVisible()


def test_a_tab_drops_from_one_window_into_another(qt, hub, tabs, dragging):
    """Two torn-off windows: a tab dropped from one onto the other's
    strip joins it there, and the emptied window goes down."""
    shown(qt, tabs, shells=2)
    first = tear_off(qt, tabs, 0)
    second = tear_off(qt, tabs, 0)
    assert bare_titles(first.strip) == ["1", "+"]
    assert bare_titles(second.strip) == ["2", "+"]

    def path(drag):
        assert drag_enter(qt, first.strip, over(qt, first.strip, 0), no=2)
        drop_on(qt, first.strip, over(qt, first.strip, 0), no=2)
        FakeDrag.action = qt.QtCore.Qt.DropAction.MoveAction

    left_drag(qt, second.strip, 0, path)
    assert bare_titles(first.strip) == ["1", "2", "+"]
    assert not second.isVisible()
    assert [s.holder for s in hub.shells("ws-a")] == [first, first]


def test_a_shell_closing_mid_drag_lands_nothing(qt, hub, tabs, dragging):
    """The dragged shell closing while the drag runs — the program
    inside exited — leaves nothing to seat: every strip refuses the
    drag's payload, and the drag's end tears off no window."""
    shown(qt, tabs, shells=2)

    def path(drag):
        hub.close_shell("ws-a", 1)
        assert drag_enter(qt, tabs, over(qt, tabs, 0), no=1) is False
        assert drop_on(qt, tabs, over(qt, tabs, 0), no=1).isAccepted() is False
        drag_off(qt, tabs)
        qt.QtGui.QCursor.setPos(qt.QtCore.QPoint(700, 700))

    left_drag(qt, tabs, 0, path, qt.QtCore.Qt.DropAction.IgnoreAction)
    assert [s.no for s in hub.shells("ws-a")] == [2]
    assert all(s.holder is None for s in hub.shells("ws-a"))
    assert tabs.hub.drag.pressed is None


def test_the_drag_seam_makes_a_qt_drag(qt, hub, tabs):
    """The seam the suite stands in for (:func:`shelldrag.make_drag`)
    makes a real Qt drag of the bar. The drag's loop — the pixmap
    under the pointer, the drop's action answered — is the
    desktop's own and runs nowhere offscreen."""
    from msks.client.gui import shelldrag

    shown(qt, tabs, shells=1)
    drag = shelldrag.make_drag(tabs.tabBar())
    assert isinstance(drag, qt.QtGui.QDrag)
    assert drag.source() is tabs.tabBar()


# --- the torn-off window's own strip ------------------------------------


def test_the_windows_close_button_ends_the_shell(qt, hub, tabs, dragging):
    """The tab's close button in the window ends the session — the
    widget's teardown ran, the record leaves the hub — and the
    window goes down with its last shell."""
    shown(qt, tabs)
    session = hub.shells("ws-a")[0]
    window = tear_off(qt, tabs, 0)
    button = window.strip.tabBar().tabButton(
        0, qt.QtWidgets.QTabBar.ButtonPosition.RightSide
    )
    button.click()
    assert session.widget._closed
    assert [s.no for s in hub.shells("ws-a")] == [2, 3]
    assert not window.isVisible()


def test_closing_the_window_ends_its_shells(qt, hub, tabs, dragging):
    """A close of the window itself is the tab's close for every
    shell it holds: the records go down."""
    shown(qt, tabs)
    window = tear_off(qt, tabs, 0)
    window.open_shell()  # shell 4 joins the window
    first, fourth = hub.find("ws-a", 1), hub.find("ws-a", 4)
    window.close()
    assert first.widget._closed and fourth.widget._closed
    assert [s.no for s in hub.shells("ws-a")] == [2, 3]
    assert not window.isVisible()


def test_a_close_from_elsewhere_takes_the_window_down(qt, hub, tabs, dragging):
    """A shell closed by the hub — the page's own close, the quit
    path — takes its torn-off window down when it held that shell
    alone, and leaves a window with other shells standing."""
    shown(qt, tabs)
    window = tear_off(qt, tabs, 0)
    hub.close_shell("ws-a", 1)
    assert not window.isVisible()
    other = tear_off(qt, tabs, 0)
    other.open_shell()  # shell 4 joins the window beside shell 2
    hub.close_shell("ws-a", 2)
    assert other.isVisible()
    assert bare_titles(other.strip) == ["4", "+"]


def test_a_double_click_on_a_window_tab_renames_the_shell(
    qt, hub, tabs, dragging
):
    """The in-place rename in the window's strip: a left
    double-click on the tab opens the editor over it; Return
    stands the name on the record, the tab and the window title
    follow; Escape cancels."""
    shown(qt, tabs, shells=1)
    session = hub.shells("ws-a")[0]
    window = tear_off(qt, tabs, 0)
    bar = window.strip.tabBar()
    rect = bar.tabRect(0)
    double_click(qt, bar, rect.center())
    editor = window.strip.editor
    assert editor is not None
    assert editor.selectedText() == "1"
    editor.setText("build")
    qt.app.sendEvent(
        editor,
        qt.QtGui.QKeyEvent(
            qt.QtCore.QEvent.Type.KeyPress,
            qt.QtCore.Qt.Key.Key_Return,
            qt.QtCore.Qt.KeyboardModifier.NoModifier,
        ),
    )
    assert window.strip.editor is None
    assert session.label == "build"
    assert window.windowTitle() == "build — msks"
    assert bare_titles(window.strip) == ["build", "+"]
    double_click(qt, bar, rect.center())
    window.strip.editor.setText("nope")
    escape = qt.QtGui.QKeyEvent(
        qt.QtCore.QEvent.Type.KeyPress,
        qt.QtCore.Qt.Key.Key_Escape,
        qt.QtCore.Qt.KeyboardModifier.NoModifier,
    )
    assert qt.app.sendEvent(window.strip.editor, escape) is True
    assert window.strip.editor is None
    assert window.windowTitle() == "build — msks"


def test_the_window_title_follows_the_current_tab(qt, hub, tabs, dragging):
    """The window's title is the current shell's label: a tab
    switch, a program title, and a rename each move it; another
    shell's news does not; a window between shells carries the
    workspace's id."""
    shown(qt, tabs, shells=2)
    first = hub.shells("ws-a")[0]
    window = tear_off(qt, tabs, 0)
    window.open_shell()  # shell 3, current
    assert window.windowTitle() == "3 — msks"
    window.strip.setCurrentIndex(0)
    assert window.windowTitle() == "1 — msks"
    first.widget.title_changed.emit("vim")
    assert window.windowTitle() == "vim — msks"
    hub.rename("ws-a", 3, "build")
    assert window.windowTitle() == "vim — msks"
    window.strip.setCurrentIndex(1)
    assert window.windowTitle() == "build — msks"
    hub.close_shell("ws-a", 3)
    assert window.windowTitle() == "vim — msks"
    # the window between shells: current on the "+" alone
    window.strip.setCurrentIndex(window.strip.indexOf(window.strip.plus_page))
    assert window.windowTitle() == "ws-a — msks"


def test_the_windows_plus_opens_a_shell_in_the_window(qt, hub, tabs, dragging):
    """The window's "+" opens one more shell seated in the same
    window: the tab beside, current and focused."""
    shown(qt, tabs, shells=1)
    window = tear_off(qt, tabs, 0)
    window.strip.tabBar().tabBarClicked.emit(window.strip.shell_count())
    assert bare_titles(window.strip) == ["1", "2", "+"]
    assert [s.holder for s in hub.shells("ws-a")] == [window, window]
    assert bare_titles(tabs) == ["+"]
    assert window.strip.currentWidget() is hub.find("ws-a", 2).widget


# --- the keys in the window ---------------------------------------------


def in_window(qt, window):
    """The torn-off window active with its current terminal
    holding the keyboard — the shape the window's keys answer
    in."""
    window.activateWindow()
    qt.app.processEvents()
    terminal = window.strip.currentWidget()
    terminal.setFocus()
    qt.app.processEvents()
    assert qt.app.focusWidget() is terminal
    return terminal


def test_the_shell_keys_stand_on_the_window(qt, window_shell, dragging):
    """The window stands the page's shell keys minus the page
    tabs: Ctrl+Shift+T and W, Ctrl+Tab both ways, and Ctrl+1 to
    Ctrl+9 — one answer per press, the window their scope."""
    shell, hub, _window = window_shell
    page = open_page(qt, shell, shells=2)
    window = tear_off(qt, page.shells, 1)
    keys = window.keys
    spellings = [[k.toString() for k in s.keys()] for s in keys.shortcuts]
    assert spellings == [
        ["Ctrl+Shift+T"],
        ["Ctrl+Shift+W"],
        ["Ctrl+Tab"],
        ["Ctrl+Shift+Tab", "Ctrl+Backtab"],
        *[[f"Ctrl+{n}"] for n in range(1, 10)],
    ]
    for shortcut in keys.shortcuts:
        assert shortcut.context() == (
            qt.QtCore.Qt.ShortcutContext.WidgetWithChildrenShortcut
        )
        assert not shortcut.autoRepeat()
        assert shortcut.parent() is window


def test_ctrl_tab_steps_the_windows_own_tabs(qt, window_shell, dragging):
    """Ctrl+Tab and Ctrl+Shift+Tab in a torn-off window step among
    that window's tabs — the page's shells and other windows'
    left out — wrapping at both ends, and the key never reaches
    the program inside."""
    shell, hub, _window = window_shell
    page = open_page(qt, shell, shells=2)
    window = tear_off(qt, page.shells, 1)  # shell 2
    window.open_shell()  # shell 3
    terminal = in_window(qt, window)
    seen = pressed_keys(qt, terminal)
    press(qt, terminal, "Key_Tab", "ctrl")
    assert qt.app.focusWidget() is hub.find("ws-a", 2).widget
    press(qt, qt.app.focusWidget(), "Key_Tab", "ctrl")
    assert qt.app.focusWidget() is terminal  # the wrap
    press(qt, terminal, "Key_Backtab", "ctrl", "shift")
    assert qt.app.focusWidget() is hub.find("ws-a", 2).widget
    assert strip_titles(page) == ["1", "+"]
    assert qt.QtCore.Qt.Key.Key_Tab not in seen
    assert qt.QtCore.Qt.Key.Key_Backtab not in seen


def test_ctrl_tab_in_a_window_alone_changes_nothing(
    qt, window_shell, dragging
):
    """The window's one shell: the stepping keys have no other stop
    to make, and still never reach the program inside."""
    shell, hub, _window = window_shell
    page = open_page(qt, shell, shells=2)
    window = tear_off(qt, page.shells, 1)
    terminal = in_window(qt, window)
    seen = pressed_keys(qt, terminal)
    press(qt, terminal, "Key_Tab", "ctrl")
    press(qt, terminal, "Key_Backtab", "ctrl", "shift")
    press(qt, terminal, "Key_2", "ctrl")
    qt.app.processEvents()
    assert qt.app.focusWidget() is terminal
    assert qt.QtCore.Qt.Key.Key_Tab not in seen
    assert qt.QtCore.Qt.Key.Key_2 not in seen


def test_the_number_keys_jump_the_windows_tabs(qt, window_shell, dragging):
    """Ctrl+N in a window jumps to its Nth shell; a position past
    the window's tabs changes nothing; Alt+N stands nowhere in a
    window with no page tabs and reaches the program."""
    shell, hub, _window = window_shell
    page = open_page(qt, shell, shells=2)
    window = tear_off(qt, page.shells, 1)
    window.open_shell()  # shell 3
    terminal = in_window(qt, window)
    seen = pressed_keys(qt, terminal)
    press(qt, terminal, "Key_1", "ctrl")
    assert qt.app.focusWidget() is hub.find("ws-a", 2).widget
    press(qt, qt.app.focusWidget(), "Key_9", "ctrl")
    assert qt.app.focusWidget() is hub.find("ws-a", 2).widget
    press(qt, qt.app.focusWidget(), "Key_2", "ctrl")
    assert qt.app.focusWidget() is terminal
    press(qt, terminal, "Key_2", "alt")
    assert qt.QtCore.Qt.Key.Key_2 in seen
    assert window.strip.currentIndex() == 1


def test_ctrl_shift_w_in_the_window_closes_its_shell(
    qt, window_shell, dragging
):
    """Ctrl+Shift+W in a torn-off window ends the window's current
    shell — the next tab takes the keyboard — and the last one's
    close takes the window down; the W never reaches the
    program."""
    shell, hub, _window = window_shell
    page = open_page(qt, shell, shells=2)
    window = tear_off(qt, page.shells, 1)
    window.open_shell()  # shell 3
    terminal = in_window(qt, window)
    seen = pressed_keys(qt, terminal)
    press(qt, terminal, "Key_W", "ctrl", "shift")
    assert [s.no for s in hub.shells("ws-a")] == [1, 2]
    assert qt.app.focusWidget() is hub.find("ws-a", 2).widget
    assert window.isVisible()
    press(qt, qt.app.focusWidget(), "Key_W", "ctrl", "shift")
    assert [s.no for s in hub.shells("ws-a")] == [1]
    assert not window.isVisible()
    assert qt.QtCore.Qt.Key.Key_W not in seen


def test_ctrl_shift_t_in_the_window_opens_a_shell_there(
    qt, window_shell, dragging
):
    """Ctrl+Shift+T in a torn-off window opens one more shell in
    that window — the tab current and its terminal focused — not
    in the page."""
    shell, hub, _window = window_shell
    page = open_page(qt, shell, shells=2)
    window = tear_off(qt, page.shells, 1)
    terminal = in_window(qt, window)
    seen = pressed_keys(qt, terminal)
    press(qt, terminal, "Key_T", "ctrl", "shift")
    assert [s.no for s in hub.shells("ws-a")] == [1, 2, 3]
    assert bare_titles(window.strip) == ["2", "3", "+"]
    assert qt.app.focusWidget() is hub.find("ws-a", 3).widget
    assert strip_titles(page) == ["1", "+"]
    assert qt.QtCore.Qt.Key.Key_T not in seen


# --- terminal-only mode --------------------------------------------------


def test_the_gestures_answer_in_the_terminal_only_mode(
    qt, window_shell, dragging
):
    """The tab row in terminal-only mode is the same strip: the
    left-drag tears off, a window's tab dropped on the row
    re-tabs, and the drag along the row reorders, with the chrome
    hidden."""
    shell, hub, _window = window_shell
    page = open_page(qt, shell, shells=3)
    page.terminal_only.enter()
    assert page.terminal_only.on
    tabs = page.shells
    window = tear_off(qt, tabs, 0)
    assert page.terminal_only.on
    assert strip_titles(page) == ["2", "3", "+"]
    assert window.isVisible()

    def back(drag):
        assert drag_enter(qt, tabs, over(qt, tabs, 0), no=1)
        drop_on(qt, tabs, over(qt, tabs, 0), no=1)
        FakeDrag.action = qt.QtCore.Qt.DropAction.MoveAction

    left_drag(qt, window.strip, 0, back)
    assert strip_titles(page) == ["2", "1", "3", "+"]
    assert [s.no for s in hub.shells("ws-a")] == [2, 1, 3]

    def along(drag):
        drag_over(qt, tabs, over(qt, tabs, 2), no=2)
        drop_on(qt, tabs, over(qt, tabs, 2), no=2)
        FakeDrag.action = qt.QtCore.Qt.DropAction.MoveAction

    left_drag(qt, tabs, 0, along)
    assert strip_titles(page) == ["1", "3", "2", "+"]
    assert [s.no for s in hub.shells("ws-a")] == [1, 3, 2]
    assert page.terminal_only.on


# --- the main window's close -------------------------------------------


def test_closing_the_main_window_ends_the_torn_off_shells(
    qt, window_shell, dragging
):
    """The window's close ends every shell: a torn-off window, a
    primary window of its own that would keep the application
    alive, closes with the page's shells."""
    shell, hub, window = window_shell
    page = open_page(qt, shell, shells=2)
    torn = tear_off(qt, page.shells, 0)
    assert torn.isVisible()
    window.close()
    assert hub.shells("ws-a") == []
    assert not torn.isVisible()


def test_the_window_filter_ignores_other_events(qt, window_shell, dragging):
    """The shell's close filter on the window answers the close
    alone: another event on the window passes."""
    shell, hub, window = window_shell
    open_page(qt, shell, shells=1)
    move = qt.QtGui.QMoveEvent(qt.QtCore.QPoint(1, 1), qt.QtCore.QPoint(0, 0))
    assert shell.eventFilter(window, move) is False
    assert len(hub.shells("ws-a")) == 1


def test_the_window_close_reaches_the_shells_under_the_main_strip(qt):
    """The composition the app builds — the shell under the main
    strip, the strip in the window — still carries the window's
    close to the hub: the filter reads the shell's window at the
    event, not at a parent change taken before the window
    existed."""
    from msks.client.gui.tabs import MainTabs
    from msks.client.gui.workspace import WorkspaceShell

    hub = qt.shells.ShellHub(lambda ws: [sys.executable, "-c", SLEEPER])
    shell = WorkspaceShell(StubBridge(), None, hub)
    tabs = MainTabs(shell)
    window = qt.QtWidgets.QMainWindow()
    window.setCentralWidget(tabs)
    window.show()
    shell.listing.on_succeeded("workspaces-list", [daemon_row()])
    shell.open_shell("ws-a")
    shell.open_shell("ws-a")
    qt.app.processEvents()
    window.close()
    assert hub.shells("ws-a") == []


# --- after the return ----------------------------------------------------


def test_the_returned_tab_keeps_its_button_rename_and_keys(
    qt, hub, tabs, dragging
):
    """The tab back in the page is a tab: its close button ends
    the shell, the double-click renames it, and the step keys
    walk the order it landed in."""
    shown(qt, tabs)
    bar = tabs.tabBar()
    session = hub.shells("ws-a")[0]
    window = tear_off(qt, tabs, 0)

    def back(drag):
        assert drag_enter(qt, tabs, over(qt, tabs, 0), no=1)
        drop_on(qt, tabs, over(qt, tabs, 0), no=1)
        FakeDrag.action = qt.QtCore.Qt.DropAction.MoveAction

    left_drag(qt, window.strip, 0, back)
    assert bare_titles(tabs) == ["2", "1", "3", "+"]
    assert not window.isVisible()  # gone with its last shell
    assert tabs.currentWidget() is session.widget
    rect = bar.tabRect(1)
    double_click(qt, bar, rect.center())
    assert tabs.editor is not None
    tabs.editor.setText("build")
    tabs.commit_rename()
    assert tabs.tabText(1) == "build"
    tabs.step_shell(1)
    assert tabs.currentWidget() is hub.find("ws-a", 3).widget
    button = bar.tabButton(1, qt.QtWidgets.QTabBar.ButtonPosition.RightSide)
    button.click()
    assert [s.no for s in hub.shells("ws-a")] == [2, 3]
    assert bare_titles(tabs) == ["2", "3", "+"]


def test_a_return_while_the_editor_stands_cancels_the_edit(
    qt, hub, tabs, dragging
):
    """The window's editor goes down with the window: a rename
    standing on its tab when the tab is dragged away cancels."""
    shown(qt, tabs, shells=1)
    window = tear_off(qt, tabs, 0)
    strip = window.strip
    strip.begin_rename(0)
    assert strip.editor is not None

    def back(drag):
        drag_enter(qt, tabs, over(qt, tabs, 0), no=1)
        drop_on(qt, tabs, over(qt, tabs, 0), no=1)
        FakeDrag.action = qt.QtCore.Qt.DropAction.MoveAction

    left_drag(qt, strip, 0, back)
    assert bare_titles(tabs) == ["1", "+"]
    assert hub.shells("ws-a")[0].label == "1"
    assert not window.isVisible()


# --- the pieces ---------------------------------------------------------


def test_drop_index_counts_the_midpoints_past_the_pointer(qt, hub, tabs):
    """The release index: the tabs whose midpoint the pointer
    passed, the dragged tab left out — the end when every
    midpoint lies left."""
    shown(qt, tabs)
    bar = tabs.tabBar()
    from msks.client.gui.shelldrag import drop_index

    left = bar.tabRect(0).left() - 5
    assert drop_index(bar, left, None, 3) == 0
    mid0 = bar.tabRect(0).center().x() + 1
    assert drop_index(bar, mid0, None, 3) == 1
    mid1 = bar.tabRect(1).center().x() + 1
    assert drop_index(bar, mid1, None, 3) == 2
    past = bar.tabRect(2).right() + 1
    assert drop_index(bar, past, None, 3) == 3
    # the dragged tab itself does not count
    assert drop_index(bar, mid0, 0, 3) == 0
    assert drop_index(bar, past, 1, 3) == 2
    # the "+" is no shell: a count over the shells skips it
    assert drop_index(bar, bar.tabRect(3).center().x(), None, 3) == 3


def test_the_payload_reads_back(qt):
    """The mime round-trips the workspace and the number; a payload
    without the format, or with a malformed one, reads as no
    shell."""
    from msks.client.gui.shelldrag import MIME_FORMAT, mime_shell, shell_mime

    data = shell_mime("ws-a", 3)
    assert data.formats() == [MIME_FORMAT]
    assert mime_shell(data) == ("ws-a", 3)
    assert mime_shell(qt.QtCore.QMimeData()) is None
    broken = qt.QtCore.QMimeData()
    broken.setData(MIME_FORMAT, b"ws-a:x")
    assert mime_shell(broken) is None
    broken.setData(MIME_FORMAT, b":3")
    assert mime_shell(broken) is None


def test_the_tear_off_of_an_unknown_session_makes_no_window(
    qt, hub, tabs, dragging
):
    """A tear-off naming no standing session builds nothing."""
    shown(qt, tabs, shells=1)
    assert tabs.tear_off(9) is None
    assert len(hub.drag.strips) == 1


def test_the_marker_clamps_to_the_plus(qt, hub, tabs):
    """An index past the last shell marks at the "+": the strip's
    own end."""
    shown(qt, tabs, shells=1)
    bar = tabs.tabBar()
    bar.insertion = 5
    assert bar.insertion_x() == bar.tabRect(1).left()


def test_a_move_heard_for_another_strips_shell_moves_no_tab(
    qt, hub, tabs, dragging
):
    """A move heard for a shell seated in another window, one for
    an unknown shell, and one for another workspace's land no tab
    on the page's strip."""
    shown(qt, tabs, shells=2)
    window = tear_off(qt, tabs, 0)
    hub.shell_moved.emit("ws-a", 1)
    hub.shell_moved.emit("ws-a", 9)
    hub.shell_moved.emit("ws-b", 1)
    assert bare_titles(tabs) == ["2", "+"]
    assert bare_titles(window.strip) == ["1", "+"]


def test_a_move_or_dock_naming_no_stripped_shell_changes_nothing(
    qt, hub, tabs, dragging
):
    """The hub's move and dock for an unknown shell are no-ops the
    strips never hear."""
    shown(qt, tabs, shells=2)
    heard = []
    hub.shell_moved.connect(lambda ws, no: heard.append(("m", no)))
    hub.shell_docked.connect(lambda ws, no: heard.append(("d", no)))
    window = tear_off(qt, tabs, 0)
    hub.move("ws-a", 9, 0)
    hub.dock("ws-a", 9, None)
    assert heard == [("d", 1)]
    assert bare_titles(window.strip) == ["1", "+"]


def test_a_dock_heard_for_a_gone_record_moves_nothing(qt, hub, tabs, dragging):
    """The dock news naming no record, or another workspace's,
    changes no tab."""
    shown(qt, tabs, shells=1)
    hub.shell_undocked.emit("ws-a", 9)
    hub.shell_docked.emit("ws-a", 9)
    hub.shell_undocked.emit("ws-b", 1)
    hub.shell_docked.emit("ws-b", 1)
    assert bare_titles(tabs) == ["1", "+"]


def test_the_unhost_clears_the_marker(qt, hub, tabs):
    """An unhost with a marker standing clears it with the
    tabs."""
    shown(qt, tabs, shells=1)
    tabs.tabBar().insertion = 0
    tabs.unhost()
    assert tabs.tabBar().insertion is None


def test_the_editor_commits_only_a_standing_session(qt, hub, tabs, dragging):
    """The shared editor's commit names the session: an empty
    text is the cancel — the hub's own reading — and a record
    gone behind the edit stands no name; both end the edit."""
    shown(qt, tabs, shells=1)
    session = hub.shells("ws-a")[0]
    tabs.begin_rename(0)
    tabs.editor.setText("   ")
    tabs.commit_rename()
    assert session.label == "1"
    tabs.begin_rename(0)
    editor = tabs.editor
    hub.sessions["ws-a"].clear()  # the record leaves behind the edit
    editor.setText("late")
    editor.commit()
    assert tabs.editor is None
    assert session.label == "1"


def test_the_editor_opens_on_a_shell_tab_alone(qt, hub, tabs, dragging):
    """A rename asked on an index with no shell tab — past the
    tabs, or the "+" — opens no editor."""
    shown(qt, tabs, shells=1)
    tabs.begin_rename(5)
    assert tabs.editor is None
    tabs.begin_rename(1)  # the "+"
    assert tabs.editor is None


def test_the_windows_keys_anchor_on_the_bar_when_the_plus_stands(
    qt, window_shell, dragging
):
    """A shell action from the window with the "+" current lands
    the keyboard on the bar first — the anchor — then steps to
    the shell."""
    shell, hub, _window = window_shell
    page = open_page(qt, shell, shells=2)
    window = tear_off(qt, page.shells, 1)
    strip = window.strip
    window.activateWindow()
    qt.app.processEvents()
    strip.setCurrentIndex(strip.indexOf(strip.plus_page))
    assert strip.currentWidget() is strip.plus_page
    bar = strip.tabBar()
    bar.setFocus()
    assert qt.app.focusWidget() is bar
    window.anchor_focus()
    assert qt.app.focusWidget() is bar
    press(qt, bar, "Key_Tab", "ctrl")
    assert qt.app.focusWidget() is hub.find("ws-a", 2).widget


def test_the_editors_other_escape_events_are_the_fields_own(
    qt, hub, tabs, dragging
):
    """Only the Escape press cancels and its override question
    claims: the key's release, and a press of another key, are the
    line edit's own events."""
    shown(qt, tabs, shells=1)
    tabs.begin_rename(0)
    release = qt.QtGui.QKeyEvent(
        qt.QtCore.QEvent.Type.KeyRelease,
        qt.QtCore.Qt.Key.Key_Escape,
        qt.QtCore.Qt.KeyboardModifier.NoModifier,
    )
    assert qt.app.sendEvent(tabs.editor, release) is True
    assert tabs.editor is not None
    # and the strip's own filter passes the editor's events on
    assert tabs.eventFilter(tabs.editor, release) is False
    assert tabs.editor is not None
    tabs.cancel_rename()


def test_the_controller_forgets_a_destroyed_strip(qt, hub, tabs, dragging):
    """A window going down takes its strip off the controller's
    list, and a press standing on it ends with it."""
    shown(qt, tabs, shells=2)
    window = tear_off(qt, tabs, 0)
    strip = window.strip
    assert hub.drag.strips == [tabs, strip]
    start = strip.tabBar().tabRect(0).center()
    left_press(qt, strip, start)
    assert hub.drag.source is strip
    hub.close_shell("ws-a", 1)
    window.deleteLater()
    qt.QtCore.QCoreApplication.sendPostedEvents(
        None, qt.QtCore.QEvent.Type.DeferredDelete
    )
    assert hub.drag.strips == [tabs]
    assert hub.drag.source is None
    assert hub.drag.pressed is None


def test_the_window_forgives_a_close_with_nothing_held(
    qt, hub, tabs, dragging
):
    """A close arriving after the last shell's end — the news's
    own close, repeated — tears nothing twice."""
    shown(qt, tabs, shells=1)
    window = tear_off(qt, tabs, 0)
    window.strip = None
    window.close()
    assert window.closing
    window.shell_gone("ws-a", 0)  # nothing held: no close asked
    assert not window.isVisible()


def escape(qt, tabs) -> None:
    """The Escape press that cancels the drag (#1203), as the
    desktop's loop delivers it: to the application, where the
    controller's cancel filter stands — installed by the loop's own
    timer in a real drag, by hand in the scripted one."""
    tabs.hub.drag.watch_cancel()
    key = qt.QtGui.QKeyEvent(
        qt.QtCore.QEvent.Type.KeyPress,
        qt.QtCore.Qt.Key.Key_Escape,
        qt.QtCore.Qt.KeyboardModifier.NoModifier,
    )
    qt.app.sendEvent(qt.app, key)


def test_an_escape_cancels_the_drag_and_returns_the_tab(
    qt, hub, tabs, dragging
):
    """Escape cancels the drag: the desktop sends the source bar a
    leave as on any departure, the action comes back ignored, and
    the tab still returns to the place the press found it — the
    leave alone would read as a release off every strip."""
    shown(qt, tabs)

    def path(drag):
        assert drag_over(qt, tabs, over(qt, tabs, 2), no=1)
        assert bare_titles(tabs) == ["2", "3", "1", "+"]
        escape(qt, tabs)
        drag_off(qt, tabs)  # Qt's cancel leaves the bar
        qt.QtGui.QCursor.setPos(qt.QtCore.QPoint(900, 900))

    left_drag(qt, tabs, 0, path, qt.QtCore.Qt.DropAction.IgnoreAction)
    assert bare_titles(tabs) == ["1", "2", "3", "+"]
    assert [s.no for s in hub.shells("ws-a")] == [1, 2, 3]
    assert tabs.currentWidget() is hub.find("ws-a", 1).widget
    assert tabs.hub.drag.cancel_watch is None  # the filter came off


def test_an_escape_off_every_strip_tears_nothing_off(qt, hub, tabs, dragging):
    """Escape off every strip cancels as wholly: no window opens."""
    shown(qt, tabs, shells=1)

    def path(drag):
        drag_off(qt, tabs)
        escape(qt, tabs)
        qt.QtGui.QCursor.setPos(qt.QtCore.QPoint(900, 900))

    left_drag(qt, tabs, 0, path, qt.QtCore.Qt.DropAction.IgnoreAction)
    assert bare_titles(tabs) == ["1", "+"]
    assert hub.shells("ws-a")[0].holder is None


def test_the_cancel_filter_passes_every_event_on(qt, hub, tabs, dragging):
    """The cancel filter reads the Escape press alone: other keys and
    events pass to the desktop's drag machinery untouched."""
    shown(qt, tabs, shells=1)
    d = tabs.hub.drag
    d.watch_cancel()
    assert d.cancel_watch is qt.app
    typed = qt.QtGui.QKeyEvent(
        qt.QtCore.QEvent.Type.KeyPress,
        qt.QtCore.Qt.Key.Key_A,
        qt.QtCore.Qt.KeyboardModifier.NoModifier,
        "a",
    )
    assert qt.app.sendEvent(qt.app, typed) is False
    assert d.cancelled is False
    d.unwatch_cancel()
    assert d.cancel_watch is None
    d.unwatch_cancel()  # off already: nothing to take off


def test_a_release_after_a_leave_tears_off_whatever_the_cursor_says(
    qt, hub, tabs, dragging
):
    """The tear-off decision reads the drag's events, not the
    cursor: after the leave, a release on no strip tears off even
    where the cursor's position reads stale at the drag's start —
    the Wayland shape, a drag's source seeing no pointer motion."""
    shown(qt, tabs, shells=1)

    def path(drag):
        assert drag_over(qt, tabs, over(qt, tabs, 0), no=1)
        drag_off(qt, tabs)
        qt.QtGui.QCursor.setPos(
            tabs.tabBar().mapToGlobal(over(qt, tabs, 0))
        )  # stale: still over the bar

    left_drag(qt, tabs, 0, path, qt.QtCore.Qt.DropAction.IgnoreAction)
    assert bare_titles(tabs) == ["+"]
    assert hub.shells("ws-a")[0].holder.isVisible()


def test_a_fast_start_past_the_bar_tears_off_with_no_event(
    qt, hub, tabs, dragging
):
    """A drag whose start move already left the bar needs no leave
    to count as off it: a release on no strip tears off."""
    shown(qt, tabs, shells=1)
    bar = tabs.tabBar()
    start = bar.tabRect(0).center()
    FakeDrag.script = None
    FakeDrag.action = qt.QtCore.Qt.DropAction.IgnoreAction
    left_press(qt, tabs, start)
    far = qt.QtCore.QPoint(bar.width() + 50, bar.height() + 50)
    assert bar.rect().contains(far) is False
    left_move(qt, tabs, far)
    left_release(qt, tabs, far)
    assert bare_titles(tabs) == ["+"]
    assert hub.shells("ws-a")[0].holder.isVisible()


def test_a_source_strip_hidden_mid_drag_tears_off(qt, hub, tabs, dragging):
    """The page's strip hiding under the drag — the page's tab
    switched — leaves the drop nowhere to land: the tab tears off
    into a window of its own."""
    shown(qt, tabs, shells=2)

    def path(drag):
        tabs.hide()
        drag_off(qt, tabs)  # the hidden bar is left behind
        qt.QtGui.QCursor.setPos(qt.QtCore.QPoint(900, 900))

    left_drag(qt, tabs, 0, path, qt.QtCore.Qt.DropAction.IgnoreAction)
    assert hub.shells("ws-a")[0].holder.isVisible()
    assert bare_titles(tabs) == ["2", "+"]


def test_a_right_release_under_a_left_press_keeps_the_press(
    qt, hub, tabs, dragging
):
    """A right-button release while the left press stands clears
    nothing: only the left button's release ends it."""
    shown(qt, tabs, shells=1)
    bar = tabs.tabBar()
    start = bar.tabRect(0).center()
    left_press(qt, tabs, start)
    Qt = qt.QtCore.Qt
    on_bar(
        qt,
        tabs,
        qt.QtCore.QEvent.Type.MouseButtonRelease,
        start,
        Qt.MouseButton.RightButton,
        Qt.MouseButton.LeftButton,
    )
    assert tabs.hub.drag.pressed is hub.shells("ws-a")[0]


def test_a_drop_after_the_shell_closed_is_refused(qt, hub, tabs, dragging):
    """The bar took the drag's enter while the shell stood; the
    shell closing before the drop — the program inside exited —
    leaves the drop refused and nothing seated."""
    shown(qt, tabs, shells=2)
    window = tear_off(qt, tabs, 1)  # shell 2

    def path(drag):
        assert drag_enter(qt, tabs, over(qt, tabs, 0), no=2)
        assert tabs.tabBar().insertion == 1
        hub.close_shell("ws-a", 2)
        event = drop_on(qt, tabs, over(qt, tabs, 0), no=2)
        assert event.isAccepted() is False

    left_drag(qt, window.strip, 0, path, qt.QtCore.Qt.DropAction.IgnoreAction)
    assert [s.no for s in hub.shells("ws-a")] == [1]
    assert not window.isVisible()
    assert tabs.tabBar().insertion is None


def test_an_unhosted_strip_refuses_the_drag(qt, hub, tabs, dragging):
    """A strip between workspaces — hosted by none, its "+" alone —
    is no landing for any shell."""
    shown(qt, tabs, shells=1)
    tabs.unhost()
    assert tabs.tabBar().isVisible()
    assert drag_enter(qt, tabs, over(qt, tabs, 0), no=1) is False


def test_a_tear_off_without_a_place_opens_the_window_unplaced(
    qt, hub, tabs, dragging
):
    """A tear-off asked by hand names no pointer: the window opens
    where it stands."""
    shown(qt, tabs, shells=1)
    window = tabs.tear_off(1)
    assert window.isVisible()
    assert hub.shells("ws-a")[0].holder is window


def test_the_drags_end_for_a_gone_shell_moves_nothing(qt, hub, tabs, dragging):
    """The drag's own end for a shell that closed meanwhile stands
    no order: the hub's list is the record's, and it is gone."""
    shown(qt, tabs, shells=2)
    session = hub.shells("ws-a")[0]
    hub.close_shell("ws-a", 1)
    tabs.stand_order(session)
    assert [s.no for s in hub.shells("ws-a")] == [2]


def test_a_restored_tab_already_home_moves_nothing(qt, hub, tabs, dragging):
    """The restore of a tab standing where the press found it
    moves nothing and keeps it current."""
    shown(qt, tabs, shells=2)
    session = hub.shells("ws-a")[0]
    tabs.restore_tab(session, 0)
    assert bare_titles(tabs) == ["1", "2", "+"]
    tabs.removeTab(0)
    tabs.sync_plus_index()
    tabs.restore_tab(session, 0)  # no tab of its own left to move
    assert bare_titles(tabs) == ["2", "+"]


def test_a_window_losing_its_last_shell_under_a_drag_waits_it_out(
    qt, hub, tabs, dragging
):
    """The dragged shell closing while its window holds nothing else
    — the program inside exited — leaves the window standing under
    the drag's loop: its bar is the drag's source. The window goes
    down once the drag ended, and nothing tears off for a shell
    that is gone."""
    shown(qt, tabs, shells=1)
    window = tear_off(qt, tabs, 0)
    strip = window.strip

    def path(drag):
        hub.close_shell("ws-a", 1)
        assert window.isVisible()
        assert hub.drag.held == [window]
        drag_off(qt, strip)
        qt.QtGui.QCursor.setPos(qt.QtCore.QPoint(900, 900))

    left_drag(qt, strip, 0, path, qt.QtCore.Qt.DropAction.IgnoreAction)
    assert hub.shells("ws-a") == []
    assert not window.isVisible()
    assert hub.drag.held == []


def test_a_press_under_a_running_drag_is_the_drags(qt, hub, tabs, dragging):
    """A press reaching a bar while a drag runs — the desktop owns
    the pointer, but a synthetic one can — is consumed and names no
    new drag: the running drag's press stands."""
    shown(qt, tabs, shells=2)
    d = tabs.hub.drag
    pressed = d.pressed  # noqa: F841 — the running drag's, read below

    def path(drag):
        before = d.pressed
        assert (
            on_bar(
                qt,
                tabs,
                qt.QtCore.QEvent.Type.MouseButtonPress,
                tabs.tabBar().tabRect(1).center(),
                qt.QtCore.Qt.MouseButton.LeftButton,
                qt.QtCore.Qt.MouseButton.LeftButton,
            )
            is True
        )
        assert d.pressed is before
        drag_off(qt, tabs)
        qt.QtGui.QCursor.setPos(qt.QtCore.QPoint(900, 900))

    left_drag(qt, tabs, 0, path, qt.QtCore.Qt.DropAction.IgnoreAction)
    assert bare_titles(tabs) == ["2", "+"]
