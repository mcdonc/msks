"""The create-workspace dialog (#962): the GUI counterpart of
the TUI's create form (#309) — one labeled field per row, blank
fields left to the daemon's defaults, whole numbers checked
locally with the daemon's validation the authority. The hints
(the image catalog, the create defaults) load through the
bridge; a refusal keeps the dialog standing with the failure on
its note line; a landed create closes the dialog and the list
behind it refreshes.

This module imports PySide6 at module scope — the same contract
as the screens' modules: nothing imports it at CLI parse time,
and the shell imports it beside the screens.
"""

import contextlib

from PySide6 import QtCore, QtWidgets

from ..create import invoking_user
from ..data import RestFailure
from ..images import ordered_image_rows

#: The create call's key (#962): every open dialog shares it —
#: an answer lands on the dialog whose flight is open, the
#: closed ones having unhooked and cancelled theirs.
CREATE_KEY = "workspace-create"

#: The hint reads' keys (#962): the image catalog for the image
#: picker, the daemon's defaults for the size placeholders.
IMAGES_KEY = "create-images"
DEFAULTS_KEY = "create-defaults"

#: The form's fields (#309's own set): the key the body carries
#: and the label the row shows, in walk order.
FORM_FIELDS = (
    ("name", "name"),
    ("image", "image ref"),
    ("cpus", "vcpus"),
    ("mem_mib", "memory MiB"),
    ("root_mib", "root MiB"),
    ("home_mib", "home MiB"),
    ("user", "user"),
)

#: The fields whose values must be whole numbers (#309).
INT_FIELDS = frozenset({"cpus", "mem_mib", "root_mib", "home_mib"})

#: The static placeholders the TUI's own hints carry (#309): the
#: sizes' unit and default, the vcpus count.
STATIC_PLACEHOLDERS = {
    "cpus": "default 4",
    "mem_mib": "MiB — 8192",
}

#: The image picker's stand-in entry (#962): an untouched picker
#: stays on it, and its value is nothing — the create body
#: carries no image, and the daemon answers its designated
#: default, the TUI's own blank-select behavior.
DEFAULT_IMAGE_LABEL = "the daemon's default"


def whole_number(value: str) -> bool:
    """Whether a form value is a whole number (#309's own rule):
    ASCII digits only — ``int()`` refuses some unicode digits
    ``isdigit()`` accepts, and a paste can carry them."""
    return value.isascii() and value.isdigit()


def land_value(field: str, value: str, body: dict) -> str | None:
    """One filled value into the body (#962, #309's own rule):
    blank stays unset — the daemon's default — a whole number
    lands as an integer, a bad one answers its note and lands
    nothing."""
    if not value:
        return None
    if field in INT_FIELDS and not whole_number(value):
        return f"{field}: a whole number, or leave it blank"
    body[field] = int(value) if field in INT_FIELDS else value
    return None


def create_body(values: dict) -> tuple[dict | None, str]:
    """The create body from the filled fields (#962, #309's
    arithmetic): only the fields the operator filled, whole
    numbers checked locally — the daemon's validation stays the
    authority. A local refusal answers ``(None, note)``; a
    whole body ``((body), "")``."""
    body: dict = {}
    for field, _label in FORM_FIELDS:
        error = land_value(field, (values.get(field) or "").strip(), body)
        if error is not None:
            return None, error
    if "name" not in body:
        return None, "a workspace name is required"
    return body, ""


def image_plain_refs(rows: list[dict]) -> dict[str, str]:
    """Each reference's plain-value holder: the first delivered
    row of the reference. The catalog lists a reference's rows
    oldest import first, and the daemon's name:version
    resolution scans that same order — so the plain reference
    and the daemon agree on which build it names."""
    plain: dict[str, str] = {}
    for row in rows:
        plain.setdefault(f"{row['name']}:{row['version']}", row["hash"])
    return plain


def row_option(row: dict, plain: dict[str, str]) -> tuple[str, str]:
    """One catalog row as the picker's value and label: the
    reference, riding the hash when another row of the same
    reference holds the daemon's own plain resolution, the
    designated default carrying the asterisk on its label."""
    ref = f"{row['name']}:{row['version']}"
    value = (
        ref if plain[ref] == row["hash"] else (f"{row['name']}@{row['hash']}")
    )
    label = f"{ref} *" if row.get("default") else ref
    return value, label


def image_options(rows: object) -> list[tuple[str, str]]:
    """The image picker's options (#962, the TUI select's own
    arithmetic): one per catalog row, distro ascending and
    newest import first, a shared reference riding the hash in
    every value but the daemon's own pick."""
    listed = list(rows or [])
    plain = image_plain_refs(listed)
    return [row_option(row, plain) for row in ordered_image_rows(listed)]


def size_placeholders(defaults: object) -> dict[str, str]:
    """The size fields' placeholders (#962): the MiB unit beside
    the default a blank field lands on — the TUI's own hint
    shape over the daemon's answer."""
    return {
        field: f"MiB — {defaults[field]}"
        for field in ("root_mib", "home_mib")
        if isinstance(defaults, dict) and field in defaults
    }


