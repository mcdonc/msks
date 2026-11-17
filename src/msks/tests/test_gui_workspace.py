"""The workspace detail screen (#945): the page the list opens
on one row — the presenter's sections as widgets, refreshed on
the listing's own cadence plus a manual action through the
bridge, a failed refresh flashing the status line while the
sections keep their content. The shell swaps the list and the
page. The widgets run under the offscreen platform on every
host (#945); the screens' slots are driven directly, the
bridge's own tests holding the real worker's contract.
"""

import importlib.util
import os
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


@pytest.fixture(autouse=True)
def pinned_clock(monkeypatch: pytest.MonkeyPatch) -> None:
    """The created line reads one fixed clock (the shared leaf's
    seam, #939's fixture shape): the row's Jan 2 stamp reads
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
    """The bridge's surface for the screens' tests: the signals
    the screens and the shell read — the calls' answers, the
    feed's frames and state (#946) — and the one verb they call,
    every call recorded — no worker, no network."""

    def __init__(self) -> None:
        self.calls: list[tuple] = []
        self.succeeded = FakeSignal()
        self.failed = FakeSignal()
        self.landed = FakeSignal()
        self.feed_dropped = FakeSignal()
        self.feed_reconnected = FakeSignal()

    def call(self, key: str, method: str, *args: object) -> None:
        self.calls.append((key, method, args))


def daemon_row() -> dict:
    """The workspace the page reads: two mounts, one the gates
    refused, with the next-start change waiting."""
    return {
        "id": "ws-a",
        "name": "alpha",
        "status": "running",
        "image_ref": "msks/fedora:41",
        "created_at": "2026-01-02T12:00:00",
        "mounts": [
            {"host": "/srv/ok", "guest": "/mnt/ok", "mode": "rw"},
            {"host": "/srv/gone", "guest": "/mnt/gone"},
        ],
        "mount_issues": [
            {
                "host": "/srv/gone",
                "guest": "/mnt/gone",
                "kind": "stopped",
                "detail": "the host path left",
            }
        ],
        "mounts_pending": True,
    }


def other_row() -> dict:
    """A second workspace the listing can carry."""
    return {
        "id": "ws-b",
        "name": "beta",
        "status": "stopped",
        "created_at": "2026-01-03T12:00:00",
    }


@pytest.fixture()
def qt(tmp_path: Path):
    """The offscreen ``QApplication`` the screens need, the
    list's own fixture shape: the silent fontconfig, the
    platform pin, and the Qt names imported inside the fixture
    (a collection-time controller has no business loading the
    binding)."""
    fonts = tmp_path / "fonts.conf"
    fonts.write_text("<fontconfig/>\n")
    os.environ["FONTCONFIG_FILE"] = str(fonts)
    os.environ["QT_QPA_PLATFORM"] = "offscreen"
    from msks.client.gui import workspace as workspace_mod
    from PySide6 import QtCore, QtGui, QtWidgets

    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication(
        ["msks-test"]
    )
    return SimpleNamespace(
        app=app,
        QtCore=QtCore,
        QtGui=QtGui,
        QtWidgets=QtWidgets,
        page_mod=workspace_mod,
    )


def make_shell(qt, rows=None) -> tuple:
    """The shell over the stub bridge in a real window — the
    list shown, its first listing answered (so the rows and the
    model map stand), the page still hidden."""
    bridge = StubBridge()
    shell = qt.page_mod.WorkspaceShell(bridge)
    window = qt.QtWidgets.QMainWindow()
    window.setCentralWidget(shell)
    window.show()
    shell.listing.on_succeeded(
        "workspaces-list", rows if rows is not None else [daemon_row()]
    )
    return shell, window, bridge


def make_page(qt) -> tuple:
    """The page alone over the stub bridge, shown in a real
    window and opened on the workspace — the shape every page
    test reads: first refresh issued, the row and the doc one
    answer away."""
    bridge = StubBridge()
    page = qt.page_mod.WorkspaceDetail(bridge)
    window = qt.QtWidgets.QMainWindow()
    window.setCentralWidget(page)
    window.show()
    page.open("ws-a", "alpha")
    bridge.calls.clear()
    return page, window, bridge


def land(qt, page, bridge, row=None, doc=None) -> None:
    """The page's two answers landed: the row (this workspace's,
    unless a listing is handed in) and the health doc."""
    bridge.calls.clear()
    page.on_succeeded(
        qt.page_mod.ROW_KEY,
        [daemon_row() if row is None else row],
    )
    page.on_succeeded(
        qt.page_mod.HEALTH_KEY,
        {"status": "ok", "version": "0.9.2", "machine": "x86_64"},
    )
    bridge.calls.clear()


def test_opening_a_row_shows_the_page(qt) -> None:
    """The list's opened row swaps to the page (#945): the
    title reads the label the list holds, and the page's first
    refresh goes out — the row read and the health read, the
    same seam the TUI's workspace page reads."""
    shell, window, bridge = make_shell(qt)
    shell.listing.opened.emit("ws-a")
    assert shell.currentWidget() is shell.detail
    assert shell.detail.title.text() == "alpha"
    assert ("detail-row", "workspaces", ()) in bridge.calls
    assert ("detail-health", "health", ()) in bridge.calls
    assert shell.detail.timer.interval() == 5000


