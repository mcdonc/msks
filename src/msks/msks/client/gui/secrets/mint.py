"""The mint form (phase 4.6 of #924, #966, #997): the dialog
the listing's Mint action opens — the TUI mint screen's counterpart
(#393, #423). One labeled field per row (name, the
comma-separated destinations, the coverage pick, the workspace
picker a scoped pick shows, the TTL choice, and the value — a
masked input the operator types or pastes into, the form taking
the secret's bytes directly). The local checks land on the
form's note line, a refusal keeps the fields; a landed mint
accepts with its reply — the row carrying the sentinel the
page no longer displays (#997) — and the value field clears
with the dialog, so the typed secret leaves the widget tree.
The workspace picker seeds from the tree's own list through the
bridge; a refused seed notes itself and the daemon-wide mint
needs no list.

This module imports PySide6 at module scope — the package's
init imports none of it at parse time (#927).
"""

from PySide6 import QtCore, QtWidgets

from ...data import RestFailure
from ..dialogs import Centered
from ..secretrows import MINT_TTLS
from . import cells

#: The mint call's key (#966): the dialog owns its flight, the
#: closed dialog cancelling it.
MINT_KEY = "secrets-mint"

#: The workspace-list seed's key (#966).
WORKSPACES_KEY = "secrets-mint-workspaces"

#: The form's standing note (#966, #997): its title line,
#: back on a refused seed or a landed refusal once the note
#: clears — the page carries no sentinel display, so the note
#: names the action alone.
MINT_NOTE = "Mint a placeholder"


def picker_edge(key: int, current: int, last: int) -> bool | None:
    """The picker's edge keys (#966): Up from the first option
    hands the keyboard back to the form above, Down from the
    last to the field below — None keeps the walk inside the
    options, the spatial-navigation rule the TUI picker's own
    edge bridges carry."""
    if key == QtCore.Qt.Key.Key_Up and current <= 0:
        return False
    if key == QtCore.Qt.Key.Key_Down and current >= last:
        return True
    return None


class WorkspacePicker(QtWidgets.QListWidget):
    """The mint form's workspace picker (#966): a multi-select
    whose edges (:func:`picker_edge`) hand the keyboard back to
    the form — the picker joins the walk, no focus trap."""

    def keyPressEvent(self, event) -> None:
        hand = picker_edge(event.key(), self.currentRow(), self.count() - 1)
        if hand is None:
            super().keyPressEvent(event)
            return
        QtWidgets.QWidget.focusNextPrevChild(self, hand)


