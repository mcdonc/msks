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
import time
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
        "PySide6 is not installed on this host",
        allow_module_level=True,
    )

from msks.client import ages as ages_mod  # noqa: E402
from msks.client import data as clientdata  # noqa: E402
from msks.client.bridge import ReHolder  # noqa: E402


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
        self.feed_served = FakeSignal()
        self.decider_landed = FakeSignal()
        self.decider_rejected = FakeSignal()
        #: The decider registration's bracket (#999): the page's
        #: asks and retires, recorded for the lifetime asserts,
        #: with the controller each ask carried beside it — the
        #: presenter's state seam (#1000).
        self.decider_starts: list[str] = []
        self.decider_controllers: list[object] = []
        self.decider_stops = 0
        #: The bridge re-hold watch's surface (#1127): the session
        #: policy the shell's watch reads, over a spawn the tests
        #: record — the detached child never runs here.
        self.holder_spawns: list[str] = []

        def record_spawn(ws: str) -> bool:
            self.holder_spawns.append(ws)
            return True

        self.reholder = ReHolder(spawn=record_spawn)

    def call(self, key: str, method: str, *args: object) -> None:
        self.calls.append((key, method, args))

    def start_decider(self, workspace_id: str, controller=None) -> None:
        self.decider_starts.append(workspace_id)
        self.decider_controllers.append(controller)

    def stop_decider(self) -> None:
        self.decider_stops += 1


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


def bar_index(page, label: str) -> int:
    """The tab's index on the page's bar, by its label (#987):
    the bar's own order is the page's business, not the
    tests'."""
    for index in range(page.bar.count()):
        if page.bar.tabText(index) == label:
            return index
    raise AssertionError(f"no {label} tab on the bar")


def show_tab(page, label: str) -> None:
    """One of the non-default tabs current (#974, #987): the
    tab's surface shows, and a non-current page of the body
    reads as hidden."""
    page.bar.setCurrentIndex(bar_index(page, label))


def show_mounts(page) -> None:
    """The Mounts tab current (#987): the editable surface
    under it."""
    page.bar.setCurrentIndex(bar_index(page, "Mounts"))


