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
        },
        {
            "id": "ws-2",
            "name": "beta",
            "status": "running",
            "image_ref": None,
            "image_hash": "b" * 64,
            "created_at": "2026-01-04T09:00:00",
            "mount_issues": [{"kind": "skipped"}],
        },
        {
            "id": "ws-3",
            "name": None,
            "status": "starting",
            "image_ref": None,
            "image_hash": None,
            "created_at": None,
            "mount_issues": [],
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
    from PySide6 import QtCore, QtWidgets

    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication(
        ["msks-test"]
    )
    return SimpleNamespace(
        app=app,
        QtCore=QtCore,
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
    labels = [screen.table.item(row, 0).text() for row in range(3)]
    assert labels == ["alpha", "beta", "ws-3"]
    assert screen.table.item(0, 1).text() == "stopped"
    assert screen.table.item(1, 1).text() == "✗1 running"
    # The ID column's cell (#849's vocabulary): the immutable id.
    assert screen.table.item(2, 2).text() == "ws-3"
    # The IMAGE cell: the reference first, the hash when no
    # reference came, ``-`` when neither did.
    assert screen.table.item(0, 3).text() == "debian:13.6"
    assert screen.table.item(1, 3).text() == "b" * 64
    assert screen.table.item(2, 3).text() == "-"
    # The CREATED cell: the shared relative ladder (#350).
    assert screen.table.item(0, 4).text() == "2d ago"
    assert screen.table.item(1, 4).text() == "today"
    assert screen.table.item(2, 4).text() == "-"
    rank = qt.screen_mod.RANK_ROLE
    assert (
        screen.table.item(1, 1).data(rank) == 2  # running's lifecycle rank
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
    screen.on_succeeded("workspaces-list", daemon_rows()[:1])
    assert screen.status.text() == ""  # the answer cleared it
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
    image = screen.table.item(1, 3).text()  # the 64-char hash row
    assert image != "b" * 64
    assert "…" in image
    assert image.startswith("b") and image.endswith("b")
    # Every other cell reads whole.
    assert screen.table.item(1, 0).text() == "beta"
    assert screen.table.item(0, 3).text() == "debian:13.6"
    window.resize(1400, 700)
    qt.app.processEvents()
    assert screen.table.item(1, 3).text() == "b" * 64
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
    assert "…" in screen.table.item(1, 3).text()
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
    select_cell(qt, screen, 1, 1)  # beta's status cell
    rows = daemon_rows()
    screen.on_succeeded(
        "workspaces-list", [rows[1], rows[0], rows[2]]
    )  # beta first now
    current = screen.table.currentIndex()
    assert (current.row(), current.column()) == (0, 1)  # beta moved up
    assert screen.table.item(0, 0).text() == "beta"
    selected_rows = {index.row() for index in screen.table.selectedIndexes()}
    assert selected_rows == {0}  # beta's whole row, wherever it landed
    # beta leaves the listing: the hold goes with it, quietly.
    screen.on_succeeded("workspaces-list", daemon_rows()[:1])
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
