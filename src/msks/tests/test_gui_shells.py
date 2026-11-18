"""The shell-tab tests (#954, #974): the hub that owns a
workspace's terminal sessions, the tab strip that hosts them
inside the page's Shells tab, and the window shell's shell and
external-terminal actions — offscreen, over real sleeper
children through the hub's injected argv factory (the widget
suite's own pattern)."""

import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
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
    carries no Shell button."""
    shell, hub, _window = window_shell
    chrome_buttons = [
        button.text()
        for button in shell.detail.findChildren(type(shell.detail.back_button))
        if button.text()
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
    shell.listing.table.selectRow(0)
    shell.listing.shell_button.click()
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
    """The external-terminal action runs the configured
    launcher with the ssh argv appended — the same argv the
    TUI's windowed shell action builds — detached."""
    shell, hub, _window = launcher_shell
    seen = []
    monkeypatch.setattr(
        qt.QtCore.QProcess,
        "startDetached",
        staticmethod(
            lambda program, args: seen.append((program, args)) or True
        ),
    )
    shell.listing.table.selectRow(0)
    shell.listing.external_button.click()
    from msks.client.gui.termsession import shell_argv

    assert seen == [("theterm", ["-e", *shell_argv("ws-a")])]


def test_the_external_action_names_the_setting_when_unconfigured(
    qt, window_shell
):
    """Without a launcher the notice names the setting: the GUI
    has no terminal of its own to hand the session."""
    shell, hub, _window = window_shell
    shell.open_external("ws-a")
    assert (
        shell.listing.status.text()
        == "set terminal_open_cmd to open external terminals"
    )


def test_the_failed_launcher_flashes(qt, launcher_shell, monkeypatch):
    """A launcher that cannot start says so on the listing's
    status line."""
    shell, hub, _window = launcher_shell
    monkeypatch.setattr(
        qt.QtCore.QProcess,
        "startDetached",
        staticmethod(lambda program, args: False),
    )
    shell.open_external("ws-a")
    assert "failed" in shell.listing.status.text()


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


def test_the_shell_actions_without_a_hold_do_nothing(qt, window_shell):
    """The shell and external actions sit disabled with no row
    held, and their slots send nothing: the window shell's
    paths stay untaken."""
    shell, hub, _window = window_shell
    shell.listing.table.clearSelection()  # no row held
    assert not shell.listing.shell_button.isEnabled()
    assert not shell.listing.external_button.isEnabled()
    seen: list[str] = []
    shell.listing.shell_requested.connect(lambda ws: seen.append(ws))
    shell.listing.external_requested.connect(lambda ws: seen.append(ws))
    shell.listing.shell_selected()
    shell.listing.external_selected()
    assert seen == []


def test_the_page_shell_action_without_a_workspace_sends_nothing(
    qt, window_shell
):
    """The page's shell action with no workspace taken sends
    nothing."""
    shell, hub, _window = window_shell
    seen: list[str] = []
    shell.detail.shell_wanted.connect(lambda ws: seen.append(ws))
    shell.detail.workspace_id = ""
    shell.detail.want_shell()
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
