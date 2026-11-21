"""The main window's top-level tab strip (#962): the workspaces
page under the first tab, the arrow keys crossing the strip —
Left and Right between the tabs, Down from the bar into the page
below — and the workspace screens unchanged beneath it. The
widgets run under the offscreen platform on every host (#945's
fixture shape); the strip's slots are driven directly.
"""

import importlib.util
import os
from types import SimpleNamespace

import pytest

if importlib.util.find_spec("PySide6") is None:
    pytest.skip(
        "the gui extra is not installed on this host",
        allow_module_level=True,
    )

from msks.client.gui.tabs import WORKSPACES_LABEL, MainTabs  # noqa: E402


@pytest.fixture()
def qt(tmp_path):
    """The offscreen ``QApplication`` the strip needs, the list's
    own fixture shape (#939): the silent fontconfig, the platform
    pin, and the Qt names imported inside the fixture (a
    collection-time controller has no business loading the
    binding)."""
    fonts = tmp_path / "fonts.conf"
    fonts.write_text("<fontconfig/>\n")
    os.environ["FONTCONFIG_FILE"] = str(fonts)
    os.environ["QT_QPA_PLATFORM"] = "offscreen"
    from PySide6 import QtCore, QtGui, QtWidgets

    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication(
        ["msks-test"]
    )
    ns = SimpleNamespace(
        app=app,
        QtCore=QtCore,
        QtGui=QtGui,
        QtWidgets=QtWidgets,
        keep=[],
    )
    yield ns
    del ns.keep[:]
    # The same teardown the screens' fixtures owe their worker
    # (#959): close and delete every top-level widget, then flush
    # the deferred deletions.
    for widget in app.topLevelWidgets():
        widget.close()
        widget.deleteLater()
    QtCore.QCoreApplication.sendPostedEvents(
        None, QtCore.QEvent.Type.DeferredDelete
    )
    app.processEvents()


class Signal:
    """A Qt signal's connect/emit surface for the stub bridge:
    an emit calls the connected slots at once."""

    def __init__(self) -> None:
        self.slots: list = []

    def connect(self, slot) -> None:
        self.slots.append(slot)

    def emit(self, *args) -> None:
        for slot in self.slots:
            slot(*args)


def press(qt, widget, key) -> None:
    """One key pressed on the widget, through Qt's own event
    path: the strip's filter reads it there, the same delivery
    the platform makes."""
    from PySide6.QtTest import QTest

    QTest.keyClick(widget, key)


def window_over(qt, tabs):
    """The strip shown in a real window — the shape every strip
    test reads, the window kept alive for the test's span."""
    window = qt.QtWidgets.QMainWindow()
    window.setCentralWidget(tabs)
    window.show()
    qt.keep.append(window)
    return window


def test_the_strip_opens_on_the_workspaces_tab(qt) -> None:
    """The window's first tab (#962): the workspaces page under
    it, its label plain, the tab current — the images and
    secrets pages land beside it later (#963–#966)."""
    page = qt.QtWidgets.QWidget()
    tabs = MainTabs(page)
    window_over(qt, tabs)
    assert tabs.count() == 1
    assert tabs.tabText(0) == WORKSPACES_LABEL
    assert tabs.currentIndex() == 0
    assert tabs.currentWidget() is page


def test_the_workspaces_tab_carries_the_create_opener(qt) -> None:
    """The create opener (#962): a plus button embedded in the
    Workspaces tab, its click asking for the create dialog —
    the shell that owns the bridge answers it."""
    page = qt.QtWidgets.QWidget()
    tabs = MainTabs(page)
    window_over(qt, tabs)
    plus = tabs.tabBar().tabButton(
        0, qt.QtWidgets.QTabBar.ButtonPosition.RightSide
    )
    assert plus is not None
    assert plus.text() == ""
    # The badge (#964): a drawn, multicolor green icon — not
    # the bare text glyph the opener first wore.
    assert not plus.icon().isNull()
    size = plus.icon().availableSizes()[0]
    assert (size.width(), size.height()) == (32, 32)
    # The pointing hand over the badge (#964).
    assert plus.cursor().shape() == qt.QtCore.Qt.CursorShape(
        qt.QtCore.Qt.CursorShape.PointingHandCursor
    )
    asked: list[bool] = []
    tabs.create_requested.connect(lambda: asked.append(True))
    plus.click()
    assert asked == [True]