def test_the_activated_row_opens_the_workspace(qt) -> None:
    """A row's activation opens its workspace (#945): the
    keypress or double-click the platform serves lands on the
    activated index's row, and the signal carries the row's
    workspace id."""
    shell, window, bridge = make_shell(qt)
    table = shell.listing.table
    opened = []
    shell.listing.opened.connect(opened.append)
    table.activated.emit(table.model().index(0, 2))
    assert opened == ["ws-a"]


def test_the_page_renders_the_sections(qt) -> None:
    """The sections land as widgets (#945): the mounts table
    reads the entries — the spec, the derived state, the gate's
    reason — the next-start note stands under it, the health
    facts read one pair a line, and the title's meta line
    carries the row's own facts."""
    page, window, bridge = make_page(qt)
    land(qt, page, bridge)
    assert page.title.text() == "alpha"
    assert page.meta.text() == (
        "id ws-a  ·  image msks/fedora:41  ·  created 2d ago"
    )
    assert page.mounts.rowCount() == 2
    cells = [
        page.mounts.item(row, column).text()
        for row in range(2)
        for column in range(5)
    ]
    assert cells == [
        "/srv/ok",
        "/mnt/ok",
        "rw",
        "healthy",
        "-",
        "/srv/gone",
        "/mnt/gone",
        "rw",
        "skipped",
        "stopped: the host path left",
    ]
    assert page.pending_label.isVisibleTo(page)
    assert page.pending_label.text() == "applies at next start"
    assert page.health.isVisibleTo(page)
    assert page.health.text() == (
        "status  ok\nversion  0.9.2\nmachine  x86_64\nimage  -"
    )
    assert page.empty_health.isHidden()
    # No frame has landed: the events section reads its muted
    # stand-in.
    assert page.empty_events.isVisibleTo(page)
    assert page.events.isHidden()


def test_a_workspace_that_mounts_nothing_reads_the_stand_in(
    qt,
) -> None:
    """An empty mounts section (#945): the table stands aside
    for the muted note — the presenter's empty section, the
    widget's honest stand-in."""
    page, window, bridge = make_page(qt)
    row = other_row()
    page.workspace_id = "ws-b"
    land(qt, page, bridge, row=row)
    assert page.mounts.isHidden()
    assert page.empty_mounts.isVisibleTo(page)
    assert page.pending_label.isHidden()


def test_pushed_frames_fill_the_events_section(qt) -> None:
    """A pushed frame lands in the buffer and re-renders the
    section (#945's seam, #946's feed): this workspace's frames
    read, newest last, another workspace's stay out, and the
    buffer keeps its bound."""
    page, window, bridge = make_page(qt)
    land(qt, page, bridge)
    page.push_frame(
        {
            "event": "workspace.status",
            "data": {"id": "ws-b", "status": "running"},
        }
    )
    assert page.empty_events.isVisibleTo(page)
    page.push_frame({"event": "workspace.status", "data": {"id": "ws-a"}})
    assert page.events.isVisibleTo(page)
    assert [page.events.item(row).text() for row in range(1)] == ["status ?"]
    page.push_frame(
        {
            "event": "workspace.status",
            "data": {"id": "ws-a", "status": "stopped"},
        }
    )
    texts = [page.events.item(row).text() for row in range(2)]
    assert texts[-1] == "status stopped"
    for n in range(qt.page_mod.FRAMES_BUFFER + 5):
        page.push_frame({"event": "noise", "data": {"id": "ws-a"}})
    assert len(page.frames) == qt.page_mod.FRAMES_BUFFER


