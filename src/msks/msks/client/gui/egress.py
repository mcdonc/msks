"""The workspace Egress tab (#1042): the page's egress posture
surface — the workspace's current mode with its meaning, the
mode switch in place through the policy route the CLI's
``egress mode`` drives (#1049), and the static allowlist editor
(#1050): the list from the rules frame, one entry added or
removed at a time, each write posting the whole new list
through the same policy route with the standing mode. A pick
on the switcher commits at once, the picked mode's meaning
beside the pick. Allow and deny of a live hold stay in the
consent panel (#999, #1000), and the audit trail and the
decider presence indicator are #1042's later sub-issues. The
nothing-effectively-allowed refusal to ``static`` — the
offline-workspace escape — reaches the operator as a question
with an explicit confirm, never a silent failure or a raw
error: the switch asks before it sends when the standing read
shows the gate, and the route's own refusal (a read gone stale
between the two) asks the same question again — the editor's
writes take the same two arms, a remove that empties a static
posture being the one edit that can meet the gate.

This module imports PySide6 at module scope — the screens' own
contract: nothing imports it at CLI parse time, and the
workspace page (itself lazily imported) owns the import.
"""

from dataclasses import replace

from PySide6 import QtCore, QtGui, QtWidgets

from .consent import (
    EGRESS_MODES,
    EMPTY_STATIC_QUESTION,
    EgressRules,
    effective_allows,
    parse_rules,
)
from .dialogs import Centered
from .forms import confirm, muted_note, save_cancel_row

#: The rules read's key stem (#1042): each flight carries its own
#: key off it, the per-flight rule the tabs share — a late answer
#: from a cancelled flight reaches nothing.
RULES_STEM = "egress-rules"

#: The mode switch's key stem (#1042): one switch at a time, its
#: key its own.
SWITCH_STEM = "egress-switch"

#: The allowlist writes' key stem (#1050): one write at a time,
#: its key its own beside the read's and the switch's.
WRITE_STEM = "egress-allowlist"

#: The route's refusal marker (#280, #1042): the policy route's
#: nothing-effectively-allowed refusal is the one answer whose
#: remedy is an explicit confirm — its message names the
#: ``confirm_empty`` request flag, and no other refusal does.
#: The server's own refusal test pins the token into the
#: message, and this tab's tests match the marker against the
#: message's verbatim text — the spellings cannot drift apart
#: quietly.
CONFIRM_MARKER = "confirm_empty"

#: Each mode's meaning in place (#1042): what the posture does,
#: one plain sentence beside the switcher — the same facts the
#: spec's mode words carry.
MODE_WORDS = {
    "allow": (
        "every outbound flow goes out: no destination is refused "
        "and no question is asked"
    ),
    "static": (
        "only the allowlist's names and the in-effect verdicts "
        "answer; every other name answers NXDOMAIN"
    ),
    "interactive": (
        "each new destination holds until a decider answers "
        "(the consent panel's queue)"
    ),
}

#: The landing note's effect words (#1042), the TUI switch's own:
#: the reply's ``applied`` fact picks one.
IN_EFFECT_NOW = "in effect now"
NEXT_START = "takes effect at next start"

#: The Add form's hint (#1050): the daemon's own accepted forms,
#: the same words ``parse_allowlist`` answers — the route is the
#: authority on an entry's shape, and a refusal it makes names
#: itself on the tab's note line.
ENTRY_HINT = (
    "one entry: a host, a scope (.debian.org, *.example.com), "
    "an address, a CIDR, or host:port"
)

#: The allowlist section's heading (#1050).
ALLOWLIST_HEADING = "static allowlist"

#: The empty list's stand-in (#1050): the TUI rules line's own
#: shape, ``(empty)``, as the tab's muted note.
ALLOWLIST_EMPTY = "no entries"


class ModePicker(QtWidgets.QComboBox):
    """The switcher's combo (#1049 review): the wheel passes
    through — a scroll across the pane never switches a live
    posture — while popup picks and typed selection commit
    through ``activated`` as every deliberate pick does."""

    def wheelEvent(self, event: QtGui.QWheelEvent) -> None:
        """One wheel event over the switcher ignores itself: the
        parent takes the scroll, the pick stands."""
        event.ignore()


