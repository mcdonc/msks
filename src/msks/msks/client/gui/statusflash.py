"""The screens' shared status-line flash (#939, #945, #904,
#1129): the workspace list and the workspace page paint the
same one-line status above their content, and the flash's
contract is one — a refusal clears itself after its span, an
outage stands for the next answer to resolve, and the
daemon-down state behind the outage is the screen's own, not
the flash's (the TUI listing's marker shape): the events feed's
heartbeat flashing over a standing outage (#946) gives the
outage's text back when its span ends, and the first answered
read while the state stands says the recovery once. The mixin
carries the slots; a host screen supplies ``status`` (the
label), sets ``flash_seq``, ``daemon_down``, and ``outage_text``
at construction, and calls :meth:`StatusFlash.recover` from its
landing with its own words.

This module imports PySide6 at module scope — the screens' own
contract: nothing imports it at CLI parse time, and the two
screens load it beside their own Qt-bearing imports.
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

    def recover(self, text: str) -> None:
        """A standing outage's resolve (#904, #1129): an answer
        landed while the daemon was down, and the recovery
        flashes once in the outage's place — the host's own
        words, clearing with the span."""
        if not self.daemon_down:
            return
        self.daemon_down = False
        self.flash(text)
