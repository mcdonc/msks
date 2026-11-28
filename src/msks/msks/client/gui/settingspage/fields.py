"""The settings subtabs' shared form (#1226): the panel every
subtab builds on — the fields by key, their labels carrying the
setting's description, the seed from the page's standing document
and pins, and the edits Apply writes — beside the two file pickers
a path field's **Browse…** button opens. The panels own their rows;
this module owns the shape every row keeps, so a field reads the
same on the Connection subtab as on Screenshot.

An edit is a field whose value differs from the value the seed
wrote. Apply writes the edits alone: a field the operator left
alone stays out of the file, and a value inherited from the system
file stays inherited. A cleared field is an edit whose value is
the unset form, and the save deletes the key, so the layer under
applies again.

This module imports PySide6 at module scope, the screens' own
contract: nothing imports it at CLI parse time (#927).
"""

from __future__ import annotations

from PySide6 import QtCore, QtWidgets

from .vocabulary import LABELS, PIN_LINE, TIPS, display_value, pin_notice

#: The Browse button's label (#1226).
BROWSE = "Browse…"


def pick_file(parent, start: str | None) -> str:  # pragma: no cover
    """One existing file (#1226): the platform's open dialog over
    *parent*, the standing path's directory the opening one; an
    empty string for a cancelled dialog. The platform's modal
    dialog itself is the suite's fake (``pick_file`` is the seam);
    the body stays outside the coverage gate."""
    path, _filter = QtWidgets.QFileDialog.getOpenFileName(
        parent, "Choose a file", start or ""
    )
    return path


def pick_directory(parent, start: str | None) -> str:  # pragma: no cover
    """One directory (#1226): the platform's directory dialog over
    *parent*; an empty string for a cancelled dialog. The modal
    dialog is the suite's fake, as ``pick_file``'s."""
    return QtWidgets.QFileDialog.getExistingDirectory(
        parent, "Choose a directory", start or ""
    )


