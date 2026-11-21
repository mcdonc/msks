"""The dialogs' shared placement seams (#1014): the centering
rule every popup takes — the dialog's frame moved onto its
parent window's center before the modal loop — and the mixin
that runs it on ``exec``. The widgets run under the offscreen
platform on every host, the dialog tests' own fixture shape.
"""

import importlib.util
import os
from types import SimpleNamespace

import pytest

if importlib.util.find_spec("PySide6") is None:
    pytest.skip(
        "PySide6 is not installed on this host",
        allow_module_level=True,
    )

from msks.client.gui.dialogs import (  # noqa: E402
    Centered,
    center_on_parent,
)


@pytest.fixture()
def qt(tmp_path):
    """The offscreen ``QApplication`` the placement needs, the
    dialog tests' own fixture shape (#962)."""
    fonts = tmp_path / "fonts.conf"
    fonts.write_text("<fontconfig/>\n")
    os.environ["FONTCONFIG_FILE"] = str(fonts)
    os.environ["QT_QPA_PLATFORM"] = "offscreen"
    from PySide6 import QtWidgets

    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication(
        ["msks-test"]
    )
    ns = SimpleNamespace(app=app, QtWidgets=QtWidgets, keep=[])
    yield ns
    del ns.keep[:]
    for widget in app.topLevelWidgets():
        widget.close()
        widget.deleteLater()
    from PySide6 import QtCore

    QtCore.QCoreApplication.sendPostedEvents(
        None, QtCore.QEvent.Type.DeferredDelete
    )
    app.processEvents()


def make_window(qt) -> qt.QtWidgets.QMainWindow:
    """A shown window at a known off-screen rect — the parent
    the dialogs center on."""
    window = qt.QtWidgets.QMainWindow()
    window.setGeometry(200, 100, 800, 600)
    window.show()
    qt.keep.append(window)
    return window


def test_centering_moves_a_dialog_onto_the_windows_center(qt) -> None:
    """The placement rule (#1014): a dialog parented to a widget
    of a window moves until its center is the window's center —
    the bottom-right corner a corner-placing manager would
    answer with holds no more."""
    window = make_window(qt)
    dialog = qt.QtWidgets.QDialog(window)
    qt.keep.append(dialog)
    center_on_parent(dialog)
    assert dialog.geometry().center() == window.geometry().center()


def test_a_dialog_deeper_in_the_window_centers_the_same(qt) -> None:
    """The parent chain (#1014): a dialog parented to a widget
    deep in the window — the tab that opens it, not the window
    itself — centers on the window the tab lives in."""
    window = make_window(qt)
    page = qt.QtWidgets.QWidget(window)
    window.setCentralWidget(page)
    dialog = qt.QtWidgets.QDialog(page)
    qt.keep.append(dialog)
    center_on_parent(dialog)
    assert dialog.geometry().center() == window.geometry().center()


def test_a_parentless_dialog_stays_put(qt) -> None:
    """The parentless dialog (#1014): nothing names a window to
    sit on, so the seam moves nothing — the dialog stands where
    the manager put it."""
    dialog = qt.QtWidgets.QDialog()
    qt.keep.append(dialog)
    spot = dialog.geometry()
    center_on_parent(dialog)
    assert dialog.geometry() == spot


def test_the_mixin_centers_on_exec(qt, monkeypatch) -> None:
    """The mixin (#1014): a dialog class that takes ``Centered``
    centers itself on its opener's window when ``exec`` opens
    it, before the modal loop starts — and the loop's own
    answer passes through."""

    class Placed(Centered, qt.QtWidgets.QDialog):
        pass

    window = make_window(qt)
    dialog = Placed(window)
    qt.keep.append(dialog)
    seen: list[object] = []

    def fake_exec(self) -> int:
        seen.append(self.geometry().center())
        return 7

    monkeypatch.setattr(qt.QtWidgets.QDialog, "exec", fake_exec)
    assert dialog.exec() == 7
    assert seen == [window.geometry().center()]