class EntryDialog(Centered, QtWidgets.QDialog):
    """The Add form (#1050): one allowlist entry, typed whole —
    the daemon's accepted forms named on the form's note line,
    and the route the authority on the entry's shape (a refused
    entry names itself on the tab's note line, the form long
    closed). A blank field keeps the form standing; an accepted
    form carries the entry on ``entry``. The opener's widget
    lands as the parent (#1014): the form opens centered on the
    main window."""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.entry = ""
        self.setWindowTitle("Add an allowlist entry")
        self.note = QtWidgets.QLabel(ENTRY_HINT)
        self.note.setWordWrap(True)
        self.field = QtWidgets.QLineEdit()
        self.field.setPlaceholderText("example.com")
        form = QtWidgets.QFormLayout()
        form.addRow("Entry", self.field)
        self.save_button, cancel, buttons = save_cancel_row(self.submit)
        cancel.clicked.connect(self.reject)
        layout = QtWidgets.QVBoxLayout(self)
        layout.addWidget(self.note)
        layout.addLayout(form)
        layout.addLayout(buttons)

    @QtCore.Slot()
    def submit(self) -> None:
        """The field to one entry (#1050): blank keeps the form
        standing with its reason; a whole entry accepts the
        dialog carrying it."""
        entry = self.field.text().strip()
        if not entry:
            self.note.setText("the entry is the form's one required field")
            return
        self.entry = entry
        self.accept()