def land(qt, page, bridge, row=None) -> None:
    """The page's answers landed: the row (this workspace's,
    unless a listing is handed in) and the capacity report."""
    bridge.calls.clear()
    page.on_succeeded(
        qt.page_mod.ROW_KEY,
        [daemon_row() if row is None else row],
    )
    page.on_succeeded(
        qt.page_mod.STORAGE_KEY,
        {"workspaces": []},
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
    assert ("detail-storage", "storage", ()) in bridge.calls
    # The health read is gone with the Info pane (#1017): the
    # daemon's facts are not the workspace's, and the page asks
    # for no doc it renders nowhere.
    assert not [call for call in bridge.calls if call[1] == "health"]
    # The show that follows the open adds no carried read (#1129):
    # the open's own reads stand for it, and their landing asks
    # nothing more — one pair of reads per open.
    land(qt, shell.detail, bridge)
    assert bridge.calls == []


def test_the_page_carries_the_tab_strip(qt) -> None:
    """The tab row on the back arrow's own row (#974, #1017,
    #1042): the arrow, then Shells — the default — Mounts,
    Proxies, Events, and Egress, the status line filling the
    rest; the bar drives the stack, and the Info pane is gone
    with its duplicated sections."""
    page, window, bridge = make_page(qt)
    assert page.chrome.itemAt(0).widget() is page.back_button
    assert page.chrome.itemAt(1).widget() is page.bar
    assert [page.bar.tabText(i) for i in range(page.bar.count())] == [
        "Shells",
        "Mounts",
        "Proxies",
        "Egress",
        "Events",
    ]
    assert page.bar.currentIndex() == 0  # Shells, the default
    # The active tab's mark (#1012): the strip wears the shared
    # sheet, the same one the main window's strip carries.
    assert "QTabBar::tab:selected" in page.bar.styleSheet()
    assert page.stack.currentIndex() == 0
    assert page.stack.widget(0) is page.shells_tab
    assert page.shells_tab.shells is page.shells
    assert page.stack.widget(3) is page.egress_tab  # the bar's own order
    assert page.stack.widget(4) is page.events_tab
    assert page.stack.count() == 5
    show_tab(page, "Egress")
    assert page.stack.currentWidget() is page.egress_tab


def test_open_hosts_consent_before_registration(qt) -> None:
    from test_gui_consent import request_raw

    page, window, bridge = make_page(qt)

    def register(workspace_id, controller):
        controller.apply_frame(request_raw(ws=workspace_id))
        bridge.decider_landed.emit("added", None)

    bridge.start_decider = register
    page.open("ws-a")
    qt.app.processEvents()
    panel = page.shells_tab.consent_panel
    assert not panel.isHidden()
    assert (
        panel.findChild(qt.QtWidgets.QLabel, "destination-req-1").text()
        == "api.github.com:443"
    )
    assert panel.geometry().bottom() < page.shells.geometry().top()


def test_workspace_switch_discards_old_consent_actions_and_results(qt) -> None:
    from test_gui_consent import request_raw

    page, window, bridge = make_page(qt)
    panel = page.shells_tab.consent_panel
    page.consent.controller.apply_frame(request_raw())
    panel.refresh()
    action = panel.findChild(qt.QtWidgets.QToolButton, "allow-req-1")
    old_choice = action.menu().actions()[0]
    action.click()
    old_key = bridge.calls[-1][0]
    page.open("ws-b")
    page.consent.controller.apply_frame(request_raw(ws="ws-b"))
    panel.refresh()
    before = list(bridge.calls)
    old_choice.trigger()
    bridge.failed.emit(old_key, RuntimeError("old refusal"))
    assert bridge.calls == before
    assert panel.note.text() == ""
    assert len(page.consent.rows()) == 1


def test_reopen_uses_fresh_presenter_without_closing_shells(qt) -> None:
    page, window, bridge = make_page(qt)
    terminal = page.shells.currentWidget()
    first = page.consent
    page.open("ws-b")
    page.open("ws-a")
    assert page.consent is not first
    assert page.shells_tab.consent_panel.presenter is page.consent
    assert page.shells.currentWidget() is terminal
    assert page.consent.rows() == []


@pytest.mark.parametrize("mode", ["allow", "static"])
def test_noninteractive_empty_consent_is_collapsed(qt, mode) -> None:
    from test_gui_consent import rules_raw

    page, window, bridge = make_page(qt)
    page.consent.controller.apply_frame(rules_raw(mode=mode))
    bridge.decider_landed.emit("rules", None)
    assert page.shells_tab.consent_panel.isHidden()


def test_reset_collapses_panel_and_resolution_returns_terminal_focus(
    qt,
) -> None:
    from test_gui_consent import request_raw, resolved_raw

    page, window, bridge = make_page(qt)
    panel = page.shells_tab.consent_panel
    page.consent.controller.apply_frame(request_raw())
    panel.refresh()
    qt.app.processEvents()
    action = panel.findChild(qt.QtWidgets.QToolButton, "allow-req-1")
    action.setFocus()
    assert action.hasFocus()
    page.consent.controller.apply_frame(resolved_raw())
    bridge.decider_landed.emit("resolved", None)
    assert page.shells.currentWidget().hasFocus()
    page.consent.controller.apply_frame(request_raw())
    bridge.decider_landed.emit("added", None)
    assert not panel.isHidden()
    page.consent.controller.reset()
    page.shells_tab.timer.timeout.emit()
    assert panel.isHidden()


def test_other_workspace_tabs_keep_decider_registration(qt) -> None:
    page, window, bridge = make_page(qt)
    stops = bridge.decider_stops
    show_tab(page, "Events")
    assert not page.shells_tab.timer.isActive()
    assert bridge.decider_stops == stops
    page.bar.setCurrentIndex(0)
    assert page.shells_tab.timer.isActive()


def test_a_mounts_save_lands_on_the_page(qt) -> None:
    """The Mounts tab's landed save (#987): the tab's answer is
    a fresh daemon row — the page takes it whole, the footer
    reading the same facts the tab painted, and the tab's own
    hosting of it changing nothing."""
    page, window, bridge = make_page(qt)
    land(qt, page, bridge)
    show_mounts(page)
    entry = {"host": "/srv/new", "guest": "/mnt/new", "mode": "ro"}
    fresh = {
        **daemon_row(),
        "mounts": [*daemon_row()["mounts"], entry],
        "mounts_pending": True,
    }
    page.mounts_tab.row_updated.emit(fresh)
    assert page.row is fresh
    assert page.mounts_tab.table.rowCount() == 3
    assert "1 change applies at the next start" in page.footer.text()
    window.close()


def test_the_proxies_tab_rides_the_page(qt) -> None:
    """The Proxies tab's page integration (#988): the bar carries
    it beside Mounts, the page's refresh pokes its listing, and a
    pushed proxy frame refreshes it without a manual action.

    The Egress tab rides the same seams (#1042): its posture read
    pokes on the page's refresh, and a pushed rules frame
    repaints it without a manual action."""
    page, window, bridge = make_page(qt)
    assert bar_index(page, "Proxies") == 2
    assert bar_index(page, "Egress") == 3
    assert bar_index(page, "Events") == 4
    land(qt, page, bridge)
    # The open's own refresh already poked a listing; answer it
    # so the next refresh's poke runs.
    bridge.succeeded.emit(page.proxies_tab.list_key, [])
    bridge.succeeded.emit(
        page.egress_tab.list_key,
        {
            "workspace_id": page.workspace_id,
            "mode": "interactive",
            "allow_list": [],
            "allowed": [],
            "denied": [],
        },
    )
    bridge.calls.clear()
    page.refresh()  # a row-affecting frame's path drives the listing
    assert [c for c in bridge.calls if c[1] == "workspace_proxies"]
    assert [c for c in bridge.calls if c[1] == "egress_rules"]
    bridge.succeeded.emit(page.proxies_tab.list_key, [])
    bridge.succeeded.emit(
        page.egress_tab.list_key,
        {
            "workspace_id": page.workspace_id,
            "mode": "interactive",
            "allow_list": [],
            "allowed": [],
            "denied": [],
        },
    )
    bridge.calls.clear()
    page.push_frame(
        {"event": "proxy.listening", "data": {"id": page.workspace_id}}
    )
    assert [c[1] for c in bridge.calls if c[1] == "workspace_proxies"]
    # A pushed rules frame — a switch the CLI made elsewhere —
    # repaints the tab without a read of its own, and the row
    # re-reads behind it (#1042): the footer's posture word
    # follows the switch instead of standing on the stale row.
    land(qt, page, bridge)  # the refresh's row and storage answers
    bridge.calls.clear()
    page.push_frame(
        {
            "event": "egress.rules",
            "data": {
                "workspace_id": page.workspace_id,
                "mode": "static",
                "allow_list": [".debian.org"],
                "allowed": [],
                "denied": [],
            },
        }
    )
    assert page.egress_tab.mode_line.text() == "current mode: static"
    assert (qt.page_mod.ROW_KEY, "workspaces", ()) in bridge.calls
    bridge.succeeded.emit(
        qt.page_mod.ROW_KEY,
        [{**daemon_row(), "egress_mode": "static"}],
    )
    assert "egress static" in page.footer.text()
    window.close()


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
    while a non-shell tab shows, the current shell's terminal
    while the Shells tab shows one."""
    page, window, bridge = make_page(qt)
    show_tab(page, "Events")
    page.anchor_focus()
    assert window.focusWidget() is page.back_button
    page.bar.setCurrentIndex(0)  # Shells, a shell current
    page.anchor_focus()
    assert window.focusWidget() is page.hub.find("ws-a", 1).widget


def test_escape_answers_back_on_the_chrome_not_the_terminal(qt) -> None:
    """The page's own Esc contract (#1099): the Back shortcut
    answers while the chrome holds the keyboard — the Back
    button the anchor sets — and yields the key while a
    shell's terminal holds it, the press delivered through
    the shortcut dispatch a real platform serves. The
    terminal half's own byte pin lives with the terminal's
    tests; this one pins the page the shortcut stands on."""
    page, window, bridge = make_page(qt)
    backs = []
    page.back.connect(lambda: backs.append(1))
    Qt = qt.QtCore.Qt

    def escape_press() -> object:
        return qt.QtGui.QKeyEvent(
            qt.QtCore.QEvent.Type.KeyPress,
            int(Qt.Key.Key_Escape),
            Qt.KeyboardModifier.NoModifier,
            "",
        )

    # The Shells tab shows a shell: its terminal holds the
    # keyboard, and the press reaches the encoder instead.
    # The window must be the app's active one — a window
    # shortcut answers only there, and without the activation
    # this arm would pass with the fix removed.
    window.activateWindow()
    qt.app.processEvents()
    session = page.hub.find("ws-a", 1)
    page.anchor_focus()
    assert window.focusWidget() is session.widget
    qt.app.sendEvent(session.widget, escape_press())
    assert backs == []
    # The chrome's own holder: the Back button the anchor
    # names, and the shortcut answers from there.
    show_tab(page, "Events")
    page.anchor_focus()
    qt.app.processEvents()
    qt.app.sendEvent(page.back_button, escape_press())
    assert backs == [1]


def test_escape_cancels_a_standing_rename_not_the_page(qt) -> None:
    """The rename editor's own Esc (#1099): the page's Back
    shortcut stands on the bare key, and before the editor
    claimed its ShortcutOverride the dispatch handed the
    press to Back — the page navigated away with the editor
    standing. The claim yields the shortcut: the editor's
    cancel arm reads the press and the page stays."""
    page, window, bridge = make_page(qt)
    backs = []
    page.back.connect(lambda: backs.append(1))
    window.activateWindow()
    qt.app.processEvents()
    page.shells.begin_rename(0)
    editor = page.shells.editor
    assert editor is not None and editor.hasFocus()
    qt.app.sendEvent(
        editor,
        qt.QtGui.QKeyEvent(
            qt.QtCore.QEvent.Type.KeyPress,
            int(qt.QtCore.Qt.Key.Key_Escape),
            qt.QtCore.Qt.KeyboardModifier.NoModifier,
            "",
        ),
    )
    qt.app.processEvents()
    assert page.shells.editor is None  # the cancel arm read it
    assert backs == []


def test_a_reopen_resets_the_row_and_anchors_the_terminal(qt) -> None:
    """Every open starts on Shells (#974): a prior visit's tab
    must not hand the next workspace a hidden terminal holding
    the keyboard. The reopen drives the page's own show — the
    anchor is the show's, not the strip's."""
    shell, window, bridge = make_shell(qt)
    shell.listing.opened.emit("ws-a")
    show_tab(shell.detail, "Events")
    shell.detail.go_back()
    shell.listing.opened.emit("ws-a")  # reopen: a fresh open's show
    assert shell.detail.bar.currentIndex() == 0  # Shells again
    assert shell.detail.stack.currentWidget() is shell.detail.shells_tab
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


def test_the_page_renders_the_footer(qt) -> None:
    """The row lands on the footer strip (#945, #971): the
    same one-line read the listing's strip gives — identity,
    posture, costs — storage as the honest dash until a report
    lands. The sections the page once rendered under an Info
    pane are gone with it (#1017): the mounts read through the
    Mounts tab, the events through the Events tab, and the
    daemon's health facts nowhere, by the decision that took
    the pane."""
    page, window, bridge = make_page(qt)
    land(qt, page, bridge)
    assert page.footer.text() == (
        "alpha  ·  id ws-a  ·  image msks/fedora:41"
        "  ·  cpu -  ·  mem -  ·  egress -"
        "  ·  mounts ✗1  ·  1 change applies at the next start"
        "  ·  storage -"
    )
    # The mounts table the page renders is the Mounts tab's
    # own (#987): the read-only copy is gone with the pane.
    assert page.mounts_tab.table.rowCount() == 2
    assert not page.findChildren(qt.QtWidgets.QGroupBox)


def test_a_workspace_that_mounts_nothing_reads_no_rows(qt) -> None:
    """An empty mounts listing (#945): the row lands with no
    entries and the Mounts tab's table stands empty — the page
    renders no read-only copy of its own (#1017)."""
    page, window, bridge = make_page(qt)
    row = other_row()
    page.workspace_id = "ws-b"
    land(qt, page, bridge, row=row)
    assert page.mounts_tab.table.rowCount() == 0


def test_pushed_frames_fill_the_events_tab(qt) -> None:
    """A pushed frame lands on the page's tabs (#945's seam,
    #946's feed, #989): this workspace's frames read, newest
    last, another workspace's stay out, and the window caps at
    its own limit — while a frame that changes nothing the row
    holds costs the Mounts tab's table its selection not at
    all, the hold surviving a noisy feed whole."""
    page, window, bridge = make_page(qt)
    land(qt, page, bridge)
    page.push_frame(
        {
            "event": "workspace.status",
            "data": {"id": "ws-b", "status": "running"},
        }
    )
    assert page.events_tab.rows.count() == 0  # a foreign frame
    page.push_frame({"event": "workspace.status", "data": {"id": "ws-a"}})
    assert not page.events_tab.rows.isHidden()
    (stamp, _kind, summary) = (
        page.events_tab.rows.item(0).text().split("  ", 2)
    )
    assert stamp  # the receipt stamp reads
    assert summary == "status ?"
    page.push_frame(
        {
            "event": "workspace.status",
            "data": {"id": "ws-a", "status": "stopped"},
        }
    )
    summaries = [
        page.events_tab.rows.item(row).text().split("  ", 2)[2]
        for row in range(2)
    ]
    assert summaries[-1] == "status stopped"
    # A frame that changes nothing the row holds keeps the
    # Mounts tab's selection whole (#989's rule, on the table
    # that stands now): the repaint lands on an equal row.
    table = page.mounts_tab.table
    table.selectRow(min(1, table.rowCount() - 1))
    held = table.selectedItems()[0].text()
    page.push_frame({"event": "workspace.status", "data": {"id": "ws-a"}})
    assert table.selectedItems()[0].text() == held
    # The tab's window caps at its own limit (#989): a long
    # noisy feed keeps the tail, the oldest rows falling off.
    from msks.client.gui.detail import EVENTS_LIMIT

    for _ in range(EVENTS_LIMIT + 5):
        page.push_frame({"event": "noise", "data": {"id": "ws-a"}})
    assert page.events_tab.rows.count() == EVENTS_LIMIT


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


def test_the_page_registers_as_the_workspace_decider(qt) -> None:
    """The registration's lifetime tracks the page (#999): the
    open claims the workspace's holds, a re-hosted page claims
    the new workspace, and every path that hides the page —
    Back, the removed workspace's close — retires the claim:
    the daemon holds flows only while a page watches, and the
    socket's close is the retire."""
    shell, window, bridge = make_shell(qt)
    # The page's open claims its workspace — twice while the
    # open raises a hidden page, once for the open itself and
    # once for the show it causes; the bridge's own test pins
    # the second ask's idempotence. The window's show before
    # any open claims nothing (no workspace stands).
    assert bridge.decider_starts == []
    shell.listing.opened.emit("ws-a")
    assert bridge.decider_starts == ["ws-a", "ws-a"]
    assert bridge.decider_stops == 0
    # Back hides the page: the claim retires with it.
    shell.detail.go_back()
    assert bridge.decider_stops == 1
    # A re-open claims again, the open and the show it raises.
    shell.listing.opened.emit("ws-a")
    assert bridge.decider_starts == ["ws-a", "ws-a", "ws-a", "ws-a"]
    # A second workspace re-hosts the page already showing: the
    # open alone asks, the claim following the workspace the
    # page shows.
    shell.listing.opened.emit("ws-b")
    assert bridge.decider_starts[-1] == "ws-b"
    assert len(bridge.decider_starts) == 5
    # The removed workspace's close hides the page its own way:
    # the retire tells the same — a listing that cannot see the
    # page's workspace answers with another workspace's row.
    shell.detail.on_succeeded("detail-row", [daemon_row()])
    assert bridge.decider_stops == 2


def test_a_reshown_page_claims_again(qt) -> None:
    """The show after a hide without an open (#999): the page
    hid under whatever hid it — a Back whose return never
    re-opened, a tab switch away and back — and the workspace's
    holds need their decider back the moment the page shows
    again, the claim the hide retired."""
    page, window, bridge = make_page(qt)
    assert bridge.decider_starts == ["ws-a"]
    page.hide()
    assert bridge.decider_stops == 1
    page.show()
    assert bridge.decider_starts == ["ws-a", "ws-a"]


def test_each_open_hands_a_fresh_presenter_to_the_registration(qt) -> None:
    """The decider state seam (#1000): every open builds a
    fresh consent presenter — the holds of a prior visit must
    not linger in the next one's rows — and each ask carries
    that presenter's controller, so the link's frames land on
    the state the panel's rows build from."""
    page, window, bridge = make_page(qt)
    first = page.consent
    assert first is not None
    assert first.workspace_id == "ws-a"
    assert bridge.decider_controllers[0] is first.controller
    assert first.rows() == []
    # A re-open of the same workspace builds a fresh presenter:
    # its controller is its own, and the ask carries it.
    page.open("ws-a")
    second = page.consent
    assert second is not None
    assert second is not first
    assert second.controller is not first.controller
    assert bridge.decider_controllers[-1] is second.controller
    # A different workspace re-hosts the presenter the same
    # fresh way.
    page.open("ws-b")
    assert page.consent.workspace_id == "ws-b"
    assert bridge.decider_starts[-1] == "ws-b"
    assert bridge.decider_controllers[-1] is page.consent.controller


def test_a_refused_registration_flashes_the_current_screen(qt) -> None:
    """The daemon's refusal of the registration (#999): the
    reason flashes on the screen the operator sees — the page
    while it owns the window, the list after the page went
    back — one flash the whole tell, the link having ended
    itself."""
    shell, window, bridge = make_shell(qt)
    shell.decider_refused("unknown workspace")
    assert shell.listing.status.text() == (
        "the decider registration was refused — unknown workspace"
    )
    shell.listing.opened.emit("ws-a")
    shell.decider_refused("the workspace left")
    assert shell.detail.status.text() == (
        "the decider registration was refused — the workspace left"
    )


def test_the_create_opener_opens_the_dialog_over_the_list(
    qt, monkeypatch
) -> None:
    """The listing's Add button (#1011, the plus badge's action
    until the badge left the tab strip, #962): the click crosses
    to the shell's create dialog — the wiring pin the badge's
    own test once held — and a landed create refreshes the
    listing behind it."""
    shell, window, bridge = make_shell(qt)
    opened: list[object] = []
    accepted: list[object] = []
    parents: list[object] = []

    class FakeDialog:
        """The dialog's stub: the constructor's bridge, the
        accepted signal's connect surface, and an exec that
        returns at once (the stub never enters a loop)."""

        def __init__(self, bridge: object, parent=None) -> None:
            opened.append(bridge)
            parents.append(parent)

    class FakeAccepted:
        def connect(self, slot: object) -> None:
            accepted.append(slot)

    FakeDialog.accepted = FakeAccepted()  # type: ignore[attr-defined]

    def fake_exec(self: FakeDialog) -> int:
        return 0

    FakeDialog.exec = fake_exec  # type: ignore[method-assign]
    from msks.client.gui import workspace as workspace_mod

    monkeypatch.setattr(workspace_mod, "CreateDialog", FakeDialog)
    shell.listing.add_button.click()
    assert opened == [bridge]
    assert parents == [shell]  # the dialog parents to the shell (#1014)
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


def test_a_failed_refresh_flashes_and_keeps_the_tabs(qt) -> None:
    """A refused refresh (#945, AGENTS.md's exception rule): the
    failure flashes on the status line — an outage stands until
    an answer resolves it — while the footer and the Mounts
    tab keep the content they hold."""
    page, window, bridge = make_page(qt)
    land(qt, page, bridge)
    footer = page.footer.text()
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
    assert page.footer.text() == footer
    assert page.mounts_tab.table.rowCount() == 2
    page.clear_stale(page.flash_seq - 1)  # the armed timeout's slot
    assert page.status.text() == "the daemon is gone"
    # The feed's heartbeat flashes over the outage and gives the
    # text back when its span ends (#1129).
    page.flash("the events feed dropped — the dial failed")
    page.clear_stale(page.flash_seq)
    assert page.status.text() == "the daemon is gone"
    land(qt, page, bridge)
    # The answer resolves it with the recovery's one flash
    # (#1129), the row's landing — the storage answer landing
    # after it finds no outage standing and flashes nothing.
    assert page.status.text() == "daemon back — page resumed"


def test_a_volume_frame_under_a_standing_storage_read_is_carried(qt) -> None:
    """The carry arms on either read (#1129): a volume frame moves
    the storage strip as a lifecycle frame moves the row, and the
    row read landing first is the common half-landed state. The
    row re-reads at once when ``home.imported`` arrives, and the
    standing storage read is re-read behind it — the carried
    refresh served once no read stands — so the footer's figure
    follows the import."""
    page, window, bridge = make_page(qt)
    land(qt, page, bridge)
    row = (qt.page_mod.ROW_KEY, "workspaces", ())
    storage = (qt.page_mod.STORAGE_KEY, "storage", ())

    def reads() -> list:
        return [c for c in bridge.calls if c[0] in (row[0], storage[0])]

    page.refresh()  # both reads out
    page.on_succeeded(qt.page_mod.ROW_KEY, [daemon_row()])  # the row first
    bridge.calls.clear()
    frame = {"event": "home.imported", "data": {"workspace_id": "ws-a"}}
    page.push_frame(frame)
    assert reads() == [row]  # the row re-reads; the storage read stands
    page.on_succeeded(qt.page_mod.STORAGE_KEY, {"workspaces": []})
    assert reads() == [row]  # the frame's row read still stands
    page.on_succeeded(qt.page_mod.ROW_KEY, [daemon_row()])
    assert reads() == [row, row, storage]  # no read stands: served


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
    A wrapper without its Back button answers nothing: no
    focus, no refresh, no decider ask, instead of an
    AttributeError tracing to whatever test is running."""
    page, window, bridge = make_page(qt)
    window.close()
    starts, stops = len(bridge.decider_starts), bridge.decider_stops
    calls = len(bridge.calls)
    # The two teardown shapes leave a wrapper with no instance
    # attributes — the same state the guards answer.
    del page.back_button
    assert page.showEvent(qt.QtGui.QShowEvent()) is None
    assert len(bridge.calls) == calls  # no refresh
    assert len(bridge.decider_starts) == starts  # no decider ask
    assert page.hideEvent(qt.QtGui.QHideEvent()) is None
    assert bridge.decider_stops == stops + 1  # the retire alone


def test_an_unchanged_answer_keeps_the_mounts_held_row(qt) -> None:
    """An answer that changes nothing costs the Mounts tab's
    selection nothing (#945, the listing's #470 L2 rule): a
    hold on the table survives a refresh whose row reads
    exactly as the last."""
    page, window, bridge = make_page(qt)
    land(qt, page, bridge)
    page.mounts_tab.table.selectRow(1)
    land(qt, page, bridge)
    assert page.mounts_tab.table.selectionModel().selectedRows() == [
        page.mounts_tab.table.model().index(1, 0)
    ]


def test_the_removed_workspace_closes_its_page(qt) -> None:
    """A listing that cannot see the workspace (#945, the TUI's
    close_removed shape): the page emits, the list returns with
    the notice on its status line."""
    shell, window, bridge = make_shell(qt)
    # An outage standing on the list resolves behind the notice —
    # the row's read is a daemon answer — and the listing's own
    # read lands without the recovery's words over it (#1129,
    # #776's mask).
    shell.listing.on_failed(
        "workspaces-list",
        clientdata.RestFailure("daemon away", reach=True),
    )
    shell.listing.opened.emit("ws-a")
    shell.detail.on_succeeded("detail-row", [other_row()])
    assert shell.currentWidget() is shell.listing
    assert "alpha removed" in shell.listing.status.text()
    assert not shell.listing.daemon_down
    shell.listing.on_succeeded("workspaces-list", [daemon_row()])
    assert "alpha removed" in shell.listing.status.text()  # the notice stood


def test_opening_resets_the_page(qt) -> None:
    """A second workspace's open resets the page (#945): the
    row and the tabs all start fresh — the first workspace's
    tail cannot leak onto the second's page."""
    page, window, bridge = make_page(qt)
    land(qt, page, bridge)
    page.push_frame({"event": "workspace.status", "data": {"id": "ws-a"}})
    page.open("ws-b")  # the open resets the row to Shells
    assert page.footer.text() == ""
    assert page.events_tab.rows.count() == 0
    assert not page.events_tab.empty.isHidden()
    assert ("detail-row", "workspaces", ()) in bridge.calls


def test_the_answers_land_in_either_order(qt) -> None:
    """The two flights land independently (#945): a capacity
    report before the row renders nothing (there is nothing to
    render against yet), and the row's own landing renders the
    footer with the report already standing — the storage fact
    reads on the first render, no dash between."""
    page, window, bridge = make_page(qt)
    page.on_succeeded(
        qt.page_mod.STORAGE_KEY,
        {
            "workspaces": [
                {"id": "ws-a", "root_bytes": 10737418240, "home_bytes": 1}
            ]
        },
    )
    assert page.footer.text() == ""
    page.on_succeeded(qt.page_mod.ROW_KEY, [daemon_row()])
    assert "storage 10G" in page.footer.text()


def test_the_open_signal_guards_a_rowless_index(qt) -> None:
    """The open seam's guard (#945): an activated index with no
    name item under it — an index Qt hands over a table mid-
    rebuild — opens nothing."""
    shell, window, bridge = make_shell(qt)
    opened = []
    shell.listing.opened.connect(opened.append)
    shell.listing.open_current(qt.QtCore.QModelIndex())
    assert opened == []


def test_a_storage_answer_resolves_the_standing_outage(qt) -> None:
    """The outage tick's real shape (#945, #717, #904): the row
    read fails — the daemon answered nothing — and the outage
    flash stands until one of the page's own answers resolves
    it. The health read's best-effort silence is gone with the
    read itself (#1017): the storage read's answer resolves
    the outage, the row's own landing beside it."""
    page, window, bridge = make_page(qt)
    land(qt, page, bridge)
    page.on_failed(
        qt.page_mod.ROW_KEY,
        clientdata.RestFailure("the daemon is gone", reach=True),
    )
    assert page.status.text() == "the daemon is gone"
    page.on_succeeded(qt.page_mod.STORAGE_KEY, {"workspaces": []})
    assert page.status.text() == "daemon back — page resumed"  # #1129's tell


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
    """One pushed frame (#946, #1019): the listing re-reads
    only on a listing-affecting frame — the standing tick the
    frames replace — and the page's own row filter decides its
    re-read whatever screen owns the window."""
    shell, window, bridge = make_shell(qt)
    bridge.calls.clear()
    shell.frame_landed({"event": "workspace.status", "data": {"id": "ws-a"}})
    # The list owns the window: its refresh flew.
    assert ("workspaces-list", "workspaces", ()) in bridge.calls
    assert ("detail-row", "workspaces", ()) not in bridge.calls
    # No workspace is open on the page: the frame names none it
    # holds, so nothing lands on its tabs.
    assert shell.detail.events_tab.rows.count() == 0
    # A frame no listing reads is not the listing's read
    # (#1019): the secret and hold families paint through their
    # own surfaces.
    bridge.succeeded.emit("workspaces-list", [])
    bridge.calls.clear()
    shell.frame_landed({"event": "secret.minted", "data": {}})
    assert bridge.calls == []
    shell.listing.opened.emit("ws-a")
    land(qt, shell.detail, bridge)  # the open's own flights answered
    bridge.calls.clear()
    shell.frame_landed({"event": "workspace.status", "data": {"id": "ws-a"}})
    assert ("detail-row", "workspaces", ()) in bridge.calls
    # The page takes its own workspace's frame: its events tab
    # is the frame's own record.
    assert shell.detail.events_tab.rows.count() == 1
    # A busy sibling cannot crowd it (#946): the page takes
    # only this workspace's frames.
    for _ in range(150):
        shell.frame_landed(
            {"event": "workspace.status", "data": {"id": "ws-b"}}
        )
    assert shell.detail.events_tab.rows.count() == 1


def test_a_catalog_move_re_reads_the_open_row(qt) -> None:
    """A catalog move (#1019): ``image.changed`` names no
    workspace, but it moves every row's image cell — the open
    page re-reads its row, the listing stays quiet while the
    page owns the window."""
    shell, window, bridge = make_shell(qt)
    shell.listing.opened.emit("ws-a")
    land(qt, shell.detail, bridge)  # the open's own flights answered
    bridge.calls.clear()
    shell.frame_landed({"event": "image.changed", "data": {}})
    assert ("detail-row", "workspaces", ()) in bridge.calls
    assert ("workspaces-list", "workspaces", ()) not in bridge.calls


def test_a_serving_feed_re_reads_the_current_screen(qt) -> None:
    """The feed's serve poke (#1019): every connection that
    comes to serve refreshes the screen the operator sees —
    first connection included — because frames published while
    no connection stood never landed. The listing answers
    while it owns the window; the page answers once Back
    swapped it."""
    shell, window, bridge = make_shell(qt)
    bridge.succeeded.emit("workspaces-list", [])
    bridge.calls.clear()
    bridge.feed_served.emit()
    assert bridge.calls == [("workspaces-list", "workspaces", ())]
    bridge.succeeded.emit("workspaces-list", [])
    shell.listing.opened.emit("ws-a")
    land(qt, shell.detail, bridge)
    bridge.calls.clear()
    bridge.feed_served.emit()
    assert ("detail-row", "workspaces", ()) in bridge.calls


def test_a_late_daemon_recovers_the_listing_on_the_feed_connection(
    qt,
) -> None:
    """The window opened against a daemon that was down (#1129):
    the listing's opening read failed as an outage, and the
    message stands on an empty table. While the daemon is down
    the feed's heartbeat flashes over the outage on every failed
    dial (#946) — the outage's text returns when each flash's
    span ends, the daemon-down state the screen's own and not
    the flash's. The daemon comes up and the feed connects — no
    frame yet, an idle daemon publishes none — and the
    connection's serve poke re-reads the listing, whose answer
    fills the row and says the recovery once. The poke waits for
    no frame (#1129 over #1019): the feed's own tests pin the
    connection's entry as its moment."""
    shell, window, bridge = make_shell(qt, rows=[])
    bridge.failed.emit(
        "workspaces-list",
        clientdata.RestFailure("msks: the daemon is unreachable", reach=True),
    )
    assert "unreachable" in shell.listing.status.text()
    assert shell.listing.table.rowCount() == 0
    # The heartbeat: a failed dial's flash runs its own span over
    # the standing outage, whose text takes the line back.
    bridge.feed_dropped.emit("the dial failed: connection refused")
    heartbeat = shell.listing.status.text()
    assert "the dial failed: connection refused" in heartbeat
    shell.listing.clear_stale(shell.listing.flash_seq)  # the span ends
    assert "unreachable" in shell.listing.status.text()  # the outage stood
    bridge.calls.clear()
    bridge.feed_served.emit()  # the connection's poke, no frame
    assert bridge.calls == [("workspaces-list", "workspaces", ())]
    bridge.succeeded.emit("workspaces-list", [daemon_row()])
    # The answer resolves the outage with the recovery's one
    # flash (#1129), and the row stands on the table.
    assert shell.listing.status.text() == "daemon back — listing resumed"
    assert shell.listing.table.rowCount() == 1


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
    # The feed's flash runs its own span over the screens'
    # daemon-down state, which it does not set: a REST answer
    # must not read as the feed's recovery, and the state is the
    # screen's own, not the flash's (#1129).
    assert not shell.listing.daemon_down
    shell.listing.on_succeeded("workspaces-list", [])
    assert shell.listing.status.text() == (
        "the events feed dropped — the socket broke"
    )
    shell.feed_back()
    assert shell.listing.status.text() == "the events feed reconnected"
    assert not shell.listing.daemon_down
    # The page flashes its own copy when it owns the window.
    shell.listing.opened.emit("ws-a")
    shell.feed_dropped("the dial failed")
    assert shell.detail.status.text() == (
        "the events feed dropped — the dial failed"
    )


def test_a_refresh_in_flight_is_carried(qt) -> None:
    """The in-flight guard carries the ask (#945, #470's one
    flight at a time, #1129's carry — the TUI listing's own arm
    over #1019): a refresh asked while the row read is out adds
    no second request, and the moment no read stands the
    deferred refresh goes out whole — the row read and the
    storage read again, the tabs' own pokes beside them. Not
    before: one read landing while the other stands serves
    nothing yet. Another key's answer is not this page's to
    take."""
    page, window, bridge = make_page(qt)
    land(qt, page, bridge)
    # The tabs' own reads land first, so the page's flights below
    # are the row's and the storage's alone.
    bridge.succeeded.emit(page.proxies_tab.list_key, [])
    bridge.succeeded.emit(
        page.egress_tab.list_key,
        {
            "workspace_id": page.workspace_id,
            "mode": "interactive",
            "allow_list": [],
            "allowed": [],
            "denied": [],
        },
    )
    row = (qt.page_mod.ROW_KEY, "workspaces", ())
    storage = (qt.page_mod.STORAGE_KEY, "storage", ())
    reads = [c for c in bridge.calls if c[0] in (row[0], storage[0])]
    assert reads == []
    page.refresh()  # both reads out
    page.refresh()  # carried: no second request
    reads = [c for c in bridge.calls if c[0] in (row[0], storage[0])]
    assert reads == [row, storage]
    page.on_succeeded(qt.page_mod.ROW_KEY, [daemon_row()])
    reads = [c for c in bridge.calls if c[0] in (row[0], storage[0])]
    assert reads == [row, storage]  # the storage read still stands
    page.on_succeeded(qt.page_mod.STORAGE_KEY, {"workspaces": []})
    reads = [c for c in bridge.calls if c[0] in (row[0], storage[0])]
    assert reads == [row, storage, row, storage]  # no read stands: served
    bridge.calls.clear()
    page.on_succeeded(qt.page_mod.ROW_KEY, [daemon_row()])
    page.on_succeeded(qt.page_mod.STORAGE_KEY, {"workspaces": []})
    assert bridge.calls == []  # nothing carried, nothing asked
    # Another key's answer is not this page's to take.
    page.on_succeeded("workspaces-list", [])
    page.on_failed("workspaces-list", clientdata.RestFailure("x", reach=False))
    assert page.status.text() == ""


def gui_lost_frame(ws: str = "ws-a") -> dict:
    """The failure the daemon publishes when the last holder went
    (#1127): one frame per forward row, the missing-holder cause
    riding the reason."""
    return {
        "event": "proxy.failed",
        "data": {
            "id": ws,
            "proxy": 7,
            "reason": (
                "no client holds the bridge — start the workspace "
                "from a client (the holder it leaves serves after "
                "the command exits), or run 'msks proxy hold "
                "<workspace>'"
            ),
        },
    }


def gui_failed_rows() -> list[dict]:
    """One workspace's forward rows as a failing read serves them
    (#1127)."""
    return [
        {
            "id": 7,
            "workspace_id": "ws-a",
            "direction": "forward",
            "listen_port": 8080,
            "target_port": 80,
            "status": "failed",
            "reason": gui_lost_frame()["data"]["reason"],
        }
    ]


def test_the_shell_reholds_a_lost_bridge(qt) -> None:
    """#1127: a holder's death publishes its rows' failure; the
    shell's watch reads the rows over the bridge, spawns the
    detached holder, and the listener that follows says the
    recovery once on the screen the operator sees."""
    shell, window, bridge = make_shell(qt)
    bridge.calls.clear()
    shell.frame_landed(gui_lost_frame())
    # The burst one failure publishes per row lands as one read.
    shell.frame_landed(gui_lost_frame())
    reads = [call for call in bridge.calls if call[0].startswith("rehold:")]
    assert reads == [("rehold:ws-a", "workspace_proxies", ("ws-a",))]
    bridge.succeeded.emit("rehold:ws-a", gui_failed_rows())
    assert bridge.holder_spawns == ["ws-a"]
    # A listener that answers no spawn of this session says
    # nothing; the one that answers the spawn says it once.
    assert shell.listing.status.text() == ""
    shell.frame_landed({"event": "proxy.listening", "data": {"id": "ws-b"}})
    assert shell.listing.status.text() == ""
    shell.frame_landed({"event": "proxy.listening", "data": {"id": "ws-a"}})
    assert "bridge re-held" in shell.listing.status.text()
    assert "alpha" in shell.listing.status.text()
    shell.frame_landed({"event": "proxy.listening", "data": {"id": "ws-a"}})
    assert shell.listing.status.text().count("bridge re-held") == 1


def force_rehold_windows(bridge) -> None:
    """Move the stub's real-clock re-hold windows past (#1127):
    the drain's ruling then reads exactly as a lapsed window
    would on the wall clock."""
    bridge.reholder.window_until.clear()
    bridge.reholder.awaited.clear()


def running_frame(ws: str = "ws-a") -> dict:
    """The watcher's running transition (#1127, #1198)."""
    return {
        "event": "workspace.status",
        "data": {"id": ws, "status": "running"},
    }


def rehold_reads(bridge) -> list[tuple]:
    """The rows reads the re-hold watch asked of the stub
    bridge, in order."""
    return [call for call in bridge.calls if call[0].startswith("rehold:")]


def test_the_shell_reholds_a_start_that_left_no_holder(qt) -> None:
    """#1127, #1198: a start over the raw API leaves no holder;
    the running transition it publishes opens the watch's
    window, the poll's drain weighs the rows once it closes, a
    failure over the spawn it made defers to the drain again,
    and a standing holder's listening rows spawn nothing."""
    shell, window, bridge = make_shell(qt)
    bridge.calls.clear()
    shell.frame_landed(running_frame())
    # The boot's window stands (#1198): no read yet, the holder
    # a booting client's start path leaves gets its time.
    assert rehold_reads(bridge) == []
    force_rehold_windows(bridge)
    shell.drain_rehold()
    assert rehold_reads(bridge) == [
        ("rehold:ws-a", "workspace_proxies", ("ws-a",))
    ]
    bridge.succeeded.emit("rehold:ws-a", gui_failed_rows())
    assert bridge.holder_spawns == ["ws-a"]
    # A failure over the spawn's outcome window defers: no
    # second read until the poll's drain re-rules it.
    shell.frame_landed(gui_lost_frame())
    assert len(rehold_reads(bridge)) == 1
    force_rehold_windows(bridge)
    shell.drain_rehold()
    assert len(rehold_reads(bridge)) == 2
    listening = [
        {**gui_failed_rows()[0], "status": "listening", "reason": None}
    ]
    bridge.succeeded.emit("rehold:ws-a", listening)
    assert bridge.holder_spawns == ["ws-a"]


def test_the_shell_stacks_no_holder_on_a_boot_another_client_makes(
    qt,
) -> None:
    """#1198: a shell the GUI opens boots a stopped workspace
    through a session child, whose own start path leaves the
    holder. The running frame opens the watch's window and reads
    nothing on its own; the holder's own listener ends the wait,
    so the watch reads no rows, spawns nothing, and spends
    nothing."""
    shell, window, bridge = make_shell(qt)
    bridge.calls.clear()
    # The boot's binds publish their listeners before the running
    # word: they answer no spawn of this session.
    shell.frame_landed(
        {"event": "proxy.listening", "data": {"id": "ws-a", "proxy": 7}}
    )
    shell.frame_landed(running_frame())
    assert rehold_reads(bridge) == []
    assert bridge.reholder.pending == {"ws-a"}
    # The booting client's holder registers: the hub's listener
    # resolves the deferred running signal.
    shell.frame_landed(
        {"event": "proxy.listening", "data": {"id": "ws-a", "proxy": 7}}
    )
    assert bridge.reholder.pending == set()
    force_rehold_windows(bridge)
    shell.drain_rehold()
    assert rehold_reads(bridge) == []
    assert bridge.holder_spawns == []
    assert bridge.reholder.spent == {}
    # No spawn of this session: the listener says no recovery.
    assert "bridge" not in shell.listing.status.text()


def test_the_shell_reads_the_rows_once_when_the_boots_listener_is_missed(
    qt,
) -> None:
    """#1198: a running frame whose holder's listener never
    reached this client is read when the window closes, and rows
    that then read listening spawn nothing and spend nothing."""
    shell, window, bridge = make_shell(qt)
    bridge.calls.clear()
    shell.frame_landed(running_frame())
    assert rehold_reads(bridge) == []
    assert bridge.holder_spawns == []
    force_rehold_windows(bridge)
    shell.drain_rehold()
    assert rehold_reads(bridge) == [
        ("rehold:ws-a", "workspace_proxies", ("ws-a",))
    ]
    listening = [
        {**gui_failed_rows()[0], "status": "listening", "reason": None}
    ]
    bridge.succeeded.emit("rehold:ws-a", listening)
    assert bridge.holder_spawns == []
    assert bridge.reholder.spent == {}
    assert "bridge" not in shell.listing.status.text()


def test_the_shell_names_the_watchs_give_up(qt) -> None:
    """#1127: the failure that lands after the session's last
    spawn meets the give-up line once the outcome window lapses
    — the drain carries it, the row's cause stands, and the
    failures after it stay quiet."""
    shell, window, bridge = make_shell(qt)
    # The clock moves before each spend, so the last spawn's
    # outcome window stands after the loop.
    for _ in range(ReHolder.MAX_TRIES):
        force_rehold_windows(bridge)
        assert bridge.reholder.rehold("ws-a", gui_failed_rows()) is True
    shell.frame_landed(gui_lost_frame())
    # The last spawn's outcome window stands: the failure
    # defers, and the line waits for the poll's drain.
    assert shell.listing.status.text() == ""
    force_rehold_windows(bridge)
    shell.drain_rehold()
    text = shell.listing.status.text()
    assert "bridge not re-held" in text
    assert "msks proxy hold" in text


def test_the_shells_timer_drains_the_deferrals(qt) -> None:
    """#1127: the poll's own path — a deferral whose windows
    closed re-rules through the timer, not by hand: the failure
    the window swallowed takes its rows read once the shell's
    clockwork fires, and a second spawn lands."""
    shell, window, bridge = make_shell(qt)
    bridge.calls.clear()
    shell.frame_landed(gui_lost_frame())
    assert ("rehold:ws-a", "workspace_proxies", ("ws-a",)) in bridge.calls
    bridge.succeeded.emit("rehold:ws-a", gui_failed_rows())
    assert bridge.holder_spawns == ["ws-a"]
    # The crash-loop death lands inside the window: deferred.
    shell.frame_landed(gui_lost_frame())
    force_rehold_windows(bridge)
    # The timer alone drives the drain from here.
    shell.rehold_timer.setInterval(10)
    shell.rehold_timer.start(10)
    deadline = time.monotonic() + 10.0
    answered = 0
    while time.monotonic() < deadline and len(bridge.holder_spawns) < 2:
        qt.app.processEvents()
        time.sleep(0.01)
        reads = [
            call for call in bridge.calls if call[0].startswith("rehold:")
        ]
        while answered < len(reads):
            # Answer each drained read the way the daemon would.
            key = reads[answered][0]
            bridge.succeeded.emit(key, gui_failed_rows())
            answered += 1
    assert bridge.holder_spawns == ["ws-a", "ws-a"]
