"""The macOS menu bar's suite (#1007, phase 6 of #924): the
bar's construction and wiring pinned offscreen on every host —
the platform patched to darwin for the build, the other
platforms' windows pinned barless. The host-specific drawing
(the global bar the desktop itself shows) is the platform's
own, not a branch this module carries; the offscreen build
pins everything the client owns."""

import importlib.util
import os
import sys
from types import SimpleNamespace

import pytest

if importlib.util.find_spec("PySide6") is None:
    pytest.skip(
        "PySide6 is not installed on this host",
        allow_module_level=True,
    )

from msks.client.gui import menubar as menubar_mod  # noqa: E402
from msks.client.gui.rows import WINDOW_TITLE  # noqa: E402
from msks.spec.version import __version__  # noqa: E402


@pytest.fixture()
def qt(tmp_path):
    """The offscreen ``QApplication`` the bar needs, the dialog
    tests' own fixture shape (#962)."""
    fonts = tmp_path / "fonts.conf"
    fonts.write_text("<fontconfig/>\n")
    os.environ["FONTCONFIG_FILE"] = str(fonts)
    os.environ["QT_QPA_PLATFORM"] = "offscreen"
    from PySide6 import QtCore, QtGui, QtWidgets

    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication(
        ["msks-test"]
    )
    ns = SimpleNamespace(
        app=app, QtCore=QtCore, QtGui=QtGui, QtWidgets=QtWidgets, keep=[]
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


def make_window(qt) -> qt.QtWidgets.QMainWindow:
    """A shown window the bar installs on — the asker the About
    box centers on and the closer the Quit role answers."""
    window = qt.QtWidgets.QMainWindow()
    window.setGeometry(150, 90, 900, 640)
    window.show()
    qt.keep.append(window)
    return window


def bar_menu(qt, window) -> qt.QtWidgets.QMenu:
    """The installed bar's one menu, the client's own name on
    it — the shape both roles' tests read."""
    menus = window.findChildren(qt.QtWidgets.QMenu)
    assert len(menus) == 1
    assert menus[0].title() == WINDOW_TITLE
    return menus[0]


def role_action(menu, text: str):
    """The named action off the menu — the separator's None
    entry skipped by the text."""
    matches = [action for action in menu.actions() if action.text() == text]
    assert len(matches) == 1
    return matches[0]


def test_the_bar_carries_the_two_standard_roles(
    qt, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The darwin window path builds the bar (#1007): one menu
    under the client's name, the About and Quit roles in it —
    the roles Qt's platform integration seats in the
    application menu — with Quit carrying the platform's
    standard binding, Cmd+Q on macOS. The shortcut lives on the
    bar's own action, so the window carries none beside the
    zoom's."""
    monkeypatch.setattr(sys, "platform", "darwin")
    window = make_window(qt)
    menubar_mod.install_bar(window)
    menu = bar_menu(qt, window)
    about = role_action(menu, f"About {WINDOW_TITLE}")
    quit_action = role_action(menu, f"Quit {WINDOW_TITLE}")
    assert (
        about.menuRole() == qt.QtGui.QAction.MenuRole.AboutRole
    )  # the standard About seat
    assert quit_action.menuRole() == qt.QtGui.QAction.MenuRole.QuitRole
    # The standard Quit binding, read platform-neutrally: the
    # offscreen host spells it for its own platform, so the
    # match — not the spelling — is what pins it.
    standard = qt.QtGui.QKeySequence(qt.QtGui.QKeySequence.StandardKey.Quit)
    assert (
        quit_action.shortcut().matches(standard)
        == qt.QtGui.QKeySequence.SequenceMatch.ExactMatch
    )
    # About carries no shortcut: the role opens from the bar
    # alone, and the window carries no zoom shortcut beside its
    # own three.
    assert about.shortcut().isEmpty()


def test_quit_answers_through_the_windows_close_path(
    qt, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Cmd+Q quits through the window's own close (#1007): the
    role closes the window, and the close is the path the
    application's ``aboutToQuit`` answers — the one that stops
    the bridge and closes every shell (#954)."""
    monkeypatch.setattr(sys, "platform", "darwin")
    window = make_window(qt)
    menubar_mod.install_bar(window)
    assert window.isVisible()
    quit_action = role_action(bar_menu(qt, window), f"Quit {WINDOW_TITLE}")
    quit_action.trigger()
    assert not window.isVisible()  # the window's own close ran


def test_about_opens_the_house_shaped_box(
    qt, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The About role opens the house dialog shape (#1007): a
    box built over the window, centered on it (#1014), the
    client's name and version its text, the information icon
    the ask carries — under its own modal loop."""
    monkeypatch.setattr(sys, "platform", "darwin")
    window = make_window(qt)
    menubar_mod.install_bar(window)
    shown: list[qt.QtWidgets.QMessageBox] = []

    def record_exec(self) -> int:
        shown.append(self)
        return 0

    monkeypatch.setattr(qt.QtWidgets.QMessageBox, "exec", record_exec)
    about = role_action(bar_menu(qt, window), f"About {WINDOW_TITLE}")
    about.trigger()
    assert len(shown) == 1
    box = shown[0]
    assert box.parent() is window
    assert box.text() == menubar_mod.ABOUT_TEXT
    assert box.text() == f"{WINDOW_TITLE} {__version__}"
    assert box.icon() == qt.QtWidgets.QMessageBox.Icon.Information
    assert box.geometry().center() == window.geometry().center()


@pytest.mark.parametrize("platform", ["linux", "win32"])
def test_other_platforms_build_no_bar(
    qt, monkeypatch: pytest.MonkeyPatch, platform: str
) -> None:
    """The bar belongs to macOS (#1007): a Linux or Windows
    window keeps the shape it has — no menu bar built on it,
    read through the widget tree rather than ``menuBar()``,
    whose own call would create the bar it asks about."""
    monkeypatch.setattr(sys, "platform", platform)
    window = make_window(qt)
    menubar_mod.install_bar(window)
    assert window.findChildren(qt.QtWidgets.QMenuBar) == []
