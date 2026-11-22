"""The workspace list screen (#939, #972): the thin widget over
the presenter's row models — the TUI listing's five fields,
NAME, STATUS with the unresolved-mounts marker, ID, IMAGE,
CREATED, and the row-actions column beside them — refreshed
through the bridge on the screen's own open and on the
listing-affecting frames the events feed pushes (#1019), with
a failed refresh flashing the status line while the rows
stand. The widget runs under the offscreen platform on every
host (#939); the screen's slots are driven directly and
through the stub bridge's signal emits, the bridge's own tests
holding the real worker's contract.
"""

import importlib.util
import os
import time
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

if importlib.util.find_spec("PySide6") is None:
    pytest.skip(
        "PySide6 is not installed on this host",
        allow_module_level=True,
    )

from msks.client import ages as ages_mod  # noqa: E402
from msks.client import data as clientdata  # noqa: E402
from msks.client.gui import listlayout  # noqa: E402
from msks.client.gui import rows as gui_rows  # noqa: E402

#: The poll cadence for the offscreen event loop, seconds — the
#: timer test pumps Qt's queue until the tick lands.
TICK = 0.01


@pytest.fixture(autouse=True)
def pinned_clock(monkeypatch: pytest.MonkeyPatch) -> None:
    """The created cells read one fixed clock (#350's seam, on
    the shared leaf since #939): the fixture's Jan 2 stamp reads
    ``2d ago`` under the pinned Jan 4."""
    monkeypatch.setattr(
        ages_mod,
        "clock_now",
        lambda: datetime.fromisoformat("2026-01-04T12:00:00"),
    )


class FakeSignal:
    """A Qt signal's connect/emit surface for the stub bridge:
    an emit calls the connected slots at once, in the GUI thread
    the real signal's queued delivery would land them in."""

    def __init__(self) -> None:
        self.slots: list = []

    def connect(self, slot) -> None:
        self.slots.append(slot)

    def emit(self, *args) -> None:
        for slot in self.slots:
            slot(*args)


class StubBridge:
    """The bridge's surface for the screen's tests: the two
    signals the screen reads and the one verb it calls, every
    call recorded — no worker, no network."""

    def __init__(self) -> None:
        self.calls: list[tuple] = []
        self.succeeded = FakeSignal()
        self.failed = FakeSignal()

    def call(self, key: str, method: str, *args: object) -> None:
        self.calls.append((key, method, args))


def daemon_rows() -> list[dict]:
    """Three daemon rows out of lifecycle order, one with
    unresolved mounts — the listing an answer carries, every
    cell's field on every row."""
    return [
        {
            "id": "ws-1",
            "name": "alpha",
            "status": "stopped",
            "image_ref": "debian:13.6",
            "image_hash": "a" * 64,
            "created_at": "2026-01-02T03:04:05",
            "mount_issues": [],
            "cpus": 2,
            "mem_mib": 8192,
            "root_mib": 8192,
            "home_mib": 16384,
            "egress_mode": "allow",
            "mounts_pending": False,
            "sizes_pending": False,
        },
        {
            "id": "ws-2",
            "name": "beta",
            "status": "running",
            "image_ref": None,
            "image_hash": "b" * 64,
            "created_at": "2026-01-04T09:00:00",
            "mount_issues": [{"kind": "skipped"}],
            "cpus": 4,
            "mem_mib": 16384,
            "root_mib": 4096,
            "home_mib": 8192,
            "egress_mode": "allow",
            "mounts_pending": True,
            "sizes_pending": False,
        },
        {
            "id": "ws-3",
            "name": None,
            "status": "starting",
            "image_ref": None,
            "image_hash": None,
            "created_at": None,
            "mount_issues": [],
            "cpus": None,
            "mem_mib": None,
            "root_mib": None,
            "home_mib": None,
            "egress_mode": None,
            "mounts_pending": False,
            "sizes_pending": False,
        },
    ]


@pytest.fixture()
def qt(tmp_path: Path):
    """The offscreen ``QApplication`` the screen needs, with the
    silent fontconfig the window test uses (Qt's shutdown-time
    re-scan runs after fixture teardown, so the variables sit on
    the environment directly, not monkeypatch — undo would hand
    the re-scan the host /etc/fonts). The Qt names import here
    too, inside the fixture: this module is imported at
    collection time by the test runner's controller process as
    well, and a controller has no business loading the binding
    (test_gui_bridge's docstring states the trap)."""
    fonts = tmp_path / "fonts.conf"
    fonts.write_text("<fontconfig/>\n")
    os.environ["FONTCONFIG_FILE"] = str(fonts)
    os.environ["QT_QPA_PLATFORM"] = "offscreen"
    from msks.client.gui import listcells as listcells_mod
    from msks.client.gui import workspaces as workspaces_mod
    from PySide6 import QtCore, QtGui, QtWidgets

    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication(
        ["msks-test"]
    )
    yield SimpleNamespace(
        app=app,
        QtCore=QtCore,
        QtGui=QtGui,
        QtWidgets=QtWidgets,
        screen_mod=workspaces_mod,
        cells_mod=listcells_mod,
    )
    # The teardown every GUI test owes its worker (#959): close
    # and delete every top-level widget the test left, then
    # flush Qt's deferred deletions and posted events. A C++
    # window that outlives its test keeps receiving events, and
    # the screen↔bridge reference cycle hands their delivery to
    # the cycle collector at an arbitrary later moment — an
    # empty wrapper PySide6 raises then, whose handlers would
    # trace to the stderr of whatever unrelated test runs. The
    # guards in the screens answer those deliveries, and this
    # teardown leaves nothing behind to deliver to.
    for widget in app.topLevelWidgets():
        widget.close()
        widget.deleteLater()
    QtCore.QCoreApplication.sendPostedEvents(
        None, QtCore.QEvent.Type.DeferredDelete
    )
    app.processEvents()


def make_screen(qt) -> tuple:
    """The screen over the stub bridge, shown in a real window —
    the shape every test reads: open, first refresh issued (and
    answered, so later refreshes run)."""
    bridge = StubBridge()
    screen = qt.screen_mod.WorkspaceList(bridge)
    window = qt.QtWidgets.QMainWindow()
    window.setCentralWidget(screen)
    window.show()
    screen.on_succeeded("workspaces-list", daemon_rows())
    return screen, window, bridge


def test_the_open_screen_issues_its_first_refresh(qt) -> None:
    """The window opens on the list (#939, #1019): showing the
    screen issues the first refresh through the bridge. The
    standing tick is gone — the listing re-reads on the frames
    the events feed pushes, the shell's own filter deciding
    which."""
    bridge = StubBridge()
    screen = qt.screen_mod.WorkspaceList(bridge)
    window = qt.QtWidgets.QMainWindow()
    window.setCentralWidget(screen)
    assert bridge.calls == []  # construction alone asks nothing
    window.show()
    assert bridge.calls == [("workspaces-list", "workspaces", ())]
    assert not hasattr(screen, "timer")  # no standing tick stands
    window.close()


def test_the_window_opens_on_the_standing_sort(qt) -> None:
    """The default sort arrow shows at first render (#939): the
    CREATED header carries the descending arrow, and the rows
    open in that order — newest first, the TUI's own standing
    sort (#470 L1)."""
    screen, window, _bridge = make_screen(qt)
    header = screen.table.horizontalHeader()
    assert header.isSortIndicatorShown()
    assert header.sortIndicatorSection() == qt.cells_mod.CREATED_COLUMN
    assert (
        header.sortIndicatorOrder() == qt.QtCore.Qt.SortOrder.DescendingOrder
    )
    labels = [screen.table.item(row, 0).text() for row in range(3)]
    assert labels == ["beta", "alpha", "ws-3"]
    window.close()


