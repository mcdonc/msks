"""The shell-tab tests (#954, #974): the hub that owns a
workspace's terminal sessions, the tab strip that hosts them
inside the page's Shells tab, the window shell's shell and
external-terminal actions — offscreen, over real sleeper
children through the hub's injected argv factory (the widget
suite's own pattern) — and the tab titles the program inside
sets and the keys that drive the strip (#1163), pressed through
Qt's own event path on a focused terminal."""

import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from test_gui_consentpanel import FakePresenter
from test_gui_workspace import SLEEPER, StubBridge, daemon_row

# The hub's pty children spawn through ``fork()`` on Qt's own
# threads (termsession's pty design, #951); Python 3.14 warns
# on the multi-threaded fork by design of the spawn, and the
# warning is the suite's own noise, not a defect to fix here.
pytestmark = pytest.mark.filterwarnings(
    "ignore:This process.*multi-threaded.*:DeprecationWarning"
)


@pytest.fixture()
def qt(tmp_path: Path):
    """The offscreen ``QApplication``, the screens' own fixture
    shape: the platform pin, the Qt names, and the shells module
    imported inside the fixture."""
    fonts = tmp_path / "fonts.conf"
    fonts.write_text("<fontconfig/>\n")
    os.environ["FONTCONFIG_FILE"] = str(fonts)
    os.environ["QT_QPA_PLATFORM"] = "offscreen"
    from msks.client.gui import shells as shells_mod
    from msks.client.gui.workspace import WorkspaceShell
    from PySide6 import QtCore, QtGui, QtWidgets

    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication(
        ["msks-test"]
    )
    yield SimpleNamespace(
        app=app,
        QtCore=QtCore,
        QtGui=QtGui,
        QtWidgets=QtWidgets,
        shells=shells_mod,
        shell_class=WorkspaceShell,
    )
    for widget in app.topLevelWidgets():
        widget.close()
        widget.deleteLater()
    QtCore.QCoreApplication.sendPostedEvents(
        None, QtCore.QEvent.Type.DeferredDelete
    )
    app.processEvents()


@pytest.fixture()
def hub(qt):
    """The hub over sleeper children — every test's sessions
    close at teardown, whatever the test itself closed."""
    made = qt.shells.ShellHub(lambda ws: [sys.executable, "-c", SLEEPER])
    yield made
    made.shutdown()


@pytest.fixture()
def tabs(qt, hub):
    """The strip alone, before any host."""
    made = qt.shells.ShellTabs(hub)
    yield made


@pytest.fixture()
def window_shell(qt):
    """The window shell over the stub bridge, in a real window
    with the listing answered — the workspace-page suite's own
    shape — over a sleeper hub. The window comes with the
    shell: the widgets live only while a Python hand holds
    their top-level window, and the hub shuts down at teardown
    (a window close never reaches the terminals, and
    aboutToQuit never fires without a loop)."""
    hub = qt.shells.ShellHub(lambda ws: [sys.executable, "-c", SLEEPER])
    shell = qt.shell_class(StubBridge(), None, hub)
    window = qt.QtWidgets.QMainWindow()
    window.setCentralWidget(shell)
    window.show()
    shell.listing.on_succeeded("workspaces-list", [daemon_row()])
    yield shell, hub, window
    hub.shutdown()


@pytest.fixture()
def launcher_shell(qt):
    """The window shell with a launcher configured — the
    external action's stand, with the fixture's own teardown
    duty."""
    shell, hub, window = make_shell(qt, terminal_cmd=["theterm", "-e"])
    yield shell, hub, window
    hub.shutdown()


@pytest.fixture()
def consent_tab(qt, hub):
    from msks.client.gui.consent import ConsentRow

    bridge = StubBridge()
    held = FakePresenter([ConsentRow("req", "host:443", 20, "20s")])
    tab = qt.shells.ShellsTab(hub, bridge)
    tab.resize(800, 600)
    tab.host("ws-a", held)
    tab.show()
    qt.app.processEvents()
    return tab, held, bridge


def test_shells_container_places_consent_above_terminals(qt, consent_tab):
    tab, held, bridge = consent_tab
    assert tab.consent_panel.geometry().bottom() < tab.shells.geometry().top()
    assert tab.shells.height() > tab.consent_panel.height()


def test_empty_panel_keeps_terminal_area(qt, consent_tab):
    tab, held, bridge = consent_tab
    held.models.clear()
    bridge.decider_landed.emit("resolved", None)
    qt.app.processEvents()
    assert tab.consent_panel.isHidden()
    assert tab.shells.geometry() == tab.rect()


def test_container_timer_refreshes_countdown(qt, consent_tab):
    from msks.client.gui.consent import ConsentRow

    tab, held, bridge = consent_tab
    held.models[0] = ConsentRow("req", "host:443", 19, "19s")
    assert tab.timer.interval() == 1000
    tab.timer.timeout.emit()
    countdown = tab.findChild(qt.QtWidgets.QLabel, "countdown-req")
    assert countdown.text() == "19s"


def test_empty_visible_shells_detects_first_hold(qt, consent_tab):
    tab, held, bridge = consent_tab
    models = held.models
    held.models = []
    tab.timer.timeout.emit()
    assert tab.consent_panel.isHidden()
    assert tab.timer.isActive()
    held.models = models
    tab.timer.timeout.emit()
    assert not tab.consent_panel.isHidden()


def test_hidden_shells_stops_timer_and_show_refreshes(qt, consent_tab):
    tab, held, bridge = consent_tab
    tab.hide()
    assert not tab.timer.isActive()
    held.models.clear()
    bridge.decider_landed.emit("resolved", None)
    assert not tab.timer.isActive()
    tab.show()
    assert tab.timer.isActive()
    assert tab.consent_panel.isHidden()


def test_stale_notification_reads_only_active_presenter(qt, consent_tab):
    tab, held, bridge = consent_tab
    tab.host("ws-b", FakePresenter())
    bridge.decider_landed.emit("added", object())
    assert tab.consent_panel.isHidden()
    assert held.models


def test_focused_row_removal_reaches_terminal_strip(qt, consent_tab):
    from msks.client.gui.consent import ConsentRow

    tab, held, bridge = consent_tab
    held.models += [ConsentRow("next", "next:443", 20, "20s")]
    tab.timer.timeout.emit()
    first = tab.findChild(qt.QtWidgets.QToolButton, "allow-req")
    first.setFocus()
    held.models.pop(0)
    tab.timer.timeout.emit()
    assert tab.findChild(qt.QtWidgets.QToolButton, "allow-next").hasFocus()
    held.models.clear()
    tab.timer.timeout.emit()
    assert tab.shells.tabBar().hasFocus()


def test_background_resolution_does_not_steal_terminal_focus(qt, consent_tab):
    tab, held, bridge = consent_tab
    tab.shells.tabBar().setFocus()
    held.models.clear()
    tab.timer.timeout.emit()
    assert tab.shells.tabBar().hasFocus()


