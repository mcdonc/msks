"""The workspace Proxies tab (phase 5.8 of #924, #988): the
page's proxy surface — the TUI's proxies page
(:mod:`msks.client.tui.proxies`) as a tab beside Mounts. The
table reads the presenter's cells (the direction
letter, the spec, the label, the live status word with its
cause); Add and Edit parse the spec through the CLI's own
parsers before anything sends, Removal asks for confirmation,
and the rows move through the REST surface the ``msks proxy``
commands ride. Every proxy transition the daemon publishes for
the open workspace refreshes the tab through the events feed,
so a bind that fails while the tab stands paints itself.

This module imports PySide6 at module scope — the screens' own
contract: nothing imports it at CLI parse time, and the
workspace page (itself lazily imported) owns the import.
"""

from PySide6 import QtCore, QtWidgets

from .dialogs import Centered
from .forms import confirm, read_only_table, save_cancel_row
from .kinds import ERROR
from .proxyrows import (
    DIRECTION_CHOICES,
    direction_letter,
    direction_spec,
    form_seeds,
    parsed_body,
    proxy_transition,
    spec_text,
    status_kind,
    status_text,
)
from .tints import SEMANTIC_ROLE, TintedLabel

#: The listing read's key stem (#988): each flight carries its
#: own key off it, the per-flight rule the mounts tab taught —
#: a late answer from a cancelled flight reaches nothing.
LIST_STEM = "proxies-list"

#: The mutations' key stem (#988): add, edit, and remove, each
#: flight its own key.
CHANGE_STEM = "proxies-change"

#: The tab's row of columns (#988): the TUI page's own set with
#: the direction in its own column.
PROXY_COLUMNS = ("DIRECTION", "SPEC", "LABEL", "STATUS")
STATUS_COLUMN = PROXY_COLUMNS.index("STATUS")

#: The columns that hug their text (#1025): the direction and
#: the status stay at their content width; the spec and the
#: label share the pane's remainder.
PROXY_CONTENT_COLUMNS = (0, 3)


class ProxyDialog(Centered, QtWidgets.QDialog):
    """The add/edit form (#988, the TUI's one form): the
    direction (ssh's two letters), the spec in the CLI's own
    grammar, and a label — parsed with the CLI's own parser, so
    a local refusal names itself on the note line in the same
    words the command prints. An accepted form carries the body
    on ``body``. The opener's widget lands as the parent
    (#1014): the form opens centered on the main window."""

    def __init__(self, row: dict | None = None, parent=None) -> None:
        super().__init__(parent)
        seeded = form_seeds(row)
        self.body: dict | None = None
        self.setWindowTitle("Add a proxy" if row is None else "Edit a proxy")
        self.note = QtWidgets.QLabel(
            "-L [BIND:]PORT:HOST:PORT · -R RPORT:HOST:TPORT"
        )
        form = QtWidgets.QFormLayout()
        self.letter = QtWidgets.QComboBox()
        self.letter.addItems(list(DIRECTION_CHOICES))
        self.letter.setCurrentIndex(DIRECTION_CHOICES.index(seeded["letter"]))
        form.addRow("Direction", self.letter)
        self.spec = QtWidgets.QLineEdit()
        self.spec.setText(seeded["spec"])
        self.spec.setPlaceholderText("8080:localhost:80")
        form.addRow("Spec", self.spec)
        self.label = QtWidgets.QLineEdit()
        self.label.setText(seeded["label"])
        self.label.setPlaceholderText("web")
        form.addRow("Label", self.label)
        self.save_button, cancel, buttons = save_cancel_row(self.submit)
        cancel.clicked.connect(self.reject)
        layout = QtWidgets.QVBoxLayout(self)
        layout.addWidget(self.note)
        layout.addLayout(form)
        layout.addLayout(buttons)

    @QtCore.Slot()
    def submit(self) -> None:
        """The fields to one body (#988): the parser's own words
        on a refusal, the form standing; a whole body accepts the
        dialog carrying it, the label on it when filled."""
        body, error = parsed_body(
            self.letter.currentText(), self.spec.text().strip()
        )
        if body is None:
            self.note.setText(error)
            return
        label = self.label.text().strip()
        if label:
            body["label"] = label
        self.body = body
        self.accept()