def test_back_returns_to_the_list(qt) -> None:
    """The back action (#945): the button and the Escape key
    both return the list to the window."""
    from PySide6 import QtTest

    shell, window, bridge = make_shell(qt)
    shell.listing.opened.emit("ws-a")
    # The synthetic key needs what the live loop gives a real
    # open: a turn to activate the window and land the focused
    # child the Escape shortcut rides.
    qt.app.processEvents()
    QtTest.QTest.keyClick(window, qt.QtCore.Qt.Key.Key_Escape)
    assert shell.currentWidget() is shell.listing
    shell.listing.opened.emit("ws-a")
    page = shell.detail
    back = next(
        button
        for button in page.findChildren(qt.QtWidgets.QPushButton)
        if button.accessibleName() == "Back"
    )
    back.click()
    assert shell.currentWidget() is shell.listing


def test_a_failed_refresh_flashes_and_keeps_the_sections(
    qt,
) -> None:
    """A refused refresh (#945, AGENTS.md's exception rule): the
    failure flashes on the status line — an outage stands until
    an answer resolves it — while every section keeps the
    content it holds."""
    page, window, bridge = make_page(qt)
    land(qt, page, bridge)
    # A refusal first arms its own timeout; the outage after it
    # arms nothing — the armed timeout's stale serial clears
    # nothing, and the outage stands for an answer.
    page.on_failed(
        qt.page_mod.ROW_KEY,
        clientdata.RestFailure("first refusal", reach=False),
    )
    page.on_failed(
        qt.page_mod.ROW_KEY,
        clientdata.RestFailure("the daemon is gone", reach=True),
    )
    assert page.status.text() == "the daemon is gone"
    assert page.mounts.rowCount() == 2
    assert page.health.text().startswith("status")
    page.clear_stale(page.flash_seq - 1)  # the armed timeout's slot
    assert page.status.text() == "the daemon is gone"
    land(qt, page, bridge)
    assert page.status.text() == ""


def test_a_refusal_flash_clears_on_its_timeout(qt) -> None:
    """A refusal's own span (#945): the flash clears when its
    timeout lands, and a later flash outlives an earlier
    timeout — the serial the two share."""
    page, window, bridge = make_page(qt)
    land(qt, page, bridge)
    page.on_failed(
        qt.page_mod.ROW_KEY,
        clientdata.RestFailure("refused", reach=False),
    )
    page.clear_stale(page.flash_seq)
    assert page.status.text() == ""
    page.on_failed(
        qt.page_mod.ROW_KEY,
        clientdata.RestFailure("first", reach=False),
    )
    page.on_failed(
        qt.page_mod.ROW_KEY,
        clientdata.RestFailure("second", reach=False),
    )
    page.clear_stale(page.flash_seq - 1)
    assert page.status.text() == "second"
    page.clear_stale(page.flash_seq)
    assert page.status.text() == ""


def test_an_unchanged_answer_renders_nothing(qt) -> None:
    """An answer that changes nothing rebuilds nothing (#945,
    the listing's #470 L2 rule): a hold on the mounts table
    survives a refresh whose model reads exactly as the last."""
    page, window, bridge = make_page(qt)
    land(qt, page, bridge)
    page.mounts.selectRow(1)
    land(qt, page, bridge)
    assert page.mounts.selectionModel().selectedRows() == [
        page.mounts.model().index(1, 0)
    ]


def test_the_removed_workspace_closes_its_page(qt) -> None:
    """A listing that cannot see the workspace (#945, the TUI's
    close_removed shape): the page emits, the list returns with
    the notice on its status line."""
    shell, window, bridge = make_shell(qt)
    shell.listing.opened.emit("ws-a")
    shell.detail.on_succeeded("detail-row", [other_row()])
    assert shell.currentWidget() is shell.listing
    assert "alpha removed" in shell.listing.status.text()