def test_the_list_renders_the_row_models(qt) -> None:
    """The TUI listing's five fields (#939): one row per model,
    the label, the status word with the unresolved-mounts marker
    ahead of it, and the id, image, and created cells — the
    image falling back to the hash, the created reading the
    shared ladder, ``-`` where the row carries nothing."""
    screen, window, _bridge = make_screen(qt)
    # A pane wide enough under any style's margins: every whole
    # value fits, no ellipsis.
    window.resize(2000, 700)
    qt.app.processEvents()
    assert screen.table.rowCount() == 3
    # The standing sort orders the rows (#939): CREATED
    # newest-first, the TUI's own default.
    labels = [screen.table.item(row, 0).text() for row in range(3)]
    assert labels == ["beta", "alpha", "ws-3"]
    assert screen.table.item(0, 1).text() == "✗1 ↻1 running"
    assert screen.table.item(1, 1).text() == "stopped"
    # The ID column's cell (#849's vocabulary): the immutable id.
    assert screen.table.item(2, 2).text() == "ws-3"
    # The IMAGE cell: the reference first, the hash when no
    # reference came, ``-`` when neither did.
    assert screen.table.item(1, 3).text() == "debian:13.6"
    assert screen.table.item(0, 3).text() == "b" * 64
    assert screen.table.item(2, 3).text() == "-"
    # The CREATED cell: the shared relative ladder (#350).
    assert screen.table.item(0, 4).text() == "today"
    assert screen.table.item(1, 4).text() == "2d ago"
    assert screen.table.item(2, 4).text() == "-"
    rank = qt.cells_mod.RANK_ROLE
    assert (
        screen.table.item(0, 1).data(rank) == 2  # running's lifecycle rank
    )
    window.close()


def test_a_later_answer_replaces_the_rows(qt) -> None:
    """Each answer rebuilds the table whole (#939): the second
    listing replaces the first — no accumulation, and an empty
    listing empties the screen without leaving the last rows
    behind."""
    screen, window, _bridge = make_screen(qt)
    screen.on_succeeded("workspaces-list", [])
    assert screen.table.rowCount() == 0
    screen.on_succeeded("workspaces-list", daemon_rows()[:1])
    assert screen.table.rowCount() == 1
    window.close()


def test_a_refresh_in_flight_is_skipped(qt) -> None:
    """The in-flight guard (#939, the TUI listing's own #470
    rule): while a refresh waits on its answer, the tick and the
    button add nothing — a slow call does not stack identical
    requests behind itself. Whichever answer comes (a listing or
    a refusal) retires the flight, and the next refresh
    runs."""
    screen, window, bridge = make_screen(qt)
    bridge.calls.clear()
    screen.refresh()  # the flight opens
    assert bridge.calls == [("workspaces-list", "workspaces", ())]
    screen.refresh()
    screen.refresh()
    assert bridge.calls == [("workspaces-list", "workspaces", ())]
    screen.on_succeeded("workspaces-list", daemon_rows()[:1])
    bridge.calls.clear()
    screen.refresh()
    assert bridge.calls == [("workspaces-list", "workspaces", ())]
    screen.refresh()
    assert bridge.calls == [
        ("workspaces-list", "workspaces", ())
    ]  # one flight at a time
    screen.on_failed(
        "workspaces-list",
        clientdata.RestFailure("409: workspace is running", reach=False),
    )
    bridge.calls.clear()
    screen.refresh()  # a refusal retired the flight the same way
    assert bridge.calls == [("workspaces-list", "workspaces", ())]
    window.close()


def test_a_failed_refresh_flashes_and_keeps_the_rows(qt) -> None:
    """The failure contract (#939, AGENTS.md \"Exception
    handling\"): a refused refresh flashes its message on the
    status line while the rows already on screen stand — the
    operator keeps the list the last answer built — and the next
    successful answer clears the flash with the rows it
    rebuilds."""
    screen, window, _bridge = make_screen(qt)
    screen.on_failed(
        "workspaces-list",
        clientdata.RestFailure("409: workspace is running", reach=False),
    )
    assert "409" in screen.status.text()
    assert screen.table.rowCount() == 3  # the rows stand
    # A refusal runs its own span (#940): a listing answer
    # landing inside it does not wipe it — only a standing
    # outage resolves on an answer.
    screen.on_succeeded("workspaces-list", daemon_rows()[:1])
    assert "409" in screen.status.text()
    window.close()


def test_a_refusal_flash_clears_on_its_timeout(qt, monkeypatch) -> None:
    """The flash's own span (#939): a refusal clears itself when
    its timeout lands — proven by elapsed time, a tiny flash
    span pumped through the real event queue."""
    screen, window, _bridge = make_screen(qt)
    monkeypatch.setattr(qt.screen_mod, "FLASH_MS", 5)
    screen.on_failed(
        "workspaces-list",
        clientdata.RestFailure("nope", reach=False),
    )
    assert "nope" in screen.status.text()
    deadline = time.monotonic() + 10.0
    while time.monotonic() < deadline and screen.status.text():
        qt.app.processEvents()
        time.sleep(TICK)
    assert screen.status.text() == ""
    window.close()


def test_an_outage_stands_until_an_answer(qt, monkeypatch) -> None:
    """An outage is not a flash (#939, #904's contract): a
    failure the daemon never answered (``reach``) stands on the
    status line — the same tiny flash span proves nothing clears
    it — and the next successful answer resolves it."""
    screen, window, _bridge = make_screen(qt)
    monkeypatch.setattr(qt.screen_mod, "FLASH_MS", 5)
    screen.on_failed(
        "workspaces-list",
        clientdata.RestFailure("msks: the daemon is unreachable", reach=True),
    )
    assert "unreachable" in screen.status.text()
    deadline = time.monotonic() + 0.2
    while time.monotonic() < deadline:
        qt.app.processEvents()
        time.sleep(TICK)
    assert "unreachable" in screen.status.text()  # it stood
    screen.on_succeeded("workspaces-list", [])
    assert screen.status.text() == ""  # the answer resolved it
    window.close()


def test_a_later_flash_outlives_an_earlier_timeout(qt) -> None:
    """The flash serial (#939): a second refusal inside the
    first's flash span keeps its own message — the first's
    timeout clears only the flash it was armed for."""
    screen, window, _bridge = make_screen(qt)
    screen.flash("first refusal")
    first = screen.flash_seq
    screen.flash("second refusal")
    assert screen.status.text() == "second refusal"
    screen.clear_stale(first)  # the first's timeout, late
    assert screen.status.text() == "second refusal"
    screen.clear_stale(screen.flash_seq)
    assert screen.status.text() == ""
    window.close()


def test_events_on_a_wrapper_past_construction_touch_nothing(
    qt,
) -> None:
    """The handlers tolerate the two half-built shapes Qt can
    deliver events to (#959): a screen whose construction
    never finished, and the empty wrapper PySide6 raises at
    teardown for a C++ object that outlived its Python one.
    A wrapper without its ``table`` and ``status``
    answers nothing: no refresh, no filtering, and
    Qt's own handling runs, instead of an AttributeError
    tracing to whatever test is running."""
    screen, window, _bridge = make_screen(qt)
    window.close()
    # The two teardown shapes leave a wrapper with no instance
    # attributes — the same state the guards answer.
    del screen.table
    del screen.status
    screen.showEvent(qt.QtGui.QShowEvent())
    screen.resizeEvent(
        qt.QtGui.QResizeEvent(qt.QtCore.QSize(10, 10), qt.QtCore.QSize(5, 5))
    )
    watched = qt.QtCore.QObject()
    press = qt.QtCore.QEvent(qt.QtCore.QEvent.Type.MouseButtonPress)
    assert screen.eventFilter(watched, press) is False


def test_another_keys_answer_is_not_the_screens(qt) -> None:
    """The key filter (#939): the bridge carries every screen's
    calls on the one worker — this screen answers its own key
    alone, so another key's result does not rebuild the table,
    retire a flight, or flash the line."""
    screen, window, bridge = make_screen(qt)
    screen.on_failed(
        "workspaces-list",
        clientdata.RestFailure("nope", reach=False),
    )
    screen.on_succeeded("some-other-key", [])
    assert screen.table.rowCount() == 3
    assert "nope" in screen.status.text()
    screen.on_failed(
        "some-other-key", clientdata.RestFailure("x", reach=False)
    )
    assert screen.status.text() != ""
    # The foreign answer retired nothing: the screen's own
    # flight still holds a refresh opened before it.
    bridge.calls.clear()
    screen.refresh()
    assert bridge.calls == [("workspaces-list", "workspaces", ())]
    screen.on_succeeded("some-other-key", [])
    screen.refresh()
    assert bridge.calls == [("workspaces-list", "workspaces", ())]
    window.close()


def test_the_refresh_slot_refreshes_through_the_bridge(qt) -> None:
    """The refresh verb (#939, #972): the open's call, a
    listing-affecting frame, and the feed's serve poke share
    it, all through the bridge — the toolbar's button is gone,
    the rows hold the actions now."""
    screen, window, bridge = make_screen(qt)
    bridge.calls.clear()  # the open refresh is already answered
    screen.refresh()
    assert bridge.calls == [("workspaces-list", "workspaces", ())]
    window.close()


