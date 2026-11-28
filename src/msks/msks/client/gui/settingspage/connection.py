"""The Connection subtab (#1226): the global address keys — the URL
or the local socket, the token file, the CA file, and the expected
image — the active alias, and the ``daemons:`` editor: the file's
aliases as a list with add, rename, and remove, and the selected
alias's entry as a form under it. The panel edits the merged
document's aliases (the user's file over the system's, #1224) and
Apply writes the entries that changed into the user's file, each
through :func:`msks.configio.set_daemon` in place. The loader
checks every entry when the save lands: the one-address rule (a
``url`` xor a ``socket``, over the entry merged with the system
file's) and the entry vocabulary, so an entry with no address is
refused with its message and nothing is written.

A socket entry authenticates by peer credential, so the token and
CA fields stand dark while the entry's kind is Socket. An alias
the system file defines comes back after a remove — the user's
file cannot delete a layer's entry — and the panel says so. The
connection picker in the listing's status row stays a pick for the
session (#1009); the active alias here is the durable pick a bare
``msks`` reads.

This module imports PySide6 at module scope, the screens' own
contract: nothing imports it at CLI parse time (#927).
"""

from __future__ import annotations

from PySide6 import QtCore, QtWidgets

from ..forms import ModePicker, prompt_text
from .fields import Panel
from .vocabulary import CONNECTION_LABEL, LABELS, TIPS

#: The entry kinds' labels (#1226) with the key each writes.
ADDRESS_KINDS = (("URL", "url"), ("Socket", "socket"))

#: The active alias picker's first item (#1226): no alias, the
#: unset form.
NO_ACTIVE = "(none)"

#: The aliases group's title (#1226).
DAEMONS_LABEL = "Daemon aliases"

#: The buttons' words (#1226).
ADD = "Add…"
RENAME = "Rename…"
REMOVE = "Remove"

#: The prompts' titles and labels (#1226).
ADD_TITLE = "Add daemon"
ADD_LABEL = "Alias name:"
RENAME_TITLE = "Rename daemon"


def entry_address(entry: dict) -> str:
    """One entry's address for its row (#1226): the url, else the
    socket, else the honest blank — an entry mid-edit."""
    return str(entry.get("url") or entry.get("socket") or "")


def entry_kind(entry: dict) -> str:
    """One entry's address kind (#1226): ``socket`` when it carries
    one, ``url`` otherwise — the default a new entry opens with."""
    return "socket" if "socket" in entry else "url"


class AliasList(QtWidgets.QListWidget):
    """The aliases' list (#1226): the first row's Up hands the
    keyboard to the focus chain's previous stop, the way every
    table in the window leaves upward (#962)."""

    def keyPressEvent(self, event) -> None:  # noqa: N802
        if (
            event.key() == QtCore.Qt.Key.Key_Up
            and self.currentRow() <= 0
            and self.count()
        ):
            QtWidgets.QWidget.focusNextPrevChild(self, False)
            return
        super().keyPressEvent(event)


