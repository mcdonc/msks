"""The GUI's unlock flow (#1147, #515): the client's shared unlock
run as two flights over the bridge, the passphrase prompt between
them on the window's own thread.

The window offers the flow at every landing — the first tree and
each of the picker's re-dials (#1009). The first flight reads the
daemon's locked rows and the configured key file's state
(:meth:`msks.client.gui.data.GuiData.unlock_prepare`); a file
that asks for a passphrase opens the window's own prompt
(:func:`msks.client.gui.forms.prompt_password`) before the second
flight loads the key, uploads the rows it opens, and answers with
the outcome line
(:meth:`msks.client.gui.data.GuiData.unlock_finish`). The prompt
is the window's one per session, whichever surface asks: the
Secrets tab's Unlock button asks by hand, the automatic flow
never asks twice, and a start against a daemon that answered
nothing arms one re-run on the feed's next serve (#920) — the
poke every screen re-reads on when the daemon comes back. The
outcome lands on the status line of the screen the ask stood on:
the listing's for the automatic flow, the Secrets tab's for the
button.

The failure rides opaquely here — ``msks.client.data`` sits at the
import fan-in ceiling — so the slots read the failure's message
and its ``reach`` flag off the object.

This module imports PySide6 at module scope — the screens' own
contract: nothing imports it at CLI parse time, and ``.app``'s
window path loads it after its guards.
"""

from PySide6 import QtCore

from ..unlock import SPENT_PROMPT_LINE, prompt_refusal
from .forms import prompt_password

#: The two flights' keys (#1147): the bridge answers each by its
#: own, and another key's answer is not the flow's to take.
PREPARE_KEY = "unlock-prepare"
FINISH_KEY = "unlock-finish"

#: The prompt's frame (#1147): the window's title, the note the
#: TUI modal shows (#916 — the key file's name leading, its
#: purpose after it), and the OK button's word.
PROMPT_TITLE = "Unlock secrets"
PROMPT_NOTE = "{} passphrase for secrets unlock"
PROMPT_ACCEPT = "Unlock"

#: The by-hand action the spent prompt's line points at (#920).
BY_HAND = "the Secrets tab's Unlock button"