def test_shells_events_tolerate_partial_teardown(qt, consent_tab):
    tab, held, bridge = consent_tab
    tab.hide()
    del tab.consent_panel
    del tab.timer
    tab.resizeEvent(qt.QtGui.QResizeEvent(tab.size(), tab.size()))
    tab.showEvent(qt.QtGui.QShowEvent())
    tab.hideEvent(qt.QtGui.QHideEvent())


def test_large_queue_leaves_terminal_space(qt, consent_tab):
    from msks.client.gui.consent import ConsentRow

    tab, held, bridge = consent_tab
    held.models = [
        ConsentRow(str(i), f"host{i}:443", 20, "20s") for i in range(5)
    ]
    tab.timer.timeout.emit()
    qt.app.processEvents()
    assert tab.shells.height() > tab.consent_panel.height()
    assert tab.consent_panel.scroll.verticalScrollBar().maximum() > 0


def make_shell(qt, terminal_cmd=None):
    """The window shell over a launcher-configured stub, for the
    external action's tests — the fixture's shape with the
    launcher passed in, and the same teardown duty."""
    hub = qt.shells.ShellHub(lambda ws: [sys.executable, "-c", SLEEPER])
    shell = qt.shell_class(StubBridge(), terminal_cmd, hub)
    window = qt.QtWidgets.QMainWindow()
    window.setCentralWidget(shell)
    window.show()
    shell.listing.on_succeeded("workspaces-list", [daemon_row()])
    qt.app.processEvents()
    return shell, hub, window


def test_numbers_assign_per_workspace_in_open_order(hub):
    """One workspace's sessions number in open order, and two
    workspaces number their own."""
    first = hub.open_shell("ws-a")
    second = hub.open_shell("ws-a")
    other = hub.open_shell("ws-b")
    assert (first.no, second.no, other.no) == (1, 2, 1)


def test_a_closed_number_is_not_reused(hub):
    """A closed session's number stays its own: the next shell
    takes a fresh one, so a title never names two sessions."""
    first = hub.open_shell("ws-a")
    hub.close_shell("ws-a", first.no)
    fresh = hub.open_shell("ws-a")
    assert fresh.no == 2


def test_a_rename_stands_for_the_sessions_life(hub):
    """The custom title replaces the number on the record, and
    stands across host and unhost."""
    session = hub.open_shell("ws-a")
    assert session.label == "1"
    hub.rename("ws-a", 1, "  build  ")
    assert session.label == "build"


def test_an_empty_rename_changes_nothing(hub):
    """A blank rename reads as its cancel: the number stays the
    title."""
    session = hub.open_shell("ws-a")
    hub.rename("ws-a", 1, "   ")
    assert session.label == "1"


def test_a_rename_for_an_unknown_session_changes_nothing(hub):
    """A rename naming no standing session is a no-op."""
    hub.open_shell("ws-a")
    hub.rename("ws-a", 9, "build")
    assert hub.shells("ws-a")[0].label == "1"


def test_closing_ends_the_session(hub):
    """A tab's close ends its session: the widget's own teardown
    runs — the child collected with a status, the widget
    closed — and the record leaves the hub."""
    session = hub.open_shell("ws-a")
    hub.close_shell("ws-a", 1)
    assert session.widget._closed
    assert session.widget.session.exit_status is not None
    assert hub.shells("ws-a") == []


def test_closing_an_unknown_session_changes_nothing(hub):
    """A close naming no standing session is a no-op."""
    session = hub.open_shell("ws-a")
    hub.close_shell("ws-a", 9)
    assert hub.shells("ws-a") == [session]


def test_shutdown_closes_every_workspace(hub):
    """The quit path ends every session in every workspace."""
    one = hub.open_shell("ws-a")
    two = hub.open_shell("ws-b")
    hub.shutdown()
    assert one.widget._closed and two.widget._closed
    assert hub.shells("ws-a") == [] and hub.shells("ws-b") == []


def test_hosting_shows_one_tab_per_shell(hub, tabs):
    """The strip carries the workspace's sessions in open
    order, the newest current."""
    hub.open_shell("ws-a")
    hub.open_shell("ws-a")
    tabs.host("ws-a")
    assert [tabs.tabText(i) for i in range(tabs.count())] == [
        "1",
        "2",
        "+",
    ]
    assert tabs.currentIndex() == 1  # the newest shell, not the +


def test_unhosting_keeps_the_sessions_running(hub, tabs):
    """Taking the tabs off closes nothing: the widgets stay
    whole under the hub, ready to host again."""
    session = hub.open_shell("ws-a")
    tabs.host("ws-a")
    tabs.unhost()
    assert tabs.count() == 0
    assert not session.widget._closed
    assert hub.shells("ws-a") == [session]


def test_rehosting_shows_the_titles(hub, tabs):
    """A rehost reads every record's title — the number or the
    name that stands."""
    hub.open_shell("ws-a")
    hub.open_shell("ws-a")
    hub.rename("ws-a", 2, "build")
    tabs.host("ws-a")
    tabs.host("ws-b")
    tabs.host("ws-a")
    assert [tabs.tabText(i) for i in range(tabs.count())] == [
        "1",
        "build",
        "+",
    ]


def test_an_open_while_hosted_adds_the_tab_beside(hub, tabs):
    """A shell opened while its workspace shows takes a tab
    beside the standing ones, current and focused."""
    hub.open_shell("ws-a")
    tabs.host("ws-a")
    fresh = hub.open_shell("ws-a")
    assert tabs.currentIndex() == 1
    assert tabs.currentWidget() is fresh.widget


def test_an_open_for_another_workspace_adds_nothing(hub, tabs):
    """A shell opened on a workspace the strip does not host
    adds no tab to it."""
    tabs.host("ws-a")
    hub.open_shell("ws-b")
    assert [tabs.tabText(i) for i in range(tabs.count())] == ["+"]


def test_a_close_removes_the_tab(hub, tabs):
    """A closed session's tab comes off the strip."""
    hub.open_shell("ws-a")
    hub.open_shell("ws-a")
    tabs.host("ws-a")
    hub.close_shell("ws-a", 1)
    assert [tabs.tabText(i) for i in range(tabs.count())] == [
        "2",
        "+",
    ]


def test_the_close_button_ends_its_session(hub, tabs, qt):
    """Each shell tab's own close button ends its session — the
    "+" carries none."""
    hub.open_shell("ws-a")
    tabs.host("ws-a")
    side = qt.QtWidgets.QTabBar.ButtonPosition.RightSide
    button = tabs.tabBar().tabButton(0, side)
    assert tabs.tabBar().tabButton(tabs.indexOf(tabs.plus_page), side) is None
    button.click()
    assert [tabs.tabText(i) for i in range(tabs.count())] == ["+"]
    assert hub.shells("ws-a") == []


