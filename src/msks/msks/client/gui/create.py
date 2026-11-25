"""The create-workspace dialog (#962): the GUI counterpart of
the TUI's create form (#309) — one labeled field per row, blank
fields left to the daemon's defaults, sizes read in the human
units the rest of the client speaks (#1164), whole numbers
checked locally with the daemon's validation the authority.
The hints (the image catalog, the create defaults) load
through the bridge; a refusal keeps the dialog standing with
the failure on its note line; a landed create closes the
dialog and the list behind it refreshes. A summary line under
the fields restates the four sizes as they are typed, the
defaults filling the blanks.

This module imports PySide6 at module scope — the same contract
as the screens' modules: nothing imports it at CLI parse time,
and the shell imports it beside the screens.
"""

import contextlib

from PySide6 import QtCore, QtGui, QtWidgets

from ..create import invoking_user
from ..data import RestFailure
from ..images import ordered_image_rows
from .dialogs import (
    SIZE_FIELDS,
    BridgeClose,
    Centered,
    mib_text,
    parse_size,
    read_size,
    seed_size,
    whole_number,
)

#: The create call's key (#962): every open dialog shares it —
#: an answer lands on the dialog whose flight is open, the
#: closed ones having unhooked and cancelled theirs.
CREATE_KEY = "workspace-create"

#: The hint reads' keys (#962): the image catalog for the image
#: picker, the daemon's defaults for the size placeholders.
IMAGES_KEY = "create-images"
DEFAULTS_KEY = "create-defaults"

#: The form's fields (#309's own set): the key the body carries
#: and the label the row shows, in walk order — the sizes under
#: the names people think in (#1164), the unit in the
#: placeholder.
FORM_FIELDS = (
    ("name", "Name"),
    ("image", "Image ref"),
    ("cpus", "VCPUs"),
    ("mem_mib", "Memory"),
    ("root_mib", "Root disk"),
    ("home_mib", "Home disk"),
    ("user", "User"),
)

#: The defaults a blank field lands on that the client knows
#: before the daemon answers (#309's own hints): the vcpu count
#: and the memory. The root and home join when the create
#: defaults answer lands (#1164).
STATIC_DEFAULTS = {"cpus": 4, "mem_mib": 8192}

#: The summary line's parts (#1164), in the form's order: the
#: field and the role its value reads beside.
SUMMARY_PARTS = (
    ("cpus", "vCPU"),
    ("mem_mib", "RAM"),
    ("root_mib", "root"),
    ("home_mib", "home"),
)

#: The fields the summary reads (#1164): the line's own parts'
#: fields, the ones whose edits restate it.
SUMMARY_FIELDS = frozenset(field for field, _role in SUMMARY_PARTS)

#: The summary line's separator (#1164): the quick-facts
#: strip's own middle dot.
SUMMARY_SEP = " · "

#: The summary's stand-in for a default the daemon has not
#: named yet (#1164): the footer strip's own honest dash.
UNKNOWN = "—"

#: The image picker's stand-in entry (#962): an untouched picker
#: stays on it, and its value is nothing — the create body
#: carries no image, and the daemon answers its designated
#: default, the TUI's own blank-select behavior.
DEFAULT_IMAGE_LABEL = "the daemon's default"


def placeholder_text(field: str, default: int) -> str:
    """One field's placeholder (#1164): the default a blank
    field lands on — the count for the vcpus, the human
    spelling for a size (`default 8G`, `default 8193M` for a
    size the GiB spelling would round)."""
    if field not in SIZE_FIELDS:
        return f"default {default}"
    return f"default {seed_size(default)}"


#: The static placeholders the TUI's own hints carry (#309): the
#: defaults a blank field lands on, in the unit the field reads.
STATIC_PLACEHOLDERS = {
    field: placeholder_text(field, default)
    for field, default in STATIC_DEFAULTS.items()
}


def land_value(field: str, value: str, body: dict) -> str | None:
    """One filled value into the body (#962, #309's own rule,
    #1164's size forms): blank stays unset — the daemon's
    default — a value lands as :func:`read_value` reads it, a
    bad one answers its note and lands nothing."""
    if not value:
        return None
    landed, error = read_value(field, value)
    if error is not None:
        return error
    body[field] = landed
    return None


def read_value(field: str, value: str) -> tuple[object, str | None]:
    """One filled field's value for the body and its refusal
    note (#1164): a size as its MiB count, the vcpu count as a
    whole number, any other field as its text — the note in
    place of the value when the form refuses."""
    if field in SIZE_FIELDS:
        return read_size(field, value)
    if field != "cpus":
        return value, None
    if whole_number(value):
        return int(value), None
    return None, f"{field}: a whole number, or leave it blank"


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
    """The size fields' placeholders (#962, #1164): the default
    a blank field lands on, in the unit the field reads —
    `default 4G` — over the daemon's answer. A field the answer
    leaves out keeps whatever placeholder it had."""
    return {
        field: placeholder_text(field, mib)
        for field, mib in size_defaults(defaults).items()
    }


def size_defaults(defaults: object) -> dict[str, int]:
    """The sizes the create defaults answer names (#1164), as
    the summary line reads them: the root and home MiB counts
    under their fields; an answer without a whole number for a
    field names nothing for it."""
    return {
        field: defaults[field]
        for field in ("root_mib", "home_mib")
        if isinstance(defaults, dict) and isinstance(defaults.get(field), int)
    }


def summary_text(values: dict, defaults: dict) -> str:
    """The live summary line (#1164): the four sizes as they
    stand, each field's own value when it reads, the default
    filling a blank, a dash until the daemon's answer names
    one — the vcpu count, a size in GiB and MiB as
    :func:`mib_text` spells it (`0.5G` reading `512 MiB`, a
    size the GiB spelling would round in its MiB count). A
    value mid-typing that does not read yet shows as typed, so
    the line follows every keystroke."""
    return SUMMARY_SEP.join(
        summary_part(field, role, values, defaults)
        for field, role in SUMMARY_PARTS
    )


