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
import sys
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

# The hub's pty children spawn through ``fork()`` on Qt's own
# threads (termsession's pty design, #951); Python 3.14 warns
# on the multi-threaded fork by design of the spawn, and the
# warning is the suite's own noise, not a defect to fix here.
pytestmark = pytest.mark.filterwarnings(
    "ignore:This process.*multi-threaded.*:DeprecationWarning"
)

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


#: The argv the sleeper hubs spawn (#974): a child that stands
#: until the close path takes it down, the widget suite's
#: sleeper — the page's own open now takes a first shell.
SLEEPER = "import time; time.sleep(60)"


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
    ns = SimpleNamespace(
        app=app,
        QtCore=QtCore,
        QtGui=QtGui,
        QtWidgets=QtWidgets,
        page_mod=workspace_mod,
        hubs=[],
        keep=[],
    )
    yield ns
    # The sleeper hubs the helpers build (#974): the page's own
    # open takes a first shell, so every hub a test built owns
    # a child by teardown unless the test closed it. The windows
    # stand until the hubs are down — a dropped top-level
    # reference deletes the C++ tree, terminal widgets included,
    # ahead of the close that reaps their children.
    for hub in ns.hubs:
        hub.shutdown()
    del ns.keep[:]
    # The same teardown the list's own fixture owes its worker
    # (#959): close and delete every top-level widget, then
    # flush the deferred deletions — nothing C++-side outlives
    # the test to hand the cycle collector an empty wrapper
    # later, under an unrelated test's stderr.
    for widget in app.topLevelWidgets():
        widget.close()
        widget.deleteLater()
    QtCore.QCoreApplication.sendPostedEvents(
        None, QtCore.QEvent.Type.DeferredDelete
    )
    app.processEvents()


def make_shell(qt, rows=None) -> tuple:
    """The shell over the stub bridge in a real window — the
    list shown, its first listing answered (so the rows and the
    model map stand), the page still hidden. The hub runs
    sleeper children (#974): the page's own open takes a first
    shell, and the qt fixture shuts the hub down at teardown."""
    bridge = StubBridge()
    hub = qt.page_mod.ShellHub(lambda ws: [sys.executable, "-c", SLEEPER])
    qt.hubs.append(hub)
    shell = qt.page_mod.WorkspaceShell(bridge, None, hub)
    window = qt.QtWidgets.QMainWindow()
    window.setCentralWidget(shell)
    window.show()
    qt.keep.append(window)
    shell.listing.on_succeeded(
        "workspaces-list", rows if rows is not None else [daemon_row()]
    )
    return shell, window, bridge


def make_page(qt) -> tuple:
    """The page alone over the stub bridge, shown in a real
    window and opened on the workspace — the shape every page
    test reads: first refresh issued, the row and the doc one
    answer away, a first shell taken (#974)."""
    bridge = StubBridge()
    hub = qt.page_mod.ShellHub(lambda ws: [sys.executable, "-c", SLEEPER])
    qt.hubs.append(hub)
    page = qt.page_mod.WorkspaceDetail(bridge, hub)
    window = qt.QtWidgets.QMainWindow()
    window.setCentralWidget(page)
    window.show()
    qt.keep.append(window)
    page.open("ws-a")
    bridge.calls.clear()
    return page, window, bridge


def show_info(page) -> None:
    """The Info tab current (#974): the sections render under
    it, and a non-current page of the body reads as hidden."""
    page.bar.setCurrentIndex(1)


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
    # The workspace's name lives in the window title (#971):
    # the page carries no header of its own.
    assert window.windowTitle() == "msks — alpha"
    assert ("detail-row", "workspaces", ()) in bridge.calls
    assert ("detail-health", "health", ()) in bridge.calls
    assert ("detail-storage", "storage", ()) in bridge.calls
    assert shell.detail.timer.interval() == 5000