def test_a_double_click_opens_the_editor_and_return_stands(hub, tabs, qt):
    """A double-click on a shell tab edits its title in place;
    Return stands the edit as the session's title."""
    hub.open_shell("ws-a")
    tabs.host("ws-a")
    bar = tabs.tabBar()
    rect = bar.tabRect(0)
    double = qt.QtGui.QMouseEvent(
        qt.QtGui.QMouseEvent.Type.MouseButtonDblClick,
        qt.QtCore.QPointF(rect.center()),
        qt.QtCore.QPointF(rect.center()),
        qt.QtCore.Qt.MouseButton.LeftButton,
        qt.QtCore.Qt.MouseButton.LeftButton,
        qt.QtCore.Qt.KeyboardModifier.NoModifier,
    )
    assert tabs.eventFilter(bar, double) is True
    assert tabs.editor is not None
    assert tabs.editor.text() == "1"
    tabs.editor.setText("build")
    tabs.commit_rename()
    assert tabs.editor is None
    assert tabs.tabText(0) == "build"
    assert hub.shells("ws-a")[0].label == "build"


def test_the_editors_escape_cancels(hub, tabs, qt):
    """The open editor's Escape takes it down as a cancel: the
    standing title keeps. The editor's other events pass
    through to it — a typed letter must reach the field."""
    hub.open_shell("ws-a")
    tabs.host("ws-a")
    tabs.begin_rename(0)
    tabs.editor.setText("nope")
    Qt = qt.QtCore.Qt
    typed = qt.QtGui.QKeyEvent(
        qt.QtCore.QEvent.Type.KeyPress,
        Qt.Key.Key_B,
        Qt.KeyboardModifier.NoModifier,
        "b",
    )
    assert tabs.eventFilter(tabs.editor, typed) is False
    escape = qt.QtGui.QKeyEvent(
        qt.QtCore.QEvent.Type.KeyPress,
        Qt.Key.Key_Escape,
        Qt.KeyboardModifier.NoModifier,
    )
    assert tabs.eventFilter(tabs.editor, escape) is True
    assert tabs.editor is None
    assert tabs.tabText(0) == "1"


def test_the_editors_override_keeps_the_escape_for_the_cancel(hub, tabs, qt):
    """The override claim before the dispatch (#1099): the
    page's Back shortcut asks the editor first — Qt sends the
    ShortcutOverride the shortcut matched — and the filter's
    accept yields the shortcut and keeps the press for the
    cancel arm. The question alone cancels nothing, and
    another key's question passes through to the editor."""
    hub.open_shell("ws-a")
    tabs.host("ws-a")
    tabs.begin_rename(0)
    Qt = qt.QtCore.Qt
    override = qt.QtGui.QKeyEvent(
        qt.QtCore.QEvent.Type.ShortcutOverride,
        Qt.Key.Key_Escape,
        Qt.KeyboardModifier.NoModifier,
    )
    assert tabs.eventFilter(tabs.editor, override) is True
    assert override.isAccepted()
    assert tabs.editor is not None  # the press cancels, not the question
    other = qt.QtGui.QKeyEvent(
        qt.QtCore.QEvent.Type.ShortcutOverride,
        Qt.Key.Key_A,
        Qt.KeyboardModifier.NoModifier,
        "a",
    )
    assert tabs.eventFilter(tabs.editor, other) is False
    tabs.cancel_rename()


def test_the_editor_never_stacks(hub, tabs):
    """A second edit takes the first editor down, not beside
    it."""
    hub.open_shell("ws-a")
    hub.open_shell("ws-a")
    tabs.host("ws-a")
    tabs.begin_rename(0)
    first = tabs.editor
    tabs.begin_rename(1)
    assert tabs.editor is not first


def test_a_double_click_on_the_plus_edits_nothing(hub, tabs, qt):
    """The "+" carries no title to edit: its double-click
    passes through."""
    hub.open_shell("ws-a")
    tabs.host("ws-a")
    bar = tabs.tabBar()
    rect = bar.tabRect(tabs.indexOf(tabs.plus_page))
    dbl = qt.QtGui.QMouseEvent(
        qt.QtGui.QMouseEvent.Type.MouseButtonDblClick,
        qt.QtCore.QPointF(rect.center()),
        qt.QtCore.QPointF(rect.center()),
        qt.QtCore.Qt.MouseButton.LeftButton,
        qt.QtCore.Qt.MouseButton.LeftButton,
        qt.QtCore.Qt.KeyboardModifier.NoModifier,
    )
    assert tabs.eventFilter(bar, dbl) is False
    assert tabs.editor is None


def test_an_open_for_an_unknown_number_adds_nothing(hub, tabs):
    """An opened signal naming no standing session adds no tab —
    the strip reads the hub's record, and no record stands."""
    tabs.host("ws-a")
    hub.shell_opened.emit("ws-a", 99)
    assert [tabs.tabText(i) for i in range(tabs.count())] == ["+"]


def test_a_close_for_an_unknown_number_removes_nothing(hub, tabs):
    """A closed signal naming no hosted session moves no tab."""
    hub.open_shell("ws-a")
    tabs.host("ws-a")
    hub.shell_closed.emit("ws-a", 99)
    assert [tabs.tabText(i) for i in range(tabs.count())] == [
        "1",
        "+",
    ]


def test_a_close_for_a_widget_out_of_the_strip_removes_nothing(hub, tabs, qt):
    """A close whose map entry points at no hosted tab — the
    strip's own map drifted behind the tabs — moves no tab."""
    hub.open_shell("ws-a")
    tabs.host("ws-a")
    stranger = qt.QtWidgets.QWidget()
    tabs.tab_widgets[9] = stranger
    hub.shell_closed.emit("ws-a", 9)
    assert [tabs.tabText(i) for i in range(tabs.count())] == [
        "1",
        "+",
    ]


def test_a_commit_for_a_gone_record_changes_nothing(hub, tabs):
    """A commit whose tab's record left while the editor stood
    stands no title."""
    hub.open_shell("ws-a")
    tabs.host("ws-a")
    tabs.begin_rename(0)
    session = hub.shells("ws-a")[0]
    hub.sessions["ws-a"].remove(session)
    tabs.editor.setText("build")
    tabs.commit_rename()
    assert tabs.tabText(0) == "1"


def test_a_wide_tab_keeps_its_width_for_the_editor(hub, tabs, qt):
    """A tab wide enough to type in hands the editor its own
    rect — width and mapped place both — a narrow one the
    editor's floor width."""
    session = hub.open_shell("ws-a")
    hub.rename("ws-a", session.no, "a-title-wide-enough")
    tabs.host("ws-a")
    bar = tabs.tabBar()
    bar.resize(600, bar.height())
    rect = bar.tabRect(0)
    corner = bar.mapTo(tabs, rect.topLeft())
    tabs.begin_rename(0)
    assert tabs.editor.width() == rect.width()
    assert tabs.editor.pos() == corner


