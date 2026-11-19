"""The workspace Mounts tab (phase 5.7 of #924, #987): the
page's editable mounts surface — the TUI's mounts editor
(:mod:`msks.client.tui.mounts`) as a tab beside Shells and
Info. The table reads the presenter's entries (host, guest,
mode, the derived state, the reason a gate named); Add, Edit,
and Remove compose the next whole list and post it through the
route ``msks mount`` drives, next-start semantics like the
resize. A landed save repaints from the daemon's refreshed row
and the page behind it takes the row; a refusal names itself on
the note line and the working list rolls back to the snapshot
the change took — the daemon is the authority, and a refused
edit must not ride a later save.

This module imports PySide6 at module scope — the screens' own
contract: nothing imports it at CLI parse time, and the
workspace page (itself lazily imported) owns the import.
"""

from PySide6 import QtCore, QtWidgets

from .detail import mount_entries
from .mountrows import (
    MODE_CHOICES,
    added_mount,
    dialog_seeds,
    mount_entry,
    mount_pair,
    pending_words,
    removed_mount,
    replaced_mount,
    resolved_index,
    saved_note,
)

#: The save call's key stem (#987): each flight carries its
#: own key off it — ``workspace-mounts-N``, one counter per tab
#: — so the tab takes only its own current flight's answers. A
#: refusal the prior workspace's finished flight delivers past a
#: switch carries the old key and reaches nothing: the failure
#: signal names no row, and the key is the one identity it does
#: carry. A change already out gates the next, exactly the
#: listing's own rule.
MOUNTS_KEY = "workspace-mounts"

#: The tab's row of columns (#987): the Info tab's own mounts
#: table's set — the spec the operator wrote beside the state
#: the daemon derived.
MOUNT_COLUMNS = ("HOST", "GUEST", "MODE", "STATE", "REASON")


class MountDialog(QtWidgets.QDialog):
    """The add/edit form (#987, the TUI's one form, #886): the
    host path, the guest path, and the mode, composed into one
    entry the CLI's own parser reads — a local refusal names
    itself on the note line and the form stands. An accepted
    form carries the entry on ``entry``."""

    def __init__(self, entry: dict | None = None) -> None:
        super().__init__()
        seeded = dialog_seeds(entry)
        self.entry: dict | None = None
        self.setWindowTitle("Add a mount" if entry is None else "Edit a mount")
        self.note = QtWidgets.QLabel("HOST_PATH:GUEST_PATH — MODE ro or rw")
        form = QtWidgets.QFormLayout()
        self.host = QtWidgets.QLineEdit()
        self.host.setText(seeded["host"])
        self.host.setPlaceholderText("/srv/refs")
        form.addRow("host path", self.host)
        self.guest = QtWidgets.QLineEdit()
        self.guest.setText(seeded["guest"])
        self.guest.setPlaceholderText("/mnt/refs")
        form.addRow("guest path", self.guest)
        self.mode = QtWidgets.QComboBox()
        self.mode.addItems(list(MODE_CHOICES))
        mode = seeded["mode"] if seeded["mode"] in MODE_CHOICES else "rw"
        self.mode.setCurrentIndex(MODE_CHOICES.index(mode))
        form.addRow("mode", self.mode)
        self.save_button = QtWidgets.QPushButton("save")
        self.save_button.setDefault(True)
        self.save_button.clicked.connect(self.submit)
        cancel = QtWidgets.QPushButton("cancel")
        cancel.clicked.connect(self.reject)
        buttons = QtWidgets.QHBoxLayout()
        buttons.addWidget(self.save_button)
        buttons.addWidget(cancel)
        layout = QtWidgets.QVBoxLayout(self)
        layout.addWidget(self.note)
        layout.addLayout(form)
        layout.addLayout(buttons)

    @QtCore.Slot()
    def submit(self) -> None:
        """The fields to one entry (#987): the parser's own words
        on a refusal, the form standing; a whole entry accepts
        the dialog carrying it."""
        entry, error = mount_entry(
            self.host.text().strip(),
            self.guest.text().strip(),
            self.mode.currentText(),
        )
        if entry is None:
            self.note.setText(error)
            return
        self.entry = entry
        self.accept()