class ProxiesTab(QtWidgets.QWidget):
    """The page's proxy surface (#988): the table of rows with
    Add, Edit, and Remove under it, the note line a refusal
    names itself on. The listing refreshes with the page, on
    every proxy transition the events feed pushes, and after
    each mutation's answer; the mutations fly one at a time,
    each under its own key."""

    def __init__(self, bridge) -> None:
        super().__init__()
        self.bridge = bridge
        self.workspace_id = ""
        self.rows: list[dict] = []
        self.list_flight = None
        self.list_pending = False
        self.change_flight = None
        self.change_pending = False
        self.seq = 0
        self.list_key = ""
        self.change_key = ""
        self.landing_note = "saved"
        self.table = read_only_table(PROXY_COLUMNS, PROXY_CONTENT_COLUMNS)
        self.table.itemSelectionChanged.connect(self.sync_buttons)
        self.add_button = QtWidgets.QPushButton("Add")
        self.add_button.setEnabled(False)
        self.add_button.clicked.connect(self.add)
        self.edit_button = QtWidgets.QPushButton("Edit")
        self.edit_button.setEnabled(False)
        self.edit_button.clicked.connect(self.edit)
        self.remove_button = QtWidgets.QPushButton("Remove")
        self.remove_button.setEnabled(False)
        self.remove_button.clicked.connect(self.remove)
        buttons = QtWidgets.QHBoxLayout()
        buttons.addWidget(self.add_button)
        buttons.addWidget(self.edit_button)
        buttons.addWidget(self.remove_button)
        buttons.addStretch(1)
        self.note = TintedLabel("", self)
        self.note.setTextInteractionFlags(
            QtCore.Qt.TextInteractionFlag.TextSelectableByMouse
        )
        layout = QtWidgets.QVBoxLayout(self)
        layout.addWidget(self.table, stretch=1)
        layout.addLayout(buttons)
        layout.addWidget(self.note)
        bridge.succeeded.connect(self.on_succeeded)
        bridge.failed.connect(self.on_failed)

    def host(self, workspace_id: str) -> None:
        """The page's open (#988): a workspace switch cancels the
        flights the old workspace still had out — their keys
        reach nothing after — blanks the table, and takes the new
        workspace's first listing."""
        if workspace_id == self.workspace_id:
            return
        for flight in (self.list_flight, self.change_flight):
            if flight is not None and not flight.done():
                flight.cancel()
        self.list_flight = None
        self.change_flight = None
        self.list_pending = False
        self.change_pending = False
        self.list_key = ""
        self.change_key = ""
        self.workspace_id = workspace_id
        self.rows = []
        self.note.say("")
        self.repaint_rows()

    def poke(self) -> None:
        """One listing refresh through the bridge (#988) — the
        page's refresh and each pushed proxy transition share
        it. A refresh already in flight is skipped, the
        listing's own guard."""
        if not self.workspace_id or self.list_pending:
            return
        self.list_pending = True
        self.seq += 1
        self.list_key = f"{LIST_STEM}-{self.seq}"
        self.list_flight = self.bridge.call(
            self.list_key, "workspace_proxies", self.workspace_id
        )

    def frame(self, frame: dict) -> None:
        """One pushed frame (#988): a proxy transition refreshes
        the listing — a bind that fails while the tab stands
        paints itself — and anything else is not this tab's
        state."""
        if proxy_transition(frame):
            self.poke()

    def repaint_rows(self) -> None:
        """The table from the rows (#988): one row per proxy —
        the direction letter, the spec, the label, the status
        word with its cause."""
        self.table.setRowCount(len(self.rows))
        for index, row in enumerate(self.rows):
            values = (
                direction_letter(row),
                direction_spec(row),
                str(row.get("label") or ""),
                status_text(row),
            )
            kinds: list = [None] * len(PROXY_COLUMNS)
            kinds[STATUS_COLUMN] = status_kind(row)
            for column, value in enumerate(values):
                item = QtWidgets.QTableWidgetItem(value)
                item.setFlags(
                    item.flags() & ~QtCore.Qt.ItemFlag.ItemIsEditable
                )
                item.setData(SEMANTIC_ROLE, kinds[column])
                self.table.setItem(index, column, item)
        self.sync_buttons()

    def selected(self) -> int:
        """The selected row's index, ``-1`` when nothing holds."""
        row = self.table.currentRow()
        return row if 0 <= row < self.table.rowCount() else -1

    @QtCore.Slot()
    def sync_buttons(self) -> None:
        """The buttons' state (#988): Add on a hosted workspace
        beside the one mutation flight, Edit and Remove on a
        selected row besides."""
        held = self.selected() >= 0
        idle = bool(self.workspace_id) and not self.change_pending
        self.add_button.setEnabled(idle)
        self.edit_button.setEnabled(held and idle)
        self.remove_button.setEnabled(held and idle)

    def change(self, method: str, *args: object, note: str = "saved") -> None:
        """One mutation through the bridge (#988): add, edit, or
        remove, each under its own key — one at a time, the
        answer refreshing the listing behind it and naming its
        landing with the note the action gave."""
        if self.change_pending or not self.workspace_id:
            return
        self.change_pending = True
        self.landing_note = note
        self.seq += 1
        self.change_key = f"{CHANGE_STEM}-{self.seq}"
        self.sync_buttons()
        self.change_flight = self.bridge.call(
            self.change_key, method, self.workspace_id, *args
        )

    @QtCore.Slot()
    def add(self) -> None:
        """The Add action (#988): the form blank, an accepted
        body posting one proxy row."""
        dialog = ProxyDialog(parent=self)
        if dialog.exec() and dialog.body is not None:
            self.note.say("adding …")
            self.change("create_proxy", dialog.body)

    @QtCore.Slot()
    def edit(self) -> None:
        """The Edit action (#988): the form seeded with the
        selected row, an accepted body rewriting it whole."""
        index = self.selected()
        if index < 0:
            return
        row = self.rows[index]
        dialog = ProxyDialog(row, self)
        if dialog.exec() and dialog.body is not None:
            self.note.say("saving …")
            self.change("update_proxy", row["id"], dialog.body)

    @QtCore.Slot()
    def remove(self) -> None:
        """The Remove action (#988): the selected row's proxy
        removes after the operator confirms — the list's Delete
        shape — a refusal sending nothing."""
        index = self.selected()
        if index < 0:
            return
        row = self.rows[index]
        if not self.confirm_remove(row):
            return
        self.note.say("removing …")
        self.change("remove_proxy", row["id"], note="removed")

    def confirm_remove(self, row: dict) -> bool:
        """Remove's confirmation (#988, #1014): the question
        names the proxy's spec — No the default, both centered
        on the main window — and No keeps the row."""
        return confirm(self, "Remove proxy", f"Remove {spec_text(row)}?")

    @QtCore.Slot(str, object)
    def on_succeeded(self, key: str, result: object) -> None:
        """One answered call (#988): the listing's answer repaints
        the rows; a mutation's answer notes its landing and
        refreshes the listing. Another key's answer is not this
        tab's to take."""
        if key == self.list_key and self.list_key:
            self.listing_landed(result)
            return
        if key != self.change_key or not self.change_key:
            return
        self.change_landed()

    def listing_landed(self, result: object) -> None:
        """The listing's answer (#988): the rows repaint from
        it, whole."""
        self.list_pending = False
        self.list_flight = None
        self.rows = list(result or [])
        self.repaint_rows()

    def change_landed(self) -> None:
        """The mutation's answer (#988): the buttons return, the
        note names the landing, and the listing refreshes behind
        it."""
        self.change_pending = False
        self.change_flight = None
        self.sync_buttons()
        self.note.say(self.landing_note)
        self.poke()

    @QtCore.Slot(str, object)
    def on_failed(self, key: str, failure: object) -> None:
        """One refused call (#988): the refusal names itself on
        the note line — a named 400, a refused spec's message
        whole — and the rows stand."""
        if key == self.list_key and self.list_key:
            self.list_pending = False
            self.list_flight = None
            return
        if key != self.change_key or not self.change_key:
            return
        self.change_pending = False
        self.change_flight = None
        self.sync_buttons()
        self.note.say(f"refused: {failure}", ERROR)