class MintDialog(Centered, QtWidgets.QDialog):
    """The mint form as a dialog (#966): the create dialog's
    pattern over the mint's own fields — calls through the
    bridge, answers over its signals, refusals on the note
    line, a dialog closed mid-flight cancelling its own
    call. The opener's widget lands as the parent (#1014):
    the form opens centered on the main window."""

    def __init__(self, bridge, parent=None) -> None:
        super().__init__(parent)
        self.bridge = bridge
        self.reply: dict | None = None
        self.flight = None
        self.seeded = False
        self.closed = False
        self.setWindowTitle("Mint a placeholder")
        self.note = QtWidgets.QLabel(MINT_NOTE, self)
        self.note.setWordWrap(True)
        form = QtWidgets.QFormLayout()
        self.name = QtWidgets.QLineEdit()
        self.name.setPlaceholderText("letters, numbers, underscores")
        form.addRow("Name", self.name)
        self.dests = QtWidgets.QLineEdit()
        self.dests.setPlaceholderText("host or .suffix, comma-separated")
        form.addRow("Dests", self.dests)
        self.coverage = QtWidgets.QComboBox()
        for label, value in cells.COVERAGE_CHOICES:
            self.coverage.addItem(label, value)
        self.coverage.currentIndexChanged.connect(self.sync_coverage)
        form.addRow("Coverage", self.coverage)
        self.workspaces = WorkspacePicker()
        self.workspaces.setSelectionMode(
            QtWidgets.QAbstractItemView.SelectionMode.MultiSelection
        )
        form.addRow("Workspaces", self.workspaces)
        self.ttl = QtWidgets.QComboBox()
        for label in MINT_TTLS:
            self.ttl.addItem(label, label)
        form.addRow("TTL", self.ttl)
        self.value = QtWidgets.QLineEdit()
        self.value.setPlaceholderText("the real secret — typed or pasted")
        self.value.setEchoMode(QtWidgets.QLineEdit.EchoMode.Password)
        form.addRow("Value", self.value)
        self.mint_button = QtWidgets.QPushButton("Mint")
        self.mint_button.setDefault(True)
        self.mint_button.clicked.connect(self.submit)
        cancel = QtWidgets.QPushButton("Cancel")
        cancel.clicked.connect(self.reject)
        buttons = QtWidgets.QHBoxLayout()
        buttons.addWidget(self.mint_button)
        buttons.addWidget(cancel)
        layout = QtWidgets.QVBoxLayout(self)
        layout.addWidget(self.note)
        layout.addLayout(form)
        layout.addLayout(buttons)
        bridge.succeeded.connect(self.on_succeeded)
        bridge.failed.connect(self.on_failed)

    def showEvent(self, event) -> None:
        """The workspace seed on the first show (#966): the
        tree's own list through the bridge — a refused seed
        notes itself, and the daemon-wide mint needs no
        list."""
        super().showEvent(event)
        if not self.seeded:
            self.seeded = True
            self.sync_coverage()
            self.bridge.call(WORKSPACES_KEY, "workspaces")

    def done(self, result: int) -> None:
        """The dialog's close, every path's (#966): the bridge
        hands go — a dialog closed mid-flight cancels its own
        call — and the value field clears first, so the typed
        secret leaves the widget tree with the form."""
        self.value.clear()
        self.closed = True  # a queued answer after this reaches no one
        if self.flight is not None and not self.flight.done():
            self.flight.cancel()
        self.bridge.succeeded.disconnect(self.on_succeeded)
        self.bridge.failed.disconnect(self.on_failed)
        super().done(result)

    def picked_workspaces(self) -> list[str]:
        """The picker's selected workspace ids, sorted — the
        coverage set the mint scopes to."""
        return sorted(
            item.data(QtCore.Qt.ItemDataRole.UserRole)
            for item in self.workspaces.selectedItems()
        )

    def form_values(self) -> dict:
        """The form's raw values (#966): the fields' texts and
        picks, the body builder's own shape."""
        return {
            "name": self.name.text(),
            "dests": self.dests.text(),
            "coverage": self.coverage.currentData(),
            "workspaces": self.picked_workspaces(),
            "ttl": self.ttl.currentData(),
            "value": self.value.text(),
        }

    def sync_coverage(self) -> None:
        """The coverage pick drives the picker row (#966): the
        daemon-wide row needs no workspaces, so the picker
        stands empty and disabled; a scoped pick fills it."""
        scoped = self.coverage.currentData() == cells.COVERAGE_SCOPED
        self.workspaces.setEnabled(scoped)

    @QtCore.Slot(str, object)
    def on_succeeded(self, key: str, result: object) -> None:
        """One answered call (#966): the seed fills the picker,
        a landed mint accepts with its reply. Another key's
        answer is not this dialog's to take."""
        if self.closed:
            return  # the dialog went; the answer reaches no one
        if key == WORKSPACES_KEY:
            self.seed_landed(result)
        elif key == MINT_KEY:
            self.reply = result if isinstance(result, dict) else {}
            self.accept()

    def seed_landed(self, rows: object) -> None:
        """The workspace seed's landing (#966): the picker fills
        with the tree's rows — the label the TUI picker shows,
        the value the id."""
        for row in rows or []:
            label = row.get("name") or row["id"]
            item = QtWidgets.QListWidgetItem(label)
            item.setData(QtCore.Qt.ItemDataRole.UserRole, row["id"])
            self.workspaces.addItem(item)

    @QtCore.Slot(str, object)
    def on_failed(self, key: str, failure: RestFailure) -> None:
        """One refused call (#966): a mint refusal keeps the
        fields with the failure on the note line; a refused seed
        notes it and the form stands."""
        if self.closed:
            return  # the dialog went; the refusal reaches no one
        if key == MINT_KEY:
            self.mint_button.setEnabled(True)
            self.note.setText(str(failure))
        elif key == WORKSPACES_KEY:
            self.note.setText(f"workspace list failed: {failure}")

    @QtCore.Slot()
    def submit(self) -> None:
        """The filled fields to the daemon (#966): a local
        refusal notes itself and sends nothing; a whole body
        posts through the bridge with the Mint button held until
        the answer."""
        body, error = cells.mint_body(self.form_values())
        if body is None:
            self.note.setText(error)
            return
        self.mint_button.setEnabled(False)
        self.note.setText(f"minting {body['name']} …")
        self.flight = self.bridge.call(MINT_KEY, "mint_secret", body)