def test_the_page_carries_top_level_shells_and_info_tabs(qt) -> None:
    """The tab row on the back arrow's own row (#974): the
    arrow, then Shells — the default — then Info, the status
    line filling the rest; the body below holds the shell strip
    and the detail content, the bar driving the stack."""
    page, window, bridge = make_page(qt)
    assert page.chrome.itemAt(0).widget() is page.back_button
    assert page.chrome.itemAt(1).widget() is page.bar
    assert [page.bar.tabText(i) for i in range(page.bar.count())] == [
        "Shells",
        "Info",
    ]
    assert page.bar.currentIndex() == 0  # Shells, the default
    assert page.stack.currentIndex() == 0
    assert page.stack.widget(0) is page.shells
    info = page.stack.widget(1)
    assert info.findChildren(type(page.mounts)) == [page.mounts]
    assert page.health in info.findChildren(type(page.health))
    assert info.findChildren(type(page.events)) == [page.events]
    page.bar.setCurrentIndex(1)  # the bar drives the stack
    assert page.stack.currentWidget() is info


def test_an_open_with_no_shell_creates_one_and_focuses_it(qt) -> None:
    """The Shells tab opens with a terminal to type in (#974):
    the open takes a first shell when none stands, its subtab
    current and its terminal holding the keyboard."""
    page, window, bridge = make_page(qt)
    assert [page.shells.tabText(i) for i in range(page.shells.count())] == [
        "1",
        "+",
    ]
    session = page.hub.find("ws-a", 1)
    assert page.shells.currentWidget() is session.widget
    assert window.focusWidget() is session.widget


def test_an_open_with_shells_standing_adds_none(qt) -> None:
    """A reopen over standing shells adds no shell (#974): the
    open's first shell is only for a workspace with none."""
    page, window, bridge = make_page(qt)
    page.open("ws-a")  # one shell already stands
    assert len(page.hub.shells("ws-a")) == 1


def test_the_show_anchors_on_the_tab_the_row_shows(qt) -> None:
    """The keyboard's anchor (#974): the Back button holds it
    while the Info tab shows, the current shell's terminal
    while the Shells tab shows one."""
    page, window, bridge = make_page(qt)
    page.bar.setCurrentIndex(1)  # Info
    page.anchor_focus()
    assert window.focusWidget() is page.back_button
    page.bar.setCurrentIndex(0)  # Shells, a shell current
    page.anchor_focus()
    assert window.focusWidget() is page.hub.find("ws-a", 1).widget


