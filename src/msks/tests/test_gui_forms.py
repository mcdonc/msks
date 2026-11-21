"""The shared popups' own shape (#1014): the yes/no question and
the item picker every screen asks — built over the asking
window, centered on it, No the question's default. The widgets
run under the offscreen platform on every host, the dialog
tests' own fixture shape.
"""

import importlib.util
import os
from types import SimpleNamespace

import pytest

if importlib.util.find_spec("PySide6") is None:
    pytest.skip(
        "the gui extra is not installed on this host",
        allow_module_level=True,
    )

from msks.client.gui.forms import (  # noqa: E402
    confirm,
    confirm_box,
    pick_item,
)


@pytest.fixture()
def qt(tmp_path):
    """The offscreen ``QApplication`` the popups need, the
    dialog tests' own fixture shape (#962)."""
    fonts = tmp_path / "fonts.conf"
    fonts.write_text("<fontconfig/>\n")
    os.environ["FONTCONFIG_FILE"] = str(fonts)
    os.environ["QT_QPA_PLATFORM"] = "offscreen"
    from PySide6 import QtCore, QtWidgets

    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication(
        ["msks-test"]
    )
    ns = SimpleNamespace(app=app, QtCore=QtCore, QtWidgets=QtWidgets, keep=[])
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
    """A shown window at a known off-screen rect — the asker the
    popups center on."""
    window = qt.QtWidgets.QMainWindow()
    window.setGeometry(150, 90, 900, 640)
    window.show()
    qt.keep.append(window)
    return window


def test_the_question_box_carries_yes_no_with_no_the_default(qt) -> None:
    """The question's own box (#1014, #940's own rule): Yes and
    No under it, No the default — the operator's call, not the
    pointer's — parented to the asker and centered on its
    window before the ask shows."""
    window = make_window(qt)
    box = confirm_box(window, "Stop workspace", "Stop beta?")
    qt.keep.append(box)
    yes = qt.QtWidgets.QMessageBox.StandardButton.Yes
    no = qt.QtWidgets.QMessageBox.StandardButton.No
    assert box.parent() is window
    # The title is not asserted: the macOS build of Qt clears a
    # message box's title — Aqua shows the text alone, the
    # static question call met the same rule — so the readback
    # answers empty there. The screens' own tests pin the
    # titles each call passes.
    assert box.text() == "Stop beta?"
    assert (
        box.icon() == qt.QtWidgets.QMessageBox.Icon.Question
    )  # the question static's own icon
    assert box.standardButtons() == (yes | no)
    assert box.standardButton(box.defaultButton()) is no
    assert box.geometry().center() == window.geometry().center()


def test_the_question_answers_yes_or_no(qt) -> None:
    """The question under its modal loop (#1014): the operator's
    No answers False, the Yes answers True — the real box, the
    real loop, driven off-screen the way the images tab's own
    confirmation test drives it."""

    def answer(role) -> None:
        box = qt.app.activeModalWidget()
        if isinstance(box, qt.QtWidgets.QMessageBox):
            for button in box.buttons():
                if box.buttonRole(button) == role:
                    button.click()

    no = qt.QtWidgets.QMessageBox.ButtonRole.NoRole
    yes = qt.QtWidgets.QMessageBox.ButtonRole.YesRole
    window = make_window(qt)
    qt.QtCore.QTimer.singleShot(0, lambda: answer(no))
    assert confirm(window, "Stop workspace", "Stop beta?") is False
    qt.QtCore.QTimer.singleShot(0, lambda: answer(yes))
    assert confirm(window, "Stop workspace", "Stop beta?") is True


def test_the_picker_opens_seeded_and_centered(qt, monkeypatch) -> None:
    """The picker (#1014): the choices in a closed combo over
    the asker's window, the named choice the opening one, and
    the frame centered before the loop — a pick answers itself,
    a cancel answers nothing."""
    window = make_window(qt)
    seen: list[object] = []

    def fake_exec(self) -> int:
        seen.append(self.geometry().center())
        combo = self.findChild(qt.QtWidgets.QComboBox)
        assert combo is not None
        assert not combo.isEditable()
        assert [combo.itemText(i) for i in range(combo.count())] == [
            "6h",
            "1d",
            "7d",
        ]
        assert combo.currentText() == "1d"  # the seeded opening choice
        combo.setCurrentText("7d")
        return qt.QtWidgets.QDialog.DialogCode.Accepted

    monkeypatch.setattr(qt.QtWidgets.QInputDialog, "exec", fake_exec)
    assert pick_item(window, "Renew", "renew for", ["6h", "1d", "7d"], "1d")
    assert seen == [window.geometry().center()]


def test_a_cancelled_picker_decides_nothing(qt, monkeypatch) -> None:
    """The cancelled picker (#1014): a rejected loop answers
    None — the caller's flight decides nothing."""

    def fake_exec(self) -> int:
        return qt.QtWidgets.QDialog.DialogCode.Rejected

    monkeypatch.setattr(qt.QtWidgets.QInputDialog, "exec", fake_exec)
    window = make_window(qt)
    assert (
        pick_item(window, "Renew", "renew for", ["6h", "1d", "7d"], "1d")
        is None
    )