def test_a_reshown_screen_refreshes(qt) -> None:
    """A re-show refreshes (#939, #1019): the standing tick is
    gone, so the show's own read is the screen's answer for the
    frames that landed while it was hidden."""
    screen, window, bridge = make_screen(qt)
    screen.on_succeeded("workspaces-list", [])  # retire the open's flight
    bridge.calls.clear()
    window.close()
    assert bridge.calls == []
    window.show()
    assert bridge.calls == [("workspaces-list", "workspaces", ())]
    window.close()


def test_the_closed_window_asks_nothing(qt) -> None:
    """A closed window hides the screen and nothing asks the
    daemon on its own after that (#939, #1019): the standing
    tick is gone, so a hidden screen sends no periodic traffic
    at all — a daemon at rest receives no listing reads from an
    idle attached client, the TUI's own rule."""
    screen, window, bridge = make_screen(qt)
    screen.on_succeeded("workspaces-list", [])  # retire the open's flight
    bridge.calls.clear()
    window.close()
    for _ in range(5):
        qt.app.processEvents()
        time.sleep(TICK)
    assert bridge.calls == []
    window.close()


def test_the_created_sorts_by_date_not_by_label(qt) -> None:
    """The CREATED column sorts by the parsed moment (#939,
    #854's rule): the relative labels' text order and their
    date order disagree — ``1y ago`` sits a year from
    ``10mo ago`` in text order — and the newest-first read, the
    natural one, sinks the row with no stamp."""
    screen, window, _bridge = make_screen(qt)
    screen.on_succeeded(
        "workspaces-list",
        [
            {
                "id": "ws-1",
                "name": "ayear",
                "status": "stopped",
                "created_at": "2025-01-15T00:00:00",
                "mount_issues": [],
            },
            {
                "id": "ws-2",
                "name": "amonth",
                "status": "stopped",
                "created_at": "2025-12-15T00:00:00",
                "mount_issues": [],
            },
            {
                "id": "ws-3",
                "name": "today",
                "status": "stopped",
                "created_at": "2026-01-04T09:00:00",
                "mount_issues": [],
            },
            {
                "id": "ws-4",
                "name": "nostamp",
                "status": "stopped",
                "created_at": None,
                "mount_issues": [],
            },
        ],
    )
    screen.table.sortItems(4, qt.QtCore.Qt.SortOrder.DescendingOrder)
    labels = [screen.table.item(row, 0).text() for row in range(4)]
    assert labels == ["today", "amonth", "ayear", "nostamp"]
    screen.table.sortItems(4, qt.QtCore.Qt.SortOrder.AscendingOrder)
    labels = [screen.table.item(row, 0).text() for row in range(4)]
    assert labels == ["nostamp", "ayear", "amonth", "today"]
    window.close()


def test_the_columns_fit_their_longest_values(qt) -> None:
    """The columns' minimums (#939): every column stands at
    least as wide as its longest value — the header's own label
    included — so no cell elides, whatever the pane's width."""
    screen, window, _bridge = make_screen(qt)
    fm = screen.table.fontMetrics()
    for column, label in enumerate(qt.cells_mod.COLUMNS):
        texts = [
            screen.table.item(row, column).text()
            for row in range(screen.table.rowCount())
        ]
        longest = max(texts + [label], key=fm.horizontalAdvance)
        assert screen.table.columnWidth(column) >= fm.horizontalAdvance(
            longest
        ), (label, longest)
    window.close()


def test_a_narrow_pane_ellipsizes_the_image_alone(qt) -> None:
    """The narrow pane (#939): when the columns' whole values
    cannot all fit, the image column alone gives ground — its
    text ellipsized around the middle, head and tail kept —
    while every other column keeps its whole values; a pane
    with the room reads the image whole again."""
    screen, window, _bridge = make_screen(qt)
    window.resize(680, 700)  # the hash row alone overflows this
    qt.app.processEvents()
    image = screen.table.item(0, 3).text()  # the 64-char hash row
    assert image != "b" * 64
    assert "…" in image
    assert image.startswith("b") and image.endswith("b")
    # Every other cell reads whole.
    assert screen.table.item(0, 0).text() == "beta"
    assert screen.table.item(1, 3).text() == "debian:13.6"
    window.resize(1400, 700)
    qt.app.processEvents()
    assert screen.table.item(0, 3).text() == "b" * 64
    window.close()


def test_the_columns_fill_the_pane(qt) -> None:
    """The columns' fill (#939): the spare width beyond the
    minimums splits across the columns, so the table occupies
    the pane's whole width — and a resized pane re-flows, the
    minimums holding whatever the width."""
    screen, window, _bridge = make_screen(qt)
    screen.on_succeeded(
        "workspaces-list",
        [
            {
                "id": "ws-1",
                "name": "alpha",
                "status": "stopped",
                "image_ref": "debian:13.6",
                "created_at": "2026-01-02T03:04:05",
                "mount_issues": [],
            }
        ],
    )
    window.resize(1200, 700)
    qt.app.processEvents()
    total = sum(
        screen.table.columnWidth(column)
        for column in range(len(qt.cells_mod.COLUMNS) + 1)
    )
    assert total == screen.table.viewport().width()
    window.resize(1500, 700)
    qt.app.processEvents()
    total = sum(
        screen.table.columnWidth(column)
        for column in range(len(qt.cells_mod.COLUMNS) + 1)
    )
    assert total == screen.table.viewport().width()
    window.close()


def select_cell(qt, screen, row: int, column: int) -> None:
    """One clicked cell, as the view's row selection leaves it
    (#939): the whole row selected, the current cell on the
    click."""
    model = screen.table.model()
    selection = qt.QtCore.QItemSelection(
        model.index(row, 0),
        model.index(row, len(qt.cells_mod.COLUMNS) - 1),
    )
    screen.table.selectionModel().select(
        selection,
        qt.QtCore.QItemSelectionModel.SelectionFlag.ClearAndSelect,
    )
    screen.table.setCurrentIndex(model.index(row, column))


def test_an_unchanged_listing_rebuilds_nothing(qt) -> None:
    """The standing refresh leaves an unchanged listing alone
    (#939, the TUI listing's own #470 L2 rule): the same answer
    again rebuilds no item — the very objects stand — so the
    highlight the operator's click left cannot wander between
    rows while nothing interacts. The image reads by its whole
    value, so an elided display still counts as unchanged."""
    screen, window, _bridge = make_screen(qt)
    # The default offscreen window is narrow: the hash row's
    # image shows ellipsized.
    assert "…" in screen.table.item(0, 3).text()
    name = screen.table.item(0, 0)
    select_cell(qt, screen, 0, 1)
    screen.on_succeeded("workspaces-list", daemon_rows())
    assert screen.table.item(0, 0) is name  # nothing was rebuilt
    assert (
        len(screen.table.selectedIndexes()) == len(qt.cells_mod.COLUMNS) + 1
    )  # the whole row, the action column with it, still holds
    assert screen.table.currentIndex().column() == 1
    window.close()


def test_the_selection_follows_the_workspace(qt) -> None:
    """A rebuild the listing needs carries the hold by
    workspace id (#939): the clicked cell lands on its
    workspace's row wherever the new order puts it, and a
    workspace the new listing drops simply loses the hold."""
    screen, window, _bridge = make_screen(qt)
    select_cell(qt, screen, 1, 1)  # alpha's status cell
    # A reordered answer with the same rows rebuilds nothing:
    # the standing sort orders one set of rows the one way, and
    # the hold stands untouched.
    rows = daemon_rows()
    screen.on_succeeded("workspaces-list", [rows[1], rows[0], rows[2]])
    assert screen.table.item(1, 0).text() == "alpha"
    assert len(screen.table.selectedIndexes()) == len(qt.cells_mod.COLUMNS) + 1
    # A changed stamp moves the row under the hold: alpha's
    # creation moves to now, and the hold follows it to the top.
    moved = [dict(row) for row in daemon_rows()]
    moved[0]["created_at"] = "2026-01-04T11:00:00"  # alpha, newest now
    screen.on_succeeded("workspaces-list", moved)
    current = screen.table.currentIndex()
    assert (current.row(), current.column()) == (0, 1)
    assert screen.table.item(0, 0).text() == "alpha"
    selected_rows = {index.row() for index in screen.table.selectedIndexes()}
    assert selected_rows == {0}  # alpha's whole row, wherever it landed
    # alpha leaves the listing: the hold goes with it, quietly.
    screen.on_succeeded("workspaces-list", daemon_rows()[1:])
    assert screen.table.selectedIndexes() == []
    window.close()