class UnlockFlow(QtCore.QObject):
    """The window's unlock flow (#1147): one per window, standing
    over the re-dials — the prompt count is the session's — and
    following each landed tree through :meth:`retarget`. The
    receivers here live in the GUI thread, so the bridge's
    answers arrive queued, the prompt opens over ``window``, and
    one ask runs at a time."""

    def __init__(self, window) -> None:
        super().__init__()
        self.window = window
        self.bridge = None
        self.listing = None
        self.secrets = None
        #: The standing ask's screen (#1147): its status line
        #: takes the outcome, the listing's for the automatic
        #: flow and the Secrets tab's for the button.
        self.target = None
        #: The standing ask's kind: the button's asks by hand.
        self.explicit = False
        self.busy = False
        self.passphrase_asked = False
        self.retry = False

    def retarget(self, bridge, listing, secrets) -> None:
        """A landed tree (#1009, #1147): the flow follows the
        fresh bridge, the listing its start outcome flashes on,
        and the Secrets tab whose button asks by hand — the
        stopped tree's hooks let go first, and a flight of its
        bridge never lands. The offer stands at every landing:
        the daemon behind the fresh tree is asked for its locked
        rows at once, the TUI's own start rule (#515)."""
        if self.bridge is not None:
            self.bridge.succeeded.disconnect(self.on_succeeded)
            self.bridge.failed.disconnect(self.on_failed)
            self.bridge.feed_served.disconnect(self.served)
            self.secrets.unlock_asked.disconnect(self.by_hand)
        self.bridge, self.listing, self.secrets = bridge, listing, secrets
        bridge.succeeded.connect(self.on_succeeded)
        bridge.failed.connect(self.on_failed)
        bridge.feed_served.connect(self.served)
        secrets.unlock_asked.connect(self.by_hand)
        self.busy = False
        self.target = None
        self.retry = False
        self.automatic()

    def automatic(self) -> None:
        """The automatic flow (#515) — the landing's offer and
        the re-run alike — on the listing's status line: a flow
        already standing carries it instead, the one at a time
        the TUI's turnstile keeps."""
        if self.busy:
            return
        self.begin(self.listing, explicit=False)

    @QtCore.Slot()
    def by_hand(self) -> None:
        """The Secrets tab's Unlock button (#515, the ``u``
        action's counterpart): the ask on the tab's own status
        line, asking for the passphrase whatever the session
        already asked. A flow already standing takes no second
        ask."""
        if self.busy:
            return
        self.begin(self.secrets, explicit=True)

    def begin(self, target, explicit: bool) -> None:
        """One ask's start: the screen its outcome lands on and
        its kind remembered, the fetch out over the bridge."""
        self.busy = True
        self.target = target
        self.explicit = explicit
        self.bridge.call(PREPARE_KEY, "unlock_prepare", explicit)

    @QtCore.Slot(str, object)
    def on_succeeded(self, key: str, result: object) -> None:
        """One answered flight (#1147): the fetch's landing
        decides the prompt, the second flight's answers the
        outcome. Another key's answer is not the flow's to
        take."""
        if key == PREPARE_KEY:
            self.offered(result)
        elif key == FINISH_KEY:
            self.answer(outcome_line(result))

    def offered(self, result: object) -> None:
        """The fetch's landing (#1147): a flow that ended on the
        worker — no rows to unlock — lands its line on the
        standing screen, or nothing when the line is None (the
        automatic flow's quiet cases). An encrypted key file asks
        the window for its passphrase before the second flight; a
        refused ask names the rows locked and sends nothing.
        Otherwise the second flight flies with the answer, empty
        for a file that needs none."""
        offering = result if isinstance(result, dict) else {}
        rows, first = offering.get("rows"), offering.get("line")
        if rows is None:
            self.answer(first)
            return
        path = offering["path"]
        answer = ""
        if offering["encrypted"]:
            answer, refusal = self.ask_passphrase(path)
            if refusal is not None:
                self.answer(refusal)
                return
        self.bridge.call(FINISH_KEY, "unlock_finish", rows, path, answer)

    def ask_passphrase(self, path) -> tuple[str, str | None]:
        """The window's passphrase ask for an encrypted key file
        (#1147): the typed answer, or the line the rows stay
        locked under beside it. The automatic flow of a session
        that already asked is answered as a spent prompt (#920);
        the button's asks whatever the session asked. The ask
        spends the session's prompt, and a prompt the operator
        dismisses answers the cancel line — the rows stay
        locked, named, not retried."""
        if self.passphrase_asked and not self.explicit:
            return "", prompt_refusal(path, SPENT_PROMPT_LINE.format(BY_HAND))
        self.passphrase_asked = True
        answer = prompt_password(
            self.window,
            PROMPT_TITLE,
            PROMPT_NOTE.format(path.name),
            accept=PROMPT_ACCEPT,
        )
        if answer:
            return answer, None
        return "", prompt_refusal(path, None)

    @QtCore.Slot(str, object)
    def on_failed(self, key: str, failure: object) -> None:
        """A refused flight (#1147): the failure flashes on the
        standing screen the way every screen's does — an outage
        stands, a refusal clears with its span — and an automatic
        ask the daemon answered nothing arms the one re-run the
        feed's next serve fires (#920): a refusal, a by-hand ask,
        and a spent prompt arm nothing. Another key's failure is
        not the flow's to show."""
        if key not in (PREPARE_KEY, FINISH_KEY):
            return
        reach = bool(getattr(failure, "reach", False))
        if reach and not self.explicit and not self.passphrase_asked:
            self.retry = True
        self.answer(f"unlock failed: {failure}", reach=reach)

    @QtCore.Slot()
    def served(self) -> None:
        """The feed reached its serve loop (#920): the armed
        re-run fires once — the same automatic flow, the same
        prompt rule — and the arm clears first, so one serve
        serves one re-run. A session that spent its prompt in
        between drops the arm, and a flow already standing takes
        no second ask."""
        if not self.retry or self.busy:
            return
        spent, self.retry = self.passphrase_asked, False
        if spent:
            return
        self.automatic()

    def answer(self, line: str | None, reach: bool | None = None) -> None:
        """One ask's end (#1147): the gate opens, and the line
        lands on the standing screen's status line — an outcome
        under a reached daemon, the refusal kind when *reach*
        says which — while None says nothing, the quiet cases'
        own rule."""
        self.busy = False
        if line is None:
            return
        if reach is None:
            self.target.outcome(line)
        else:
            self.target.refuse(line, reach=reach)


def outcome_line(result: object) -> str | None:
    """The second flight's answer (#1147): the outcome line as
    the data layer sends it, anything else read as nothing to
    say."""
    return result if isinstance(result, str) else None