def summary_part(field: str, role: str, values: dict, defaults: dict) -> str:
    """One field's slot on the summary line (#1164): the typed
    value, or the default filling a blank, beside the role."""
    typed = (values.get(field) or "").strip()
    if typed:
        return f"{typed_text(field, typed)} {role}"
    return f"{default_text(field, defaults.get(field))} {role}"


def typed_text(field: str, typed: str) -> str:
    """A typed value as the line shows it (#1164): the vcpu
    count as typed, a size in its resolved MiB when its form
    reads and as typed while it does not."""
    if field not in SIZE_FIELDS:
        return typed
    mib = parse_size(typed)
    return typed if mib is None else mib_text(mib)


def default_text(field: str, default: int | None) -> str:
    """A blank's default as the line shows it (#1164): a dash
    until the answer names one, the count for the vcpus, the
    size as :func:`mib_text` spells it."""
    if default is None:
        return UNKNOWN
    if field in SIZE_FIELDS:
        return mib_text(default)
    return str(default)


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


def placeholder_for(field: str) -> str:
    """A field's standing placeholder (#962, #1164): the static
    default where the client knows one, the operator's own
    account name for the user, nothing for a size field whose
    default the daemon's answer names."""
    if field in STATIC_PLACEHOLDERS:
        return STATIC_PLACEHOLDERS[field]
    if field == "user":
        return user_placeholder()
    return ""


def field_widget(field: str, on_edit) -> QtWidgets.QWidget:
    """One form field's widget (#962, #1164): the image picker
    for the image, a line edit for the rest — its standing
    placeholder, and the summary restated on every edit of a
    field the line reads."""
    if field == "image":
        picker = QtWidgets.QComboBox()
        picker.addItem(DEFAULT_IMAGE_LABEL, None)
        return picker
    edit = QtWidgets.QLineEdit()
    edit.setPlaceholderText(placeholder_for(field))
    if field in SUMMARY_FIELDS:
        edit.textChanged.connect(on_edit)
    return edit


class CreateDialog(BridgeClose, Centered, QtWidgets.QDialog):
    """The create form as a dialog (#962): the TUI create
    popup's counterpart over the workspaces, its exchange and
    its refusals carried the screens' way — calls through the
    bridge, answers over its signals, failures on the note
    line. A dialog closed mid-flight cancels its own call and
    hears nothing after. The opener's widget lands as the
    parent (#1014): the dialog opens centered on the main
    window, not wherever the window manager puts a parentless
    window."""

    def __init__(self, bridge, parent=None) -> None:
        super().__init__(parent)
        self.bridge = bridge
        self.hints_loaded = False
        self.flight = None
        # The defaults the summary fills blanks with (#1164): the
        # client's own until the daemon's answer lands.
        self.defaults: dict[str, int] = dict(STATIC_DEFAULTS)
        self.setWindowTitle("Create a workspace")
        self.note = QtWidgets.QLabel("Create a workspace")
        form = QtWidgets.QFormLayout()
        self.fields: dict[str, QtWidgets.QWidget] = {}
        for field, label in FORM_FIELDS:
            self.fields[field] = field_widget(field, self.restate)
            form.addRow(label, self.fields[field])
        # The summary line (#1164): under the fields, always
        # present, in the placeholder's muted gray — the note
        # line above keeps its own job, the refusals and the
        # failures.
        self.summary = QtWidgets.QLabel(summary_text({}, self.defaults), self)
        self.summary.setForegroundRole(
            QtGui.QPalette.ColorRole.PlaceholderText
        )
        self.create_button = QtWidgets.QPushButton("Create")
        self.create_button.setDefault(True)
        self.create_button.clicked.connect(self.submit)
        cancel = QtWidgets.QPushButton("Cancel")
        cancel.clicked.connect(self.reject)
        buttons = QtWidgets.QHBoxLayout()
        buttons.addWidget(self.create_button)
        buttons.addWidget(cancel)
        layout = QtWidgets.QVBoxLayout(self)
        layout.addWidget(self.note)
        layout.addLayout(form)
        layout.addWidget(self.summary)
        layout.addLayout(buttons)
        self.hook_bridge(bridge)

    def values(self) -> dict:
        """The form's values by field: the picker's data value,
        every other field's text."""
        return {
            field: (
                self.fields[field].currentData()
                if isinstance(self.fields[field], QtWidgets.QComboBox)
                else self.fields[field].text()
            )
            for field, _label in FORM_FIELDS
        }

    @QtCore.Slot()
    def restate(self) -> None:
        """The summary line reads the fields again (#1164): every
        edit of a size field lands here, and so does the
        defaults answer."""
        self.summary.setText(summary_text(self.values(), self.defaults))

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
        """One answered hint (#962, #1164): the catalog fills the
        image picker after its default stand-in, the defaults the
        size placeholders and the summary's blanks."""
        if key == IMAGES_KEY:
            picker = self.fields["image"]
            for value, label in image_options(result):
                picker.addItem(label, value)
        else:
            named = size_defaults(result)
            for field, hint in size_placeholders(named).items():
                self.fields[field].setPlaceholderText(hint)
            self.defaults.update(named)
            self.restate()

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
        values = self.values()
        body, error = create_body(values)
        if body is None:
            self.note.setText(error)
            return
        self.create_button.setEnabled(False)
        self.note.setText(f"creating {body['name']} …")
        self.flight = self.bridge.call(CREATE_KEY, "create", body)