def test_the_unchanged_and_hold_reads_defensively(qt) -> None:
    """The unchanged check and the hold read only what stands
    (#939): a missing cell reads as changed, never as a crash —
    and a selected cell on a row whose name item is gone reads
    as no hold at all."""
    screen, window, _bridge = make_screen(qt)
    models = gui_rows.workspace_rows(daemon_rows())
    assert screen.rows_unchanged(models) is True
    assert not screen.rows_unchanged(models[:1])  # fewer rows
    changed = [dict(daemon_rows()[0], name="renamed")]
    assert not screen.rows_unchanged(
        gui_rows.workspace_rows(changed)
    )  # a cell moved
    screen.table.setItem(0, qt.cells_mod.IMAGE_COLUMN, None)
    assert not screen.rows_unchanged(models)  # a missing cell
    select_cell(qt, screen, 1, 1)
    screen.table.setItem(1, qt.cells_mod.NAME_COLUMN, None)
    held, now = screen.held_rows()
    assert held == set() and now is None
    window.close()


def test_a_click_takes_the_whole_row(qt) -> None:
    """A click on any cell selects its whole row (#939): no cell
    edits, so the unit the operator selects is the workspace —
    a real mouse click on one cell, the row highlight it
    leaves."""
    from PySide6 import QtTest

    screen, window, _bridge = make_screen(qt)
    spot = screen.table.visualItemRect(screen.table.item(1, 3)).center()
    QtTest.QTest.mouseClick(
        screen.table.viewport(),
        qt.QtCore.Qt.MouseButton.LeftButton,
        pos=spot,
    )
    selected_rows = {index.row() for index in screen.table.selectedIndexes()}
    assert selected_rows == {1}  # the whole row, from one cell
    assert screen.table.currentIndex().column() == 3  # the click
    window.close()


def test_the_first_rows_hold_the_first_row(qt) -> None:
    """The first load's hold (#939): the listing's first row —
    the standing sort's newest workspace, the one a create just
    made — takes the selection and the current cell, so the
    keyboard and the eye start somewhere."""
    bridge = StubBridge()
    screen = qt.screen_mod.WorkspaceList(bridge)
    window = qt.QtWidgets.QMainWindow()
    window.setCentralWidget(screen)
    window.show()
    assert screen.table.rowCount() == 0  # nothing held on nothing
    screen.on_succeeded("workspaces-list", daemon_rows())
    selected_rows = {index.row() for index in screen.table.selectedIndexes()}
    assert selected_rows == {0}  # beta: the newest, whole row
    assert screen.table.item(0, 0).text() == "beta"
    current = screen.table.currentIndex()
    assert (current.row(), current.column()) == (0, 0)
    window.close()


def test_an_empty_first_answer_holds_nothing(qt) -> None:
    """A first answer with no workspaces holds nothing (#939):
    the hold starts on the first row a listing ever shows, and
    an empty listing shows none — no current cell, no selection.
    The first row that later arrives — the workspace a create
    just made — takes the hold."""
    bridge = StubBridge()
    screen = qt.screen_mod.WorkspaceList(bridge)
    window = qt.QtWidgets.QMainWindow()
    window.setCentralWidget(screen)
    window.show()
    screen.on_succeeded("workspaces-list", [])
    assert screen.table.rowCount() == 0
    assert screen.table.selectedIndexes() == []
    assert not screen.table.currentIndex().isValid()
    screen.on_succeeded("workspaces-list", daemon_rows())
    selected_rows = {index.row() for index in screen.table.selectedIndexes()}
    assert selected_rows == {0}  # the first row ever shown holds
    window.close()


def test_a_press_on_the_blank_space_keeps_the_hold(qt) -> None:
    """A press below the rows keeps the hold (#939): the blank
    viewport space is chrome, not a select-nothing gesture — a
    real mouse press there changes nothing."""
    from PySide6 import QtTest

    screen, window, _bridge = make_screen(qt)
    select_cell(qt, screen, 1, 1)
    assert screen.table.selectedIndexes()
    blank = screen.table.visualItemRect(
        screen.table.item(screen.table.rowCount() - 1, 0)
    ).bottomLeft() + qt.QtCore.QPoint(5, 5)
    assert not screen.table.indexAt(blank).isValid()
    QtTest.QTest.mouseClick(
        screen.table.viewport(),
        qt.QtCore.Qt.MouseButton.LeftButton,
        pos=blank,
    )
    assert (
        len(screen.table.selectedIndexes()) == len(qt.cells_mod.COLUMNS) + 1
    )  # alpha's row still holds
    window.close()


def test_the_hold_keeps_its_color_unfocused(qt) -> None:
    """The hold paints the same without the focus (#939): Qt
    washes a selected row out in the palette's inactive
    highlight while another widget holds the focus, and a
    washed-out row reads as dehighlighted — the table's inactive
    colors copy its active ones."""
    screen, window, _bridge = make_screen(qt)
    palette = screen.table.palette()
    active = qt.QtGui.QPalette.ColorGroup.Active
    inactive = qt.QtGui.QPalette.ColorGroup.Inactive
    for role in (
        qt.QtGui.QPalette.ColorRole.Highlight,
        qt.QtGui.QPalette.ColorRole.HighlightedText,
    ):
        assert palette.color(inactive, role) == palette.color(active, role)
    window.close()


def test_the_page_carries_the_action_row_and_the_rows_hold_theirs(
    qt,
) -> None:
    """The page's actions (#972, #1011): the bottom row carries
    Add, Edit, and Remove — the detail pages' Mounts and
    Proxies pattern — and each row's own cell keeps the toggle
    and the shell button beside them; no other push button
    lives on the page."""
    screen, window, _bridge = make_screen(qt)
    buttons = screen.findChildren(qt.QtWidgets.QPushButton)
    assert [button.text() for button in buttons] == [
        "Add",
        "Edit",
        "Remove",
    ]
    # The row (#1011) sits at the page's bottom: the table
    # above it, the detail strip below it.
    layout = screen.layout()
    assert layout.itemAt(1).widget() is screen.table
    row = layout.itemAt(2)
    assert row.itemAt(0).widget() is screen.add_button
    assert row.itemAt(1).widget() is screen.edit_button
    assert row.itemAt(2).widget() is screen.remove_button
    assert layout.itemAt(3).widget() is screen.detail
    for workspace in ("ws-1", "ws-2", "ws-3"):
        cell = screen.table.cellWidget(
            screen.row_ids()[workspace], qt.cells_mod.ACTION_COLUMN
        )
        cell_buttons = cell.findChildren(qt.QtWidgets.QToolButton)
        assert len(cell_buttons) == 2  # the toggle and the shell
    window.close()


def test_add_asks_the_shell_for_the_create_dialog(qt) -> None:
    """The Add action (#1011): the click crosses to the shell
    that owns the bridge — the same create dialog the tab
    strip's plus badge once opened — the listing carrying no
    dialog of its own."""
    screen, window, _bridge = make_screen(qt)
    asked: list[bool] = []
    screen.create_requested.connect(lambda: asked.append(True))
    screen.add_button.click()
    assert asked == [True]
    window.close()


def test_edit_and_remove_act_on_the_selected_row(qt, monkeypatch) -> None:
    """The row's Edit and Remove (#1011): both act on the held
    row — the editor opens over its own facts, Remove asks
    first and a refusal sends nothing — and both stand disabled
    until a row holds. The flight's gate holds them besides."""
    monkeypatch.setattr(qt.screen_mod, "confirm", lambda *args: False)
    screen, window, bridge = make_screen(qt)
    opened, _accepted, _parents = fake_editor(monkeypatch, qt)
    # No hold: the row the open's first answer held clears, and
    # the buttons stand disabled until a row holds again.
    screen.table.clearSelection()
    screen.table.selectionModel().clearCurrentIndex()
    assert not screen.edit_button.isEnabled()
    assert not screen.remove_button.isEnabled()
    # The slots behind the buttons decide nothing with no hold:
    # a disabled button's click never reaches them, and a direct
    # call answers the same silence.
    bridge.calls.clear()
    screen.edit_held()
    screen.remove_held()
    assert opened == []
    assert bridge.calls == []
    row = screen.row_ids()["ws-1"]
    screen.table.selectRow(row)
    assert screen.edit_button.isEnabled()
    assert screen.remove_button.isEnabled()
    bridge.calls.clear()
    screen.edit_button.click()
    assert opened == [("ws-1",)]
    screen.remove_button.click()
    assert bridge.calls == []  # the refusal sent nothing
    window.close()


