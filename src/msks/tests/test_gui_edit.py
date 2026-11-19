"""The edit-workspace dialog (#986): the create dialog's shape
seeded from the row — the plain-body arithmetic (the changed
fields alone, the root's grow-only rule, the name's constraints
against the day the rename route lands), the submit and
refusal paths — driven over a stub bridge, the dialog tests'
own fixture shape. The widgets run under the offscreen platform
on every host.
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

from msks.client.gui.edit import (  # noqa: E402
    EDIT_FIELDS,
    EDIT_KEY,
    EditDialog,
    edit_seeds,
    editor_fields,
    name_error,
    resize_body,
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


def daemon_row() -> dict:
    """One workspace's row, every field the editor seeds from."""
    return {
        "id": "ws-1",
        "name": "alpha",
        "status": "stopped",
        "cpus": 2,
        "mem_mib": 8192,
        "root_mib": 8192,
        "home_mib": 16384,
    }


def failure(message: str, *, reach: bool = True) -> object:
    """A refused call's shape: the failure class the bridge
    carries, built the suite's stub way."""
    from msks.client.data import RestFailure

    return RestFailure(message, reach=reach)


def make_dialog(qt) -> tuple:
    """The dialog over the stub bridge, shown — the suite's own
    window-less shape: a dialog is a top-level of its own."""
    bridge = StubBridge()
    dialog = EditDialog(bridge, daemon_row())
    dialog.show()
    qt.keep.append(dialog)
    return dialog, bridge


def fill(dialog, values: dict) -> None:
    """The fields' text set."""
    for field, value in values.items():
        dialog.fields[field].setText(value)


def test_the_seeds_read_the_row() -> None:
    """The seeded values (#986, the TUI edit's own arithmetic):
    every field's current value as its own text, a row that
    predates a fact seeding its blank."""
    seeds = edit_seeds(daemon_row())
    assert seeds == {
        "name": "alpha",
        "cpus": "2",
        "mem_mib": "8192",
        "root_mib": "8192",
        "home_mib": "16384",
    }
    bare = daemon_row()
    bare.update(name=None, cpus=None, mem_mib=None, root_mib=None)
    assert edit_seeds(bare)["name"] == ""
    assert edit_seeds(bare)["root_mib"] == ""


def test_the_editor_fields_follow_the_rename_route() -> None:
    """The editor's fields (#986): the sizes and topology — the
    fields the resize route moves — with the name ahead of them
    the day the daemon's rename route exists; until it lands the
    form edits exactly what the daemon accepts."""
    assert editor_fields() == EDIT_FIELDS
    assert editor_fields(rename_supported=True) == (
        ("name", "name"),
        *EDIT_FIELDS,
    )


def test_the_body_moves_only_changed_fields() -> None:
    """The resize body's arithmetic (#986, the TUI edit's own):
    a field left at its seed rides nothing — the resize moves
    what the operator changed, a re-spelled seed the same number
    rides nothing either, so the daemon sees no no-op — whole
    numbers land as integers, and a body with nothing moved
    refuses locally, the daemon's nothing-to-resize refusal said
    where the operator can still edit."""
    seeds = edit_seeds(daemon_row())
    body, error = resize_body({"cpus": " 4 ", "mem_mib": "8192"}, seeds)
    assert (body, error) == ({"cpus": 4}, "")
    assert resize_body({"home_mib": "8192"}, seeds) == (
        {"home_mib": 8192},
        "",
    )
    assert resize_body({"root_mib": "08192"}, seeds) == (
        None,
        "nothing to resize — change a size, or cancel",
    )
    assert resize_body({}, seeds) == (
        None,
        "nothing to resize — change a size, or cancel",
    )
    assert resize_body({"cpus": "many"}, seeds) == (
        None,
        "cpus: a whole number",
    )


def test_the_root_only_grows() -> None:
    """The root's grow-only rule (#986, the daemon's own #187
    said locally): a smaller root is refused with the current
    size named, a larger one lands, and a row without a root
    fact has no local floor — the daemon owns that one. A blank
    field keeps the current value, whatever the operator meant
    by clearing it."""
    seeds = edit_seeds(daemon_row())
    assert resize_body({"root_mib": "4096"}, seeds) == (
        None,
        "the root only grows — it is 8192 MiB",
    )
    assert resize_body({"root_mib": "16384"}, seeds) == (
        {"root_mib": 16384},
        "",
    )
    assert resize_body({"root_mib": ""}, seeds)[0] is None
    bare = edit_seeds({**daemon_row(), "root_mib": None})
    assert resize_body({"root_mib": "1024"}, bare) == (
        {"root_mib": 1024},
        "",
    )