def test_the_bar_carries_the_active_tab_mark(qt) -> None:
    """The strip's active-tab mark (#1012): the bar wears the
    shared sheet — the accent tint the workspace
    detail strip carries too, so both tab levels read the same
    way."""
    page = qt.QtWidgets.QWidget()
    tabs = MainTabs(page)
    window_over(qt, tabs)
    assert "QTabBar::tab:selected" in tabs.tabBar().styleSheet()


def test_left_and_right_cross_the_tabs(qt) -> None:
    """The strip's own moves (#962): Left and Right on the bar
    change the current tab — the platform's native handling,
    pinned so a later widget cannot take the keys away."""
    first = qt.QtWidgets.QWidget()
    second = qt.QtWidgets.QWidget()
    tabs = MainTabs(first)
    tabs.addTab(second, "Later")
    window_over(qt, tabs)
    bar = tabs.tabBar()
    bar.setFocus()
    press(qt, bar, qt.QtCore.Qt.Key.Key_Right)
    assert tabs.currentIndex() == 1
    press(qt, bar, qt.QtCore.Qt.Key.Key_Left)
    assert tabs.currentIndex() == 0


def test_down_leaves_the_bar_for_the_page(qt) -> None:
    """The bar holds no trap (#962): Down moves the focus into
    the page below, at the page's first tab stop — the same
    stop the Tab key reaches, past the plain children the walk
    crosses on the way."""
    page = qt.QtWidgets.QWidget()
    qt.QtWidgets.QWidget(page)  # no tab stop: the walk advances
    box = qt.QtWidgets.QLineEdit(page)
    tabs = MainTabs(page)
    window = window_over(qt, tabs)
    bar = tabs.tabBar()
    bar.setFocus()
    press(qt, bar, qt.QtCore.Qt.Key.Key_Down)
    assert window.focusWidget() is box


def test_up_from_the_top_row_returns_to_the_bar(qt) -> None:
    """The rule's other direction (#962): Up on the list's top
    row hands the keyboard back to the tab strip — the arrows
    read top-to-bottom without a Tab, both ways."""
    from msks.client.gui.workspaces import LIST_KEY, WorkspaceList
    from PySide6.QtTest import QTest

    class ListBridge:
        """The list's bridge stub: the signals it connects and
        the verb it calls."""

        def __init__(self) -> None:
            self.succeeded = Signal()
            self.failed = Signal()

        def call(self, key, method, *args):
            return None

    listing = WorkspaceList(ListBridge())
    listing.on_succeeded(LIST_KEY, [daemon_row()])
    tabs = MainTabs(listing)
    window = window_over(qt, tabs)
    listing.table.setFocus()
    listing.table.setCurrentCell(0, 0)
    QTest.keyClick(listing.table, qt.QtCore.Qt.Key.Key_Up)
    # The focus leaves the table upward (#962): the strip above
    # the list takes it — the chain's own reading order, the
    # table the first stop under the page it fills.
    focused = window.focusWidget()
    assert focused is not listing.table
    assert focused is tabs.tabBar() or focused.parent() is tabs.tabBar()
    # The view's own keys still move the selection: the bridge
    # takes only the top row's Up.
    listing.table.setFocus()
    QTest.keyClick(listing.table, qt.QtCore.Qt.Key.Key_Right)
    assert window.focusWidget() is listing.table


def daemon_row() -> dict:
    """One listing row the list reads, the workspace suite's
    own shape."""
    return {
        "id": "ws-a",
        "name": "alpha",
        "status": "running",
        "created_at": "2026-01-02T12:00:00",
    }


def test_down_falls_back_to_the_page_itself(qt) -> None:
    """A page nothing under it takes the keyboard (#962): Down
    still leaves the bar — the page itself takes the focus,
    explicit focus standing where a Tab would refuse."""
    page = qt.QtWidgets.QWidget()
    tabs = MainTabs(page)
    window = window_over(qt, tabs)
    bar = tabs.tabBar()
    bar.setFocus()
    press(qt, bar, qt.QtCore.Qt.Key.Key_Down)
    assert window.focusWidget() is page