def test_a_rename_lands_on_its_own_session_not_the_shuffled_tab(hub, tabs):
    """The editor keys on the session's number, not the tab's
    position: a shell closing under an open editor must not
    hand the edit to the session that slid into its place, and
    the editor for the closed session goes down."""
    first = hub.open_shell("ws-a")
    second = hub.open_shell("ws-a")
    tabs.host("ws-a")
    tabs.begin_rename(0)  # first session's tab
    tabs.editor.setText("mine")
    hub.close_shell("ws-a", first.no)  # the tab under the editor closes
    assert tabs.editor is None  # the edit names a gone session
    assert tabs.tabText(0) == "2"
    assert second.label == "2"  # no wrong-session rename landed
    # the editor's commit after a tab shift still finds its own
    tabs.begin_rename(0)
    tabs.editor.setText("mine")
    tabs.commit_rename()
    assert hub.find("ws-a", second.no).label == "mine"
    # The distinguishing shape (#954): a DIFFERENT session's tab
    # closes under a standing editor — the editor's tab slides
    # to a new position — and the commit must still find its
    # own session, not whatever took the stored index.
    third = hub.open_shell("ws-a")
    tabs.begin_rename(1)  # the third session's tab
    hub.close_shell("ws-a", second.no)  # the tab before it closes
    tabs.editor.setText("third-session")
    tabs.commit_rename()
    assert third.label == "third-session"


def test_an_editor_for_a_closed_session_goes_down(hub, tabs):
    """The editor for a session that closed mid-edit goes down
    without standing a name."""
    first = hub.open_shell("ws-a")
    hub.open_shell("ws-a")
    tabs.host("ws-a")
    tabs.begin_rename(0)
    tabs.editor.setText("late")
    hub.close_shell("ws-a", first.no)
    assert tabs.editor is None
    assert tabs.tabText(0) == "2"


def test_the_rename_paths_without_an_editor_change_nothing(tabs):
    """A commit or a cancel with no editor open moves nothing."""
    tabs.commit_rename()
    tabs.cancel_rename()
    assert tabs.editor is None


def test_a_rename_for_a_record_behind_the_strip_changes_nothing(hub, tabs):
    """A rename the strip cannot place — its record gone behind
    its back — moves no tab text."""
    session = hub.open_shell("ws-a")
    tabs.host("ws-a")
    hub.sessions["ws-a"].remove(session)
    hub.shell_renamed.emit("ws-a", 1)
    assert tabs.tabText(0) == "1"


def test_the_plus_tab_takes_its_natural_width(hub, tabs):
    """The floor is the shell tabs': the "+" keeps its own
    narrow width, floored like a shell tab it would read as a
    wide blank beside the tabs it opens."""
    hub.open_shell("ws-a")
    tabs.host("ws-a")
    tabs.resize(800, 400)
    bar = tabs.tabBar()
    shell_width = bar.tabRect(0).width()
    plus_width = bar.tabRect(tabs.indexOf(tabs.plus_page)).width()
    assert shell_width >= bar.min_tab_px()
    assert plus_width < bar.min_tab_px()


def test_the_window_names_the_workspace_and_back(qt, window_shell):
    """The workspace's name lives in the window title while the
    page shows (#971), and the list takes the plain title
    back."""
    shell, hub, _window = window_shell
    shell.open_detail("ws-a")
    assert shell.window().windowTitle() == "msks — alpha"
    shell.detail.go_back()
    assert shell.window().windowTitle() == "msks"


def test_a_close_lands_on_the_remaining_shell_not_the_plus(hub, tabs):
    """A close that takes the tab current stood on lands on the
    remaining shell before the "+", never on the blank + page
    (#971); the last shell's close leaves the + alone (the
    page's next open creates a shell)."""
    hub.open_shell("ws-a")
    hub.open_shell("ws-a")
    tabs.host("ws-a")
    tabs.setCurrentIndex(1)  # the newest shell
    hub.close_shell("ws-a", 2)
    assert tabs.currentWidget() is hub.find("ws-a", 1).widget
    hub.close_shell("ws-a", 1)
    assert tabs.currentIndex() == 0  # the + alone


def test_the_plus_tooltip_follows_every_readd(hub, tabs):
    """The + tab carries its tooltip on every host, the re-added
    tab included."""
    tabs.host("ws-a")
    index = tabs.indexOf(tabs.plus_page)
    assert tabs.tabBar().tabToolTip(index) == "Open a shell tab"
    tabs.host("ws-b")
    assert (
        tabs.tabBar().tabToolTip(tabs.indexOf(tabs.plus_page))
        == "Open a shell tab"
    )


def test_a_shell_tab_never_shrinks_under_three_characters(hub, tabs):
    """A one-digit title keeps a tab the pointer can grab: the
    floor is three digits of the bar's font beside the close
    button, whatever the default hint would squeeze it to."""
    hub.open_shell("ws-a")
    tabs.host("ws-a")
    tabs.resize(800, 400)
    bar = tabs.tabBar()
    assert bar.tabRect(0).width() >= bar.min_tab_px()
    assert bar.min_tab_px() >= bar.fontMetrics().horizontalAdvance("000")


def test_the_plus_sits_directly_right_of_the_last_tab_and_opens(
    qt, window_shell
):
    """The "+" is the last tab itself — directly right of the
    rightmost shell tab, alone when none stands; a click on it
    opens a new shell tab, and a close shifting tabs onto the +
    opens nothing."""
    shell, hub, _window = window_shell
    assert tabs_plus(shell.detail) == 0  # alone before any host
    shell.open_detail("ws-a")  # the open creates the first shell
    click_plus(shell.detail)
    titles = strip_titles(shell.detail)
    assert titles == ["1", "2", "+"]
    tabs = shell.detail.shells
    bar = tabs.tabBar()
    plus = bar.tabRect(tabs_plus(shell.detail))
    last_shell = bar.tabRect(tabs_plus(shell.detail) - 1)
    assert plus.left() >= last_shell.right()  # directly right
    # a close shifting the current tab onto the + opens nothing
    hub.close_shell("ws-a", 1)
    assert [tabs.tabText(i) for i in range(tabs.count())] == ["2", "+"]
    assert len(hub.shells("ws-a")) == 1
    # a click on any other tab is just a tab: no shell opens
    tabs.tabBar().tabBarClicked.emit(0)
    assert len(hub.shells("ws-a")) == 1


def test_the_plus_opens_a_second_shell_beside(qt, window_shell):
    """A second click on the "+" opens another shell beside the
    standing one, numbered in open order."""
    shell, hub, _window = window_shell
    shell.open_detail("ws-a")
    click_plus(shell.detail)
    click_plus(shell.detail)
    assert strip_titles(shell.detail) == ["1", "2", "3", "+"]