class MountsTab(QtWidgets.QWidget):
    """The page's editable mounts surface (#987): the table of
    entries with Add, Edit, and Remove under it, the standing
    next-start note while the configured set waits, and the note
    line a refusal names itself on. Every change posts the whole
    list through the bridge; the answer repaints, and the page
    behind takes the refreshed row through ``row_updated``."""

    row_updated = QtCore.Signal(object)

    def __init__(self, bridge) -> None:
        super().__init__()
        self.bridge = bridge
        self.workspace_id = ""
        self.working: list[dict] = []
        self.issues: list[dict] = []
        self.pending_row: dict | None = None
        self.before: list[dict] | None = None
        self.flight = None
        self.pending = False
        self.landed = False
        self.flight_seq = 0
        self.flight_key = ""
        self.table = QtWidgets.QTableWidget(0, len(MOUNT_COLUMNS), self)
        self.table.setHorizontalHeaderLabels(MOUNT_COLUMNS)
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionBehavior(
            QtWidgets.QAbstractItemView.SelectionBehavior.SelectRows
        )
        self.table.setSelectionMode(
            QtWidgets.QAbstractItemView.SelectionMode.SingleSelection
        )
        self.table.setEditTriggers(
            QtWidgets.QAbstractItemView.EditTrigger.NoEditTriggers
        )
        self.table.setShowGrid(False)
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.itemSelectionChanged.connect(self.sync_buttons)
        self.add_button = QtWidgets.QPushButton("Add")
        self.add_button.clicked.connect(self.add)
        self.edit_button = QtWidgets.QPushButton("Edit")
        self.edit_button.clicked.connect(self.edit)
        self.edit_button.setEnabled(False)
        self.remove_button = QtWidgets.QPushButton("Remove")
        self.remove_button.clicked.connect(self.remove)
        self.remove_button.setEnabled(False)
        buttons = QtWidgets.QHBoxLayout()
        buttons.addWidget(self.add_button)
        buttons.addWidget(self.edit_button)
        buttons.addWidget(self.remove_button)
        buttons.addStretch(1)
        self.pending_label = QtWidgets.QLabel("", self)
        self.pending_label.setVisible(False)
        self.note = QtWidgets.QLabel("", self)
        self.note.setTextInteractionFlags(
            QtCore.Qt.TextInteractionFlag.TextSelectableByMouse
        )
        layout = QtWidgets.QVBoxLayout(self)
        layout.addWidget(self.table, stretch=1)
        layout.addLayout(buttons)
        layout.addWidget(self.pending_label)
        layout.addWidget(self.note)
        bridge.succeeded.connect(self.on_succeeded)
        bridge.failed.connect(self.on_failed)

    def host(self, workspace_id: str, row: dict) -> None:
        """The page's row landing on the tab (#987): a new
        workspace cancels a flight the old one still has out —
        its answer is not this workspace's to take — and the
        fresh facts stand. A flight already out for this
        workspace keeps its ground: the answer repaints, and the
        tick's older snapshot cannot roll the posted list back
        out from under it."""
        if workspace_id != self.workspace_id:
            if self.flight is not None and not self.flight.done():
                self.flight.cancel()
            self.flight = None
            self.pending = False
            self.before = None
            self.workspace_id = workspace_id
            # The open takes no answers from before it (#987): a
            # flight the prior workspace still had out — its
            # cancel a no-op on a future already finished — can
            # deliver its answer past this reset, and a key no
            # standing flight holds reaches nothing.
            self.flight_key = ""
            # A fresh workspace's note starts blank (#987): the
            # prior one's refusal must not name itself on a page
            # that never asked.
            self.note.setText("")
        self.pending_row = row
        if not self.pending:
            self.take_row(row)

    def take_row(self, row: dict) -> None:
        """The daemon's facts into the tab's standing state
        (#987): the working list is the row's own — the daemon
        is the authority between changes — with the issues and
        the pending flag beside it. A row with nothing in it is
        the open's blank, not a landing: the Add stays gated
        until a real row arrives."""
        self.landed = bool(row)
        self.working = list(row.get("mounts") or [])
        self.issues = list(row.get("mount_issues") or [])
        self.repaint(row)

    def repaint(self, row: dict) -> None:
        """The table from the working list (#987): one row per
        entry — the spec the operator wrote beside the state the
        daemon derived — with the standing next-start note while
        the configured set waits and the buttons following the
        selection."""
        entries = mount_entries(
            {"mounts": self.working, "mount_issues": self.issues}
        )
        self.table.setRowCount(len(entries))
        for index, entry in enumerate(entries):
            values = (
                entry.host,
                entry.guest,
                entry.mode,
                entry.state,
                entry.reason or "-",
            )
            for column, value in enumerate(values):
                item = QtWidgets.QTableWidgetItem(value)
                item.setFlags(
                    item.flags() & ~QtCore.Qt.ItemFlag.ItemIsEditable
                )
                self.table.setItem(index, column, item)
        self.table.resizeColumnsToContents()
        words = pending_words(row)
        self.pending_label.setVisible(words is not None)
        if words is not None:
            self.pending_label.setText(words)
        self.sync_buttons()

    def selected(self) -> int:
        """The selected row's index, ``-1`` when nothing holds."""
        row = self.table.currentRow()
        return row if 0 <= row < self.table.rowCount() else -1

    @QtCore.Slot()
    def sync_buttons(self) -> None:
        """The buttons' state (#987): Add gated on a landed row
        beside the one flight — a blank tab cannot compose a
        whole-list replacement for facts it never read — and
        Edit and Remove on a selected row besides."""
        held = self.selected() >= 0
        self.add_button.setEnabled(self.landed and not self.pending)
        self.edit_button.setEnabled(held and not self.pending)
        self.remove_button.setEnabled(held and not self.pending)

    @QtCore.Slot()
    def add(self) -> None:
        """The Add action (#987): the form blank, an accepted
        entry joining the list's end — the TUI editor's own
        composition, the save posting the whole list."""
        dialog = MountDialog()
        if dialog.exec() and dialog.entry is not None:
            self.change(added_mount(self.working, dialog.entry))

    @QtCore.Slot()
    def edit(self) -> None:
        """The Edit action (#987): the form seeded with the
        selected row's mount, an accepted entry taking its slot
        — the slot re-resolved by the mount's (host, guest) pair
        after the form's wait, the TUI editor's own rule: a
        refresh that landed inside the dialog re-created every
        dict, and a mount that left the list decides nothing."""
        index = self.selected()
        if index < 0:
            return
        mount = self.working[index]
        pair = mount_pair(mount)
        dialog = MountDialog(mount)
        if dialog.exec() and dialog.entry is not None:
            fresh = resolved_index(self.working, pair)
            if fresh < 0:
                return
            self.change(replaced_mount(self.working, fresh, dialog.entry))

    @QtCore.Slot()
    def remove(self) -> None:
        """The Remove action (#987): the selected row's mount
        leaves the list, the remaining list posting whole — the
        TUI editor's own rule, no confirmation beside it: a
        remove the next start never takes can always be added
        back."""
        index = self.selected()
        if index < 0:
            return
        self.change(removed_mount(self.working, index))

    def change(self, mounts: list) -> None:
        """One composed list to the daemon (#987): the snapshot
        stands for the rollback, the working list takes the
        composition and the table shows it — the operator reads
        their edit the moment they make it, the daemon's answer
        confirming — and the save posts the whole list through
        the bridge, one flight at a time."""
        if self.pending:
            return
        self.before = self.working
        self.working = mounts
        self.pending = True
        self.flight_seq += 1
        self.flight_key = f"{MOUNTS_KEY}-{self.flight_seq}"
        self.repaint(self.pending_row or {})
        self.flight = self.bridge.call(
            self.flight_key, "set_mounts", self.workspace_id, mounts
        )

    @QtCore.Slot(str, object)
    def on_succeeded(self, key: str, result: object) -> None:
        """One answered save (#987): the daemon's refreshed row
        repaints the table with its derived state, the note
        names what the save waits for, and the page behind takes
        the row. Another key's answer is not this tab's to
        take."""
        if key != self.flight_key:
            # Another surface's answer, or a flight this tab no
            # longer holds — a switch cancelled it, or its
            # delivery raced one — is not this tab's to take.
            return
        fresh = result if isinstance(result, dict) else {}
        self.pending = False
        self.before = None
        self.flight = None
        self.take_row(fresh)
        self.note.setText(saved_note(fresh))
        self.row_updated.emit(fresh)

    @QtCore.Slot(str, object)
    def on_failed(self, key: str, failure: object) -> None:
        """One refused save (#987): the refusal names itself on
        the note line — a named 400 from the daemon's gates, the
        message whole — and the working list rolls back to the
        snapshot the change took: the daemon is the authority,
        and a refused edit must not ride a later save."""
        if key != self.flight_key:
            # Another surface's refusal, or a flight this tab no
            # longer holds — the failure signal carries no row,
            # and the key is the one identity it does.
            return
        self.pending = False
        self.flight = None
        if self.before is not None:
            self.working = self.before
            self.before = None
        self.repaint(self.pending_row or {})
        self.note.setText(f"refused: {failure}")