def user_placeholder() -> str:
    """The user field's placeholder (#962): the operator's own
    account name, the CLI create's own default. A name the
    login charset refuses — a capitalized or accented one, a
    host getpass cannot answer — leaves the placeholder empty:
    the create read itself surfaces that refusal as a failure
    the note line shows (#309's SystemExit never reaches the
    event loop here)."""
    with contextlib.suppress(SystemExit):
        return invoking_user()
    return ""


class CreateDialog(QtWidgets.QDialog):
    """The create form as a dialog (#962): the TUI create
    popup's counterpart over the workspaces, its exchange and
    its refusals carried the screens' way — calls through the
    bridge, answers over its signals, failures on the note
    line. A dialog closed mid-flight cancels its own call and
    hears nothing after."""

    def __init__(self, bridge) -> None:
        super().__init__()
        self.bridge = bridge
        self.hints_loaded = False
        self.flight = None
        self.setWindowTitle("Create a workspace")
        self.note = QtWidgets.QLabel("create a workspace")
        form = QtWidgets.QFormLayout()
        self.fields: dict[str, QtWidgets.QWidget] = {}
        for field, label in FORM_FIELDS:
            if field == "image":
                picker = QtWidgets.QComboBox()
                picker.addItem(DEFAULT_IMAGE_LABEL, None)
                self.fields[field] = picker
            else:
                edit = QtWidgets.QLineEdit()
                if field in STATIC_PLACEHOLDERS:
                    edit.setPlaceholderText(STATIC_PLACEHOLDERS[field])
                elif field == "user":
                    edit.setPlaceholderText(user_placeholder())
                self.fields[field] = edit
            form.addRow(label, self.fields[field])
        self.create_button = QtWidgets.QPushButton("create")
        self.create_button.setDefault(True)
        self.create_button.clicked.connect(self.submit)
        cancel = QtWidgets.QPushButton("cancel")
        cancel.clicked.connect(self.reject)
        buttons = QtWidgets.QHBoxLayout()
        buttons.addWidget(self.create_button)
        buttons.addWidget(cancel)
        layout = QtWidgets.QVBoxLayout(self)
        layout.addWidget(self.note)
        layout.addLayout(form)
        layout.addLayout(buttons)
        bridge.succeeded.connect(self.on_succeeded)
        bridge.failed.connect(self.on_failed)

    def showEvent(self, event) -> None:
        """The hints load on the first show (#962): the image
        catalog and the daemon's defaults, through the bridge —
        a refused hint leaves the form standing on its static
        placeholders."""
        super().showEvent(event)
        if not self.hints_loaded:
            self.hints_loaded = True
            self.bridge.call(IMAGES_KEY, "images")
            self.bridge.call(DEFAULTS_KEY, "create_defaults")

    def done(self, result: int) -> None:
        """The dialog's close, every path's (#962): the bridge
        hands go — a dialog closed mid-flight cancels its own
        call, so its late answer reaches no one and the next
        dialog's own answers land on it alone."""
        if self.flight is not None and not self.flight.done():
            self.flight.cancel()
        self.bridge.succeeded.disconnect(self.on_succeeded)
        self.bridge.failed.disconnect(self.on_failed)
        super().done(result)

    @QtCore.Slot(str, object)
    def on_succeeded(self, key: str, result: object) -> None:
        """One answered call (#962): a hint fills its field, a
        landed create closes the dialog. Another key's answer is
        not this dialog's to take."""
        if key == CREATE_KEY:
            self.accept()
        elif key in (IMAGES_KEY, DEFAULTS_KEY):
            self.hint_landed(key, result)

    def hint_landed(self, key: str, result: object) -> None:
        """One answered hint (#962): the catalog fills the image
        picker after its default stand-in, the defaults the size
        placeholders."""
        if key == IMAGES_KEY:
            picker = self.fields["image"]
            for value, label in image_options(result):
                picker.addItem(label, value)
        else:
            for field, hint in size_placeholders(result).items():
                self.fields[field].setPlaceholderText(hint)

    @QtCore.Slot(str, object)
    def on_failed(self, key: str, failure: RestFailure) -> None:
        """One refused call (#962): a create refusal keeps the
        dialog standing with the failure on its note line; a
        refused hint notes it and the form stands on its static
        placeholders."""
        if key == CREATE_KEY:
            self.create_button.setEnabled(True)
            self.note.setText(str(failure))
        elif key == IMAGES_KEY:
            self.note.setText(f"image list failed: {failure}")
        elif key == DEFAULTS_KEY:
            self.note.setText(f"defaults failed: {failure}")

    @QtCore.Slot()
    def submit(self) -> None:
        """The filled fields to the daemon (#962): a local
        refusal notes itself and sends nothing; a whole body
        posts through the bridge with the create button held
        until the answer."""
        values = {
            field: (
                self.fields[field].currentData()
                if isinstance(self.fields[field], QtWidgets.QComboBox)
                else self.fields[field].text()
            )
            for field, _label in FORM_FIELDS
        }
        body, error = create_body(values)
        if body is None:
            self.note.setText(error)
            return
        self.create_button.setEnabled(False)
        self.note.setText(f"creating {body['name']} …")
        self.flight = self.bridge.call(CREATE_KEY, "create", body)
