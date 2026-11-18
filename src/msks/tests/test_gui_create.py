"""The create-workspace dialog (#962): the TUI create form's
counterpart — the plain-body arithmetic, the hint reads, the
submit and refusal paths — driven over a stub bridge, the
screens' own fixture shape. The widgets run under the offscreen
platform on every host.
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

from msks.client.gui.create import (  # noqa: E402
    CREATE_KEY,
    DEFAULTS_KEY,
    FORM_FIELDS,
    IMAGES_KEY,
    CreateDialog,
    create_body,
    image_options,
    size_placeholders,
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
    """The bridge's surface for the dialog's tests: the signals
    it reads and the one verb it calls, every call recorded."""

    def __init__(self) -> None:
        self.calls: list[tuple] = []
        self.succeeded = FakeSignal()
        self.failed = FakeSignal()

    def call(self, key: str, method: str, *args: object) -> None:
        self.calls.append((key, method, args))


@pytest.fixture()
def qt(tmp_path):
    """The offscreen ``QApplication`` the dialog needs, the
    screens' own fixture shape (#939)."""
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


def make_dialog(qt) -> tuple:
    """The dialog over the stub bridge, shown — its hint reads
    issued — in the suite's own window-less shape: a dialog is a
    top-level of its own."""
    bridge = StubBridge()
    dialog = CreateDialog(bridge)
    dialog.show()
    qt.keep.append(dialog)
    return dialog, bridge


def fill(dialog, values: dict) -> None:
    """The fields' text set, the image picker's item picked by
    its data value."""
    for field, _label in FORM_FIELDS:
        widget = dialog.fields[field]
        if hasattr(widget, "setCurrentIndex"):
            for index in range(widget.count()):
                if widget.itemData(index) == values.get(field):
                    widget.setCurrentIndex(index)
        elif field in values:
            widget.setText(values[field])


def failure(message: str, *, reach: bool = True) -> object:
    """A refused call's shape: the failure class the bridge
    carries, built the suite's stub way."""
    from msks.client.data import RestFailure

    return RestFailure(message, reach=reach)


def test_blank_fields_stay_the_daemons_defaults() -> None:
    """The body's arithmetic (#962, #309's own): blank fields
    stay unset — the daemon's defaults answer — whole numbers
    land as integers, and a body without a name refuses
    locally."""
    body, error = create_body({"name": "alpha", "cpus": " 2 "})
    assert body == {"name": "alpha", "cpus": 2}
    assert error == ""
    body, error = create_body({"cpus": "2"})
    assert body is None
    assert error == "a workspace name is required"
    body, error = create_body({"name": "a", "root_mib": "half"})
    assert body is None
    assert error == "root_mib: a whole number, or leave it blank"
    assert create_body({}) == (None, "a workspace name is required")


def test_the_image_options_mark_the_default() -> None:
    """The picker's options (#962): the reference as the value,
    the designated default's asterisk on its label alone."""
    rows = [
        {"name": "msks", "version": "41", "default": True},
        {"name": "msks", "version": "40"},
    ]
    assert image_options(rows) == [
        ("msks:41", "msks:41 *"),
        ("msks:40", "msks:40"),
    ]
    assert image_options(None) == []


def test_the_size_placeholders_read_the_defaults() -> None:
    """The placeholders (#962): the MiB unit beside the default
    a blank field lands on; a defaults answer without the field
    leaves the field its static hint."""
    assert size_placeholders({"root_mib": 8192}) == {"root_mib": "MiB — 8192"}
    assert size_placeholders({}) == {}


def test_the_dialog_loads_its_hints_on_show(qt) -> None:
    """The hint reads (#962): the first show asks for the image
    catalog and the create defaults; the catalog fills the
    picker, the defaults the size placeholders."""
    dialog, bridge = make_dialog(qt)
    assert bridge.calls == [
        (IMAGES_KEY, "images", ()),
        (DEFAULTS_KEY, "create_defaults", ()),
    ]
    bridge.succeeded.emit(
        IMAGES_KEY,
        [{"name": "msks", "version": "41", "default": True}],
    )
    combo = dialog.fields["image"]
    assert combo.count() == 1
    assert combo.itemText(0) == "msks:41 *"
    bridge.succeeded.emit(DEFAULTS_KEY, {"root_mib": 4096})
    assert dialog.fields["root_mib"].placeholderText() == "MiB — 4096"
    # A second show loads the hints once only: the reads the
    # first show issued stand.
    dialog.hide()
    dialog.show()
    assert bridge.calls == [
        (IMAGES_KEY, "images", ()),
        (DEFAULTS_KEY, "create_defaults", ()),
    ]


def test_a_submit_posts_the_body_and_a_landed_create_closes(
    qt,
) -> None:
    """The submit path (#962): a whole body posts through the
    bridge with the create button held; the daemon's answer
    closes the dialog."""
    dialog, bridge = make_dialog(qt)
    fill(dialog, {"name": "alpha", "mem_mib": "4096"})
    dialog.submit()
    assert bridge.calls[-1] == (
        CREATE_KEY,
        "create",
        ({"name": "alpha", "mem_mib": 4096},),
    )
    assert not dialog.create_button.isEnabled()
    closed: list[bool] = []
    dialog.accepted.connect(lambda: closed.append(True))
    bridge.succeeded.emit(CREATE_KEY, {"id": "ws-a"})
    assert closed == [True]


def test_a_local_refusal_sends_nothing(qt) -> None:
    """A local refusal (#962): the note carries it and the
    bridge hears no call — the daemon's validation stays the
    authority, but its surface sees only whole bodies."""
    dialog, bridge = make_dialog(qt)
    bridge.calls.clear()
    fill(dialog, {"name": "alpha", "cpus": "many"})
    dialog.submit()
    assert dialog.note.text() == "cpus: a whole number, or leave it blank"
    assert bridge.calls == []


def test_another_keys_answer_is_not_the_dialogs_to_take(qt) -> None:
    """The bridge is shared (#962): a foreign key's answer and
    a foreign key's failure change nothing — the screens that
    own those keys take them."""
    dialog, bridge = make_dialog(qt)
    fill(dialog, {"name": "alpha"})
    note = dialog.note.text()
    bridge.succeeded.emit("workspaces-list", [])
    bridge.failed.emit("detail-health", failure("not this dialog's"))
    assert dialog.note.text() == note
    assert dialog.create_button.isEnabled()


def test_a_refused_create_keeps_the_dialog_standing(qt) -> None:
    """A refused create (#962): the failure lands on the note
    line, the create button returns, and the dialog stands —
    the operator keeps the fields they filled."""
    dialog, bridge = make_dialog(qt)
    fill(dialog, {"name": "alpha"})
    dialog.submit()
    bridge.failed.emit(CREATE_KEY, failure("name taken"))
    assert dialog.note.text() == "name taken"
    assert dialog.create_button.isEnabled()


def test_a_refused_hint_notes_it_and_the_form_stands(qt) -> None:
    """A refused hint (#962): the note carries the failure and
    the fields keep their static placeholders — a blank image
    still creates against the daemon's default."""
    dialog, bridge = make_dialog(qt)
    bridge.failed.emit(IMAGES_KEY, failure("the daemon is down"))
    assert dialog.note.text() == "image list failed: the daemon is down"
    bridge.failed.emit(DEFAULTS_KEY, failure("no defaults"))
    assert "defaults failed" in dialog.note.text()
