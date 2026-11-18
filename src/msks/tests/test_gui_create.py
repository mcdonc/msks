"""The create-workspace dialog (#962): the TUI create form's
counterpart — the plain-body arithmetic, the hint reads, the
submit and refusal paths — driven over a stub bridge, the
screens' own fixture shape. The widgets run under the offscreen
platform on every host.
"""

import importlib.util
import os
from concurrent.futures import Future
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

    def disconnect(self, slot) -> None:
        self.slots.remove(slot)

    def emit(self, *args) -> None:
        for slot in self.slots:
            slot(*args)


class StubBridge:
    """The bridge's surface for the dialog's tests: the signals
    it reads and the one verb it calls, every call recorded."""

    def __init__(self) -> None:
        self.calls: list[tuple] = []
        self.flights: list[Future] = []
        self.succeeded = FakeSignal()
        self.failed = FakeSignal()

    def call(self, key: str, method: str, *args: object) -> Future:
        self.calls.append((key, method, args))
        self.flights.append(Future())
        return self.flights[-1]


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


def test_only_ascii_digits_are_whole_numbers() -> None:
    """The whole-number rule (#962, the TUI's own hazard):
    unicode digits a paste can carry refuse locally — ``int``
    would crash on some of them and the daemon would take the
    rest as numbers the TUI's own form refuses."""
    for pasted in ("\u00b2", "\u0663", "+3", "-3", "3.0"):
        body, error = create_body({"name": "a", "cpus": pasted})
        assert body is None, pasted
        assert error == "cpus: a whole number, or leave it blank"


def test_the_image_options_mark_the_default() -> None:
    """The picker's options (#962, the TUI select's own
    arithmetic): distro ascending, newest import first within
    it, the designated default's asterisk on its label alone,
    and a reference two builds share riding the hash in every
    value but the daemon's own pick."""
    rows = [
        {
            "name": "msks",
            "version": "41",
            "hash": "h-old",
            "imported": "2026-01-01T00:00:00",
        },
        {
            "name": "msks",
            "version": "41",
            "hash": "h-new",
            "imported": "2026-02-01T00:00:00",
            "default": True,
        },
        {
            "name": "alt",
            "version": "9",
            "hash": "h-alt",
            "imported": "2026-03-01T00:00:00",
        },
    ]
    assert image_options(rows) == [
        ("alt:9", "alt:9"),
        # newest first within the group; the daemon's own
        # name:version resolution picks the oldest import
        # (h-old), so the newer build rides the hash
        ("msks@h-new", "msks:41 *"),
        ("msks:41", "msks:41"),
    ]
    assert image_options(None) == []


def test_a_name_the_charset_refuses_leaves_the_placeholder_empty(
    monkeypatch,
) -> None:
    """The placeholder's own refusal (#962): a login name the
    charset refuses — the CLI create's own SystemExit — leaves
    the field's hint empty instead of taking the window down;
    the create read itself surfaces the refusal as a failure."""
    from msks.client.gui import create as create_mod

    def refused() -> str:
        raise SystemExit("msks: your username")

    monkeypatch.setattr(create_mod, "invoking_user", refused)
    assert create_mod.user_placeholder() == ""


def test_the_size_placeholders_read_the_defaults() -> None:
    """The placeholders (#962): the MiB unit beside the default
    a blank field lands on; a defaults answer without the field
    leaves the field its static hint."""
    assert size_placeholders({"root_mib": 8192}) == {"root_mib": "MiB — 8192"}
    assert size_placeholders({}) == {}


def test_the_dialog_loads_its_hints_on_show(qt) -> None:
    """The hint reads (#962): the first show asks for the image
    catalog and the create defaults; the catalog fills the
    picker behind its default stand-in, the defaults the size
    placeholders. An untouched picker stays on the stand-in —
    the create body carries no image, and the daemon answers
    its designated default."""
    dialog, bridge = make_dialog(qt)
    assert bridge.calls == [
        (IMAGES_KEY, "images", ()),
        (DEFAULTS_KEY, "create_defaults", ()),
    ]
    picker = dialog.fields["image"]
    assert picker.itemText(0) == "the daemon's default"
    assert picker.currentData() is None
    bridge.succeeded.emit(
        IMAGES_KEY,
        [
            {"name": "msks", "version": "41", "hash": "h1", "default": True},
        ],
    )
    assert picker.count() == 2
    assert picker.itemText(1) == "msks:41 *"
    assert picker.currentIndex() == 0
    bridge.succeeded.emit(DEFAULTS_KEY, {"root_mib": 4096})
    assert dialog.fields["root_mib"].placeholderText() == "MiB — 4096"
    # A second show loads the hints once only: the reads the
    # first show issued stand.
    reads = list(bridge.calls)
    dialog.hide()
    dialog.show()
    assert bridge.calls == reads
    # An untouched picker submits no image: the daemon answers
    # its designated default.
    fill(dialog, {"name": "alpha"})
    dialog.submit()
    assert dialog.bridge.calls[-1] == (
        CREATE_KEY,
        "create",
        ({"name": "alpha"},),
    )


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


def test_a_dialog_closed_mid_flight_cancels_its_own_call(qt) -> None:
    """The closed dialog hears nothing (#962): a cancel while
    the create is in flight cancels the call through its
    handle, and the late answer — had it raced the cancel —
    reaches no dialog: the bridge's hands are off, and the
    next dialog opened takes only its own answers."""
    dialog, bridge = make_dialog(qt)
    fill(dialog, {"name": "alpha"})
    dialog.submit()
    flight = dialog.flight
    assert flight is not None
    dialog.reject()
    assert flight.cancelled()
    # A late answer on the shared key reaches the closed
    # dialog's unhooked slots: nothing.
    second, _bridge2 = make_dialog(qt)
    landed: list[object] = []
    second.accepted.connect(lambda: landed.append(True))
    bridge.succeeded.emit(CREATE_KEY, {"id": "ws-a"})
    assert landed == []  # the closed dialog's answer is no one's
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
