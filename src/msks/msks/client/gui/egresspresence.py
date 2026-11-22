"""The Egress tab's decider presence indicator (#1052): the
page's decider registration — the link that claims the
workspace's holds (#999) — painted live under the posture row.
The link tells its presence on the wire (:mod:`msks.client.gui.
decider`): the registration's answer tells the serve, every
connection end and failed dial tells the drop, the daemon's
refusal tells its reason; the bridge crosses them
(:mod:`msks.client.gui.bridge`) and this label paints them. The
modes that hold no prompts answer for themselves — the mode's
own words where nothing prompts, so an unprompted workspace
names which case it is in: the mode asks nothing, the
registration is down, or the daemon refused it.

This module imports PySide6 at module scope — the screens' own
contract: nothing imports it at CLI parse time, and the
workspace page (itself lazily imported) owns the import.
"""

from PySide6 import QtCore, QtWidgets

#: The indicator's state keys (#1052): the page's registration
#: as the bridge's signals tell it — the tab's own vocabulary,
#: the link's tells mapped one to one over the signals.
PRESENCE_CONNECTING = "connecting"
PRESENCE_CONNECTED = "connected"
PRESENCE_DOWN = "down"
PRESENCE_REFUSED = "refused"

#: The indicator's words for the modes that hold no prompts
#: (#1052): the mode itself answers why nothing prompts — the
#: registration's state stays beside them for the modes that
#: hold prompts.
NO_PROMPTS_WORDS = {
    "allow": "no holds wait — allow mode gates nothing",
    "static": "no holds wait — static mode gates without questions",
}

#: The indicator's words for the registration's own states
#: (#1052): the modes that hold prompts read these, a mode the
#: read has not landed among them.
PRESENCE_WORDS = {
    PRESENCE_CONNECTING: "the decider registration is connecting",
    PRESENCE_CONNECTED: (
        "holds wait for this page — the decider registration is connected"
    ),
    PRESENCE_DOWN: "the decider registration is down — reconnecting",
}

#: The refusal's leading words (#1052): the daemon's refusal
#: leads whatever the mode says — a standing fault the page's
#: own re-open alone retries.
PRESENCE_REFUSED_WORDS = "the daemon refused the decider registration"


def presence_text(mode: str, state: str, reason: str = "") -> str:
    """The indicator's one line (#1052): the daemon's refusal of
    the registration leads — a standing fault, its reason beside
    it — then the mode's own words where the mode holds no
    prompts, then the registration's state words for the modes
    that hold prompts. A mode the words do not carry reads the
    state's words alone."""
    if state == PRESENCE_REFUSED:
        suffix = f" — {reason}" if reason else ""
        return f"{PRESENCE_REFUSED_WORDS}{suffix}"
    if mode in NO_PROMPTS_WORDS:
        return NO_PROMPTS_WORDS[mode]
    return PRESENCE_WORDS[state]


class PresenceLine(QtWidgets.QLabel):
    """The registration's presence painted live (#1052): the
    page's own link's tells — the bridge's ``decider_served``,
    ``decider_dropped``, and ``decider_rejected`` signals, each
    carrying its link's workspace — connected here, a foreign
    workspace's tell told and ignored (a retired link's drop
    must not paint the workspace the page moved to), each own
    tell naming its state, the standing mode told beside them
    (the no-prompts modes answer for themselves). The open
    resets the line to connecting: the new workspace's
    registration is its own, and its first tell paints the
    line."""

    def __init__(self, bridge) -> None:
        super().__init__()
        self.workspace_id = ""
        self.mode = ""
        self.state = PRESENCE_CONNECTING
        self.reason = ""
        self.setTextInteractionFlags(
            QtCore.Qt.TextInteractionFlag.TextSelectableByMouse
        )
        bridge.decider_served.connect(self.served)
        bridge.decider_dropped.connect(self.dropped)
        bridge.decider_rejected.connect(self.refused)

    def host(self, workspace_id: str) -> None:
        """The open's reset (#1052): the line takes the new
        workspace and its registration's starting state — the
        link the page launches beside the host tells the rest."""
        self.workspace_id = workspace_id
        self.mode = ""
        self.state = PRESENCE_CONNECTING
        self.reason = ""
        self.repaint_line()

    def take(self, mode: str) -> None:
        """The standing mode (#1052), told with every landed
        read: the no-prompts modes' words take the line — a
        posture that holds prompts reads the registration's
        state words under it."""
        self.mode = mode
        self.repaint_line()

    @QtCore.Slot(str)
    def served(self, workspace_id: str) -> None:
        """The registration served (#1052): the daemon answered
        it — an owned frame landed — and the line says the holds
        wait for this page while the mode holds prompts."""
        if workspace_id != self.workspace_id:
            return
        self.state = PRESENCE_CONNECTED
        self.reason = ""
        self.repaint_line()

    @QtCore.Slot(str, str)
    def dropped(self, workspace_id: str, reason: str) -> None:
        """The registration's connection ended or its dial
        failed (#1052): the line reads down — the link
        re-registers on its own, and the window's feed flashes
        the daemon-wide outage the same failure names. The
        reason stays off the line: the feed's flash carries
        it."""
        if workspace_id != self.workspace_id:
            return
        self.state = PRESENCE_DOWN
        self.repaint_line()

    @QtCore.Slot(str, str)
    def refused(self, workspace_id: str, reason: str) -> None:
        """The daemon refused the registration (#1052): the
        refusal leads the line whatever the mode says, its
        reason beside it — the link ended, and the page's own
        re-open alone retries."""
        if workspace_id != self.workspace_id:
            return
        self.state = PRESENCE_REFUSED
        self.reason = reason
        self.repaint_line()

    def repaint_line(self) -> None:
        """The line from the standing facts (#1052): the
        refusal leading, the mode's own answer beside it, the
        registration's state words under them. The unhosted
        line keeps its construction blank — the page hosts
        before any tell of its own paints, and a foreign
        workspace's tell never reaches here."""
        self.setText(presence_text(self.mode, self.state, self.reason))
