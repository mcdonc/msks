"""The screens' shared status-line flash (#939, #945, #904,
#1129, #1132): the workspace list, the workspace page, and the
Images and Secrets tabs paint the same one-line status above
their content, and the flash's contract is one — a refusal
clears itself after its span, an outage stands for the next
answer to resolve, and the daemon-down state behind the outage
is the screen's own, not the flash's (the TUI listing's marker
shape): the events feed's heartbeat flashing over a standing
outage (#946) gives the outage's text back when its span ends,
and the first answered read while the state stands says the
recovery once. The mixin carries the slots; a host screen
supplies ``status`` (the label), sets ``flash_seq``,
``daemon_down``, and ``outage_text`` at construction, and calls
:meth:`StatusFlash.recover` from its landing with its own words.

This module imports PySide6 at module scope — the screens' own
contract: nothing imports it at CLI parse time, and the screens
load it beside their own Qt-bearing imports.
"""

from PySide6 import QtCore

#: How long a flashed refusal stays on the status line,
#: milliseconds — long enough to read, gone before it reads as
#: the screen's standing state. An outage (the daemon answered
#: nothing, :attr:`RestFailure.reach`) never times out: the line
#: holds its message until an answer resolves it, the #904
#: contract the TUI's own listing keeps.
FLASH_MS = 4000


class StatusFlash:
    """A screen's status-line flash: :meth:`flash` shows a
    message, :meth:`clear_stale` is the span's timeout slot, and
    :meth:`recover` resolves a standing outage with one recovery
    flash. The host screen supplies the ``status`` label and the
    three attributes the methods read."""

    def flash(self, message: str, *, outage: bool = False) -> None:
        """The status line's flash (#939, #945, #904): the message
        shows; a refusal clears itself after the flash span, an
        outage stands for the next answer to resolve — the
        daemon-down state its own, so a flash over it (the feed's
        heartbeat, #946) gives the outage's text back when its
        span ends (#1129). Each flash carries its own serial, so
        a later flash's message is not erased by an earlier
        flash's timeout."""
        if outage:
            self.daemon_down = True
            self.outage_text = message
        self.flash_seq += 1
        self.status.setText(message)
        if outage:
            return
        seq = self.flash_seq
        # The screen is the timeout's receiver (#959): the
        # pending callback is severed when the widget's C++
        # object goes, instead of firing later on a wrapper
        # whose widgets are already destroyed.
        QtCore.QTimer.singleShot(FLASH_MS, self, lambda: self.clear_stale(seq))

    def clear_stale(self, seq: int) -> None:
        """The flash timeout's slot: the line clears only when
        this timeout's flash is still the one on it — and takes
        the standing outage's text back while the daemon is
        down."""
        if seq == self.flash_seq:
            self.status.setText(self.outage_text if self.daemon_down else "")

    def refuse(self, message: str, *, reach: bool) -> None:
        """A refused call's flash, the two kinds #904 tells apart:
        an outage (the daemon answered nothing) stands and marks
        the daemon down; a refusal is an answer, and a standing
        outage resolves behind the refusal's own flash — without
        the recovery's words, so the answer the operator just
        read keeps the line (#776's mask). The TUI's marker
        leaves the same way on a refusal."""
        if reach:
            self.flash(message, outage=True)
            return
        self.resolve()
        self.flash(message)

    def outcome(self, message: str) -> None:
        """An action's answer (#776's mask): the daemon answered
        through the action, so a standing outage resolves behind
        the outcome's own flash — without the recovery's words,
        which the read the action triggers would otherwise put
        over the outcome one round trip later."""
        self.resolve()
        self.flash(message)

    def resolve(self) -> None:
        """The daemon answered through another door while the
        outage stood — an action's outcome, a refusal, a removed
        row's notice: the state clears without the recovery's
        words, the answer's own flash keeping the line (#776's
        mask). The feed's flashes never call it: a drop is no
        answer, and a reconnect's flash follows the serve poke's
        re-read, which carries the recovery itself."""
        self.daemon_down = False

    def recover(self, text: str) -> None:
        """A standing outage's resolve (#904, #1129): an answer
        landed while the daemon was down, and the recovery
        flashes once in the outage's place — the host's own
        words, clearing with the span."""
        if not self.daemon_down:
            return
        self.resolve()
        self.flash(text)
