"""The workspace page's terminal-only mode (#1152): the toggle
on the chrome row hides the page's controls and asks the
window's strip to hide its own bar, the corner over the shell
tabs carries the muted workspace name, the status line's seat,
and the restore button, and every path off the page brings the
chrome back. The widgets run under the offscreen platform on
every host (#945's fixture shape); the page's slots are driven
directly over the stub bridge, the page's own suite's shape.
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
        "PySide6 is not installed on this host",
        allow_module_level=True,
    )

from msks.client import ages as ages_mod  # noqa: E402


@pytest.fixture(autouse=True)
def pinned_clock(monkeypatch: pytest.MonkeyPatch) -> None:
    """The created line reads one fixed clock (the shared leaf's
    seam, #939's fixture shape)."""
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
    """The bridge's surface for the page's tests: the signals
    the page reads and the one verb it calls, every call
    recorded — no worker, no network."""

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
        self.decider_starts: list[str] = []
        self.decider_stops = 0

    def call(self, key: str, method: str, *args: object) -> None:
        self.calls.append((key, method, args))

    def start_decider(self, workspace_id: str, controller=None) -> None:
        self.decider_starts.append(workspace_id)

    def stop_decider(self) -> None:
        self.decider_stops += 1


def daemon_row(name: str = "alpha", workspace_id: str = "ws-a") -> dict:
    """The workspace the page reads: one running row."""
    return {
        "id": workspace_id,
        "name": name,
        "status": "running",
        "image_ref": "msks/fedora:41",
        "created_at": "2026-01-02T12:00:00",
    }


#: The argv the sleeper hubs spawn (#974): a child that stands
#: until the close path takes it down.
SLEEPER = "import time; time.sleep(60)"


@pytest.fixture()
def qt(tmp_path: Path):
    """The offscreen ``QApplication`` the page needs, the list's
    own fixture shape: the silent fontconfig, the platform pin,
    and the Qt names imported inside the fixture (a
    collection-time controller has no business loading the
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
    # The sleeper hubs the helpers build (#974) own a child by
    # teardown unless the test closed it; the windows stand
    # until the hubs are down, then close and delete (#959's
    # teardown: nothing C++-side outlives the test).
    for hub in ns.hubs:
        hub.shutdown()
    del ns.keep[:]
    for widget in app.topLevelWidgets():
        widget.close()
        widget.deleteLater()
    QtCore.QCoreApplication.sendPostedEvents(
        None, QtCore.QEvent.Type.DeferredDelete
    )
    app.processEvents()


def make_hub(qt):
    """A hub spawning sleeper children, shut down at teardown."""
    hub = qt.page_mod.ShellHub(lambda ws: [sys.executable, "-c", SLEEPER])
    qt.hubs.append(hub)
    return hub


def make_page(qt) -> tuple:
    """The page alone over the stub bridge, shown in a real
    window and opened on the workspace — the page's own suite's
    shape: a first shell taken (#974), the row one answer
    away."""
    bridge = StubBridge()
    page = qt.page_mod.WorkspaceDetail(bridge, make_hub(qt))
    window = qt.QtWidgets.QMainWindow()
    window.setCentralWidget(page)
    window.show()
    qt.keep.append(window)
    page.open("ws-a", "alpha")
    bridge.calls.clear()
    return page, window, bridge


def make_strip(qt) -> tuple:
    """The shell under the main strip, in a real window — the
    window path's composition (#962, #1152): the page's ask
    wired to the strip's own hide, the listing answered."""
    from msks.client.gui.tabs import MainTabs

    bridge = StubBridge()
    shell = qt.page_mod.WorkspaceShell(bridge, None, make_hub(qt))
    tabs = MainTabs(shell)
    shell.detail.chrome_hidden.connect(tabs.set_bar_hidden)
    window = qt.QtWidgets.QMainWindow()
    window.setCentralWidget(tabs)
    window.show()
    qt.keep.append(window)
    shell.listing.on_succeeded("workspaces-list", [daemon_row()])
    return shell, tabs, window


def asks(page) -> list[bool]:
    """The page's asks so far, through a recording slot the test
    connects itself."""
    heard: list[bool] = []
    page.chrome_hidden.connect(heard.append)
    return heard


def enter(page) -> None:
    """The toggle's click: the mode on."""
    page.terminal_only.toggle.click()


def land(qt, page, bridge, row=None) -> None:
    """The page's answers landed: the row and the capacity
    report."""
    bridge.calls.clear()
    page.on_succeeded(
        qt.page_mod.ROW_KEY,
        [daemon_row() if row is None else row],
    )
    page.on_succeeded(qt.page_mod.STORAGE_KEY, {"workspaces": []})
    bridge.calls.clear()


def test_the_toggle_joins_the_chrome_row(qt) -> None:
    """The toggle on the page's top row (#1152), before the
    external-terminal button — the rightmost control keeps its
    corner — with its tooltip and accessible name; the corner
    over the shell tabs stands hidden until the mode asks, and
    the page's status label is the chrome row's own."""
    page, _window, _bridge = make_page(qt)
    mode = page.terminal_only
    assert page.chrome.count() == 5
    assert page.chrome.itemAt(3).widget() is mode.toggle
    assert page.chrome.itemAt(4).widget() is page.external_button
    assert mode.toggle.toolTip() == "Show only the shell tabs and the terminal"
    assert mode.toggle.accessibleName() == "Terminal only"
    assert mode.restore.toolTip() == (
        "Show the workspace page's controls again"
    )
    assert mode.restore.accessibleName() == "Show the page controls"
    assert (
        page.shells.cornerWidget(qt.QtCore.Qt.Corner.TopRightCorner)
        is mode.corner
    )
    assert mode.corner.isHidden()
    assert not mode.on
    assert page.status is mode.chrome_status
    assert page.escape.isEnabled()


def test_entering_hides_the_chrome_and_shows_the_corner(qt) -> None:
    """The toggle's action (#1152): the Shells tab shows when
    another page tab stood there, the chrome row, the footer,
    and the Back shortcut hide, the strip hears the ask, and the
    corner stands at the tab row's right end — the workspace's
    name in the muted gray, the restore button — while the
    terminal takes the keyboard."""
    page, window, bridge = make_page(qt)
    mode = page.terminal_only
    land(qt, page, bridge)
    page.hub.open_shell("ws-a")
    page.bar.setCurrentIndex(page.bar.count() - 1)  # Events
    qt.app.processEvents()
    before = page.stack.geometry()
    heard = asks(page)
    enter(page)
    assert mode.on
    assert heard == [True]
    assert page.stack.currentWidget() is page.shells_tab
    assert page.bar.currentIndex() == 0
    for widget in (
        page.back_button,
        page.bar,
        mode.chrome_status,
        page.external_button,
        mode.toggle,
    ):
        assert widget.isHidden()
    assert page.footer.isHidden()
    assert not page.escape.isEnabled()
    # The hidden row and footer leave their rows to the
    # terminals: the stack rises to the page's own margin and
    # grows by what they held.
    qt.app.processEvents()
    after = page.stack.geometry()
    assert after.top() < before.top()
    assert after.height() > before.height()
    assert not mode.corner.isHidden()
    assert mode.name.text() == "alpha"
    assert mode.name.foregroundRole() == (
        qt.QtGui.QPalette.ColorRole.PlaceholderText
    )
    assert not mode.restore.isHidden()
    # The terminal takes the keyboard (#1152): the page's own
    # anchor for a showing Shells tab.
    assert window.focusWidget() is page.shells.currentWidget()


def test_a_standing_ask_changes_nothing(qt) -> None:
    """The switch is idempotent (#1152): a second enter while
    the mode stands hides nothing twice and asks the strip
    once; a second leave after it ended asks nothing."""
    page, _window, _bridge = make_page(qt)
    mode = page.terminal_only
    heard = asks(page)
    enter(page)
    enter(page)
    assert heard == [True]
    mode.restore.click()
    mode.leave()
    assert heard == [True, False]


def test_the_status_line_takes_the_corner_seat(qt) -> None:
    """The flash keeps flashing in the mode (#1152): the
    standing text comes along to the corner's label, a flash or
    an outage writes there, the span's timeout clears whichever
    label stands, and the text returns to the chrome row's
    label at the leave."""
    page, _window, _bridge = make_page(qt)
    mode = page.terminal_only
    page.flash("before the mode")
    enter(page)
    assert page.status is mode.status
    assert mode.status.text() == "before the mode"
    page.flash("in the mode")
    assert mode.status.text() == "in the mode"
    assert mode.chrome_status.text() == "before the mode"
    seq = page.flash_seq
    page.clear_stale(seq)
    assert mode.status.text() == ""
    # An outage stands for an answer (#904): on the corner's
    # label while the mode stands, and back on the chrome's
    # afterwards with the daemon-down state still its own.
    page.refuse("daemon down", reach=True)
    assert mode.status.text() == "daemon down"
    mode.leave()
    assert page.status is mode.chrome_status
    assert mode.chrome_status.text() == "daemon down"
    assert page.daemon_down
    # A flash whose span ends after the leave clears the label
    # that stands then — the chrome row's, to the standing
    # outage's text (#1129) — never the hidden corner's.
    enter(page)
    page.flash("short")
    mode.leave()
    page.clear_stale(page.flash_seq)
    assert mode.chrome_status.text() == "daemon down"
    assert mode.status.text() == "short"
    page.resolve()
    enter(page)
    page.flash("gone")
    mode.leave()
    page.clear_stale(page.flash_seq)
    assert mode.chrome_status.text() == ""


def test_restoring_shows_the_chrome_back_and_keeps_the_shells(
    qt,
) -> None:
    """The restore button's action (#1152): every hidden control
    shows again, the Back shortcut arms, the corner goes, the
    strip hears the mode ended — and the current shell tab and
    every session stand as they were."""
    page, _window, _bridge = make_page(qt)
    mode = page.terminal_only
    page.hub.open_shell("ws-a")
    second = page.shells.currentWidget()
    enter(page)
    heard = asks(page)
    mode.restore.click()
    assert not mode.on
    assert heard == [False]
    for widget in (
        page.back_button,
        page.bar,
        mode.chrome_status,
        page.external_button,
        mode.toggle,
    ):
        assert not widget.isHidden()
    assert not page.footer.isHidden()
    assert page.escape.isEnabled()
    assert mode.corner.isHidden()
    assert page.shells.currentWidget() is second
    assert len(page.hub.shells("ws-a")) == 2


def test_the_hide_ends_the_mode_but_a_minimize_keeps_it(qt) -> None:
    """The mode ends at the page's own hide (#1152) — whatever
    hid the page, the chrome is back before another screen
    shows — while a spontaneous hide (the window minimized)
    leaves the mode standing for the window the operator
    restores."""
    page, window, _bridge = make_page(qt)
    mode = page.terminal_only
    enter(page)
    heard = asks(page)
    # A minimized window hands its children spontaneous hide
    # events — the platform's own, not Qt's show and hide.
    window.showMinimized()
    qt.app.processEvents()
    assert mode.on
    assert heard == []
    window.showNormal()
    qt.app.processEvents()
    page.hide()
    assert not mode.on
    assert heard == [False]
    assert not page.bar.isHidden()
    page.show()


def test_a_wrapper_past_construction_hides_nothing(qt) -> None:
    """The hide handler tolerates the two half-built shapes Qt
    can deliver events to (#959): a wrapper without the mode
    answers nothing, and one with the mode but without its
    chrome leaves a mode that never began."""
    page, _window, _bridge = make_page(qt)
    del page.back_button
    page.hideEvent(qt.QtGui.QHideEvent())
    page.close()
    del page.terminal_only
    page.hideEvent(qt.QtGui.QHideEvent())


def test_a_leaving_page_shows_the_listing_under_the_strip(qt) -> None:
    """Every path off the page restores the chrome first
    (#1152): Back's return and the removed workspace's both
    end the mode before the listing shows, so the list never
    stands under a hidden strip."""
    shell, tabs, _window = make_strip(qt)
    page = shell.detail
    heard = asks(page)
    shell.open_detail("ws-a")
    enter(page)
    assert tabs.tabBar().isHidden()
    shell.show_listing()
    assert shell.currentWidget() is shell.listing
    assert not page.terminal_only.on
    assert not tabs.tabBar().isHidden()
    assert heard == [True, False]
    shell.open_detail("ws-a")
    enter(page)
    page.on_succeeded(qt.page_mod.ROW_KEY, [])  # the workspace gone
    assert shell.currentWidget() is shell.listing
    assert not page.terminal_only.on
    assert not tabs.tabBar().isHidden()
    assert heard == [True, False, True, False]


def test_the_strip_hides_its_bar_and_the_page_fills(qt) -> None:
    """The strip's own hide (#1152): the ask hides the bar and
    the page grows into its row — the tabbed terminal app's
    shape — with the current top-level tab unchanged, and the
    ended mode gives the bar its row back."""
    from msks.client.gui.tabs import IMAGES_LABEL

    shell, tabs, _window = make_strip(qt)
    tabs.addTab(qt.QtWidgets.QWidget(), IMAGES_LABEL)
    qt.app.processEvents()
    shell.open_detail("ws-a")
    qt.app.processEvents()
    before = shell.geometry()
    enter(shell.detail)
    qt.app.processEvents()
    assert tabs.tabBar().isHidden()
    assert tabs.currentIndex() == 0
    after = shell.geometry()
    assert after.height() > before.height()
    assert after.top() <= before.top()
    shell.detail.terminal_only.restore.click()
    qt.app.processEvents()
    assert not tabs.tabBar().isHidden()
    assert shell.geometry() == before


def test_the_name_follows_the_stand_in_and_the_row(qt) -> None:
    """The corner's name (#1152) is the page's own label: the
    list's stand-in at the open, the id when the list holds
    none, and the row's own name once its answer lands — a
    rename reads on the corner at once. A long name elides in
    the middle with the whole name in the tooltip; a short one
    carries no tooltip."""
    page, _window, bridge = make_page(qt)
    mode = page.terminal_only
    assert mode.name.text() == "alpha"
    page.open("ws-b")
    assert mode.name.text() == "ws-b"
    land(qt, page, bridge, daemon_row("beta", "ws-b"))
    assert mode.name.text() == "beta"
    assert page.label == "beta"
    assert mode.name.toolTip() == ""
    long_name = "n" * 80
    land(qt, page, bridge, daemon_row(long_name, "ws-b"))
    assert "\u2026" in mode.name.text()
    assert mode.name.text() != long_name
    assert mode.name.toolTip() == long_name


def test_the_keyboard_reaches_the_restore_button(qt) -> None:
    """The restore button is a tab stop on the shell tab row
    (#1152): Tab from the shell tab bar reaches it through the
    page's chain, and Space on it restores the chrome — the
    keyboard's own way back once the pointer is not on the
    row."""
    from PySide6.QtTest import QTest

    page, window, _bridge = make_page(qt)
    mode = page.terminal_only
    enter(page)
    bar = page.shells.tabBar()
    bar.setFocus()
    for _ in range(8):
        QTest.keyClick(window.focusWidget(), qt.QtCore.Qt.Key.Key_Tab)
        if window.focusWidget() is mode.restore:
            break
    assert window.focusWidget() is mode.restore
    QTest.keyClick(mode.restore, qt.QtCore.Qt.Key.Key_Space)
    assert not mode.on
    assert not page.bar.isHidden()


def test_the_consent_panel_keeps_its_place(qt) -> None:
    """A waiting egress hold still shows its panel above the
    terminals in the mode (#1152): the verdict's countdown must
    not hide with the chrome."""
    from test_gui_consent import request_raw

    page, _window, _bridge = make_page(qt)
    page.consent.controller.apply_frame(request_raw())
    enter(page)
    panel = page.shells_tab.consent_panel
    panel.refresh()
    qt.app.processEvents()
    assert not panel.isHidden()
    assert panel.geometry().bottom() <= page.shells.geometry().top()