def test_the_page_chrome_carries_no_shell_button(qt, window_shell):
    """The "+" owns the page's new-shell action: the chrome
    carries no Shell button — the tabs under it carry their own
    (the Mounts tab's Add/Edit/Remove, #987), out of the
    chrome's row."""
    shell, hub, _window = window_shell
    chrome = shell.detail.chrome
    chrome_buttons = [
        chrome.itemAt(i).widget().text()
        for i in range(chrome.count())
        if isinstance(
            chrome.itemAt(i).widget(), type(shell.detail.back_button)
        )
        and chrome.itemAt(i).widget().text()
    ]
    assert chrome_buttons == []


def tabs_plus(page) -> int:
    """The shell strip's "+" tab index."""
    return page.shells.indexOf(page.shells.plus_page)


def strip_titles(page) -> list[str]:
    """The shell strip's tab texts, in order."""
    return [page.shells.tabText(i) for i in range(page.shells.count())]


def click_plus(page) -> None:
    """A real click on the "+" tab: the bar's own clicked
    signal, the gesture the strip routes."""
    page.shells.tabBar().tabBarClicked.emit(tabs_plus(page))


# --- the window shell's actions ---------------------------------------


def test_the_shell_action_opens_the_detail_on_a_new_tab(qt, window_shell):
    """The list's shell action opens the detail page on its
    Shells tab, the open's own first shell current and focused
    (#954, #974)."""
    shell, hub, _window = window_shell
    # The row's own shell button (#972) drives the same flow.
    shell.listing.shell_requested.emit("ws-a")
    assert shell.currentWidget() is shell.detail
    assert shell.detail.bar.currentIndex() == 0  # Shells, the default
    assert strip_titles(shell.detail) == ["1", "+"]
    assert shell.detail.shells.currentIndex() == 0


def test_a_second_shell_takes_the_tab_beside(qt, window_shell):
    """A new ssh connection opens a tab beside the standing
    ones, numbered in open order."""
    shell, hub, _window = window_shell
    shell.open_shell("ws-a")
    shell.detail.want_shell()
    assert strip_titles(shell.detail) == ["1", "2", "+"]


def test_the_back_action_closes_no_shell(qt, window_shell):
    """Back to the list closes no tab and ends no session: the
    terminals keep running while the page is closed."""
    shell, hub, _window = window_shell
    shell.open_shell("ws-a")
    session = hub.shells("ws-a")[0]
    shell.detail.go_back()
    assert shell.currentWidget() is shell.listing
    assert not session.widget._closed
    assert hub.shells("ws-a")


def test_reopening_shows_every_shell_with_its_title(qt, window_shell):
    """Revisiting the workspace's page shows a tab for every
    active connection, each with its title."""
    shell, hub, _window = window_shell
    shell.open_shell("ws-a")
    shell.open_shell("ws-a")
    hub.rename("ws-a", 2, "build")
    shell.detail.go_back()
    shell.open_detail("ws-a")
    assert strip_titles(shell.detail) == ["1", "build", "+"]


def test_a_shell_for_another_workspace_swaps_the_page(qt, window_shell):
    """A shell action naming a workspace the page does not show
    takes the page to that workspace first."""
    shell, hub, _window = window_shell
    shell.open_shell("ws-a")
    shell.open_shell("ws-b")
    assert shell.detail.workspace_id == "ws-b"
    assert strip_titles(shell.detail) == ["1", "+"]
    assert len(hub.shells("ws-a")) == 1


def test_the_external_action_spawns_the_launcher_argv(
    qt, launcher_shell, monkeypatch
):
    """The external-terminal action (#954, #972) lives on the
    detail page: it runs the configured launcher with the ssh
    argv appended — the same argv the TUI's windowed shell
    action builds — detached."""
    shell, hub, _window = launcher_shell
    seen = []
    monkeypatch.setattr(
        qt.QtCore.QProcess,
        "startDetached",
        staticmethod(
            lambda program, args: seen.append((program, args)) or True
        ),
    )
    shell.open_detail("ws-a")
    shell.detail.external_button.click()
    from msks.client.gui.termsession import shell_argv

    assert seen == [("theterm", ["-e", *shell_argv("ws-a")])]


def test_the_external_action_names_the_setting_when_unconfigured(
    qt, window_shell
):
    """Without a launcher the notice names the setting on the
    screen the operator sees (#972): the action lives on the
    detail page, and the GUI has no terminal of its own to hand
    the session."""
    shell, hub, _window = window_shell
    shell.open_detail("ws-a")
    shell.detail.external_button.click()
    assert (
        shell.detail.status.text()
        == "set terminal_open_cmd to open external terminals"
    )


def test_the_failed_launcher_flashes(qt, launcher_shell, monkeypatch):
    """A launcher that cannot start says so on the detail
    page's status line — the screen the operator sees (#972)."""
    shell, hub, _window = launcher_shell
    shell.open_detail("ws-a")
    monkeypatch.setattr(
        qt.QtCore.QProcess,
        "startDetached",
        staticmethod(lambda program, args: False),
    )
    shell.detail.external_button.click()
    assert "failed" in shell.detail.status.text()


def test_a_shell_action_from_the_list_over_standing_shells(qt, window_shell):
    """The list's shell action with the page closed behind
    standing shells (#974): the page returns showing them and
    one more shell opens, the new tab current."""
    shell, hub, _window = window_shell
    shell.open_shell("ws-a")  # the open creates the first shell
    shell.detail.go_back()
    shell.open_shell("ws-a")
    assert shell.currentWidget() is shell.detail
    assert strip_titles(shell.detail) == ["1", "2", "+"]
    assert shell.detail.shells.currentWidget() is hub.find("ws-a", 2).widget


def test_the_page_shell_action_without_a_workspace_sends_nothing(
    qt, window_shell
):
    """The page's shell and external actions with no workspace
    taken send nothing."""
    shell, hub, _window = window_shell
    seen: list[str] = []
    shell.detail.shell_wanted.connect(lambda ws: seen.append(ws))
    shell.detail.external_wanted.connect(lambda ws: seen.append(ws))
    shell.detail.workspace_id = ""
    shell.detail.want_shell()
    shell.detail.want_external()
    assert seen == []


def test_the_shell_builds_without_an_app_instance(qt, monkeypatch):
    """The window shell builds with no application standing: the
    quit-path connect simply waits for one (the suite's own
    stand-in never enters a loop)."""
    from test_gui_workspace import StubBridge as Bridge

    monkeypatch.setattr(
        qt.QtWidgets.QApplication, "instance", staticmethod(lambda: None)
    )
    hub = qt.shells.ShellHub(lambda ws: [sys.executable, "-c", SLEEPER])
    made = qt.shell_class(Bridge(), None, hub)
    assert made.hub is hub
    hub.shutdown()


# --- the launcher's read off the config --------------------------------


def test_the_terminal_command_reads_the_config():
    """The configured launcher arrives as a list; a missing
    config or an empty setting read as None — the TUI's own
    read."""
    from msks.client.gui.app import terminal_command

    class Conf:
        terminal_open_cmd = ["theterm", "-e"]

    class Empty:
        terminal_open_cmd = None

    assert terminal_command(Conf()) == ["theterm", "-e"]
    assert terminal_command(Empty()) is None
    assert terminal_command(None) is None


