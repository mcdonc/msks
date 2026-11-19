"""The whole-client zoom's suite (#976, phase 6 of #924): the
application shortcuts that step the UI fonts and the terminal
fonts together, the clamps each set holds, the window's resize
around the visible terminal's grid, and the chrome that follows
the application font — offscreen, over real sleeper children
through the hub's injected argv factory (the widget suite's own
pattern)."""

import os
import sys
import textwrap
from pathlib import Path
from types import SimpleNamespace

import pytest

#: A child that announces itself and then waits for its parent
#: to hang up: the zoom never reads the child's bytes, but the
#: sessions it steps are real children on real ptys.
SLEEPER = textwrap.dedent(
    """
    import time
    print("ready", flush=True)
    time.sleep(30)
    """
)

# The hub's pty children spawn through ``fork()`` on Qt's own
# threads (termsession's pty design, #951); Python 3.14 warns
# on the multi-threaded fork by design of the spawn, and the
# warning is the suite's own noise, not a defect to fix here.
pytestmark = pytest.mark.filterwarnings(
    "ignore:This process.*multi-threaded.*:DeprecationWarning"
)


@pytest.fixture()
def qt(tmp_path: Path):
    """The offscreen ``QApplication`` the screens need — the
    terminal suite's own fixture shape (the host's fontconfig
    intact: the terminal faces measure against the database),
    with the zoom module imported inside (the window path's
    contract, #927)."""
    os.environ["QT_QPA_PLATFORM"] = "offscreen"
    from msks.client.gui import zoom as zoom_mod
    from PySide6 import QtCore, QtGui, QtWidgets

    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication(
        ["msks-test"]
    )
    yield SimpleNamespace(
        app=app,
        QtCore=QtCore,
        QtGui=QtGui,
        QtWidgets=QtWidgets,
        zoom=zoom_mod,
    )
    for widget in app.topLevelWidgets():
        widget.close()
        widget.deleteLater()
    QtCore.QCoreApplication.sendPostedEvents(
        None, QtCore.QEvent.Type.DeferredDelete
    )
    app.processEvents()


@pytest.fixture()
def zoomed(qt):
    """One zoom over a real hub and a shown window: the hub's
    shells are sleeper children, the window hosts the first
    one's terminal laid out at its grid. The application font
    the zoom steps is the shared test app's own — restored at
    teardown so later tests in this worker measure the
    platform's default."""
    from msks.client.gui.shells import ShellHub

    hub = ShellHub(
        argv_factory=lambda workspace_id: [
            sys.executable,
            "-c",
            SLEEPER,
        ]
    )
    window = qt.QtWidgets.QMainWindow()
    default = qt.app.font().pointSizeF()
    zoom = qt.zoom.Zoom(hub, window)
    window.show()
    qt.app.processEvents()
    yield SimpleNamespace(hub=hub, window=window, zoom=zoom, default=default)
    hub.shutdown()
    window.close()
    restored = qt.app.font()
    restored.setPointSizeF(default)
    qt.app.setFont(restored)


def hosted(qt, zoomed):
    """The hub's first shell's terminal inside the window, laid
    out at its grid: the shape the zoom's window resize pins
    read."""
    session = zoomed.hub.open_shell("ws")
    zoomed.window.setCentralWidget(session.widget)
    zoomed.window.resize(session.widget.sizeHint())
    qt.app.processEvents()
    return session.widget


def test_one_step_moves_both_font_sets(qt, zoomed):
    """Ctrl+Plus then Ctrl+Minus (#976): the application font
    behind the chrome and every live terminal's face step
    together — one point each way, in every workspace the hub
    holds."""
    from msks.client.gui.terminal import fonts

    first = hosted(qt, zoomed)
    zoomed.hub.open_shell("other")
    chrome = qt.app.font().pointSizeF()
    zoomed.zoom.grow()
    assert qt.app.font().pointSizeF() == chrome + 1
    for session in [
        *zoomed.hub.shells("ws"),
        *zoomed.hub.shells("other"),
    ]:
        assert session.widget.font.pointSize() == fonts.DEFAULT_PT + 1
    zoomed.zoom.shrink()
    assert qt.app.font().pointSizeF() == chrome
    assert first.font.pointSize() == fonts.DEFAULT_PT


