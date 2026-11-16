"""The workspace list screen (#939): the thin widget over the
presenter's row models — the TUI listing's five fields, NAME,
STATUS with the unresolved-mounts marker, ID, IMAGE, CREATED —
refreshed on a timer and a manual action through the bridge,
with a failed refresh flashing the status line while the rows
stand. The widget runs under the offscreen platform on every
host (#939); the screen's slots are driven directly and through
the stub bridge's signal emits, the bridge's own tests holding
the real worker's contract.
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
        "the gui extra is not installed on this host",
        allow_module_level=True,
    )

from msks.client import ages as ages_mod  # noqa: E402
from msks.client import data as clientdata  # noqa: E402
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
    from msks.client.gui import workspaces as workspaces_mod
    from PySide6 import QtCore, QtGui, QtWidgets

    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication(
        ["msks-test"]
    )
    return SimpleNamespace(
        app=app,
        QtCore=QtCore,
        QtGui=QtGui,
        QtWidgets=QtWidgets,
        screen_mod=workspaces_mod,
    )


def make_screen(qt) -> tuple:
    """The screen over the stub bridge, shown in a real window —
    the shape every test reads: open, first refresh issued (and
    answered, so later refreshes run), tick running."""
    bridge = StubBridge()
    screen = qt.screen_mod.WorkspaceList(bridge)
    window = qt.QtWidgets.QMainWindow()
    window.setCentralWidget(screen)
    window.show()
    screen.on_succeeded("workspaces-list", daemon_rows())
    return screen, window, bridge


def test_the_open_screen_issues_its_first_refresh(qt) -> None:
    """The window opens on the list (#939): showing the screen
    issues the first refresh through the bridge and starts the
    standing tick at the listing's cadence — the TUI's own five
    seconds (#470 L2)."""
    bridge = StubBridge()
    screen = qt.screen_mod.WorkspaceList(bridge)
    window = qt.QtWidgets.QMainWindow()
    window.setCentralWidget(screen)
    assert bridge.calls == []  # construction alone asks nothing
    window.show()
    assert bridge.calls == [("workspaces-list", "workspaces", ())]
    assert screen.timer.isActive()
    assert screen.timer.interval() == qt.screen_mod.REFRESH_MS
    window.close()


def test_the_window_opens_on_the_standing_sort(qt) -> None:
    """The default sort arrow shows at first render (#939): the
    CREATED header carries the descending arrow, and the rows
    open in that order — newest first, the TUI's own standing
    sort (#470 L1)."""
    screen, window, _bridge = make_screen(qt)
    header = screen.table.horizontalHeader()
    assert header.isSortIndicatorShown()
    assert header.sortIndicatorSection() == qt.screen_mod.CREATED_COLUMN
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
    assert screen.table.item(0, 1).text() == "✗1 running"
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
    rank = qt.screen_mod.RANK_ROLE
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
    screen.refresh_button.click()
    assert bridge.calls == [("workspaces-list", "workspaces", ())]
    screen.on_succeeded("workspaces-list", daemon_rows()[:1])
    bridge.calls.clear()
    screen.refresh()
    assert bridge.calls == [("workspaces-list", "workspaces", ())]
    screen.refresh_button.click()
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


def test_the_manual_action_refreshes_through_the_bridge(qt) -> None:
    """The manual action (#939): the button's click and the
    timer's tick share the one refresh verb, both through the
    bridge."""
    screen, window, bridge = make_screen(qt)
    bridge.calls.clear()  # the open refresh is already answered
    screen.refresh_button.click()
    assert bridge.calls == [("workspaces-list", "workspaces", ())]
    screen.on_succeeded("workspaces-list", [])  # retire the flight
    bridge.calls.clear()
    # A one-millisecond tick: the pump spins Qt's queue until
    # the standing timer's refresh lands — the wiring the click
    # alone cannot pin.
    screen.timer.setInterval(1)
    deadline = time.monotonic() + 10.0
    while time.monotonic() < deadline and not bridge.calls:
        qt.app.processEvents()
        time.sleep(TICK)
    assert bridge.calls == [("workspaces-list", "workspaces", ())]
    window.close()


def test_the_tick_stops_with_the_closed_window(qt) -> None:
    """A timer tick must not touch widgets after close (#939):
    closing the window hides the screen and its hide hook stops
    the tick; re-showing resumes it with a fresh refresh."""
    screen, window, bridge = make_screen(qt)
    assert screen.timer.isActive()
    window.close()
    assert not screen.timer.isActive()
    bridge.calls.clear()
    window.show()
    assert screen.timer.isActive()
    assert bridge.calls == [("workspaces-list", "workspaces", ())]
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
    for column, label in enumerate(qt.screen_mod.COLUMNS):
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
    window.resize(600, 700)  # the hash row alone overflows this
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
        for column in range(len(qt.screen_mod.COLUMNS))
    )
    assert total == screen.table.viewport().width()
    window.resize(1500, 700)
    qt.app.processEvents()
    total = sum(
        screen.table.columnWidth(column)
        for column in range(len(qt.screen_mod.COLUMNS))
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
        model.index(row, len(qt.screen_mod.COLUMNS) - 1),
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
    assert len(screen.table.selectedIndexes()) == len(
        qt.screen_mod.COLUMNS
    )  # the whole row still holds
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
    assert len(screen.table.selectedIndexes()) == len(qt.screen_mod.COLUMNS)
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
    screen.table.setItem(0, qt.screen_mod.IMAGE_COLUMN, None)
    assert not screen.rows_unchanged(models)  # a missing cell
    select_cell(qt, screen, 1, 1)
    screen.table.setItem(1, qt.screen_mod.NAME_COLUMN, None)
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
    assert len(screen.table.selectedIndexes()) == len(
        qt.screen_mod.COLUMNS
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


def test_start_runs_on_the_selection_and_refreshes(qt) -> None:
    """The Start action (#940): the held workspace starts through
    the bridge, the buttons lock while the flight is out, the
    outcome the daemon's answer carries flashes at once (the
    TUI's own #776 mask), and the list refreshes when the daemon
    answers."""
    screen, window, bridge = make_screen(qt)
    bridge.calls.clear()
    assert screen.start_button.isEnabled()  # the first-row hold
    screen.start_button.click()
    assert bridge.calls == [("workspaces-start", "start", ("ws-2",))]
    assert not screen.start_button.isEnabled()  # one flight at a time
    screen.on_succeeded(
        "workspaces-start", {"id": "ws-2", "status": "running"}
    )
    assert screen.status.text() == "beta running"  # the outcome
    assert bridge.calls[-1] == ("workspaces-list", "workspaces", ())
    assert screen.start_button.isEnabled()
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
    screen.start_button.click()
    assert bridge.calls == [("workspaces-start", "start", ("ws-2",))]
    screen.on_succeeded(
        "workspaces-start", {"id": "ws-2", "status": "running"}
    )
    assert screen.status.text() == "beta running"  # shown at once
    assert bridge.calls == [("workspaces-start", "start", ("ws-2",))]
    # no new listing went out — the in-flight one serves it
    assert screen.pending  # and its flag stands
    assert screen.start_button.isEnabled()
    # The unreadable-answer arcs still name the workspace —
    # each on a flight of its own, the target on it.
    screen.start_button.click()
    screen.on_succeeded("workspaces-start", {"no": "status"})
    assert screen.status.text() == "beta"
    screen.start_button.click()
    screen.on_succeeded("workspaces-start", None)
    assert screen.status.text() == "beta"
    window.close()


def test_a_failed_action_over_a_listing_flight(qt) -> None:
    """The failed interleaving (#940): a listing flight is out
    when the action refuses. The failure flashes, the rows
    stand, the buttons take clicks again, and the listing's own
    pending flag survives untouched."""
    screen, window, bridge = make_screen(qt)
    screen.refresh()  # the listing flight opens
    bridge.calls.clear()
    screen.start_button.click()
    screen.on_failed(
        "workspaces-start",
        clientdata.RestFailure("409: workspace is running", reach=False),
    )
    assert "409" in screen.status.text()
    assert screen.table.rowCount() == 3  # the rows stand
    assert screen.pending  # the listing flight still holds
    assert screen.start_button.isEnabled()
    window.close()


def test_stop_asks_before_it_runs(qt, monkeypatch) -> None:
    """The Stop action's gate (#940): the question names the
    workspace with No as the default, and a refusal sends
    nothing to the daemon — a confirmation runs the stop, and
    the list refreshes when it lands."""
    asked: list[tuple] = []

    def refuse_or_allow(parent, title, text, buttons=None, default=None):
        asked.append((title, text, buttons, default))
        no = qt.QtWidgets.QMessageBox.StandardButton.No
        yes = qt.QtWidgets.QMessageBox.StandardButton.Yes
        return no if len(asked) == 1 else yes

    monkeypatch.setattr(qt.QtWidgets.QMessageBox, "question", refuse_or_allow)
    screen, window, bridge = make_screen(qt)
    bridge.calls.clear()
    screen.stop_button.click()
    assert bridge.calls == []  # the refusal sent nothing
    assert screen.stop_button.isEnabled()
    screen.stop_button.click()
    assert bridge.calls == [("workspaces-stop", "stop", ("ws-2",))]
    assert "beta" in asked[1][1]  # the question named the workspace
    assert (
        asked[0][3] == qt.QtWidgets.QMessageBox.StandardButton.No
    )  # No is the default
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
    assert screen.start_button.isEnabled()
    assert screen.stop_button.isEnabled()
    window.close()


def test_the_actions_follow_the_hold(qt) -> None:
    """The actions sit on the selection (#940): no rows, no
    buttons; the first-row hold arms them; a listing that drops
    the held workspace disarms them."""
    bridge = StubBridge()
    screen = qt.screen_mod.WorkspaceList(bridge)
    window = qt.QtWidgets.QMainWindow()
    window.setCentralWidget(screen)
    window.show()
    assert not screen.start_button.isEnabled()
    assert not screen.stop_button.isEnabled()
    screen.on_succeeded("workspaces-list", daemon_rows())
    assert screen.start_button.isEnabled()  # the first-row hold
    # The listing drops the held workspace (beta): the hold
    # goes with it.
    dropped = [daemon_rows()[0], daemon_rows()[2]]
    screen.on_succeeded("workspaces-list", dropped)
    assert not screen.start_button.isEnabled()  # the hold went
    assert not screen.stop_button.isEnabled()
    window.close()


def test_the_action_slots_stand_guard(qt) -> None:
    """The action slots guard their own gate (#940): a call with
    no workspace held sends nothing, a second action while a
    flight is out sends nothing, and a hold on a row with no
    name item reads as no hold. The buttons carry the same gates
    for clicks; the slots carry them for whatever calls a slot —
    a keyboard shortcut later, a test today."""
    screen, window, bridge = make_screen(qt)
    screen.table.clearSelection()
    screen.table.selectionModel().clearCurrentIndex()
    bridge.calls.clear()
    screen.start_selected()  # nothing held
    screen.stop_selected()
    assert bridge.calls == []
    select_cell(qt, screen, 0, 1)
    screen.start_button.click()  # the flight opens
    bridge.calls.clear()
    screen.stop_selected()  # the flight holds the gate
    screen.start_selected()
    assert bridge.calls == []
    # A selected row whose name item is gone holds nothing.
    screen.on_succeeded("workspaces-start", {})  # land the flight
    screen.table.setItem(0, qt.screen_mod.NAME_COLUMN, None)
    assert screen.held_workspace() is None
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
    assert "mounts ✗1" in line and "next ↻1" in line
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
    same but whose posture facts changed — a pending change
    landed — still refreshes the model map and the strip. The
    strip cannot lie about facts the columns never show."""
    screen, window, _bridge = make_screen(qt)
    assert "next ↻1" in screen.detail.text()  # beta's wait
    settled = [dict(row) for row in daemon_rows()]
    settled[1]["mounts_pending"] = False  # the wait landed
    screen.on_succeeded("workspaces-list", settled)
    assert "next" not in screen.detail.text()
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
    screen.start_button.click()
    screen.on_succeeded(
        "workspaces-start", {"id": "ws-2", "status": "running"}
    )
    assert screen.status.text() == "beta running"
    # The stale listing answer lands: same rows, no rebuild.
    screen.on_succeeded("workspaces-list", daemon_rows())
    assert screen.status.text() == "beta running"  # it stood
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
        for column in range(len(qt.screen_mod.COLUMNS)):
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