# --- the program's title (#1163) ----------------------------------------


def titles_heard(hub) -> list:
    """Every ``shell_titled`` announce so far, as (workspace, number)."""
    heard: list = []
    hub.shell_titled.connect(lambda ws, no: heard.append((ws, no)))
    return heard


def test_a_program_title_titles_the_tab_until_cleared(hub):
    """OSC 0/2 from the program inside (#1163): the record takes
    the title and the label reads it; the empty title — the
    program clearing it — reads the number again. Each change
    announces itself to the hosting strip."""
    session = hub.open_shell("ws-a")
    heard = titles_heard(hub)
    session.widget.title_changed.emit("build")
    assert session.program_title == "build"
    assert session.label == "build"
    session.widget.title_changed.emit("")
    assert session.program_title == ""
    assert session.label == "1"
    assert heard == [("ws-a", 1), ("ws-a", 1)]


def test_a_rename_wins_over_the_program_title(hub):
    """A renamed tab keeps its name for the session's life
    (#1163): a program title landing after the rename changes
    the record's text and not the label."""
    session = hub.open_shell("ws-a")
    hub.rename("ws-a", 1, "build")
    session.widget.title_changed.emit("vim")
    assert session.program_title == "vim"
    assert session.label == "build"


def test_a_title_for_a_closed_session_changes_nothing(hub):
    """A late title from a widget whose session closed (#1163):
    the record is gone, so the text lands nowhere and nothing
    is announced."""
    session = hub.open_shell("ws-a")
    hub.close_shell("ws-a", 1)
    heard = titles_heard(hub)
    session.widget.title_changed.emit("late")
    assert hub.shells("ws-a") == []
    assert heard == []


def test_the_tab_follows_the_programs_title(hub, tabs):
    """A hosted tab follows the record (#1163): the program's
    title while one stands, the number once the program clears
    it, and the whole title under the pointer while one stands."""
    session = hub.open_shell("ws-a")
    tabs.host("ws-a")
    assert tabs.tabText(0) == "1"
    session.widget.title_changed.emit("build")
    assert tabs.tabText(0) == "build"
    assert tabs.tabToolTip(0) == "build"
    session.widget.title_changed.emit("")
    assert tabs.tabText(0) == "1"
    assert tabs.tabToolTip(0) == ""


def test_a_renamed_tab_keeps_its_name_over_a_program_title(hub, tabs):
    """The rename wins on the tab too (#1163): a program title
    after it changes neither the text nor the tooltip — the
    name is the operator's own, and it reads whole on the tab."""
    session = hub.open_shell("ws-a")
    tabs.host("ws-a")
    hub.rename("ws-a", 1, "build")
    session.widget.title_changed.emit("vim")
    assert tabs.tabText(0) == "build"
    assert tabs.tabToolTip(0) == ""


def test_a_hosted_tab_shows_the_title_set_while_unhosted(hub, tabs):
    """A shell that retitled while its page was elsewhere shows
    the title at the host (#1163): the tab takes the record as
    it reads, the title and the tooltip both."""
    session = hub.open_shell("ws-a")
    session.widget.title_changed.emit("build")
    tabs.host("ws-a")
    assert tabs.tabText(0) == "build"
    assert tabs.tabToolTip(0) == "build"


def test_a_title_for_another_workspace_retitles_nothing(hub, tabs):
    """The strip hosts one workspace (#954): a program title for
    another workspace's shell retitles no tab here."""
    hub.open_shell("ws-a")
    tabs.host("ws-a")
    other = hub.open_shell("ws-b")
    other.widget.title_changed.emit("build")
    assert bare_titles(tabs) == ["1", "+"]


def bare_titles(tabs) -> list[str]:
    """A bare strip's tab texts, in order."""
    return [tabs.tabText(i) for i in range(tabs.count())]


def test_a_long_title_stops_at_the_ceiling_and_reads_in_the_tooltip(
    hub, tabs, qt
):
    """A program's title runs as long as a path (#1163): the
    tab's hint stops at the ceiling and the bar elides the text
    at paint, the tab's text and tooltip keep the whole title,
    and the "+" keeps its natural width."""
    session = hub.open_shell("ws-a")
    tabs.host("ws-a")
    bar = tabs.tabBar()
    plus = bar.tabSizeHint(1).width()
    long = "user@alpha: ~/projects/msks/docs/changes.md — vim"
    session.widget.title_changed.emit(long)
    assert bar.tabSizeHint(0).width() == bar.max_tab_px()
    assert tabs.tabText(0) == long
    assert tabs.tabToolTip(0) == long
    assert bar.tabSizeHint(1).width() == plus
    assert bar.elideMode() == qt.QtCore.Qt.TextElideMode.ElideRight
    # A short title sits between the floor and the ceiling.
    session.widget.title_changed.emit("vim")
    assert bar.min_tab_px() <= bar.tabSizeHint(0).width() < bar.max_tab_px()


# --- the strip's own actions (#1163) ------------------------------------


def test_stepping_cycles_the_shell_tabs_and_skips_the_plus(hub, tabs):
    """Next and previous move by one among the shell tabs,
    wrapping at both ends; the "+" is never a stop — a current
    standing on it starts from the first or the last shell."""
    for _ in range(3):
        hub.open_shell("ws-a")
    tabs.host("ws-a")
    assert tabs.currentIndex() == 2  # the newest shell
    tabs.step_shell(1)
    assert tabs.currentIndex() == 0
    tabs.step_shell(1)
    assert tabs.currentIndex() == 1
    tabs.step_shell(-1)
    assert tabs.currentIndex() == 0
    tabs.setCurrentIndex(tabs.indexOf(tabs.plus_page))
    tabs.step_shell(1)
    assert tabs.currentIndex() == 0
    tabs.setCurrentIndex(tabs.indexOf(tabs.plus_page))
    tabs.step_shell(-1)
    assert tabs.currentIndex() == 2


def test_stepping_the_plus_alone_changes_nothing(tabs):
    """Only the "+" standing — no shell — the step has no stop
    to make: the current tab stays where it was."""
    assert tabs.shell_count() == 0
    tabs.step_shell(1)
    assert tabs.currentIndex() == 0
    tabs.step_shell(-1)
    assert tabs.currentIndex() == 0


def test_selecting_the_nth_shell(hub, tabs):
    """The Nth shell, counting from one: its tab current; a
    position under one or past the standing shells changes
    nothing."""
    for _ in range(3):
        hub.open_shell("ws-a")
    tabs.host("ws-a")
    tabs.select_shell(1)
    assert tabs.currentIndex() == 0
    tabs.select_shell(4)
    assert tabs.currentIndex() == 0
    tabs.select_shell(0)
    assert tabs.currentIndex() == 0
    tabs.select_shell(3)
    assert tabs.currentIndex() == 2