def test_the_window_resizes_around_the_visible_terminal(qt, zoomed):
    """A step on a visible terminal (#976): the window takes the
    grid's own growth — new cell metrics against old ones — and
    the terminal's columns and rows hold at the new size."""
    widget = hosted(qt, zoomed)
    cols, rows = widget.cols, widget.rows
    before = zoomed.window.size()
    old_grid = widget.sizeHint()
    zoomed.zoom.grow()
    qt.app.processEvents()
    new_grid = widget.sizeHint()
    assert zoomed.window.size() == qt.QtCore.QSize(
        before.width() + new_grid.width() - old_grid.width(),
        before.height() + new_grid.height() - old_grid.height(),
    )
    assert (widget.cols, widget.rows) == (cols, rows)
    # A re-apply at the same size moves nothing (#976): the
    # widget's pixels already answer its grid — no step, no
    # slack — and the window holds.
    steady = zoomed.window.size()
    widget.apply_font_size(widget.font.pointSize())
    qt.app.processEvents()
    assert zoomed.window.size() == steady
    assert (widget.cols, widget.rows) == (cols, rows)


def test_a_hidden_terminal_re_faces_without_the_window(qt, zoomed):
    """A terminal the window does not show (#976): a step
    re-faces it and resizes nothing — the window moves only
    around a terminal it shows, and the hidden one re-derives
    its grid at the new cell size when its own pixels hold."""
    from msks.client.gui.terminal import fonts

    zoomed.hub.open_shell("ws")
    zoomed.hub.open_shell("ws")
    before = zoomed.window.size()
    zoomed.zoom.grow()
    for session in zoomed.hub.shells("ws"):
        assert session.widget.font.pointSize() == fonts.DEFAULT_PT + 1
    assert zoomed.window.size() == before
    hidden = zoomed.hub.shells("ws")[0].widget
    hidden.resize(hidden.sizeHint())  # its own pixels at the new cells
    hidden.resizeEvent(qt.QtGui.QResizeEvent(hidden.size(), hidden.size()))
    assert (hidden.cols, hidden.rows) == (80, 24)


def test_each_set_holds_its_own_clamp(qt, zoomed):
    """Steps past the ends (#976): the terminal holds its own
    window (7–28) and the chrome holds its own — one set still
    moves while the other stands at its clamp, and a press with
    both at their clamp changes nothing."""
    from msks.client.gui.terminal import fonts

    hosted(qt, zoomed)
    zoom = zoomed.zoom
    for _ in range(fonts.MAX_PT - fonts.DEFAULT_PT):
        zoom.grow()  # the terminal to its ceiling
    assert zoom.term_pt == fonts.MAX_PT
    # The offscreen default chrome (9pt scaled to 13.5) still
    # has room at the terminal's ceiling — a host whose
    # platform default already sits near the UI ceiling would
    # clamp here instead.
    assert qt.app.font().pointSizeF() < qt.zoom.UI_MAX_PT
    for _ in range(64):
        zoom.grow()  # the chrome joins the ceiling; both hold
    assert qt.app.font().pointSizeF() == qt.zoom.UI_MAX_PT
    for _ in range(128):
        zoom.shrink()  # both floors
    assert zoom.term_pt == fonts.MIN_PT
    assert qt.app.font().pointSizeF() == qt.zoom.UI_MIN_PT