def test_the_action_row_follows_the_flight(qt, monkeypatch) -> None:
    """The row's gate (#1011): a start/stop/delete flight out
    holds Edit and Remove beside the row toggles, and the
    landing frees them again."""
    monkeypatch.setattr(qt.screen_mod, "confirm", lambda *args: True)
    screen, window, bridge = make_screen(qt)
    row = screen.row_ids()["ws-2"]
    screen.table.selectRow(row)
    bridge.calls.clear()
    screen.remove_button.click()
    assert bridge.calls == [("workspaces-delete", "remove", ("ws-2",))]
    assert not screen.edit_button.isEnabled()
    assert not screen.remove_button.isEnabled()
    # Add stands: the create dialog's flight is its own, not
    # the row actions'.
    assert screen.add_button.isEnabled()
    screen.on_succeeded("workspaces-delete", {"id": "ws-2", "status": "gone"})
    assert screen.edit_button.isEnabled()
    assert screen.remove_button.isEnabled()
    window.close()


def test_start_runs_on_the_row_and_refreshes(qt) -> None:
    """The Start action (#940, #972): the stopped row's own
    toggle starts that workspace through the bridge, the
    toggles lock while the flight is out, the outcome the
    daemon's answer carries flashes at once (the TUI's own #776
    mask), and the list refreshes when the daemon answers."""
    screen, window, bridge = make_screen(qt)
    bridge.calls.clear()
    assert screen.row_actions["ws-1"].isEnabled()
    screen.row_actions["ws-1"].click()
    assert bridge.calls == [("workspaces-start", "start", ("ws-1",))]
    assert not screen.row_actions["ws-2"].isEnabled()  # one flight
    screen.on_succeeded(
        "workspaces-start", {"id": "ws-1", "status": "running"}
    )
    assert screen.status.text() == "alpha running"  # the outcome
    assert bridge.calls[-1] == ("workspaces-list", "workspaces", ())
    assert screen.row_actions["ws-1"].isEnabled()
    window.close()


def test_the_toggle_reads_the_rows_own_action(qt) -> None:
    """The toggle's icon and tooltip read the row's state (#972):
    the play gesture while the workspace stands stopped or only
    created, the stop sign otherwise — the presenter's own
    choice, rendered."""
    screen, window, _bridge = make_screen(qt)
    stop = screen.style().standardIcon(
        qt.QtWidgets.QStyle.StandardPixmap.SP_MediaStop
    )
    # ws-1 stopped: the play gesture, not the stop sign;
    # ws-3 starting: the stop sign itself — pinned by the
    # rendered pixels, the icon's own equality runs by cache
    # key and two standard icons never share one.
    assert screen.row_actions["ws-1"].icon().cacheKey() != stop.cacheKey()
    stop_png = stop.pixmap(16).toImage()
    assert screen.row_actions["ws-3"].icon().pixmap(16).toImage() == stop_png
    assert screen.row_actions["ws-1"].toolTip() == "Start"
    assert screen.row_actions["ws-3"].toolTip() == "Stop"
    # A created row reads as start, the presenter's other arm.
    screen.on_succeeded(
        "workspaces-list",
        [
            {
                "id": "ws-4",
                "name": "new",
                "status": "created",
                "created_at": "2026-01-04T11:00:00",
                "mount_issues": [],
            }
        ],
    )
    assert screen.row_actions["ws-4"].toolTip() == "Start"
    window.close()


def test_an_action_landing_over_a_listing_flight(qt) -> None:
    """The interleaving (#940): a tick's listing flight is out
    when the action's answer lands. The action flashes its own
    outcome at once; the swallowed refresh is served by the
    in-flight listing's answer; the listing's own pending flag
    survives untouched, so the next tick runs.
    An answer without a readable status still names its
    workspace."""
    screen, window, bridge = make_screen(qt)
    screen.refresh()  # the listing flight opens
    bridge.calls.clear()
    screen.row_actions["ws-1"].click()
    assert bridge.calls == [("workspaces-start", "start", ("ws-1",))]
    screen.on_succeeded(
        "workspaces-start", {"id": "ws-1", "status": "running"}
    )
    assert screen.status.text() == "alpha running"  # shown at once
    assert bridge.calls == [("workspaces-start", "start", ("ws-1",))]
    # no new listing went out — the in-flight one serves it
    assert screen.pending  # and its flag stands
    assert screen.row_actions["ws-1"].isEnabled()
    # The unreadable-answer arcs still name the workspace —
    # each on a flight of its own, the target on it.
    screen.row_actions["ws-1"].click()
    screen.on_succeeded("workspaces-start", {"no": "status"})
    assert screen.status.text() == "alpha"
    screen.row_actions["ws-1"].click()
    screen.on_succeeded("workspaces-start", None)
    assert screen.status.text() == "alpha"
    window.close()


def test_a_failed_action_over_a_listing_flight(qt) -> None:
    """The failed interleaving (#940): a listing flight is out
    when the action refuses. The failure flashes, the rows
    stand, the buttons take clicks again, and the listing's own
    pending flag survives untouched."""
    screen, window, bridge = make_screen(qt)
    screen.refresh()  # the listing flight opens
    bridge.calls.clear()
    screen.row_actions["ws-1"].click()
    screen.on_failed(
        "workspaces-start",
        clientdata.RestFailure("409: workspace is running", reach=False),
    )
    assert "409" in screen.status.text()
    assert screen.table.rowCount() == 3  # the rows stand
    assert screen.pending  # the listing flight still holds
    assert screen.row_actions["ws-1"].isEnabled()
    window.close()


def test_stop_asks_before_it_runs(qt, monkeypatch) -> None:
    """The Stop action's gate (#940): the question names the
    workspace, and a refusal sends nothing to the daemon — a
    confirmation runs the stop, and the list refreshes when it
    lands."""
    asked: list[tuple] = []

    def refuse_or_allow(parent, title, text):
        asked.append((title, text))
        return len(asked) > 1

    monkeypatch.setattr(qt.screen_mod, "confirm", refuse_or_allow)
    screen, window, bridge = make_screen(qt)
    bridge.calls.clear()
    # The running row's toggle reads stop: the confirmation
    # gate stands before the flight.
    screen.row_actions["ws-2"].click()
    assert bridge.calls == []  # the refusal sent nothing
    assert screen.row_actions["ws-2"].isEnabled()
    screen.row_actions["ws-2"].click()
    assert bridge.calls == [("workspaces-stop", "stop", ("ws-2",))]
    assert "beta" in asked[1][1]  # the question named the workspace
    screen.on_succeeded("workspaces-stop", {})
    assert bridge.calls[-1] == ("workspaces-list", "workspaces", ())
    window.close()


def test_a_failing_action_flashes_and_the_list_survives(qt) -> None:
    """A refused action (#940): the failure flashes the status
    line a refused refresh uses, the rows already on screen
    stand, and the buttons take clicks again."""
    screen, window, _bridge = make_screen(qt)
    screen.on_failed(
        "workspaces-start",
        clientdata.RestFailure("409: workspace is running", reach=False),
    )
    assert "409" in screen.status.text()
    assert screen.table.rowCount() == 3  # the rows stand
    assert screen.row_actions["ws-1"].isEnabled()
    assert screen.row_actions["ws-2"].isEnabled()
    window.close()


def test_the_row_buttons_follow_the_listing(qt) -> None:
    """The row buttons sit on their rows (#972): a listing that
    drops the workspace drops its buttons with it, and the rows
    that stand keep theirs."""
    bridge = StubBridge()
    screen = qt.screen_mod.WorkspaceList(bridge)
    window = qt.QtWidgets.QMainWindow()
    window.setCentralWidget(screen)
    window.show()
    screen.on_succeeded("workspaces-list", daemon_rows())
    assert set(screen.row_actions) == {"ws-1", "ws-2", "ws-3"}
    # The listing drops beta: its button goes with its row.
    dropped = [daemon_rows()[0], daemon_rows()[2]]
    screen.on_succeeded("workspaces-list", dropped)
    assert set(screen.row_actions) == {"ws-1", "ws-3"}
    window.close()