def test_closing_the_current_shell_mirrors_the_button(hub, tabs):
    """The current shell's close is the tab's own close button
    (#954): the record goes down and the tab with it; the "+"
    standing current closes nothing."""
    for _ in range(2):
        hub.open_shell("ws-a")
    tabs.host("ws-a")
    tabs.close_current()
    assert bare_titles(tabs) == ["1", "+"]
    assert [s.no for s in hub.shells("ws-a")] == [1]
    tabs.setCurrentIndex(tabs.indexOf(tabs.plus_page))
    tabs.close_current()
    assert bare_titles(tabs) == ["1", "+"]


# --- the shell keys (#1163) ---------------------------------------------

MODS = {
    "ctrl": "ControlModifier",
    "shift": "ShiftModifier",
    "alt": "AltModifier",
}


def press(qt, widget, key, *mods: str) -> None:
    """One key pressed on the widget that holds the keyboard,
    the named modifiers (``ctrl``, ``shift``, ``alt``) held —
    through Qt's own event path, the delivery the platform
    makes (#1152's shape)."""
    from PySide6.QtTest import QTest

    Modifier = qt.QtCore.Qt.KeyboardModifier
    held = Modifier.NoModifier
    for name in mods:
        held |= getattr(Modifier, MODS[name])
    QTest.keyClick(widget, getattr(qt.QtCore.Qt.Key, key), held)


def pressed_keys(qt, widget) -> list:
    """Every key the widget receives as a press from now on: a
    filter before the widget's own handler. The shortcut map
    runs before the widget, so a key a shortcut took never
    lands here (#1152)."""
    seen: list = []

    class Log(qt.QtCore.QObject):
        def eventFilter(self, _watched, event) -> bool:  # noqa: N802
            if event.type() == qt.QtCore.QEvent.Type.KeyPress:
                seen.append(event.key())
            return False

    widget.installEventFilter(Log(widget))
    return seen


def open_page(qt, shell, shells: int = 1):
    """The page on ws-a with ``shells`` shells, the newest
    current and its terminal holding the keyboard — the window
    takes the focus on its event pass (#1152)."""
    for _ in range(shells):
        shell.open_shell("ws-a")
    qt.app.processEvents()
    page = shell.detail
    assert qt.app.focusWidget() is page.shells.currentWidget()
    return page


@pytest.fixture()
def strip_shell(qt):
    """The window shell under the main strip in a real window,
    an Images page beside it (#962's composition): the keys'
    scope reads against the other pages."""
    from msks.client.gui.tabs import MainTabs

    hub = qt.shells.ShellHub(lambda ws: [sys.executable, "-c", SLEEPER])
    shell = qt.shell_class(StubBridge(), None, hub)
    tabs = MainTabs(shell)
    images = qt.QtWidgets.QWidget()
    tabs.addTab(images, "Images")
    window = qt.QtWidgets.QMainWindow()
    window.setCentralWidget(tabs)
    window.show()
    shell.listing.on_succeeded("workspaces-list", [daemon_row()])
    qt.app.processEvents()
    yield shell, tabs, hub, window
    hub.shutdown()


def test_the_shell_keys_stand_on_the_page_in_the_toggle_keys_scope(
    qt, window_shell
):
    """One shortcut per action on the page (#1163), the scope
    #1152's toggle key opened: the page and its children, so a
    terminal answers and the listing does not. Each answers
    once per press."""
    shell, hub, _window = window_shell
    keys = shell.detail.shell_keys
    assert [
        [k.toString() for k in shortcut.keys()] for shortcut in keys.shortcuts
    ] == [
        ["Ctrl+Shift+T"],
        ["Ctrl+Shift+W"],
        ["Ctrl+Tab"],
        ["Ctrl+Shift+Tab", "Ctrl+Backtab"],
        *[[f"Ctrl+{n}"] for n in range(1, 10)],
        *[[f"Alt+{n}"] for n in range(1, 6)],
    ]
    for shortcut in keys.shortcuts:
        assert shortcut.context() == (
            qt.QtCore.Qt.ShortcutContext.WidgetWithChildrenShortcut
        )
        assert not shortcut.autoRepeat()
        assert shortcut.parent() is shell.detail


def test_the_new_shell_key_opens_a_shell_from_the_terminal(qt, window_shell):
    """Ctrl+Shift+T with a terminal holding the keyboard
    (#1163): one more shell opens beside the standing one, its
    tab current and its terminal focused, and the program
    inside reads nothing of T."""
    shell, hub, _window = window_shell
    page = open_page(qt, shell)
    terminal = page.shells.currentWidget()
    seen = pressed_keys(qt, terminal)
    press(qt, terminal, "Key_T", "ctrl", "shift")
    assert strip_titles(page) == ["1", "2", "+"]
    assert page.shells.currentIndex() == 1
    assert qt.app.focusWidget() is page.shells.currentWidget()
    assert qt.QtCore.Qt.Key.Key_T not in seen


def test_the_close_shell_key_closes_the_current_shell(qt, window_shell):
    """Ctrl+Shift+W (#1163): the current shell's session ends —
    the tab comes off, the shell before it takes the window and
    the keyboard — and the program inside reads nothing of W."""
    shell, hub, _window = window_shell
    page = open_page(qt, shell, shells=2)
    terminal = page.shells.currentWidget()
    assert page.shells.currentIndex() == 1
    seen = pressed_keys(qt, terminal)
    press(qt, terminal, "Key_W", "ctrl", "shift")
    assert strip_titles(page) == ["1", "+"]
    assert page.shells.currentIndex() == 0
    assert qt.app.focusWidget() is page.shells.currentWidget()
    assert qt.QtCore.Qt.Key.Key_W not in seen


def test_closing_the_last_shell_by_key_leaves_the_plus_alone(qt, window_shell):
    """Ctrl+Shift+W on the only shell (#1163, #971): the strip
    holds the "+" alone, current on it, and the closed terminal
    hands the keyboard to the strip's bar — a hidden terminal
    holds it no more, and the page's next open creates a shell."""
    shell, hub, _window = window_shell
    page = open_page(qt, shell)
    terminal = page.shells.currentWidget()
    press(qt, terminal, "Key_W", "ctrl", "shift")
    assert strip_titles(page) == ["+"]
    assert page.shells.currentIndex() == 0
    assert hub.shells("ws-a") == []
    assert qt.app.focusWidget() is page.shells.tabBar()