def test_reset_restores_both_defaults(qt, zoomed):
    """Ctrl+0 (#976): the terminal face back to its default and
    the chrome back to the size the window opened with — and a
    reset over the defaults changes nothing."""
    from msks.client.gui.terminal import fonts

    hosted(qt, zoomed)
    zoomed.zoom.grow()
    zoomed.zoom.grow()
    zoomed.zoom.reset()
    assert zoomed.zoom.term_pt == fonts.DEFAULT_PT
    assert qt.app.font().pointSizeF() == pytest.approx(zoomed.default)
    zoomed.zoom.reset()  # over the defaults: the no-op arm
    assert zoomed.zoom.term_pt == fonts.DEFAULT_PT


def test_a_shell_opened_after_a_zoom_joins_it(qt, zoomed):
    """A shell opened while the zoom stands (#976): the fresh
    terminal takes the zoomed face, not the default — one read
    against the zoomed chrome."""
    from msks.client.gui.terminal import fonts

    hosted(qt, zoomed)
    zoomed.zoom.grow()
    zoomed.zoom.grow()
    session = zoomed.hub.open_shell("ws")
    assert session.widget.font.pointSize() == fonts.DEFAULT_PT + 2
    zoomed.hub.open_shell("plain")  # a fresh shell joins the zoom too
    joined = zoomed.hub.find("plain", 1)
    assert joined.widget.font.pointSize() == fonts.DEFAULT_PT + 2


def test_the_keys_sit_on_the_window_application_wide(qt, zoomed):
    """The zoom's shortcuts (#976): three of them on the window,
    application scope — the focus never gates them — with
    every plus spelling on the grow one (the numpad plus, the
    bare equal, and the shifted equal a main-row plus asks
    on a keyboard with no numpad)."""
    Qt = qt.QtCore.Qt
    zooms = [
        shortcut
        for shortcut in zoomed.window.findChildren(qt.QtGui.QShortcut)
        if shortcut.context() == Qt.ShortcutContext.ApplicationShortcut
    ]
    keys = sorted(
        tuple(sorted(key.toString() for key in shortcut.keys()))
        for shortcut in zooms
    )
    assert keys == [
        ("Ctrl++", "Ctrl+=", "Ctrl+Shift++"),
        ("Ctrl+-",),
        ("Ctrl+0",),
    ]


def test_the_grid_holds_through_the_windows_real_chrome(qt, zoomed):
    """A step with the window's real chrome around the
    terminal (#976): the application-font write moves the
    chrome's own metrics in the same step, and the window's
    settle pass takes the remainder — the terminal's grid
    holds exactly, and a zoom round trip ends where it
    began."""
    session = zoomed.hub.open_shell("ws")
    widget = session.widget
    shells = qt.QtWidgets.QTabWidget()
    shells.addTab(widget, "1")
    page = qt.QtWidgets.QTabWidget()
    page.addTab(shells, "Shells")
    page.addTab(qt.QtWidgets.QWidget(), "Info")
    outer = qt.QtWidgets.QTabWidget()
    outer.addTab(page, "Workspaces")
    zoomed.window.setCentralWidget(outer)
    zoomed.window.show()
    qt.app.processEvents()
    held = (widget.cols, widget.rows)
    zoomed.zoom.grow()
    qt.app.processEvents()
    assert (widget.cols, widget.rows) == held
    zoomed.zoom.shrink()
    qt.app.processEvents()
    assert (widget.cols, widget.rows) == held


def test_the_headers_follow_the_application_font(qt, zoomed):
    """The tables' bold headers (#976): an explicit font — the
    only way Qt holds a bold header — freezes against the
    application font's changes, so the zoom's after-write walk
    re-seats every bolded header on the fresh size."""
    from msks.client.gui.listtable import bold_header

    tables = [qt.QtWidgets.QTableWidget() for _ in range(2)]
    for table in tables:
        bold_header(table)
    before = tables[0].horizontalHeader().font().pointSizeF()
    zoomed.zoom.grow()
    for table in tables:
        header = table.horizontalHeader()
        assert header.font().pointSizeF() == before + 1
        assert header.font().bold()