def test_the_action_slots_stand_guard(qt) -> None:
    """The action paths guard their own gate (#940, #972): a
    toggle whose model left the listing sends nothing, a second
    action while a flight is out sends nothing — the buttons
    carry the gates for clicks, the slots for whatever calls
    them, the context menu included."""
    screen, window, bridge = make_screen(qt)
    screen.table.clearSelection()
    screen.table.selectionModel().clearCurrentIndex()
    bridge.calls.clear()
    screen.row_actions["ws-1"].click()  # the flight opens
    bridge.calls.clear()
    screen.row_actions["ws-2"].click()  # the flight holds the gate
    screen.row_actions["ws-1"].click()
    screen.start_workspace(("ws-1", "alpha"))  # the slots carry it too
    screen.stop_workspace(("ws-2", "beta"))
    screen.toggle_workspace(("ws-9", "gone"))  # no model stands
    screen.delete_workspace(("ws-9", "gone"))
    assert bridge.calls == []
    # A held row whose name item is gone holds nothing.
    screen.on_succeeded("workspaces-start", {})  # land the flight
    select_cell(qt, screen, screen.row_ids()["ws-1"], 0)  # hold it
    screen.table.setItem(
        screen.row_ids()["ws-1"], qt.cells_mod.NAME_COLUMN, None
    )
    assert screen.held_workspace() is None
    # A row the rebuild has not filled measures nothing; an
    # empty table sets no width at all.
    screen.table.setRowCount(4)  # rows without cells, mid-rebuild
    listlayout.size_action_column(screen.table)
    screen.on_succeeded("workspaces-list", [])
    window.resize(901, 600)
    qt.app.processEvents()
    window.close()


def test_the_action_column_sorts_nothing(qt) -> None:
    """The row-actions column (#972): last, with no header
    label, and a real click on its header — the press the
    header's viewport delivers — moves no ordering, while a
    real click on a sortable column still sorts."""
    from PySide6 import QtTest

    screen, window, _bridge = make_screen(qt)
    table = screen.table
    header = table.horizontalHeader()
    assert table.columnCount() == len(qt.cells_mod.COLUMNS) + 1
    assert table.horizontalHeaderItem(qt.cells_mod.ACTION_COLUMN).text() == (
        ""
    )
    before = [
        table.item(row, qt.cells_mod.NAME_COLUMN).text()
        for row in range(table.rowCount())
    ]

    # The real gesture: a click delivered to the header's
    # viewport, the surface the platform serves — at the
    # section's center, clear of the resize grips a boundary
    # hugging press would grab instead.
    def section_center(column: int) -> qt.QtCore.QPoint:
        return qt.QtCore.QPoint(
            header.sectionViewportPosition(column)
            + header.sectionSize(column) // 2,
            header.height() // 2,
        )

    QtTest.QTest.mouseClick(
        header.viewport(),
        qt.QtCore.Qt.MouseButton.LeftButton,
        pos=section_center(qt.cells_mod.ACTION_COLUMN),
    )
    assert header.sortIndicatorSection() == qt.cells_mod.CREATED_COLUMN
    after = [
        table.item(row, qt.cells_mod.NAME_COLUMN).text()
        for row in range(table.rowCount())
    ]
    assert after == before  # the standing order stands
    # The positive control: a real click on a sortable header
    # sorts by it.
    QtTest.QTest.mouseClick(
        header.viewport(),
        qt.QtCore.Qt.MouseButton.LeftButton,
        pos=section_center(qt.cells_mod.NAME_COLUMN),
    )
    assert header.sortIndicatorSection() == qt.cells_mod.NAME_COLUMN
    labels = [
        table.item(row, qt.cells_mod.NAME_COLUMN).text()
        for row in range(table.rowCount())
    ]
    assert labels == sorted(labels)
    window.close()


def test_a_rebuild_under_a_flight_keeps_the_gate(qt) -> None:
    """A changed listing landing under a standing action flight
    (#972): the rebuilt cells take the gate with them — every
    row's toggle stands disabled until the flight lands."""
    screen, window, bridge = make_screen(qt)
    screen.table.clearSelection()  # no hold: the rebuild below
    # fires no selection change, so the cells' own gate stands
    # alone against the flight
    screen.row_actions["ws-1"].click()  # the start flight opens
    assert not screen.row_actions["ws-2"].isEnabled()
    # A changed listing (a renamed row) rebuilds the cells.
    renamed = daemon_rows()
    renamed[0]["name"] = "alpha-renamed"
    screen.on_succeeded("workspaces-list", renamed)
    assert (
        screen.table.item(
            screen.row_ids()["ws-1"], qt.cells_mod.NAME_COLUMN
        ).text()
        == "alpha-renamed"
    )
    for toggle in screen.row_actions.values():
        assert not toggle.isEnabled()  # the gate survived the rebuild
    screen.on_succeeded(
        "workspaces-start", {"id": "ws-1", "status": "running"}
    )
    for toggle in screen.row_actions.values():
        assert toggle.isEnabled()  # the flight landed, the gate lifts
    window.close()


def test_the_row_shell_button_opens_the_workspace(qt) -> None:
    """The shell button beside the toggle (#972): a click opens
    its own workspace's detail page — the shell tab flow (#954),
    through the signal the screens compose by."""
    screen, window, _bridge = make_screen(qt)
    seen: list[str] = []
    screen.shell_requested.connect(seen.append)
    row = screen.row_ids()["ws-2"]
    cell = screen.table.cellWidget(row, qt.cells_mod.ACTION_COLUMN)
    buttons = cell.findChildren(qt.QtWidgets.QToolButton)
    shell = next(b for b in buttons if b.toolTip() == "Open a shell")
    shell.click()
    assert seen == ["ws-2"]
    window.close()


def test_the_context_menu_carries_the_rows_actions(qt, monkeypatch) -> None:
    """A right-click on a row opens its menu (#972, #986):
    Edit — the editor over the row's own facts — start or stop
    per the row's own action, a shell, and Delete — the same
    paths the row's buttons take. A right-click on no row opens
    nothing."""
    screen, window, bridge = make_screen(qt)
    monkeypatch.setattr(qt.screen_mod, "confirm", lambda *args: False)
    edits = fake_editor(monkeypatch, qt)[0]
    # The show is non-modal: the request itself pops the menu,
    # and a press below every row opens nothing.
    assert screen.row_menu(-1) is None
    screen.menu_requested(
        qt.QtCore.QPoint(2, screen.table.viewport().height() - 2)
    )  # below every row: nothing pops
    row = screen.row_ids()["ws-1"]
    pos = qt.QtCore.QPoint(2, screen.table.rowViewportPosition(row) + 2)
    screen.menu_requested(pos)
    menu = screen.findChildren(qt.QtWidgets.QMenu)[-1]
    labels = [action.text() for action in menu.actions()]
    assert labels == ["Edit", "Start", "Open a shell", "Delete"]
    bridge.calls.clear()
    seen: list[str] = []
    screen.shell_requested.connect(seen.append)
    for action in menu.actions():
        action.trigger()
    assert edits == [("ws-1",)]  # the editor opened this row
    assert bridge.calls == [("workspaces-start", "start", ("ws-1",))]
    assert seen == ["ws-1"]  # the shell entry ran its path
    # Delete's entry runs behind its confirmation (below).
    window.close()


def fake_editor(monkeypatch, qt) -> tuple[list, list]:
    """The editor's stub (#986): the constructor's row recorded,
    the accepted signal's connect surface with it, and an exec
    that returns at once — the stub never enters a loop. A
    landed edit refreshes the listing through the connected
    slot, exactly the create dialog's shape."""
    opened: list[tuple] = []
    accepted: list[object] = []
    parents: list[object] = []

    class FakeDialog:
        def __init__(self, bridge: object, row: dict, parent=None) -> None:
            opened.append((row["id"],))
            parents.append(parent)

    class FakeAccepted:
        def connect(self, slot: object) -> None:
            accepted.append(slot)

    FakeDialog.accepted = FakeAccepted()  # type: ignore[attr-defined]
    FakeDialog.exec = lambda self: 0  # type: ignore[method-assign]
    monkeypatch.setattr(qt.screen_mod, "EditDialog", FakeDialog)
    return opened, accepted, parents