class ConnectionPanel(Panel):
    """The Connection subtab (#1226). The seam: the text prompt the
    add and rename buttons ask through, the suite's fake."""

    def __init__(self, page, parent=None, *, ask=prompt_text) -> None:
        super().__init__(page, CONNECTION_LABEL, parent)
        self.ask = ask
        #: The aliases as the operator works them (#1226): the merged
        #: document's entries at the seed, edited, added, and removed
        #: here until Apply or Revert.
        self.daemons: dict[str, dict] = {}
        #: The aliases as the document held them at the seed (#1226).
        self.seeded_daemons: dict[str, dict] = {}
        #: The aliases the operator removed since the seed (#1226):
        #: after a save, one that still stands comes from the system
        #: file, and the panel says so.
        self.removed: set[str] = set()
        #: True while the entry form fills from a selection (#1226):
        #: its signals are no edit then.
        self.filling = False
        self.build_globals()
        self.build_aliases()
        self.load()

    # --- building -------------------------------------------------------

    def build_globals(self) -> None:
        """The default connection's rows (#1226): the URL, the
        socket, the token file and the CA file with their Browse
        buttons, the expected image, and the active alias as a
        closed picker over the aliases. The picker is last: the
        strip's Up leaves from the first row, and a line edit takes
        that key where a picker would cycle its own items."""
        self.text("url")
        self.text("socket")
        self.text("token_file", browse="file")
        self.text("cafile", browse="file")
        self.text("expected_image")
        self.active = ModePicker(self)
        self.active.addItem(NO_ACTIVE, "")
        self.row("active_daemon", self.active)

    def build_aliases(self) -> None:
        """The ``daemons:`` editor (#1226): the list of aliases with
        Add, Rename, and Remove beside it, and the selected alias's
        entry under it — the address as a kind picker beside its
        line, the token file and the CA file with Browse buttons,
        and the expected image."""
        form = self.group(DAEMONS_LABEL)
        self.aliases = AliasList(self)
        self.aliases.currentRowChanged.connect(self.show_entry)
        # Five rows show (#1226): a longer list scrolls inside its
        # own height, never growing the page under the window.
        self.aliases.setFixedHeight(
            self.aliases.fontMetrics().height() * 5 + 8
        )
        self.add_button = QtWidgets.QPushButton(ADD, self)
        self.add_button.clicked.connect(self.add_alias)
        self.rename_button = QtWidgets.QPushButton(RENAME, self)
        self.rename_button.clicked.connect(self.rename_alias)
        self.remove_button = QtWidgets.QPushButton(REMOVE, self)
        self.remove_button.clicked.connect(self.remove_alias)
        column = QtWidgets.QVBoxLayout()
        for button in (
            self.add_button,
            self.rename_button,
            self.remove_button,
        ):
            column.addWidget(button)
        column.addStretch(1)
        listing = QtWidgets.QHBoxLayout()
        listing.addWidget(self.aliases, stretch=1)
        listing.addLayout(column)
        form.addRow(listing)
        self.kind = ModePicker(self)
        for label, key in ADDRESS_KINDS:
            self.kind.addItem(label, key)
        self.kind.currentIndexChanged.connect(self.kind_changed)
        self.address = QtWidgets.QLineEdit(self)
        address_row = QtWidgets.QHBoxLayout()
        address_row.addWidget(self.kind)
        address_row.addWidget(self.address, stretch=1)
        form.addRow(LABELS["url"] + " / " + LABELS["socket"], address_row)
        self.token = QtWidgets.QLineEdit(self)
        self.ca = QtWidgets.QLineEdit(self)
        self.image = QtWidgets.QLineEdit(self)
        form.addRow(self.entry_label("token_file"), self.browsable(self.token))
        form.addRow(self.entry_label("cafile"), self.browsable(self.ca))
        form.addRow(self.entry_label("expected_image"), self.image)
        for field, key in (
            (self.address, "url"),
            (self.token, "token_file"),
            (self.ca, "cafile"),
            (self.image, "expected_image"),
        ):
            field.setToolTip(TIPS[key])
            field.textChanged.connect(self.entry_changed)
        # The kind picker's change is the edit's (#1226): it reaches
        # the page through kind_changed, never as a bare touch — a
        # selection's fill moves it too.
        self.kind.setToolTip(TIPS["daemons"])

    def entry_label(self, key: str) -> QtWidgets.QLabel:
        """One entry row's label (#1226): the setting's name with
        its description, the entry form carrying no pins."""
        label = QtWidgets.QLabel(LABELS[key], self)
        label.setToolTip(TIPS[key])
        return label

    def browsable(self, field: QtWidgets.QLineEdit) -> QtWidgets.QWidget:
        """An entry path field with its Browse button (#1226)."""
        button = QtWidgets.QPushButton("Browse…", self)
        button.clicked.connect(lambda: self.browse("file", field))
        line = QtWidgets.QHBoxLayout()
        line.addWidget(field)
        line.addWidget(button)
        return line

    # --- the seed and the edits -----------------------------------------

    def load(self, _settings=None) -> None:
        """Seed the rows from the page's standing document (#1226):
        the global keys, the aliases as the document holds them
        (the user's entries over the system's), and the picker over
        them; an alias the operator removed that still stands comes
        from the system file, and the panel says so."""
        super().load()
        self.daemons = aliases_of(self.page.doc)
        self.seeded_daemons = aliases_of(self.page.doc)
        standing = sorted(self.removed & set(self.daemons))
        for alias in standing:
            self.page.flash(
                f"{alias} still stands: the system file defines it"
            )
        self.removed = set()
        self.refresh_aliases()

    def refresh_aliases(self, select: str | None = None) -> None:
        """The list and the picker over the working aliases (#1226):
        the standing selection kept when its alias still stands,
        else the first row; *select* names the row to show when
        given. The picker's value survives the rebuild, so an alias
        renamed away from the active pick is re-pointed by the
        rename itself."""
        current = select or self.selected()
        self.fill_list()
        self.fill_picker(self.value("active_daemon"))
        self.show_row(current)

    def fill_list(self) -> None:
        """The list rebuilt over the working aliases (#1226), each
        row its alias's name beside its address, silently — the
        selection's landing is :meth:`show_row`'s."""
        self.aliases.blockSignals(True)
        self.aliases.clear()
        for alias, entry in self.daemons.items():
            self.aliases.addItem(f"{alias} — {entry_address(entry)}")
            item = self.aliases.item(self.aliases.count() - 1)
            item.setData(QtCore.Qt.ItemDataRole.UserRole, alias)
        self.aliases.blockSignals(False)

    def fill_picker(self, value: str) -> None:
        """The active alias picker's items over the working aliases
        (#1226), the standing value kept when it still stands, the
        blank item otherwise — silently."""
        self.active.blockSignals(True)
        while self.active.count() > 1:
            self.active.removeItem(1)
        for alias in self.daemons:
            self.active.addItem(alias, alias)
        self.active.setCurrentIndex(max(self.active.findData(value), 0))
        self.active.blockSignals(False)

    def show_row(self, current: str | None) -> None:
        """The row to show after a rebuild (#1226): the one carrying
        *current* when it stands, else the first; an empty list
        leaves the form blank."""
        if not self.aliases.count():
            self.show_entry(-1)
            return
        rows = [
            row
            for row in range(self.aliases.count())
            if self.row_alias(row) == current
        ]
        self.aliases.setCurrentRow(rows[0] if rows else 0)

    def row_alias(self, row: int) -> str | None:
        """The alias one listed row carries (#1226)."""
        return self.aliases.item(row).data(QtCore.Qt.ItemDataRole.UserRole)

    def selected(self) -> str | None:
        """The listed alias under the selection (#1226), None when
        the list is empty."""
        item = self.aliases.currentItem()
        if item is None:
            return None
        return str(item.data(QtCore.Qt.ItemDataRole.UserRole))

    def select(self, alias: str) -> None:
        """Show one alias's entry (#1226): the row selected and the
        form filled from it."""
        for row in range(self.aliases.count()):
            if (
                self.aliases.item(row).data(QtCore.Qt.ItemDataRole.UserRole)
                == alias
            ):
                self.aliases.setCurrentRow(row)
                return

    @QtCore.Slot(int)
    def show_entry(self, _row: int) -> None:
        """The selection moved (#1226): the form fills from the
        selected alias's entry, an empty one when none stands, and
        the buttons answer to the selection."""
        alias = self.selected()
        entry = self.daemons.get(alias, {}) if alias else {}
        # The fill is no edit (#1226): the form's own signals are
        # the operator's alone, and a selection change leaves the
        # dirty flag and the working entry as they stand.
        self.filling = True
        kind = entry_kind(entry)
        self.kind.setCurrentIndex(max(self.kind.findData(kind), 0))
        self.address.setText(str(entry.get(kind, "")))
        self.token.setText(str(entry.get("token_file", "")))
        self.ca.setText(str(entry.get("cafile", "")))
        self.image.setText(str(entry.get("expected_image", "")))
        self.filling = False
        for button in (self.rename_button, self.remove_button):
            button.setEnabled(alias is not None)
        for field in (
            self.kind,
            self.address,
            self.token,
            self.ca,
            self.image,
        ):
            field.setEnabled(alias is not None)
        self.dress_entry()  # the kind's own rule over the selection's

    def dress_entry(self) -> None:
        """The entry form follows its kind (#1226): a socket entry
        authenticates by peer credential, so its token and CA
        fields stand dark; a url entry's take both."""
        by_url = self.kind.currentData() == "url"
        for field in (self.token, self.ca):
            field.setEnabled(by_url and self.selected() is not None)
        self.address.setToolTip(TIPS["url"] if by_url else TIPS["socket"])

    @QtCore.Slot(int)
    def kind_changed(self, _index: int) -> None:
        """The kind picker moved (#1226): the entry's address moves
        to the other key — the one address stays, the kind
        changes — and the credential fields follow."""
        if self.filling:
            return
        self.entry_changed()
        self.dress_entry()

    @QtCore.Slot(str)
    @QtCore.Slot()
    def entry_changed(self, *_args) -> None:
        """The entry form changed under the operator (#1226): the
        working entry takes the form's reading, the list row shows
        its address, and the page hears of the edit."""
        alias = self.selected()
        if self.filling or alias is None:
            return
        self.daemons[alias] = self.read_entry()
        self.aliases.currentItem().setText(
            f"{alias} — {entry_address(self.daemons[alias])}"
        )
        self.changed()

    def read_entry(self) -> dict:
        """The entry form's reading (#1226): the address under its
        kind's key, the credentials for a url entry, the image; a
        blank field leaves its key out, the file's unset form."""
        entry = self.address_entry()
        if entry_kind(entry) == "url":
            entry.update(self.credentials())
        image = self.image.text().strip()
        if image:
            entry["expected_image"] = image
        return entry

    def address_entry(self) -> dict:
        """The address field's pair (#1226): the typed address under
        the kind picker's key, nothing when blank."""
        address = self.address.text().strip()
        return {str(self.kind.currentData()): address} if address else {}

    def credentials(self) -> dict:
        """The token and CA files (#1226): each typed path under its
        key, blanks left out."""
        return {
            key: field.text().strip()
            for key, field in (("token_file", self.token), ("cafile", self.ca))
            if field.text().strip()
        }

    def daemon_edits(self) -> dict[str, dict | None]:
        """The ``daemons:`` edits Apply writes (#1226): every alias
        whose working entry differs from the seed, and ``None`` for
        one the operator removed."""
        edits = {
            alias: dict(entry)
            for alias, entry in self.daemons.items()
            if self.seeded_daemons.get(alias) != entry
        }
        for alias in self.seeded_daemons:
            if alias not in self.daemons:
                edits[alias] = None
        return edits

    def stands_as_file(self) -> bool:
        """The rows and the aliases stand as the document holds them
        (#1226)."""
        return super().stands_as_file() and self.daemons == self.seeded_daemons

    # --- the aliases ----------------------------------------------------

    @QtCore.Slot()
    def add_alias(self) -> None:
        """Add… (#1226): a prompt for the name; a blank answer adds
        nothing, a standing name is refused with a flash, and a new
        one opens an empty url entry under the selection."""
        name = self.fresh_name(ADD_TITLE, ADD_LABEL, "")
        if name is None:
            return
        self.daemons[name] = {}
        self.refresh_aliases(select=name)
        self.changed()

    @QtCore.Slot()
    def rename_alias(self) -> None:
        """Rename… (#1226): a prompt for the new name; the entry
        moves to it, the active pick follows when it named the old
        one, and a standing name is refused with a flash."""
        alias = self.selected()
        if alias is None:
            return
        name = self.fresh_name(RENAME_TITLE, f"New name for {alias}:", alias)
        if name is None or name == alias:
            return
        follows = self.value("active_daemon") == alias
        self.daemons[name] = self.daemons.pop(alias)
        self.refresh_aliases(select=name)
        # The active pick follows after the rebuild (#1226): the new
        # name has its item only then.
        if follows:
            self.active.setCurrentIndex(self.active.findData(name))
        self.changed()

    def fresh_name(self, title: str, label: str, current: str) -> str | None:
        """One prompt for an alias name (#1226): the typed name, None
        for a blank answer or a name that already stands — the
        standing one refused with a flash."""
        name = self.ask(self, title, label, current)
        if not name:
            return None
        if name in self.daemons:
            self.page.flash(f"daemon {name!r} already stands")
            return None
        return name

    @QtCore.Slot()
    def remove_alias(self) -> None:
        """Remove (#1226): the selected alias leaves the working
        copy, recorded so a later seed can say when the system file
        keeps it standing; the active pick clears when it named
        it."""
        alias = self.selected()
        if alias is None:
            return
        del self.daemons[alias]
        self.removed.add(alias)
        if self.value("active_daemon") == alias:
            self.active.setCurrentIndex(0)
        self.refresh_aliases()
        self.changed()


def aliases_of(doc: dict) -> dict[str, dict]:
    """The document's ``daemons:`` entries as their own dicts
    (#1226), the document's order; anything but a mapping reads as
    no aliases (the loader refused the file already)."""
    entries = doc.get("daemons")
    if not isinstance(entries, dict):
        return {}
    return {alias: dict(entry) for alias, entry in entries.items()}
