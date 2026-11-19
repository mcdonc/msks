"""The edit-workspace dialog (#986): the GUI counterpart of
the TUI's edit dialog (#331) — the create dialog's shape
(:mod:`msks.client.gui.create`) seeded from the workspace's
row, the sizes and topology editable, the create-time fields
out. The changed fields send as one resize body through the
route ``msks resize`` drives; a landed resize closes the
dialog and the list behind it refreshes, a refusal keeps it
standing with the failure on its note line — the edit itself
never lost.

The name field follows the daemon: a workspace's row is
immutable after create today (``PATCH``/``PUT`` answer 405,
docs/api.md), so the rename route lands daemon-side first and
the editor hides the field until it does —
:func:`editor_fields` carries the seam, and the field joins
the form the day the route exists.

This module imports PySide6 at module scope — the same contract
as the create dialog's module: nothing imports it at CLI parse
time, and the list screen (itself lazily imported) owns the
import.
"""

import re

from PySide6 import QtCore, QtWidgets

from .dialogs import BridgeClose, whole_number

#: The resize call's key (#986): the one flight an open editor
#: owns — the dialog unhooks on close, so a late answer
#: reaches no one.
EDIT_KEY = "workspace-edit"

#: The editor's size and topology fields (#986, the create
#: form's own set minus its create-time half): the key the
#: body carries and the label the row shows, in walk order.
EDIT_FIELDS = (
    ("cpus", "vcpus"),
    ("mem_mib", "memory MiB"),
    ("root_mib", "root MiB"),
    ("home_mib", "home MiB"),
)

#: The name field's key and label (#986): the row the rename
#: route will edit, first among the fields the day it exists.
NAME_FIELD = "name"
NAME_LABEL = "name"

#: Whether the daemon's rename route exists (#986): the
#: workspace row is immutable after create today, so the field
#: stays off the form until the route lands daemon-side. The
#: flip's follow-up wires the whole name path — the field on
#: the form, the constraints :func:`name_error` already checks
#: wherever a value reaches the body builder, and the send
#: itself; nothing here claims the send before the route does.
RENAME_SUPPORTED = False

#: The name's charset (#986, the daemon's create rule mirrored,
#: ``WORKSPACE_ID_PATTERN`` in the server's schemas): lowercase
#: letters, digits, and inner hyphens, an honest letter or
#: digit first.
NAME_PATTERN = re.compile(r"^[a-z0-9][a-z0-9-]*$")

#: The name's length bound (#986, the daemon's own): at most
#: 64 characters.
NAME_MAX = 64


def editor_fields(
    rename_supported: bool = RENAME_SUPPORTED,
) -> tuple[tuple[str, str], ...]:
    """The editor's fields (#986): the sizes and topology, with
    the name ahead of them while the daemon's rename route
    exists. Until the route lands the editor edits exactly what
    the daemon accepts — the fields the resize route moves."""
    if rename_supported:
        return ((NAME_FIELD, NAME_LABEL), *EDIT_FIELDS)
    return EDIT_FIELDS


def edit_seeds(row: dict) -> dict[str, str]:
    """The fields' seeded values (#986, the TUI edit's own
    arithmetic): every field's current value read off the
    workspace's row as the field's own text — the sizes and
    topology as their numbers, the name as its row fact."""
    seeds: dict[str, str] = {}
    for field, _label in EDIT_FIELDS:
        value = row.get(field)
        seeds[field] = "" if value is None else str(value)
    name = row.get("name")
    seeds[NAME_FIELD] = "" if name is None else str(name)
    return seeds


def name_error(value: str) -> str | None:
    """The name's constraints, checked where the operator can
    still edit (#986, the daemon's create rule mirrored): the
    DNS-label charset and the 64-character bound. A blank value
    keeps the workspace's current name — the rename is a change
    the operator makes, not a field the body requires."""
    if not value:
        return None
    if len(value) > NAME_MAX or not NAME_PATTERN.match(value):
        return (
            "name: lowercase letters, digits, and hyphens — an "
            f"ASCII letter or digit first, at most {NAME_MAX} characters"
        )
    return None


def resize_body(values: dict, seeds: dict) -> tuple[dict | None, str]:
    """The resize body from the editor's fields (#986, the TUI
    edit's own arithmetic): only the fields whose value moved
    off its seed — the resize moves what the operator changed,
    a blank or untouched field keeps its current value — with
    the whole-number rule and the root's grow-only rule said
    locally, where the operator can still edit. A name value
    that reaches the builder takes its constraints here
    (#986): the field itself waits on the daemon's route, and
    an invalid name is refused wherever it comes from. A body
    with nothing moved refuses locally: the daemon's
    nothing-to-resize refusal, said on the note line. A local
    refusal answers ``(None, note)``; a whole body
    ``((body), "")``."""
    name = name_error((values.get(NAME_FIELD) or "").strip())
    if name is not None:
        return None, name
    body, error = sized_body(values, seeds)
    if error is not None:
        return None, error
    return body, ""