def test_edit_opens_the_editor_over_the_rows_own_facts(
    qt, monkeypatch
) -> None:
    """The Edit action (#986): the editor opens over the bridge
    it shares with the list, seeded from the daemon row the last
    listing stood beside the models — the whole facts the sizes
    read — and a landed resize refreshes the listing behind it.
    A workspace the last answer dropped opens nothing."""
    screen, window, bridge = make_screen(qt)
    opened, accepted, parents = fake_editor(monkeypatch, qt)
    screen.edit_workspace(("ws-1", "alpha"))
    assert opened == [("ws-1",)]
    assert accepted == [screen.refresh]
    assert parents == [screen]  # the editor parents to the list (#1014)
    # A workspace the last answer dropped names itself on the
    # status line: the editor seeds from the row's own facts or
    # not at all.
    opened.clear()
    screen.daemon_rows.pop("ws-1")
    screen.edit_workspace(("ws-1", "alpha"))
    assert opened == []
    assert screen.status.text() == (
        "alpha left the listing — refresh and retry the edit"
    )
    window.close()


def test_a_row_with_deferred_edits_marks_the_restart(qt) -> None:
    """The deferred-edit row (#986): a sizes edit no boot has
    taken leads the status word with the ↻N marker, the cell's
    tooltip names the restart in the words the strip reads, and
    the strip states it in words — the marker stands until a
    start takes the change and the listing agrees again."""
    screen, window, bridge = make_screen(qt)
    rows = daemon_rows()
    rows[0]["sizes_pending"] = True
    screen.on_succeeded("workspaces-list", rows)
    row = screen.row_ids()["ws-1"]
    status = screen.table.item(row, 1)
    assert status.text() == "↻1 stopped"
    assert status.toolTip() == "1 change applies at the next start"
    assert "1 change applies at the next start" in screen.detail.text()
    # A start that takes the change clears the marker: the
    # listing and the config agree again.
    rows[0]["sizes_pending"] = False
    screen.on_succeeded("workspaces-list", rows)
    status = screen.table.item(screen.row_ids()["ws-1"], 1)
    assert status.text() == "stopped"
    assert status.toolTip() == ""
    window.close()


def test_the_context_menus_stop_confirms(qt, monkeypatch) -> None:
    """The running row's menu reads Stop and asks first (#972):
    the confirmation gate the toggle keeps."""
    screen, window, bridge = make_screen(qt)
    asked: list[tuple] = []

    def refuse(parent, title, text):
        asked.append((title, text))
        return False

    monkeypatch.setattr(qt.screen_mod, "confirm", refuse)
    menu = screen.row_menu(screen.row_ids()["ws-2"])
    assert [action.text() for action in menu.actions()][:2] == [
        "Edit",
        "Stop",
    ]
    bridge.calls.clear()
    menu.actions()[1].trigger()
    assert bridge.calls == []  # the refusal sent nothing
    assert asked[0][0] == "Stop workspace"
    window.close()


def test_delete_asks_and_removes(qt, monkeypatch) -> None:
    """The Delete action (#972): the question names the workspace
    and its cost; a confirmation removes the workspace through
    the bridge, the outcome flashes, and the list refreshes
    when the daemon answers."""
    asked: list[tuple] = []

    def refuse_or_allow(parent, title, text):
        asked.append((title, text))
        return len(asked) > 1

    monkeypatch.setattr(qt.screen_mod, "confirm", refuse_or_allow)
    screen, window, bridge = make_screen(qt)
    bridge.calls.clear()
    screen.delete_workspace(("ws-2", "beta"))
    assert bridge.calls == []  # the refusal sent nothing
    assert screen.row_actions["ws-2"].isEnabled()
    screen.delete_workspace(("ws-2", "beta"))
    assert bridge.calls == [("workspaces-delete", "remove", ("ws-2",))]
    assert "beta" in asked[1][1] and "data" in asked[1][1]
    assert not screen.row_actions["ws-1"].isEnabled()  # one flight
    screen.on_succeeded("workspaces-delete", {"status": "gone"})
    assert screen.status.text() == "beta gone"
    assert bridge.calls[-1] == ("workspaces-list", "workspaces", ())
    assert screen.row_actions["ws-1"].isEnabled()
    window.close()


def test_the_detail_strip_follows_the_hold(qt) -> None:
    """The detail strip (#849's facts, #940's GUI read): the held
    row's identity, posture, and costs on one ``·``-separated
    line — the markers beside the facts, an honest dash for a
    row that predates a field, storage a dash until the capacity
    report lands — and the text selectable, so an id or a digest
    copies out. A moved hold moves the strip; no hold blanks
    it."""
    screen, window, bridge = make_screen(qt)
    # The strip's text reads with the mouse: selectable.
    flags = screen.detail.textInteractionFlags()
    assert flags & qt.QtCore.Qt.TextInteractionFlag.TextSelectableByMouse
    # make_screen's first-row hold: beta, the mounts marker, the
    # pending count, no storage yet.
    line = screen.detail.text()
    assert line.startswith("beta  ·  id ws-2  ·  image ")
    for fact in ("cpu 4", "mem 16G", "egress allow"):
        assert fact in line
    assert "mounts ✗1" in line and "1 change applies at the next start" in line
    assert "storage -" in line
    # The listing's answer fetched the capacity report beside
    # the rows; its answer fills the figure.
    assert ("workspaces-storage", "storage", ()) in bridge.calls
    screen.on_succeeded(
        "workspaces-storage",
        {
            "workspaces": [
                {"id": "ws-2", "root_bytes": 10737418240, "home_bytes": 1}
            ]
        },
    )
    assert "storage 10G" in screen.detail.text()
    # A moved hold moves the strip; a row that predates fields
    # reads its dashes.
    select_cell(qt, screen, 2, 0)  # ws-3, the oldest
    line = screen.detail.text()
    assert line.startswith("ws-3  ·  id ws-3  ·  image -")
    for fact in ("cpu -", "mem -", "egress -", "storage -"):
        assert fact in line
    # No hold blanks the strip.
    screen.table.clearSelection()
    screen.table.selectionModel().clearCurrentIndex()
    assert screen.detail.text() == ""
    window.close()


def test_the_storage_cache_keeps_standing_figures(qt) -> None:
    """The strip's storage cache (#849): a refused report keeps
    the standing figures in silence — the strip is a detail
    beside a listing that reports its own failures — and a
    report that predates the per-row fields keeps them too; a
    later listing refetches beside the rows."""
    screen, window, bridge = make_screen(qt)
    screen.on_succeeded(
        "workspaces-storage",
        {
            "workspaces": [
                {"id": "ws-2", "root_bytes": 2147483648, "home_bytes": 0}
            ]
        },
    )
    assert "storage 2G" in screen.detail.text()
    screen.on_failed(
        "workspaces-storage",
        clientdata.RestFailure("daemon away", reach=True),
    )
    assert "storage 2G" in screen.detail.text()
    assert screen.status.text() == ""  # the silence
    # Entries that predate the per-row fields — no bytes, or
    # no id — read as nothing; the good entry lands.
    screen.on_succeeded(
        "workspaces-storage",
        {
            "workspaces": [
                {"id": "ws-9"},
                {"root_bytes": 1, "home_bytes": 1},
                {"id": "ws-2", "root_bytes": 1073741824, "home_bytes": 1},
            ]
        },
    )
    assert "storage 1G" in screen.detail.text()
    screen.on_succeeded("workspaces-storage", {"workspaces": []})
    assert "storage -" in screen.detail.text()
    # The next listing answer takes another storage flight out.
    bridge.calls.clear()
    screen.on_succeeded("workspaces-list", daemon_rows())
    assert ("workspaces-storage", "storage", ()) in bridge.calls
    window.close()


def test_a_posture_change_alone_refreshes_the_strip(qt) -> None:
    """The unchanged-listing skip spares the table, not the
    strip (#940): an answer whose five displayed cells read the
    same but whose posture facts changed — a cpu edit landed —
    still refreshes the model map and the strip. The strip
    cannot lie about facts the columns never show."""
    screen, window, _bridge = make_screen(qt)
    assert "cpu 4" in screen.detail.text()  # beta's standing fact
    settled = [dict(row) for row in daemon_rows()]
    settled[1]["cpus"] = 8  # the edit landed
    screen.on_succeeded("workspaces-list", settled)
    assert "cpu 8" in screen.detail.text()
    assert screen.table.rowCount() == 3  # the table stood whole
    window.close()