def test_the_names_constraints() -> None:
    """The name's constraints (#986, the daemon's create rule
    mirrored): the DNS-label charset and the 64-character bound,
    checked wherever a value reaches the body builder — ready
    for the day the rename route lands the field. A blank value
    keeps the workspace's current name, so it carries no
    error."""
    assert name_error("alpha") is None
    assert name_error("a-1") is None
    assert name_error("2fast") is None
    assert name_error("") is None
    assert name_error("Alpha") is not None
    assert name_error("-lead") is not None
    assert name_error("under_score") is not None
    assert name_error("a" * 65) is not None
    assert name_error("a" * 64) is None
    # The builder takes the constraints wherever the value came
    # from: an invalid name refuses the whole body before any
    # size is read.
    seeds = edit_seeds(daemon_row())
    assert resize_body({"name": "Bad!", "cpus": "4"}, seeds) == (
        None,
        "name: lowercase letters, digits, and hyphens — an ASCII "
        "letter or digit first, at most 64 characters",
    )


def test_the_dialog_opens_seeded_and_submits_a_resize(qt) -> None:
    """The dialog over the row (#986): the fields open carrying
    the row's current values, the changed ones send as one body
    through the resize route with the apply button held, and the
    daemon's answer closes the dialog."""
    dialog, bridge = make_dialog(qt)
    assert dialog.note.text() == "edit alpha"
    assert dialog.fields["cpus"].text() == "2"
    assert dialog.fields["root_mib"].text() == "8192"
    assert "name" not in dialog.fields  # the route is not there yet
    fill(dialog, {"cpus": "4", "root_mib": "16384"})
    dialog.submit()
    assert bridge.calls == [
        (EDIT_KEY, "resize", ("ws-1", {"cpus": 4, "root_mib": 16384}))
    ]
    assert not dialog.apply_button.isEnabled()
    closed: list[bool] = []
    dialog.accepted.connect(lambda: closed.append(True))
    bridge.succeeded.emit(EDIT_KEY, {"status": "stopped"})
    assert closed == [True]


def test_a_local_refusal_sends_nothing(qt) -> None:
    """A local refusal (#986): the note carries it and the
    bridge hears no call — a shrink of the root never leaves the
    dialog, and an untouched form names the nothing it would
    send."""
    dialog, bridge = make_dialog(qt)
    fill(dialog, {"root_mib": "1024"})
    dialog.submit()
    assert dialog.note.text() == "the root only grows — it is 8192 MiB"
    assert bridge.calls == []
    fill(dialog, {"root_mib": "8192"})
    dialog.submit()
    assert dialog.note.text() == (
        "nothing to resize — change a size, or cancel"
    )
    assert bridge.calls == []


def test_a_refused_resize_keeps_the_dialog_standing(qt) -> None:
    """A refused resize (#986): the failure lands on the note
    line, the apply button returns, and the dialog stands — the
    operator keeps the edit they made."""
    dialog, bridge = make_dialog(qt)
    fill(dialog, {"cpus": "4"})
    dialog.submit()
    bridge.failed.emit(
        EDIT_KEY, failure("workspace ws-1's VMM reports running")
    )
    assert "VMM reports running" in dialog.note.text()
    assert dialog.apply_button.isEnabled()
    # The edit stands: a retry sends the same body.
    bridge.calls.clear()
    dialog.submit()
    assert bridge.calls == [(EDIT_KEY, "resize", ("ws-1", {"cpus": 4}))]


def test_another_keys_answer_is_not_the_dialogs_to_take(qt) -> None:
    """The bridge is shared (#986): a foreign key's answer and a
    foreign key's failure change nothing — the screens that own
    those keys take them."""
    dialog, bridge = make_dialog(qt)
    note = dialog.note.text()
    bridge.succeeded.emit("workspaces-list", [])
    bridge.failed.emit("detail-health", failure("not this dialog's"))
    assert dialog.note.text() == note
    assert dialog.apply_button.isEnabled()


def test_a_dialog_closed_mid_flight_cancels_its_own_call(qt) -> None:
    """The closed dialog hears nothing (#986): a cancel while
    the resize is in flight cancels the call through its handle,
    and a late answer reaches no dialog — the bridge's hands are
    off, and the next dialog opened takes only its own
    answers."""
    dialog, bridge = make_dialog(qt)
    fill(dialog, {"cpus": "4"})
    dialog.submit()
    flight = dialog.flight
    assert flight is not None
    dialog.reject()
    assert flight.cancelled()
    second, _bridge2 = make_dialog(qt)
    landed: list[object] = []
    second.accepted.connect(lambda: landed.append(True))
    bridge.succeeded.emit(EDIT_KEY, {"status": "stopped"})
    assert landed == []  # the closed dialog's answer is no one's


def test_a_close_qt_re_enters_unhooks_nothing_twice(qt) -> None:
    """The close path's once-only hands (#986): Qt can re-enter
    ``done`` — a close delivered beside the accept that already
    closed the dialog — and the second pass finds nothing left
    to unhook instead of failing to recognize its own
    hookup."""
    dialog, _bridge = make_dialog(qt)
    fill(dialog, {"cpus": "4"})
    dialog.submit()
    dialog.done(1)  # the accept path's own close
    dialog.done(0)  # a re-entered close: no raise, no residue
    assert dialog.hooks is None