def test_opening_resets_the_page(qt) -> None:
    """A second workspace's open resets the page (#945): the
    row, the doc, the frame buffer, and the rendered sections
    all start fresh — the first workspace's tail cannot leak
    onto the second's page."""
    page, window, bridge = make_page(qt)
    land(qt, page, bridge)
    page.push_frame({"event": "workspace.status", "data": {"id": "ws-a"}})
    page.open("ws-b", "beta")
    assert page.title.text() == "beta"
    assert page.events.isHidden()
    assert page.empty_events.isVisibleTo(page)
    assert page.frames == []
    assert page.rendered is None
    assert ("detail-row", "workspaces", ()) in bridge.calls


def test_the_answers_land_in_either_order(qt) -> None:
    """The two flights land independently (#945): a health doc
    before the row renders nothing (there is nothing to render
    against yet), and a refused health read flashes while the
    row still renders — the best-effort silence after it keeps
    the standing section."""
    page, window, bridge = make_page(qt)
    page.on_succeeded(
        qt.page_mod.HEALTH_KEY,
        {"status": "ok", "version": "0.9.2", "machine": "x86_64"},
    )
    assert page.rendered is None
    assert page.empty_health.isVisibleTo(page)
    page.on_succeeded(qt.page_mod.ROW_KEY, [daemon_row()])
    assert page.health.isVisibleTo(page)
    page.on_failed(
        qt.page_mod.HEALTH_KEY,
        clientdata.RestFailure("the health read refused", reach=False),
    )
    assert page.status.text() == "the health read refused"
    assert page.health.isVisibleTo(page)  # the section keeps it
    # A refusal runs its own span (#940's mask): a landing answer
    # inside it does not wipe what the operator just read — the
    # doc lands, the flash holds its text until its timeout.
    # The silence that follows keeps the standing doc (#717):
    # the flash holds its span, the section holds its facts.
    page.on_succeeded(qt.page_mod.HEALTH_KEY, None)
    assert page.status.text() == "the health read refused"
    assert page.health.isVisibleTo(page)
    assert page.health.text().startswith("status  ok")


def test_the_open_signal_guards_a_rowless_index(qt) -> None:
    """The open seam's guard (#945): an activated index with no
    name item under it — an index Qt hands over a table mid-
    rebuild — opens nothing."""
    shell, window, bridge = make_shell(qt)
    opened = []
    shell.listing.opened.connect(opened.append)
    shell.listing.open_current(qt.QtCore.QModelIndex())
    assert opened == []


def test_the_none_health_silence_keeps_the_outage(qt) -> None:
    """The outage tick's real shape (#945, #717, #904): the row
    read fails — the daemon answered nothing — while the health
    read, best-effort by its own contract, answers the silence
    with ``None``. The outage flash stands and the standing doc
    keeps; only a real doc resolves the one and replaces the
    other."""
    page, window, bridge = make_page(qt)
    land(qt, page, bridge)
    page.on_failed(
        qt.page_mod.ROW_KEY,
        clientdata.RestFailure("the daemon is gone", reach=True),
    )
    page.on_succeeded(qt.page_mod.HEALTH_KEY, None)
    assert page.status.text() == "the daemon is gone"
    assert page.health.isVisibleTo(page)
    assert page.health.text().startswith("status  ok")
    page.on_succeeded(
        qt.page_mod.HEALTH_KEY,
        {"status": "ok", "version": "0.9.3", "machine": "arm64"},
    )
    assert page.status.text() == ""
    assert page.health.text() == (
        "status  ok\nversion  0.9.3\nmachine  arm64\nimage  -"
    )


def test_the_meta_line_moves_with_the_clock(qt, monkeypatch) -> None:
    """The title renders on every answer (#945): the created age
    is a fact of the row, not the section models — an unchanged
    section answer still re-reads it against the moved clock."""
    from datetime import datetime as dt

    page, window, bridge = make_page(qt)
    land(qt, page, bridge)
    assert "created 2d ago" in page.meta.text()
    monkeypatch.setattr(
        ages_mod, "clock_now", lambda: dt.fromisoformat("2026-01-18T12:00:00")
    )
    land(qt, page, bridge)  # the same row, the clock 14 days on
    assert "created 2w ago" in page.meta.text()
    # The sections stood unchanged through the same answer; the
    # hold that survives them is pinned by the unchanged-answer
    # test.
    assert page.mounts.rowCount() == 2


