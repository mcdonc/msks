"""The workspace Egress tab (#1042's first sub-issue, #1049): the
page's egress posture surface — the workspace's current mode
with its meaning, and the mode switch in place through the
policy route the CLI's ``egress mode`` drives: a pick on the
switcher commits at once, the picked mode's meaning beside
the pick. The tab is the posture surface only: allow and deny
of a live hold stay in the consent panel (#999, #1000), and
the allowlist editor, the audit trail, and the decider
presence indicator are #1042's later sub-issues. The
nothing-effectively-allowed refusal to ``static`` — the
offline-workspace escape — reaches the operator
as a question with an explicit confirm, never a silent failure
or a raw error: the pick asks before it sends when the standing
read shows the gate, and the route's own refusal (a read gone
stale between the two) asks the same question again.

This module imports PySide6 at module scope — the screens' own
contract: nothing imports it at CLI parse time, and the
workspace page (itself lazily imported) owns the import.
"""

from PySide6 import QtCore, QtGui, QtWidgets

from .consent import (
    EGRESS_MODES,
    EMPTY_STATIC_QUESTION,
    EgressRules,
    effective_allows,
    parse_rules,
)
from .forms import confirm

#: The rules read's key stem (#1042): each flight carries its own
#: key off it, the per-flight rule the tabs share — a late answer
#: from a cancelled flight reaches nothing.
RULES_STEM = "egress-rules"

#: The mode switch's key stem (#1042): one switch at a time, its
#: key its own.
SWITCH_STEM = "egress-switch"

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


class ModePicker(QtWidgets.QComboBox):
    """The switcher's combo (#1049 review): the wheel passes
    through — a scroll across the pane never switches a live
    posture — while popup picks and typed selection commit
    through ``activated`` as every deliberate pick does."""

    def wheelEvent(self, event: QtGui.QWheelEvent) -> None:
        """One wheel event over the switcher ignores itself: the
        parent takes the scroll, the pick stands."""
        event.ignore()


class EgressTab(QtWidgets.QWidget):
    """The page's egress posture surface (#1042): the current mode
    with its meaning, the switcher under it — a pick commits at
    once — and the note line a refusal names itself on. The rules
    read refreshes on the page's tick and on every pushed
    ``egress.rules`` frame — a switch the CLI made elsewhere
    paints itself here — and the switch flies one flight at a
    time, its reply the fresh rules frame with the ``applied``
    fact beside it."""

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
        self.seq = 0
        self.list_key = ""
        self.switch_key = ""
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
        self.note = QtWidgets.QLabel("", self)
        self.note.setTextInteractionFlags(
            QtCore.Qt.TextInteractionFlag.TextSelectableByMouse
        )
        layout = QtWidgets.QVBoxLayout(self)
        layout.addWidget(self.mode_line)
        layout.addLayout(row)
        layout.addStretch(1)
        layout.addWidget(self.note)
        self.sync()
        bridge.succeeded.connect(self.on_succeeded)
        bridge.failed.connect(self.on_failed)

    def host(self, workspace_id: str) -> None:
        """The page's open (#1042): a workspace switch cancels the
        flights the old workspace still had out — their answers
        are not the new workspace's to take — blanks the posture,
        and lets the new workspace's own read fill it."""
        if workspace_id == self.workspace_id:
            return
        for flight in (self.list_flight, self.switch_flight):
            if flight is not None and not flight.done():
                flight.cancel()
        self.list_flight = None
        self.switch_flight = None
        self.list_pending = False
        self.pending_switch = False
        self.list_key = ""
        self.switch_key = ""
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
        """One rules frame into the tab's standing state (#1042):
        the raw frame for the empty-static gate's own words, the
        parsed posture beside it. A switch already out keeps the
        pick — its answer repaints, and an older read the page
        raced cannot un-pick the operator's send."""
        self.frame = frame if isinstance(frame, dict) else {}
        self.rules = parse_rules(self.frame)
        mode = self.rules.mode if self.rules is not None else ""
        self.mode_line.setText(
            f"current mode: {mode}" if mode else "current mode: —"
        )
        self.follow(mode)
        self.sync()

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
        the posture, whatever made the change; anything else is
        not this tab's state."""
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
        the daemon is not already in, and no switch is out — the
        gates the button the pick replaced once carried."""
        return (
            self.rules is not None
            and bool(self.workspace_id)
            and mode != self.current
            and not self.pending_switch
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
        confirmed switch, a no decides nothing."""
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

    @QtCore.Slot(str, object)
    def on_succeeded(self, key: str, result: object) -> None:
        """One answered call (#1042): the read's answer repaints
        the posture; the switch's answer repaints it too — the
        reply is the fresh rules frame — and names its effect with
        the reply's ``applied`` fact. Another key's answer is not
        this tab's to take."""
        if key == self.list_key and self.list_key:
            self.list_pending = False
            self.list_flight = None
            self.take(result)
            return
        if key != self.switch_key or not self.switch_key:
            return
        self.switch_landed(result)

    def switch_landed(self, result: object) -> None:
        """The switch's answer (#1042): the reply is the fresh
        rules frame with the ``applied`` fact beside it — the
        posture repaints and the note names its effect."""
        self.pending_switch = False
        self.switch_flight = None
        reply = result if isinstance(result, dict) else {}
        self.take(reply)
        mode = reply.get("mode") or self.picked
        effect = IN_EFFECT_NOW if reply.get("applied") else NEXT_START
        self.note.setText(f"mode {mode} — {effect}")

    @QtCore.Slot(str, object)
    def on_failed(self, key: str, failure: object) -> None:
        """One refused call (#1042): a read's refusal stands the
        posture for the next poke; a switch's refusal that names
        the escape (the read went stale between the gate and the
        send) surfaces as the question with its explicit confirm —
        a yes resends confirmed, a no keeps the standing mode —
        and any other refusal names itself on the note line."""
        if key == self.list_key and self.list_key:
            self.list_pending = False
            self.list_flight = None
            return
        if key != self.switch_key or not self.switch_key:
            return
        self.switch_refused(str(failure))

    def switch_refused(self, message: str) -> None:
        """A refused switch (#1042): a refusal that names the
        escape (the read went stale between the gate and the
        send) surfaces as the question with its explicit confirm
        — a yes resends confirmed, a no keeps the standing mode —
        and any other refusal names itself on the note line.
        Either arm returns the pick to the standing mode: the
        daemon refused, and the posture it holds is the one that
        stands."""
        self.pending_switch = False
        self.switch_flight = None
        if CONFIRM_MARKER in message:
            if self.ask_empty_static():
                self.send(self.picked, confirm_empty=True)
                return
            self.note.setText("")
            self.take(self.frame)
            return
        self.note.setText(f"refused: {message}")
        self.take(self.frame)