def test_a_reopen_resets_the_row_and_anchors_the_terminal(qt) -> None:
    """Every open starts on Shells (#974): a prior visit's Info
    tab must not hand the next workspace a hidden terminal
    holding the keyboard. The reopen drives the page's own show
    — the anchor is the show's, not the strip's."""
    shell, window, bridge = make_shell(qt)
    shell.listing.opened.emit("ws-a")
    shell.detail.bar.setCurrentIndex(1)  # Info before leaving
    shell.detail.go_back()
    shell.listing.opened.emit("ws-a")  # reopen: a fresh open's show
    assert shell.detail.bar.currentIndex() == 0  # Shells again
    assert shell.detail.stack.currentWidget() is shell.detail.shells
    assert window.focusWidget() is shell.detail.hub.find("ws-a", 1).widget


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
    facts read one pair a line, and the footer strip (#971)
    carries the same one-line read the listing's strip gives,
    storage as the honest dash until a report lands."""
    page, window, bridge = make_page(qt)
    show_info(page)
    land(qt, page, bridge)
    assert page.footer.text() == (
        "alpha  ·  id ws-a  ·  image msks/fedora:41"
        "  ·  cpu -  ·  mem -  ·  egress -"
        "  ·  mounts ✗1  ·  1 change applies at the next start"
        "  ·  storage -"
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
    show_info(page)
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
    show_info(page)
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


def test_the_create_opener_opens_the_dialog_over_the_list(
    qt, monkeypatch
) -> None:
    """The Workspaces tab's plus button (#962): the shell opens
    the create dialog over the bridge it owns, and a landed
    create — the dialog's accept — refreshes the listing behind
    it."""
    shell, window, bridge = make_shell(qt)
    opened: list[object] = []
    accepted: list[object] = []

    class FakeDialog:
        """The dialog's stub: the constructor's bridge, the
        accepted signal's connect surface, and an exec that
        returns at once (the stub never enters a loop)."""

        def __init__(self, bridge: object) -> None:
            opened.append(bridge)

    class FakeAccepted:
        def connect(self, slot: object) -> None:
            accepted.append(slot)

    FakeDialog.accepted = FakeAccepted()  # type: ignore[attr-defined]

    def fake_exec(self: FakeDialog) -> int:
        return 0

    FakeDialog.exec = fake_exec  # type: ignore[method-assign]
    from msks.client.gui import workspace as workspace_mod

    monkeypatch.setattr(workspace_mod, "CreateDialog", FakeDialog)
    shell.open_create()
    assert opened == [bridge]
    # A landed create refreshes the listing: the connected slot
    # is the list's own refresh.
    assert accepted == [shell.listing.refresh]
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


def test_events_on_a_wrapper_past_construction_touch_nothing(
    qt,
) -> None:
    """The page's handlers tolerate the two half-built shapes
    Qt can deliver events to (#959): a page whose construction
    never finished, and the empty wrapper PySide6 raises at
    teardown for a C++ object that outlived its Python one.
    A wrapper without its Back button and ``timer`` answers
    nothing: no focus, no refresh, no tick, instead of an
    AttributeError tracing to whatever test is running."""
    page, window, _bridge = make_page(qt)
    window.close()
    # The two teardown shapes leave a wrapper with no instance
    # attributes — the same state the guards answer.
    del page.back_button
    del page.timer
    page.showEvent(qt.QtGui.QShowEvent())
    page.hideEvent(qt.QtGui.QHideEvent())


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
    show_info(page)
    land(qt, page, bridge)
    page.push_frame({"event": "workspace.status", "data": {"id": "ws-a"}})
    page.open("ws-b")  # the open resets the row to Shells
    show_info(page)
    assert page.footer.text() == ""
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
    show_info(page)
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
    show_info(page)
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


def test_the_rows_own_name_refines_the_window_title(qt) -> None:
    """The window's title reads every row answer (#971): the
    list's label stands in at open, and the row's own name — a
    rename the list has not seen — takes the title the moment
    the answer lands."""
    page, window, bridge = make_page(qt)
    window.setWindowTitle("msks — alpha")  # the list's stand-in
    row = daemon_row()
    row["name"] = "renamed"
    page.on_succeeded(qt.page_mod.ROW_KEY, [row])
    assert window.windowTitle() == "msks — renamed"


def test_a_hidden_page_names_nothing(qt) -> None:
    """A pushed frame or a late answer landing while the page
    is hidden re-names the window not at all (#971): the list
    holds the plain title, and the page's show re-asserts the
    standing name."""
    shell, window, bridge = make_shell(qt)
    shell.listing.opened.emit("ws-a")
    assert window.windowTitle() == "msks — alpha"
    # the row's own answer lands and refines the name
    shell.detail.on_succeeded(
        qt.page_mod.ROW_KEY,
        [dict(daemon_row(), name="renamed")],
    )
    assert window.windowTitle() == "msks — renamed"
    shell.detail.go_back()
    assert window.windowTitle() == "msks"
    # a frame for the hidden page's workspace lands while the
    # list shows
    shell.frame_landed({"event": "workspace.status", "data": {"id": "ws-a"}})
    assert window.windowTitle() == "msks"
    # revisiting names from the list again (its row is stale),
    # and the row's own answer refines it once it lands
    shell.listing.opened.emit("ws-a")
    assert window.windowTitle() == "msks — alpha"
    shell.detail.on_succeeded(
        qt.page_mod.ROW_KEY,
        [dict(daemon_row(), name="renamed")],
    )
    assert window.windowTitle() == "msks — renamed"


def test_the_footer_reads_the_storage_report(qt) -> None:
    """The footer's storage fact reads the capacity report
    (#971): the same per-workspace cost the listing's strip
    charges, landing with the report's own answer."""
    page, window, bridge = make_page(qt)
    land(qt, page, bridge)
    assert page.footer.text().endswith("storage -")
    page.on_succeeded(
        qt.page_mod.STORAGE_KEY,
        {
            "workspaces": [
                {"id": "ws-a", "root_bytes": 10737418240, "home_bytes": 1}
            ]
        },
    )
    assert "storage 10G" in page.footer.text()


def test_a_failed_storage_read_flashes_and_keeps_the_dash(qt) -> None:
    """A refused storage read flashes the status line and the
    footer keeps its dash (#971): the strip's fact is the
    report's, not an error's."""
    page, window, bridge = make_page(qt)
    land(qt, page, bridge)
    page.on_failed(
        qt.page_mod.STORAGE_KEY,
        clientdata.RestFailure("the daemon refused", reach=False),
    )
    assert page.status.text() == "the daemon refused"
    assert page.footer.text().endswith("storage -")
    assert not page.storage_pending


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
