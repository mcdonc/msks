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
    assert plus.text() == "+"
    asked: list[bool] = []
    tabs.create_requested.connect(lambda: asked.append(True))
    plus.click()
    assert asked == [True]


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
