"""The window's settle (#1177): the gate that holds the content
clear until it is final, and the rows' rise on every table a
page shows. The widgets run under the offscreen platform on
every host (the strip's own fixture shape); the clocks are
shortened so a test reads a whole settle in well under a
second.
"""

import importlib.util
import os
import time
from types import SimpleNamespace

import pytest

if importlib.util.find_spec("PySide6") is None:
    pytest.skip(
        "PySide6 is not installed on this host",
        allow_module_level=True,
    )

from msks.client.gui import arrival  # noqa: E402

#: The settle's clocks, shortened for the tests but long enough to
#: observe mid-flight on a loaded runner: the quiet spell, the
#: cap, and the rise.
FAST = {
    "QUIET_MS": 100,
    "SETTLE_CAP_MS": 80,
    "ROW_STEP_MS": 100,
    "STAGGER_MS": 1000,
    "RISE_MS": 200,
}


@pytest.fixture(autouse=True)
def fast_clocks(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every clock of the settle read short: the module's
    constants are read when a gate or a stagger is built, so
    the patch lands on whatever the test builds after it."""
    for name, value in FAST.items():
        monkeypatch.setattr(arrival, name, value)


@pytest.fixture()
def qt(tmp_path):
    """The offscreen ``QApplication`` the settle needs, the
    strip's own fixture shape (#962): the silent fontconfig,
    the platform pin, and the Qt names imported inside the
    fixture."""
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
    for widget in app.topLevelWidgets():
        widget.close()
        widget.deleteLater()
    QtCore.QCoreApplication.sendPostedEvents(
        None, QtCore.QEvent.Type.DeferredDelete
    )
    app.processEvents()


def spin(qt, predicate, *, ms: int = 3000) -> None:
    """Turn the loop until ``predicate`` holds, or fail the
    test at the deadline."""
    deadline = time.monotonic() + ms / 1000
    while time.monotonic() < deadline:
        qt.app.processEvents()
        if predicate():
            return
        time.sleep(0.005)
    pytest.fail("the settle never reached the state the test waits for")


def make_bridge(qt, in_flight: int = 0):
    """The gate's bridge: the count the gate reads and the
    ``idle`` signal it waits on."""

    class Bridge(qt.QtCore.QObject):
        idle = qt.QtCore.Signal()

        def __init__(self) -> None:
            super().__init__()
            self.in_flight = in_flight

    bridge = Bridge()
    qt.keep.append(bridge)
    return bridge


def make_window(qt, tables: int = 1, shown: bool = True):
    """A window whose content holds a label and the given
    number of two-row tables — the page the gate clears."""
    window = qt.QtWidgets.QMainWindow()
    content = qt.QtWidgets.QWidget()
    layout = qt.QtWidgets.QVBoxLayout(content)
    layout.addWidget(qt.QtWidgets.QLabel("chrome"))
    made = []
    for _ in range(tables):
        table = qt.QtWidgets.QTableWidget(2, 1, content)
        table.setItem(0, 0, qt.QtWidgets.QTableWidgetItem("a"))
        table.setItem(1, 0, qt.QtWidgets.QTableWidgetItem("b"))
        layout.addWidget(table)
        made.append(table)
    window.setCentralWidget(content)
    if shown:
        window.show()
        qt.app.processEvents()
    return window, content, made


def test_rise_eases_out_from_the_step() -> None:
    """One row's progress (#1177): clear before its step, opaque
    at the rise's end, and in between ahead of the linear
    clock."""
    assert arrival.rise(-1) == 0.0
    assert arrival.rise(0) == 0.0
    assert arrival.rise(arrival.RISE_MS) == 1.0
    assert arrival.rise(arrival.RISE_MS * 2) == 1.0
    assert 0.5 < arrival.rise(arrival.RISE_MS // 2) < 1.0


def test_the_opening_waits_for_the_paint_the_bridge_and_the_quiet(
    qt,
) -> None:
    """The opening's gate (#1177): the content is cleared before
    the show, stays clear through the window's first paint and
    while the bridge has a flight in flight, and shows once the
    flight landed and a quiet spell passed — the effect off
    the content then."""
    window, content, _tables = make_window(qt, shown=False)
    bridge = make_bridge(qt, in_flight=1)
    gate = arrival.Settle(content, bridge)
    gate.cap.setInterval(5000)  # the gate under test, not its cap
    gate.after_first_paint(window)
    assert content.graphicsEffect().opacity() == 0.0
    window.show()
    spin(qt, lambda: gate.painted)
    spin(qt, lambda: gate.on_idle)
    qt.app.processEvents()
    time.sleep(0.03)
    qt.app.processEvents()
    # The window painted; the bridge still holds the content.
    assert gate.waiting and gate.on_idle
    assert content.graphicsEffect().opacity() == 0.0
    # The flight lands: the quiet spell runs, then the show.
    bridge.in_flight = 0
    bridge.idle.emit()
    spin(qt, lambda: content.graphicsEffect() is None)
    assert not gate.waiting and not gate.on_idle


def test_a_quiet_layout_change_delays_the_show(qt) -> None:
    """The quiet spell (#1177): a resize or a layout request
    under the content while the spell runs starts it over, so
    the content never shows a page still moving into shape; a
    change outside the content, on something that is no
    widget, or of any other kind does not."""
    _window, content, _tables = make_window(qt)
    gate = arrival.Settle(content)
    gate.cap.setInterval(5000)  # the quiet spell under test, not the cap
    gate.hold()
    spin(qt, lambda: gate.quiet.isActive())
    # The spell's starts counted, so the test reads the restart
    # itself and not a clock that a loaded runner moves under it.
    starts: list[int] = []
    original_start = gate.quiet.start

    def counting_start() -> None:
        starts.append(1)
        original_start()

    gate.quiet.start = counting_start  # type: ignore[method-assign]
    resize = qt.QtCore.QEvent(qt.QtCore.QEvent.Type.Resize)
    assert gate.eventFilter(qt.app.focusWindow(), resize) is False
    layout_request = qt.QtCore.QEvent(qt.QtCore.QEvent.Type.LayoutRequest)
    gate.eventFilter(qt.QtWidgets.QWidget(), layout_request)  # outside
    # Any other event under the content leaves the spell alone.
    gate.eventFilter(content, qt.QtCore.QEvent(qt.QtCore.QEvent.Type.User))
    assert starts == []
    gate.eventFilter(content.findChild(qt.QtWidgets.QLabel), layout_request)
    assert starts == [1]
    assert gate.quiet.isActive()
    spin(qt, lambda: content.graphicsEffect() is None)


def test_the_cap_shows_the_content_whatever_the_gate_waits_on(qt) -> None:
    """The cap (#1177): a bridge that never runs idle — a hung
    daemon — cannot leave the window blank past
    ``SETTLE_CAP_MS``."""
    window, content, _tables = make_window(qt)
    bridge = make_bridge(qt, in_flight=1)
    gate = arrival.Settle(content, bridge)
    gate.after_first_paint(window)
    spin(qt, lambda: gate.on_idle)
    spin(qt, lambda: not gate.waiting, ms=2000)
    assert not gate.on_idle
    spin(qt, lambda: content.graphicsEffect() is None)


def test_the_unpainted_window_holds_until_the_quiet_passes(qt) -> None:
    """The opening's paint hold (#1177): a quiet spell that
    passes before the window painted shows nothing; the
    paint's own iteration then does."""
    window, content, _tables = make_window(qt, shown=False)
    gate = arrival.Settle(content)
    gate.cap.setInterval(5000)  # the paint hold under test, not the cap
    gate.after_first_paint(window)
    qt.app.processEvents()
    assert not gate.painted and gate.waiting
    gate.quiet_passed()
    assert content.graphicsEffect() is not None
    window.show()
    spin(qt, lambda: gate.painted)
    spin(qt, lambda: content.graphicsEffect() is None)


def test_a_hold_under_way_starts_over_and_a_show_without_one_is_quiet(
    qt,
) -> None:
    """A second hold after the content showed (#1177) — the page
    a tab change returns to — clears it again and runs the gate
    from the start; a show asked of a gate that never held
    changes nothing."""
    _window, content, _tables = make_window(qt)
    gate = arrival.Settle(content)
    gate.hold()
    spin(qt, lambda: content.graphicsEffect() is None)
    gate.hold()
    assert content.graphicsEffect().opacity() == 0.0
    assert gate.waiting
    # A hold asked while the gate still holds clears through
    # the effect already on the content.
    gate.hold()
    assert gate.waiting and content.graphicsEffect().opacity() == 0.0
    spin(qt, lambda: content.graphicsEffect() is None)
    gate.show()
    assert content.graphicsEffect() is None and not gate.waiting
    # A layout change under a gate that shows asks nothing.
    gate.eventFilter(
        content, qt.QtCore.QEvent(qt.QtCore.QEvent.Type.LayoutRequest)
    )
    assert not gate.quiet.isActive()
    # The gate asked again after the show, or with no bridge in
    # hand, asks nothing: an idle bridge starts the quiet spell
    # without a wait, and the idle slot without a bridge only
    # starts it.
    gate.gate()
    assert not gate.waiting
    idle_bridge = make_bridge(qt, in_flight=0)
    idle_gate = arrival.Settle(content, idle_bridge)
    idle_gate.cap.setInterval(5000)
    idle_gate.hold()
    spin(qt, lambda: idle_gate.quiet.isActive())
    assert not idle_gate.on_idle
    spin(qt, lambda: content.graphicsEffect() is None)
    bare_gate = arrival.Settle(content)
    bare_gate.cap.setInterval(5000)
    bare_gate.hold()
    spin(qt, lambda: bare_gate.quiet.isActive())
    bare_gate.idle()
    assert not bare_gate.on_idle
    spin(qt, lambda: content.graphicsEffect() is None)


def test_the_delegate_paints_through_at_the_rows_progress(qt) -> None:
    """The stagger's painter (#1177): a cell under a progress
    of zero paints nothing; under a progress it paints through
    the table's own delegate at that opacity, lifted by the
    rise the progress has not undone; at one, in place. The
    size hint is the table's own."""
    calls: list[tuple[float, int]] = []

    class Original(qt.QtWidgets.QStyledItemDelegate):
        def paint(self, painter, option, index) -> None:
            calls.append((painter.opacity(), painter.transform().dy()))

        def sizeHint(self, option, index):
            return qt.QtCore.QSize(3, 4)

    table = qt.QtWidgets.QTableWidget(1, 1)
    progress = [0.0]
    stagger = SimpleNamespace(table=table, progress_of=lambda row: progress[0])
    delegate = arrival.StaggerDelegate(Original(), stagger)
    pixmap = qt.QtGui.QPixmap(8, 8)
    painter = qt.QtGui.QPainter(pixmap)
    index = table.model().index(0, 0)
    option = qt.QtWidgets.QStyleOptionViewItem()
    delegate.paint(painter, option, index)
    assert calls == []
    progress[0] = 0.5
    delegate.paint(painter, option, index)
    assert calls[-1][0] == 0.5 and calls[-1][1] == round(arrival.RISE_PX / 2)
    progress[0] = 1.0
    delegate.paint(painter, option, index)
    assert calls[-1] == (1.0, 0)
    painter.end()
    assert delegate.sizeHint(option, index) == qt.QtCore.QSize(3, 4)


def make_stagger_table(qt, rows: int = 2):
    """A table with ``rows`` rows, a button in each row's
    action column — the cells the stagger arms — shown in a
    window so it paints."""
    window = qt.QtWidgets.QMainWindow()
    table = qt.QtWidgets.QTableWidget(rows, 2)
    for row in range(rows):
        table.setItem(row, 0, qt.QtWidgets.QTableWidgetItem(f"r{row}"))
        table.setCellWidget(row, 1, qt.QtWidgets.QToolButton())
    window.setCentralWidget(table)
    window.show()
    qt.app.processEvents()
    return window, table


def test_the_rows_rise_and_land_in_place(qt) -> None:
    """The stagger's clock (#1177): the rows start clear, the
    delegate swapped in and the action cells under their own
    effects; the clock starts at the table's first paint and
    the rows rise a step apart; at the end the table is back
    on its own delegate, the cells bare, and the stagger
    gone."""
    _window, table = make_stagger_table(qt)
    original = table.itemDelegate()
    stagger = arrival.RowStagger(table)
    assert isinstance(table.itemDelegate(), arrival.StaggerDelegate)
    assert table.cellWidget(0, 1).graphicsEffect().opacity() == 0.0
    assert stagger.progress_of(0) == 0.0
    assert stagger.progress_of(9) == 1.0
    # A repaint of the table's viewport starts the clock.
    table.viewport().update()
    spin(qt, lambda: stagger.anim is not None)
    assert stagger.progress == [0.0, 0.0]
    # The clock's shape, read off the ticks themselves: a step
    # apart, each row easing out over its rise.
    step = stagger.step
    stagger.tick(5)
    assert stagger.progress[0] > 0.0 and stagger.progress[1] == 0.0
    first = stagger.progress[0]
    stagger.tick(step + 20)
    assert stagger.progress[0] > first and stagger.progress[1] > 0.0
    stagger.tick(step * 2 + arrival.RISE_MS)
    assert stagger.progress == [1.0, 1.0]
    assert table.cellWidget(0, 1).graphicsEffect().opacity() == 1.0
    spin(qt, lambda: not stagger.live)
    assert table.itemDelegate() is original
    assert table.cellWidget(0, 1).graphicsEffect() is None


def test_an_empty_table_waits_for_its_rows(qt) -> None:
    """The pages that refill on show clear first (#1177): a
    stagger on an empty table arms nothing until the rows
    land, then arms the cells the landing built and waits for
    the paint like any other."""
    _window, table = make_stagger_table(qt, rows=0)
    stagger = arrival.RowStagger(table)
    assert stagger.rows == 0 and stagger.anim is None
    table.setRowCount(1)
    table.setItem(0, 0, qt.QtWidgets.QTableWidgetItem("late"))
    table.setCellWidget(0, 1, qt.QtWidgets.QToolButton())
    spin(qt, lambda: stagger.rows == 1)
    assert table.cellWidget(0, 1).graphicsEffect().opacity() == 0.0
    table.viewport().update()
    spin(qt, lambda: not stagger.live)
    assert table.cellWidget(0, 1).graphicsEffect() is None


def test_a_refill_under_a_running_stagger_joins_the_clock(qt) -> None:
    """A table refilled while its rows rise (#1177): the new
    rows and their cells arm at the running clock's progress
    instead of standing at full strength."""
    _window, table = make_stagger_table(qt)
    stagger = arrival.RowStagger(table)
    table.viewport().update()
    spin(qt, lambda: stagger.anim is not None)
    spin(qt, lambda: stagger.progress[0] > 0.0)
    table.setRowCount(3)
    table.setItem(2, 0, qt.QtWidgets.QTableWidgetItem("new"))
    button = qt.QtWidgets.QToolButton()
    table.setCellWidget(2, 1, button)
    # The rows already risen keep their progress through the
    # landing: only the new row starts clear.
    assert stagger.progress[0] > 0.0
    assert stagger.progress[2] == 0.0
    spin(qt, lambda: button.graphicsEffect() is not None)
    assert button.graphicsEffect().opacity() == stagger.progress[2]
    spin(qt, lambda: not stagger.live)
    assert button.graphicsEffect() is None


def test_a_rewind_restarts_the_rise(qt) -> None:
    """A stagger asked again (#1177): a rise under way rewinds
    from clear with a fresh clock; a table still empty keeps
    waiting."""
    _window, table = make_stagger_table(qt)
    stagger = arrival.RowStagger(table)
    table.viewport().update()
    spin(qt, lambda: stagger.progress[0] > 0.0)
    arrival.stagger_rows(table)
    assert table.findChildren(arrival.RowStagger) == [stagger]
    assert stagger.progress == [0.0, 0.0]
    spin(qt, lambda: not stagger.live)
    empty_window, empty = make_stagger_table(qt, rows=0)
    waiting = arrival.RowStagger(empty)
    waiting.rewind()
    assert waiting.live and waiting.anim is None


def test_a_stagger_on_a_finished_table_starts_afresh(qt) -> None:
    """A table whose stagger landed takes a new one on the next
    ask (#1177): the finished stagger no longer counts as
    under way."""
    _window, table = make_stagger_table(qt)
    first = arrival.RowStagger(table)
    table.viewport().update()
    spin(qt, lambda: not first.live)
    arrival.stagger_rows(table)
    qt.app.processEvents()
    live = [s for s in table.findChildren(arrival.RowStagger) if s.live]
    assert len(live) == 1 and live[0] is not first
    table.viewport().update()
    spin(qt, lambda: not live[0].live)


def test_cell_widgets_walk_the_rows_cells(qt) -> None:
    """``cell_widgets`` (#1177): the index widgets of a row in
    column order, the plain cells skipped."""
    _window, table = make_stagger_table(qt, rows=1)
    button = table.cellWidget(0, 1)
    assert list(arrival.cell_widgets(table, 0)) == [button]
    # A widget ahead of a plain cell: the walk skips the plain
    # one and goes on.
    first = qt.QtWidgets.QToolButton()
    table.setColumnCount(3)
    table.setCellWidget(0, 2, first)
    assert list(arrival.cell_widgets(table, 0)) == [button, first]


def make_strip(qt):
    """A tab widget over two pages, each holding a table with
    two rows, shown in a window."""
    window = qt.QtWidgets.QMainWindow()
    tabs = qt.QtWidgets.QTabWidget()
    tables = []
    for label in ("first", "second"):
        page = qt.QtWidgets.QWidget()
        table = qt.QtWidgets.QTableWidget(2, 1, page)
        table.setItem(0, 0, qt.QtWidgets.QTableWidgetItem("a"))
        table.setItem(1, 0, qt.QtWidgets.QTableWidgetItem("b"))
        tabs.addTab(page, label)
        tables.append(table)
    window.setCentralWidget(tabs)
    window.show()
    qt.app.processEvents()
    return window, tabs, tables


def live_staggers(table):
    return [s for s in table.findChildren(arrival.RowStagger) if s.live]


def test_the_hidden_tables_take_no_part(qt) -> None:
    """``visible_tables`` (#1177): a hidden table under the
    content is passed over, the visible ones after it kept."""
    _window, content, tables = make_window(qt, tables=2)
    tables[0].hide()
    qt.app.processEvents()
    assert list(arrival.visible_tables(content)) == [tables[1]]


def test_a_strip_change_settles_the_page_it_shows(qt) -> None:
    """``stagger_on_change`` (#1177): a strip's tab change
    gives the page it shows the rise, its visible tables only
    — a table on the page left behind takes no part — and a
    change naming no page asks nothing."""
    window, tabs, tables = make_strip(qt)
    arrival.stagger_on_change(tabs, tabs.widget)
    tabs.setCurrentIndex(1)
    spin(qt, lambda: live_staggers(tables[1]))
    assert live_staggers(tables[0]) == []
    spin(qt, lambda: not live_staggers(tables[1]))

    class Bare(qt.QtCore.QObject):
        currentChanged = qt.QtCore.Signal(int)

    bare = Bare()
    qt.keep.append(bare)
    arrival.stagger_on_change(bare, lambda index: None)
    bare.currentChanged.emit(3)
    qt.app.processEvents()
    window.close()