def test_a_stale_listing_answer_keeps_the_outcome_flash(qt) -> None:
    """The very interleaving the outcome flash masks (#940,
    #776): a tick's listing flight is out when the action's
    answer flashes; the stale listing answer that follows — its
    rows matching, so nothing repaints — must not wipe the
    outcome. Only a standing outage clears on an answer."""
    screen, window, bridge = make_screen(qt)
    screen.refresh()  # the listing flight opens
    bridge.calls.clear()
    screen.row_actions["ws-1"].click()
    screen.on_succeeded(
        "workspaces-start", {"id": "ws-1", "status": "running"}
    )
    assert screen.status.text() == "alpha running"
    # The stale listing answer lands: same rows, no rebuild.
    screen.on_succeeded("workspaces-list", daemon_rows())
    assert screen.status.text() == "alpha running"  # it stood
    # A standing outage, though, an answer resolves.
    screen.on_failed(
        "workspaces-list",
        clientdata.RestFailure("daemon away", reach=True),
    )
    screen.on_succeeded("workspaces-list", daemon_rows())
    assert screen.status.text() == ""
    window.close()


def test_the_grid_shows_no_vertical_dividers(qt) -> None:
    """No vertical dividers between cells (#939): the grid goes
    off whole, and the row's own bottom rule — the palette's
    mid tone — stays as the one separator, so the columns read
    by their headers and alignment alone."""
    screen, window, _bridge = make_screen(qt)
    assert not screen.table.showGrid()
    assert "border-bottom" in screen.table.styleSheet()
    window.close()


def test_every_cell_renders_but_never_edits(qt) -> None:
    """The listing is a view over the daemon's state (#939): no
    cell opens an editor — an edit would paint a value only the
    display holds, and the next refresh would throw it away.
    Every cell stays selectable and enabled: the click that
    phase 4's detail page will ride still lands."""
    screen, window, _bridge = make_screen(qt)
    for row in range(screen.table.rowCount()):
        for column in range(len(qt.cells_mod.COLUMNS)):
            flags = screen.table.item(row, column).flags()
            editable = qt.QtCore.Qt.ItemFlag.ItemIsEditable
            selectable = qt.QtCore.Qt.ItemFlag.ItemIsSelectable
            enabled = qt.QtCore.Qt.ItemFlag.ItemIsEnabled
            assert not flags & editable
            assert flags & selectable and flags & enabled
    window.close()


def test_the_status_column_sorts_by_lifecycle_rank(qt) -> None:
    """The rank the presenter carries (#938) is the sort the
    widget serves (#939): a status-column click orders the rows
    by the vocabulary's own sequence — a state outside it last —
    not by the status word's spelling."""
    screen, window, _bridge = make_screen(qt)
    screen.on_succeeded(
        "workspaces-list",
        [
            {"id": "ws-1", "name": "alpha", "status": "stopped"},
            {"id": "ws-2", "name": "beta", "status": "running"},
            {"id": "ws-3", "name": "gamma", "status": "hibernating"},
            {"id": "ws-4", "name": "delta", "status": "created"},
        ],
    )
    screen.table.sortItems(1, qt.QtCore.Qt.SortOrder.AscendingOrder)
    labels = [screen.table.item(row, 0).text() for row in range(4)]
    assert labels == ["delta", "beta", "alpha", "gamma"]
    # The image column sorts by the whole value under its sort
    # role — an ellipsized display would misorder the refs.
    screen.on_succeeded(
        "workspaces-list",
        [
            {
                "id": "ws-1",
                "name": "alpha",
                "status": "running",
                "image_ref": "debian:14",
                "created_at": None,
                "mount_issues": [],
            },
            {
                "id": "ws-2",
                "name": "beta",
                "status": "running",
                "image_ref": "debian:13",
                "created_at": None,
                "mount_issues": [],
            },
        ],
    )
    screen.table.sortItems(3, qt.QtCore.Qt.SortOrder.AscendingOrder)
    labels = [screen.table.item(row, 0).text() for row in range(2)]
    assert labels == ["beta", "alpha"]
    window.close()


def test_a_widened_pane_reflows_the_columns(qt) -> None:
    """The widened screen's columns (#964, the operator's ask):
    the screen's own resize event reads the viewport one layout
    pass behind, so every widen left its last delta blank — a
    maximized window most of all. The pane's own resize re-flows
    the columns: the table consumes the pane's whole width at
    any width over the default, no horizontal scrollbar."""
    screen, window, _bridge = make_screen(qt)
    for width in (2000, 1700, 1450):
        window.resize(width, 700)
        qt.app.processEvents()
        table = screen.table
        total = sum(
            table.columnWidth(column)
            for column in range(len(qt.cells_mod.COLUMNS))
        ) + table.columnWidth(qt.cells_mod.ACTION_COLUMN)
        assert total >= table.viewport().width() - 8  # the pane filled
        assert table.horizontalScrollBar().maximum() == 0
    window.close()


def test_the_picker_rides_the_status_row(qt) -> None:
    """The connection picker's home (#1009): a tool button in
    the listing's status row, beside the flash line — never a
    pane of its own — its menu built from the presenter's
    preset rows, the standing one checked, the button's label
    naming it."""
    from msks.client.gui.connection import (
        ALIAS,
        DEV,
        ENVIRONMENT,
        ConnectionPreset,
    )

    screen, window, bridge = make_screen(qt)
    assert screen.connection.parent() is screen
    assert screen.connection.text() == "connection"
    screen.host_presets(
        [
            ConnectionPreset("farm", ALIAS, "https://10.0.0.9:9660"),
            ConnectionPreset(
                "dev daemon",
                DEV,
                "https://127.0.0.1:9661",
                active=True,
            ),
            ConnectionPreset("environment", ENVIRONMENT, "the shell"),
        ]
    )
    assert screen.connection.text() == "dev daemon"
    menu = screen.connection.menu()
    assert [
        (action.text(), action.isChecked()) for action in menu.actions()
    ] == [
        ("farm — https://10.0.0.9:9660", False),
        ("dev daemon — https://127.0.0.1:9661", True),
        ("environment — the shell", False),
    ]
    # A re-host over a fresh pick marks the new standing row and
    # re-labels the button.
    screen.host_presets(
        [
            ConnectionPreset(
                "farm", ALIAS, "https://10.0.0.9:9660", active=True
            ),
            ConnectionPreset("dev daemon", DEV, "https://127.0.0.1:9661"),
        ]
    )
    assert screen.connection.text() == "farm"
    assert [a.isChecked() for a in screen.connection.menu().actions()] == [
        True,
        False,
    ]


def test_a_picker_menu_pick_crosses_as_the_signal(qt) -> None:
    """The pick's forward (#1009): a menu action's trigger emits
    the preset it was built from — the screen holds no
    connection state of its own, the window path owns the
    re-dial."""
    from msks.client.gui.connection import ALIAS, ConnectionPreset

    screen, window, bridge = make_screen(qt)
    preset = ConnectionPreset("farm", ALIAS, "https://10.0.0.9:9660")
    screen.host_presets([preset])
    landed: list[ConnectionPreset] = []
    screen.connection_picked.connect(landed.append)
    screen.connection.menu().actions()[0].trigger()
    assert landed == [preset]


def test_a_rehosted_picker_releases_its_replaced_menu(qt) -> None:
    """The menu's lifetime (#1009): a re-host builds a fresh
    menu and the replaced one leaves with its actions — the
    button's children do not pile a menu per pick across a
    window's life."""
    import shiboken6
    from msks.client.gui.connection import ALIAS, ConnectionPreset

    screen, window, bridge = make_screen(qt)
    screen.host_presets(
        [ConnectionPreset("farm", ALIAS, "https://10.0.0.9:9660")]
    )
    gone = screen.connection.menu()
    screen.host_presets(
        [ConnectionPreset("farm", ALIAS, "https://10.0.0.9:9660", active=True)]
    )
    qt.app.processEvents()
    qt.QtCore.QCoreApplication.sendPostedEvents(
        None, qt.QtCore.QEvent.Type.DeferredDelete
    )
    qt.app.processEvents()
    assert not shiboken6.isValid(gone)
    assert screen.connection.menu() is not gone