class EgressTab(QtWidgets.QWidget):
    """The page's egress posture surface (#1042): the current mode
    with its meaning, the switcher under it — a pick commits at
    once — the static allowlist's entries under that with Add and
    Remove beside them, and the note line a refusal names itself
    on. The rules read refreshes on the page's tick and on every
    pushed ``egress.rules`` frame — a switch the CLI made
    elsewhere paints itself here — and the switch and the
    allowlist write each fly one flight at a time, the reply the
    fresh rules frame with the ``applied`` fact beside it."""

    def __init__(self, bridge) -> None:
        super().__init__()
        self.bridge = bridge
        self.workspace_id = ""
        self.frame: dict | None = None
        self.rules: EgressRules | None = None
        self.list_flight = None
        self.list_pending = False
        self.switch_flight = None
        self.pending_switch = False
        self.picked = ""
        self.write_flight = None
        self.pending_write = False
        self.working: list[str] = []
        self.wrote_mode = ""
        self.seq = 0
        self.list_key = ""
        self.switch_key = ""
        self.write_key = ""
        self.mode_line = QtWidgets.QLabel("", self)
        self.mode_line.setTextInteractionFlags(
            QtCore.Qt.TextInteractionFlag.TextSelectableByMouse
        )
        self.pick = ModePicker()
        self.pick.addItems(list(EGRESS_MODES))
        self.pick.currentIndexChanged.connect(self.sync)
        # The pick is the commit (#1049 review): ``activated`` is
        # the operator's own choice — a programmatic index change
        # (the read's follow) decides nothing.
        self.pick.activated.connect(self.switch)
        self.words = QtWidgets.QLabel("", self)
        self.words.setWordWrap(True)
        row = QtWidgets.QHBoxLayout()
        row.addWidget(self.pick)
        row.addWidget(self.words, stretch=1)
        self.heading = QtWidgets.QLabel(ALLOWLIST_HEADING, self)
        self.entries = QtWidgets.QListWidget(self)
        self.entries.itemSelectionChanged.connect(self.sync_buttons)
        self.empty = muted_note(ALLOWLIST_EMPTY)
        self.empty.setVisible(False)
        self.add_button = QtWidgets.QPushButton("Add")
        self.add_button.clicked.connect(self.add)
        self.remove_button = QtWidgets.QPushButton("Remove")
        self.remove_button.clicked.connect(self.remove)
        self.remove_button.setEnabled(False)
        buttons = QtWidgets.QHBoxLayout()
        buttons.addWidget(self.add_button)
        buttons.addWidget(self.remove_button)
        buttons.addStretch(1)
        self.note = QtWidgets.QLabel("", self)
        self.note.setTextInteractionFlags(
            QtCore.Qt.TextInteractionFlag.TextSelectableByMouse
        )
        layout = QtWidgets.QVBoxLayout(self)
        layout.addWidget(self.mode_line)
        layout.addLayout(row)
        layout.addWidget(self.heading)
        layout.addWidget(self.entries, stretch=1)
        layout.addWidget(self.empty)
        layout.addLayout(buttons)
        layout.addWidget(self.note)
        self.sync()
        bridge.succeeded.connect(self.on_succeeded)
        bridge.failed.connect(self.on_failed)

    def host(self, workspace_id: str) -> None:
        """The page's open (#1042): a workspace switch cancels the
        flights the old workspace still had out — their answers
        are not the new workspace's to take — blanks the posture
        and the editor's list, and lets the new workspace's own
        read fill them."""
        if workspace_id == self.workspace_id:
            return
        for flight in (
            self.list_flight,
            self.switch_flight,
            self.write_flight,
        ):
            if flight is not None and not flight.done():
                flight.cancel()
        self.list_flight = None
        self.switch_flight = None
        self.write_flight = None
        self.list_pending = False
        self.pending_switch = False
        self.pending_write = False
        self.list_key = ""
        self.switch_key = ""
        self.write_key = ""
        self.wrote_mode = ""
        self.workspace_id = workspace_id
        self.frame = None
        self.rules = None
        self.note.setText("")
        self.take({})

    @property
    def current(self) -> str:
        """The standing mode (#1042): blank while no read has
        landed — the switcher's gate reads it."""
        return self.rules.mode if self.rules is not None else ""

    def poke(self) -> None:
        """One rules read through the bridge (#1042) — the page's
        tick and the open share it. A read already in flight is
        skipped, the listing's own guard."""
        if not self.workspace_id or self.list_pending:
            return
        self.list_pending = True
        self.seq += 1
        self.list_key = f"{RULES_STEM}-{self.seq}"
        self.list_flight = self.bridge.call(
            self.list_key, "egress_rules", self.workspace_id
        )

    def take(self, frame: object) -> None:
        """One rules frame into the tab's standing state (#1042,
        #1050): the raw frame for the empty-static gate's own
        words, the parsed posture beside it, and the frame's
        allowlist into the editor's list. A switch already out
        keeps the pick and a write already out keeps the working
        list — each one's answer repaints, and an older read the
        page raced cannot undo the operator's send."""
        self.frame = frame if isinstance(frame, dict) else {}
        self.rules = parse_rules(self.frame)
        mode = self.rules.mode if self.rules is not None else ""
        self.mode_line.setText(
            f"current mode: {mode}" if mode else "current mode: —"
        )
        self.follow(mode)
        self.retake_entries()
        self.sync()

    def retake_entries(self) -> None:
        """The editor's list from the landed frame (#1050): the
        working list takes the frame's allowlist while no write
        is out — its answer repaints, and an older read the page
        raced cannot roll the operator's edit back out from
        under it."""
        if self.pending_write:
            return
        self.working = (
            list(self.rules.allow_list) if self.rules is not None else []
        )
        self.repaint_entries()

    def follow(self, mode: str) -> None:
        """The pick follows the daemon while no switch is out
        (#1042): its answer repaints, and an older read the page
        raced cannot un-pick the operator's send. A posture the
        combo does not carry reads as the blank."""
        if self.pending_switch:
            return
        index = EGRESS_MODES.index(mode) if mode in EGRESS_MODES else 0
        self.pick.setCurrentIndex(index)

    def push(self, frame: dict) -> None:
        """One pushed frame (#1042): a fresh ``egress.rules`` —
        the daemon broadcasts one on every policy change — repaints
        the posture and the list, whatever made the change;
        anything else is not this tab's state."""
        if frame.get("event") != "egress.rules":
            return
        data = frame.get("data") or {}
        if data.get("workspace_id") != self.workspace_id:
            return
        self.take(data)

    @QtCore.Slot()
    def sync(self) -> None:
        """The pick's meaning beside it (#1042): the picked mode's
        own words, kept current with every pick — the read's
        follow included."""
        self.words.setText(MODE_WORDS.get(self.pick.currentText(), ""))

    def repaint_entries(self) -> None:
        """The editor's list from the working list (#1050): one
        row per entry, the empty state's stand-in beneath while
        none stands, and the buttons following the ground and
        the selection. A rebuild the rows already show skips
        itself: the page's tick repaints every few seconds, and
        the selection the Remove action reads must survive the
        ticks that change nothing — a frame that changes the
        list rebuilds the rows and the selection goes with it."""
        shown = [
            self.entries.item(row).text()
            for row in range(self.entries.count())
        ]
        if shown != self.working:
            self.entries.clear()
            for entry in self.working:
                self.entries.addItem(entry)
            self.empty.setVisible(not self.working)
        self.sync_buttons()

    def editable(self) -> bool:
        """Whether the editor writes now (#1050): a landed read
        stands (facts the editor can compose against — a posture
        the tab has not seen is not one it rewrites), the
        workspace stands, and no policy flight is out — the
        route is one, and a switch and a write cannot race each
        other to it."""
        return (
            self.rules is not None
            and bool(self.workspace_id)
            and not self.pending_switch
            and not self.pending_write
        )

    @QtCore.Slot()
    def sync_buttons(self) -> None:
        """The editor buttons' state (#1050): Add on the editable
        ground, Remove on a selected row besides — the mounts
        tab's own rule."""
        held = 0 <= self.entries.currentRow() < self.entries.count()
        self.add_button.setEnabled(self.editable())
        self.remove_button.setEnabled(held and self.editable())

    @QtCore.Slot()
    def switch(self) -> None:
        """The pick's own action (#1042): the picked mode goes
        through the policy route at once, one flight at a time.
        A pick of ``static`` over a read with nothing effectively
        allowed asks first — the shared gate every surface's
        switch takes (#280) — and the route's refusal arm (a read
        gone stale between the gate and the send) asks its
        question there."""
        mode = self.pick.currentText()
        if not self.switchable(mode):
            return
        if mode == "static" and not effective_allows(self.rules):
            self.asked_static()
            return
        self.send(mode)

    def switchable(self, mode: str) -> bool:
        """Whether the pick switches now (#1042): a landed read
        stands (a posture the tab has not seen is not one it
        switches), the workspace stands, the pick names a mode
        the daemon is not already in, and no policy flight is
        out — the write's gate beside the switch's own (#1050):
        the route is one."""
        return (
            self.rules is not None
            and bool(self.workspace_id)
            and mode != self.current
            and not self.pending_switch
            and not self.pending_write
        )

    def asked_static(self) -> None:
        """The offline gate's arm (#280, #1042): the shared
        question over the standing read — a yes sends the
        confirmed switch, a no decides nothing."""
        if self.ask_empty_static():
            self.send("static", confirm_empty=True)

    def ask_empty_static(self) -> bool:
        """The offline escape's question (#280, #1042): the shared
        text every surface asks, No the default — a yes sends the
        confirmed write, a no decides nothing."""
        return confirm(self, "Switch to static", EMPTY_STATIC_QUESTION)

    def send(self, mode: str, *, confirm_empty: bool = False) -> None:
        """One mode switch through the bridge (#1042): the policy
        route the CLI's ``egress mode`` drives, under its own key
        — one at a time (the switchable gate holds the one
        flight), the answer repainting the posture and naming
        its effect."""
        self.pending_switch = True
        self.picked = mode
        self.seq += 1
        self.switch_key = f"{SWITCH_STEM}-{self.seq}"
        self.note.setText("switching …")
        self.sync()
        self.switch_flight = self.bridge.call(
            self.switch_key,
            "set_egress_mode",
            self.workspace_id,
            mode,
            confirm_empty,
        )

    @QtCore.Slot()
    def add(self) -> None:
        """The Add action (#1050): the form blank, an accepted
        entry joining the list's end — the write posting the
        whole new list through the policy route."""
        if not self.editable():
            return
        dialog = EntryDialog(self)
        if dialog.exec() and dialog.entry:
            self.change([*self.working, dialog.entry])

    @QtCore.Slot()
    def remove(self) -> None:
        """The Remove action (#1050): the selected row's entry
        leaves the list, the remaining list posting whole — no
        confirmation beside it: a remove the route refuses rolls
        back, and a landed one can always be added back."""
        index = self.entries.currentRow()
        if index < 0 or index >= len(self.working):
            return
        self.change([*self.working[:index], *self.working[index + 1 :]])

    def empties_static(self, entries: list[str]) -> bool:
        """Whether the composed write empties a static posture
        (#1050): the switch's own offline gate, taken over the
        composition — the standing mode static, the composed
        list empty, and no in-effect allowed verdict. The one
        edit that can meet the gate asks its question before the
        send; every other write flies straight. The standing
        mode's own read gates the caller (``change``), so the
        rules the composition builds on stand."""
        if self.current != "static":
            return False
        composed = replace(self.rules, allow_list=tuple(entries))
        return not effective_allows(composed)

    def change(self, entries: list[str]) -> None:
        """One composed list to the daemon (#1050): a
        composition that empties a static posture asks the
        shared question first — a yes sends the confirmed write,
        a no decides nothing. The posture the composition
        writes beside is captured here: a frame the modal ask
        lets land (a switch another surface made) cannot repoint
        the write the operator answers for."""
        if not self.editable():
            return
        mode = self.current
        if not self.empties_static(entries):
            self.write(entries, mode)
            return
        if self.ask_empty_static():
            self.write(entries, mode, confirm_empty=True)

    def write(
        self, entries: list[str], mode: str, *, confirm_empty: bool = False
    ) -> None:
        """One allowlist write through the bridge (#1050): the
        policy route the CLI's ``egress mode --allow`` drives,
        the composed posture beside the full new list — the
        editor changes the list, not the posture — under its own
        key, one at a time. The working list takes the
        composition at once (the operator reads their edit the
        moment they make it, the daemon's answer confirming) and
        the answer — the fresh rules frame — repaints it."""
        self.pending_write = True
        self.working = entries
        self.wrote_mode = mode
        self.seq += 1
        self.write_key = f"{WRITE_STEM}-{self.seq}"
        self.note.setText("saving …")
        self.repaint_entries()
        self.write_flight = self.bridge.call(
            self.write_key,
            "set_egress_mode",
            self.workspace_id,
            mode,
            confirm_empty,
            entries,
        )

    @QtCore.Slot(str, object)
    def on_succeeded(self, key: str, result: object) -> None:
        """One answered call (#1042, #1050): the read's, the
        write's, and the switch's answers each land on the arm
        that asked — each reply the fresh rules frame — and
        another key's answer is not this tab's to take."""
        if self.read_landed(key, result):
            return
        if self.write_landed(key, result):
            return
        self.switch_landed(key, result)

    def read_landed(self, key: str, result: object) -> bool:
        """The read's answer (#1042): the frame repaints the
        posture and the list. False when the key names no flight
        of the read's own."""
        if key != self.list_key or not self.list_key:
            return False
        self.list_pending = False
        self.list_flight = None
        self.take(result)
        return True

    def effect_note(self, prefix: str, reply: dict) -> None:
        """The landing note (#1042, #1050): the reply's
        ``applied`` fact picks the effect words beside the named
        action — the same two words the switch's and the
        write's landings share."""
        effect = IN_EFFECT_NOW if reply.get("applied") else NEXT_START
        self.note.setText(f"{prefix} — {effect}")

    def write_landed(self, key: str, result: object) -> bool:
        """The write's answer (#1050): the reply is the fresh
        rules frame — the list repaints from it — and the note
        names the write's effect. False when the key names no flight of the
        write's own."""
        if key != self.write_key or not self.write_key:
            return False
        self.pending_write = False
        self.write_flight = None
        self.take(result)
        reply = result if isinstance(result, dict) else {}
        self.effect_note("allowlist saved", reply)
        return True

    def switch_landed(self, key: str, result: object) -> None:
        """The switch's answer (#1042): the reply is the fresh
        rules frame with the ``applied`` fact beside it — the
        posture repaints and the note names its effect, the
        sent mode standing in when the reply carries none. A
        key none of the three arms owns changes nothing."""
        if key != self.switch_key or not self.switch_key:
            return
        self.pending_switch = False
        self.switch_flight = None
        self.take(result)
        reply = result if isinstance(result, dict) else {}
        mode = reply.get("mode") or self.picked
        self.effect_note(f"mode {mode}", reply)

    @QtCore.Slot(str, object)
    def on_failed(self, key: str, failure: object) -> None:
        """One refused call (#1042, #1050): the read's, the
        write's, and the switch's refusals each land on the arm
        that asked — the shared question's two arms over the
        offline escape, the route's own words over everything
        else — and another key's failure is not this tab's to
        take."""
        if self.read_refused(key):
            return
        if self.write_refused(key, failure):
            return
        self.switch_refused(key, failure)

    def read_refused(self, key: str) -> bool:
        """The read's refusal (#1042): the posture and the list
        stand for the next poke. False when the key names no
        flight of the read's own."""
        if key != self.list_key or not self.list_key:
            return False
        self.list_pending = False
        self.list_flight = None
        return True

    def write_refused(self, key: str, failure: object) -> bool:
        """The write's refusal (#1050): a refusal that names the
        escape (a verdict the read held left between the gate
        and the send) surfaces as the question — a yes resends
        the composition confirmed, a no decides nothing — and
        any other refusal names itself on the note line: the
        route's own words for a rejected entry, the list rolled
        back. False when the key names no flight of the write's
        own."""
        if key != self.write_key or not self.write_key:
            return False
        self.pending_write = False
        self.write_flight = None
        self.settle_write_refusal(str(failure))
        return True

    def settle_write_refusal(self, message: str) -> None:
        """The refused write's arms (#1050): the escape asks its
        question, everything else names itself — and every arm
        but a resent yes restores the list from the standing
        frame: the daemon is the authority (it refused the
        write, so its list stands), and a refused edit must not
        ride a later one."""
        if CONFIRM_MARKER in message:
            self.confirm_write_refusal()
            return
        self.note.setText(f"refused: {message}")
        self.restore_entries()

    def confirm_write_refusal(self) -> None:
        """The escape's arm over a refused write (#1050): the
        composition and its posture go to the question — a read
        the modal ask lets land can repaint the list and the
        mode mid-question, and the operator's yes confirms the
        write that was refused, not whatever landed under the
        dialog. A yes resends it confirmed; a no decides
        nothing."""
        sent = self.working
        mode = self.wrote_mode
        if self.ask_empty_static():
            self.write(sent, mode, confirm_empty=True)
            return
        self.note.setText("")
        self.restore_entries()

    def switch_refused(self, key: str, failure: object) -> None:
        """A refused switch (#1042): the refusal settles on the
        switch's own arms. A key none of the three arms owns
        changes nothing."""
        if key != self.switch_key or not self.switch_key:
            return
        self.pending_switch = False
        self.switch_flight = None
        self.settle_switch_refusal(str(failure))

    def settle_switch_refusal(self, message: str) -> None:
        """The refused switch's arms (#1042): a refusal that
        names the escape (the read went stale between the gate
        and the send) surfaces as the question with its explicit
        confirm — a yes resends confirmed, a no keeps the
        standing mode — and any other refusal names itself on
        the note line. Either arm returns the pick to the
        standing mode: the daemon refused, and the posture it
        holds is the one that stands."""
        if CONFIRM_MARKER in message:
            if self.ask_empty_static():
                self.send(self.picked, confirm_empty=True)
                return
            self.note.setText("")
            self.take(self.frame)
            return
        self.note.setText(f"refused: {message}")
        self.take(self.frame)

    def restore_entries(self) -> None:
        """The refused write's restore (#1050): the standing
        frame repaints the list — the freshest the tab has, a
        frame that landed under the question's modal loop
        included — beside the daemon's own standing list, the
        daemon having refused the write."""
        self.take(self.frame)