def sized_body(values: dict, seeds: dict) -> tuple[dict | None, str | None]:
    """The changed sizes into the body (#986): every field the
    resize route moves, landed through its own rule — the note
    on the first local refusal, the nothing-moved note when no
    field moved."""
    body: dict = {}
    for field, _label in EDIT_FIELDS:
        error = land_field(
            field, (values.get(field) or "").strip(), seeds, body
        )
        if error is not None:
            return None, error
    if not body:
        return None, "nothing to resize — change a size, or cancel"
    return body, None


def land_field(field: str, value: str, seeds: dict, body: dict) -> str | None:
    """One field into the body (#986): a blank field rides
    nothing, a whole value lands unless it states the seed's
    own number — the same size in a different spelling rides
    nothing either, so the daemon sees no no-op it must catch
    up. The note on a local refusal, ``None`` when the field
    landed or rode nothing."""
    if not value:
        return None
    error = field_error(field, value, seeds)
    if error is not None:
        return error
    if int(value) == seeded_number(seeds, field):
        return None
    body[field] = int(value)
    return None


def seeded_number(seeds: dict, field: str) -> int | None:
    """The seed's own number (#986): a row that predates the
    fact seeds blank and reads as no number, so any whole
    value the operator gives it lands."""
    seed = seeds.get(field)
    return None if not seed else int(seed)


def field_error(field: str, value: str, seeds: dict) -> str | None:
    """The moved field's own note (#986): the whole-number
    rule every field checks, then the root's grow-only rule
    the root field adds — ``None`` when the value is sound."""
    if not whole_number(value):
        return f"{field}: a whole number"
    if field != "root_mib":
        return None
    return root_error(value, seeds)


def root_error(asked: str, seeds: dict) -> str | None:
    """The root's grow-only rule (#986, the daemon's own #187
    said locally): the overlay's partition table and filesystem
    belong to the guest, so a smaller root is refused before
    the daemon ever sees it. A row without a root fact — the
    seed reads blank — has no local floor; the daemon owns that
    one."""
    seeded = seeded_number(seeds, "root_mib")
    if seeded is None or int(asked) >= seeded:
        return None
    return f"the root only grows — it is {seeded} MiB"


class EditDialog(BridgeClose, QtWidgets.QDialog):
    """The edit form as a dialog (#986): the create dialog's
    layout seeded from the workspace's row, its exchange
    carried the screens' way — one call through the bridge, the
    answer over its signals, failures on the note line. A
    dialog closed mid-flight cancels its own call and hears
    nothing after."""

    def __init__(self, bridge, row: dict) -> None:
        super().__init__()
        self.bridge = bridge
        self.row = row
        self.seeds = edit_seeds(row)
        self.flight = None
        label = row.get("name") or row["id"]
        self.setWindowTitle("Edit workspace")
        self.note = QtWidgets.QLabel(f"edit {label}")
        form = QtWidgets.QFormLayout()
        self.fields: dict[str, QtWidgets.QWidget] = {}
        for field, field_label in editor_fields():
            edit = QtWidgets.QLineEdit()
            edit.setText(self.seeds[field])
            self.fields[field] = edit
            form.addRow(field_label, edit)
        self.apply_button = QtWidgets.QPushButton("apply")
        self.apply_button.setDefault(True)
        self.apply_button.clicked.connect(self.submit)
        cancel = QtWidgets.QPushButton("cancel")
        cancel.clicked.connect(self.reject)
        buttons = QtWidgets.QHBoxLayout()
        buttons.addWidget(self.apply_button)
        buttons.addWidget(cancel)
        layout = QtWidgets.QVBoxLayout(self)
        layout.addWidget(self.note)
        layout.addLayout(form)
        layout.addLayout(buttons)
        self.hook_bridge(bridge)

    @QtCore.Slot(str, object)
    def on_succeeded(self, key: str, result: object) -> None:
        """One answered call (#986): a landed resize closes the
        dialog — the list behind it refreshes through the
        accepted signal the screen connected. Another key's
        answer is not this dialog's to take."""
        if key == EDIT_KEY:
            self.accept()

    @QtCore.Slot(str, object)
    def on_failed(self, key: str, failure: object) -> None:
        """One refused call (#986): the failure — the bridge's
        carried :class:`~msks.client.data.RestFailure` — lands on
        the note line, the apply button returns, and the dialog
        stands: the operator keeps the edit they made."""
        if key != EDIT_KEY:
            return
        self.apply_button.setEnabled(True)
        self.note.setText(str(failure))

    @QtCore.Slot()
    def submit(self) -> None:
        """The changed fields to the daemon (#986): a local
        refusal notes itself and sends nothing; a whole body
        posts through the bridge with the apply button held
        until the answer."""
        values = {field: self.fields[field].text() for field in self.fields}
        body, error = resize_body(values, self.seeds)
        if body is None:
            self.note.setText(error)
            return
        self.apply_button.setEnabled(False)
        self.note.setText("resizing …")
        self.flight = self.bridge.call(
            EDIT_KEY, "resize", self.row["id"], body
        )