def test_a_late_removed_answer_needs_the_page_current(qt) -> None:
    """The removed notice only fires on the live page (#945,
    the TUI's close_removed guard): a row read landing after
    Back hands the operator the list already — no notice for a
    close that already happened."""
    shell, window, bridge = make_shell(qt)
    shell.listing.opened.emit("ws-a")
    shell.detail.back.emit()
    assert shell.currentWidget() is shell.listing
    shell.detail.on_succeeded("detail-row", [other_row()])
    assert shell.listing.status.text() == ""


def test_a_pushed_frame_refreshes_the_current_screen(qt) -> None:
    """One pushed frame (#946): the page buffers it whatever
    screen owns the window, and the screen the operator sees
    refreshes at once through the same path its timer drives —
    the feed is the immediacy, the tick the backstop."""
    shell, window, bridge = make_shell(qt)
    bridge.calls.clear()
    shell.frame_landed({"event": "workspace.status", "data": {"id": "ws-a"}})
    # The list owns the window: its refresh flew.
    assert ("workspaces-list", "workspaces", ()) in bridge.calls
    assert ("detail-health", "health", ()) not in bridge.calls
    # No workspace is open on the page: the frame names none it
    # holds, so nothing buffers.
    assert shell.detail.frames == []
    shell.listing.opened.emit("ws-a")
    land(qt, shell.detail, bridge)  # the open's own flights answered
    bridge.calls.clear()
    shell.frame_landed({"event": "workspace.status", "data": {"id": "ws-a"}})
    assert ("detail-row", "workspaces", ()) in bridge.calls
    assert ("detail-health", "health", ()) in bridge.calls
    # The open page buffers its own workspace's frame: its
    # events section is the frame's own record.
    assert len(shell.detail.frames) == 1
    # A busy sibling cannot evict it (#946): the buffer keeps
    # only this workspace's frames.
    for _ in range(150):
        shell.frame_landed(
            {"event": "workspace.status", "data": {"id": "ws-b"}}
        )
    assert len(shell.detail.frames) == 1


def test_the_feed_outage_flashes_the_current_screen(qt) -> None:
    """The feed's drop and recovery (#946): the screen the
    operator sees flashes the outage while the feed retries — an
    outage, standing for an answer — and the recovery's own one
    line when the frames flow again."""
    shell, window, bridge = make_shell(qt)
    shell.feed_dropped("the socket broke")
    assert shell.listing.status.text() == (
        "the events feed dropped — the socket broke"
    )
    # The feed's flash runs its own span, not the screens' outage
    # flag: a REST answer must not read as the feed's recovery.
    assert not shell.listing.flash_outage
    shell.listing.on_succeeded("workspaces-list", [])
    assert shell.listing.status.text() == (
        "the events feed dropped — the socket broke"
    )
    shell.feed_back()
    assert shell.listing.status.text() == "the events feed reconnected"
    assert not shell.listing.flash_outage
    # The page flashes its own copy when it owns the window.
    shell.listing.opened.emit("ws-a")
    shell.feed_dropped("the dial failed")
    assert shell.detail.status.text() == (
        "the events feed dropped — the dial failed"
    )


def test_a_refresh_in_flight_is_not_stacked(qt) -> None:
    """The in-flight guard (#945, the listing's own #470): a
    tick under a slow call adds nothing — the landing answer
    retires the guard and the next refresh flies."""
    page, window, bridge = make_page(qt)
    assert page.row_pending and page.health_pending
    page.refresh()
    assert bridge.calls == []
    page.on_succeeded(qt.page_mod.ROW_KEY, [daemon_row()])
    page.refresh()
    assert bridge.calls == [(qt.page_mod.ROW_KEY, "workspaces", ())]
    # Another key's answer is not this page's to take.
    page.on_succeeded("workspaces-list", [])
    page.on_failed("workspaces-list", clientdata.RestFailure("x", reach=False))
    assert page.status.text() == ""