class Panel(QtWidgets.QWidget):
    """One subtab's form (#1226): a titled group over a
    :class:`QFormLayout` the subclass fills through :meth:`text`,
    :meth:`pick`, and :meth:`row`, the pins' note under it, and the
    seed-and-edit bookkeeping over the page. ``touched`` crosses to
    the page's dirty flag at every field change. The seams: the two
    pickers, the suite's fakes."""

    touched = QtCore.Signal()

    def __init__(self, page, title: str, parent=None) -> None:
        super().__init__(parent)
        self.page = page
        self.fields: dict[str, QtWidgets.QWidget] = {}
        self.labels: dict[str, QtWidgets.QLabel] = {}
        #: The side widgets beside fields (#1226): a Browse button,
        #: dark under its field's pin.
        self.sides: dict[str, QtWidgets.QWidget] = {}
        #: The values the seed wrote (#1226): an edit is a field
        #: whose value moved from here.
        self.seeded: dict[str, str] = {}
        #: True while the panel rebuilds its fields itself (#1226) —
        #: the device picker's Refresh — so its own signals are no
        #: edit.
        self.filling = False
        self.body = QtWidgets.QVBoxLayout(self)
        self.form = self.group(title)
        self.pinned_note = QtWidgets.QLabel("", self)
        self.pinned_note.setWordWrap(True)
        self.body.addWidget(self.pinned_note)
        self.body.addStretch(1)

    # --- building -------------------------------------------------------

    def group(self, title: str) -> QtWidgets.QFormLayout:
        """A titled group box on the panel (#1226) with an empty form
        inside, the form handed back for the rows."""
        box = QtWidgets.QGroupBox(title, self)
        form = QtWidgets.QFormLayout(box)
        form.setFieldGrowthPolicy(
            QtWidgets.QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow
        )
        self.body.insertWidget(self.body.count() - 2, box)
        return form

    def label(self, key: str) -> QtWidgets.QLabel:
        """A form row's label (#1214, #1226): the setting's name,
        carrying the field's tooltip, kept under ``self.labels`` so
        the seed re-tips it beside the field."""
        label = QtWidgets.QLabel(LABELS[key], self)
        self.labels[key] = label
        return label

    def row(
        self,
        key: str,
        widget: QtWidgets.QWidget,
        side: QtWidgets.QWidget | None = None,
    ) -> QtWidgets.QWidget:
        """One form row (#1226): the setting's label over *widget*, an
        optional side widget (a Browse button) after it. The field is
        registered under *key* and listened to for the dirty flag."""
        self.fields[key] = widget
        if side is None:
            self.form.addRow(self.label(key), widget)
        else:
            self.sides[key] = side
            line = QtWidgets.QHBoxLayout()
            line.addWidget(widget)
            line.addWidget(side)
            self.form.addRow(self.label(key), line)
        self.listen(widget)
        return widget

    def text(
        self, key: str, *, browse: str | None = None
    ) -> QtWidgets.QLineEdit:
        """A text field (#1226): a line edit, with a **Browse…** button
        beside it when *browse* names a picker (``file`` or
        ``dir``)."""
        field = QtWidgets.QLineEdit(self)
        side = None
        if browse is not None:
            side = QtWidgets.QPushButton(BROWSE, self)
            side.setObjectName(f"browse-{key}")
            side.clicked.connect(lambda: self.browse(browse, field))
        return self.row(key, field, side)

    def browse(self, kind: str, field: QtWidgets.QLineEdit) -> None:
        """A Browse click (#1226): the picker over the standing path,
        the pick landed in the field; a cancelled dialog changes
        nothing."""
        start = field.text().strip() or None
        chosen = (
            pick_directory(self, start)
            if kind == "dir"
            else pick_file(self, start)
        )
        if chosen:
            field.setText(chosen)

    def listen(self, field) -> None:
        """One field's change signals to the dirty flag (#1214):
        whichever the widget kind answers — a pick, a typed edit, a
        text change."""
        for name in ("currentIndexChanged", "editTextChanged", "textChanged"):
            signal = getattr(field, name, None)
            if signal is not None:
                signal.connect(self.changed)

    @QtCore.Slot()
    @QtCore.Slot(int)
    @QtCore.Slot(str)
    def changed(self, *_args) -> None:
        """A field changed under the operator (#1226): the page hears
        of it. The panel's own rebuilds — a picker refilled — are
        no edit."""
        if not self.filling:
            self.touched.emit()

    # --- the seed and the edits -----------------------------------------

    def value(self, key: str) -> str:
        """A field's value as the file spells it (#1226): a picker's
        item data, a text field's stripped text."""
        field = self.fields[key]
        if isinstance(field, QtWidgets.QComboBox) and not field.isEditable():
            return str(field.currentData() or "")
        return field.text().strip()

    def load(self, _settings=None) -> None:
        """Seed every field from the page's standing document and
        pins (#1226): a pinned key reads as the variable's value and
        stands read-only, the variable named under the form. The
        page hands the dictation record to every panel alike; the
        general panels read the document and leave it alone."""
        for key in self.fields:
            self.seed(key, *self.page.effective(key))
        self.refresh_note()

    def seed(self, key: str, value: str, pinned: bool) -> None:
        """One field's seed (#1214, #1226): the value in the field's
        own shape, and the field read-only with the variable named
        when the environment pins the key."""
        self.write(key, value)
        self.seeded[key] = value
        self.dress(key, pinned)

    def write(self, key: str, value: str) -> None:
        """The value into the field (#1226): a closed picker takes
        the item whose data is the value, the first when none
        matches; every other field takes the text."""
        field = self.fields[key]
        if isinstance(field, QtWidgets.QComboBox) and not field.isEditable():
            index = field.findData(value)
            field.setCurrentIndex(max(index, 0))
        else:
            field.setText(value)

    def dress(self, key: str, pinned: bool) -> None:
        """The field's state and tooltip after a seed (#1214, #1226):
        read-only under a pin, the description — and the pin's
        notice — on the field and its label."""
        field = self.fields[key]
        field.setEnabled(not pinned)
        if key in self.sides:
            self.sides[key].setEnabled(not pinned)
        tip = self.tip(key, pinned)
        field.setToolTip(tip)
        self.labels[key].setToolTip(tip)

    @staticmethod
    def tip(key: str, pinned: bool) -> str:
        """A field's tooltip (#1214): the setting's description, and
        for a key the environment pins, the variable's notice after
        it."""
        if pinned:
            return f"{TIPS[key]}\n\n{pin_notice(key)}"
        return TIPS[key]

    def refresh_note(self) -> None:
        """The note under the form (#1214, #1226): one line per key
        the environment pins, nothing when none."""
        lines = [
            PIN_LINE % self.page.variable_of(key)
            for key in self.fields
            if key in self.page.pinned
        ]
        self.pinned_note.setText("\n".join(lines))
        self.pinned_note.setVisible(bool(lines))

    def edits(self) -> dict[str, str | None]:
        """What Apply writes from this panel (#1226): the fields whose
        value moved from the seed, a pinned key left out (the
        variable wins it anyway), a cleared field as the unset form
        ``None``."""
        return {
            key: value or None
            for key in self.fields
            if key not in self.page.pinned
            and (value := self.value(key)) != self.seeded.get(key, "")
        }

    def daemon_edits(self) -> dict[str, dict | None]:
        """The ``daemons:`` edits Apply writes from this panel (#1226):
        none here; the Connection panel's editor answers its own."""
        return {}

    def stands_as_file(self) -> bool:
        """Whether the fields stand as the document holds them
        (#1224): every key Apply would write equals the merged
        document's value, a blank field matching an absent key."""
        return all(
            display_value(self.page.doc.get(key)) == self.value(key)
            for key in self.fields
            if key not in self.page.pinned
        )

    def first_field(self) -> QtWidgets.QWidget | None:
        """The panel's first focusable row (#1226): the first field
        built that stands enabled — a pinned key's field is dark and
        no tab stop, so the row after it is the first — the stop Up
        hands back to the strip above. None when every field is
        pinned."""
        return next(
            (field for field in self.fields.values() if field.isEnabled()),
            None,
        )