def test_the_next_and_previous_keys_cycle_the_shells(qt, window_shell):
    """Ctrl+Tab and Ctrl+Shift+Tab (#1163): the next and the
    previous shell, wrapping at both ends, each chosen shell's
    terminal taking the keyboard. Both spellings of the
    previous key answer — Backtab, and the shifted Tab — and
    neither reaches the program inside."""
    shell, hub, _window = window_shell
    page = open_page(qt, shell, shells=3)
    last = page.shells.currentWidget()
    seen = pressed_keys(qt, last)
    press(qt, last, "Key_Tab", "ctrl")
    assert page.shells.currentIndex() == 0  # the wrap
    first = page.shells.currentWidget()
    assert qt.app.focusWidget() is first
    seen_first = pressed_keys(qt, first)
    press(qt, first, "Key_Tab", "ctrl")
    assert page.shells.currentIndex() == 1
    second = page.shells.currentWidget()
    seen_second = pressed_keys(qt, second)
    press(qt, second, "Key_Backtab", "ctrl", "shift")
    assert page.shells.currentIndex() == 0
    press(qt, page.shells.currentWidget(), "Key_Tab", "ctrl", "shift")
    assert page.shells.currentIndex() == 2
    assert qt.app.focusWidget() is last
    for log in (seen, seen_first, seen_second):
        assert qt.QtCore.Qt.Key.Key_Tab not in log
        assert qt.QtCore.Qt.Key.Key_Backtab not in log


def test_the_number_keys_jump_to_the_nth_shell(qt, window_shell):
    """Ctrl+1 through Ctrl+9 (#1163): the Nth shell current and
    focused; a number past the standing shells changes nothing,
    and the digit never reaches the program inside."""
    shell, hub, _window = window_shell
    page = open_page(qt, shell, shells=3)
    terminal = page.shells.currentWidget()
    seen = pressed_keys(qt, terminal)
    press(qt, terminal, "Key_1", "ctrl")
    assert page.shells.currentIndex() == 0
    assert qt.app.focusWidget() is page.shells.currentWidget()
    press(qt, page.shells.currentWidget(), "Key_3", "ctrl")
    assert page.shells.currentIndex() == 2
    press(qt, page.shells.currentWidget(), "Key_9", "ctrl")
    assert page.shells.currentIndex() == 2
    assert qt.QtCore.Qt.Key.Key_1 not in seen


def test_the_alt_keys_jump_to_the_page_tabs(qt, window_shell):
    """Alt+1 through Alt+5 (#1163): the page's Nth tab current.
    Shells anchors the keyboard on the current shell (#974);
    another tab hands the keyboard from the hidden terminal to
    the new tab's first stop, so no hidden terminal holds it.
    The digit never reaches the program inside."""
    shell, hub, _window = window_shell
    page = open_page(qt, shell)
    terminal = page.shells.currentWidget()
    seen = pressed_keys(qt, terminal)
    press(qt, terminal, "Key_2", "alt")
    assert page.bar.currentIndex() == 1
    assert page.stack.currentWidget() is page.mounts_tab
    assert qt.app.focusWidget() is not terminal
    assert page.mounts_tab.isAncestorOf(qt.app.focusWidget())
    press(qt, qt.app.focusWidget(), "Key_5", "alt")
    assert page.bar.currentIndex() == 4
    assert page.stack.currentWidget() is page.events_tab
    press(qt, qt.app.focusWidget(), "Key_1", "alt")
    assert page.bar.currentIndex() == 0
    assert qt.app.focusWidget() is terminal
    press(qt, terminal, "Key_6", "alt")
    assert page.bar.currentIndex() == 0
    assert qt.QtCore.Qt.Key.Key_2 not in seen


def test_the_shell_keys_stay_quiet_on_the_listing(qt, window_shell):
    """The keys are the page's own (#1163): the listing holding
    the keyboard opens no shell and closes none — the press
    reaches the table as an ordinary key."""
    shell, hub, _window = window_shell
    page = open_page(qt, shell, shells=2)
    page.go_back()
    shell.listing.table.setFocus()
    qt.app.processEvents()
    assert qt.app.focusWidget() is shell.listing.table
    seen = pressed_keys(qt, shell.listing.table)
    press(qt, shell.listing.table, "Key_T", "ctrl", "shift")
    press(qt, shell.listing.table, "Key_W", "ctrl", "shift")
    assert len(hub.shells("ws-a")) == 2
    assert qt.QtCore.Qt.Key.Key_T in seen
    assert qt.QtCore.Qt.Key.Key_W in seen


def test_the_shell_keys_stay_quiet_on_the_other_pages(qt, strip_shell):
    """The Images page beside the workspaces under the main
    strip (#1163): with its widget holding the keyboard, the
    new-shell key opens nothing and reaches it as an ordinary
    key."""
    shell, tabs, hub, _window = strip_shell
    open_page(qt, shell)
    tabs.setCurrentIndex(1)
    page = tabs.widget(1)
    edit = qt.QtWidgets.QLineEdit(page)
    edit.show()
    edit.setFocus()
    qt.app.processEvents()
    assert qt.app.focusWidget() is edit
    seen = pressed_keys(qt, edit)
    press(qt, edit, "Key_T", "ctrl", "shift")
    assert len(hub.shells("ws-a")) == 1
    assert qt.QtCore.Qt.Key.Key_T in seen


def test_a_held_new_shell_key_opens_one_shell(qt, window_shell):
    """A held Ctrl+Shift+T opens one shell (#1163): the
    shortcut declines the keyboard's repeat, and Qt consumes a
    repeat the shortcut declines — no second shell, and nothing
    reaches the terminal (#1152's rule)."""
    shell, hub, _window = window_shell
    page = open_page(qt, shell)
    terminal = page.shells.currentWidget()
    seen = pressed_keys(qt, terminal)
    press(qt, terminal, "Key_T", "ctrl", "shift")
    assert strip_titles(page) == ["1", "2", "+"]
    fresh = page.shells.currentWidget()
    seen_fresh = pressed_keys(qt, fresh)
    Modifier = qt.QtCore.Qt.KeyboardModifier
    for _ in range(4):
        repeat = qt.QtGui.QKeyEvent(
            qt.QtCore.QEvent.Type.KeyPress,
            qt.QtCore.Qt.Key.Key_T,
            Modifier.ControlModifier | Modifier.ShiftModifier,
            "\x14",
            True,
        )
        repeat.ignore()
        qt.QtWidgets.QApplication.sendEvent(fresh, repeat)
    assert strip_titles(page) == ["1", "2", "+"]
    assert qt.QtCore.Qt.Key.Key_T not in seen
    assert qt.QtCore.Qt.Key.Key_T not in seen_fresh


def test_the_shell_keys_answer_in_the_terminal_only_mode(qt, window_shell):
    """The mode hides the chrome and keeps the page (#1152):
    its terminal still answers the new-shell key, and the new
    shell's tab shows under the strip."""
    shell, hub, _window = window_shell
    page = open_page(qt, shell)
    page.terminal_only.enter()
    terminal = page.shells.currentWidget()
    assert page.terminal_only.on
    press(qt, terminal, "Key_T", "ctrl", "shift")
    assert page.terminal_only.on
    assert strip_titles(page) == ["1", "2", "+"]
    assert qt.app.focusWidget() is page.shells.currentWidget()
