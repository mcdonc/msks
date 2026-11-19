"""The GUI dialogs' shared seams (#962, #986): the pieces the
create and edit dialogs hold in common — the whole-number rule
their fields check locally, and the close path that cancels a
standing flight and unhooks the bridge. Plain Python, no Qt in
it: the mixin rides whatever ``QDialog`` subclass takes it, and
the rule reads as plain data for the coverage gate.
"""


def whole_number(value: str) -> bool:
    """Whether a form value is a whole number (#309's own rule):
    ASCII digits only — ``int()`` refuses some unicode digits
    ``isdigit()`` accepts, and a paste can carry them."""
    return value.isascii() and value.isdigit()


class BridgeClose:
    """A dialog's bridge seams (#962, #986): the flight handle,
    the bridge, and the two slots are the subclass's own — this
    mixin owns the hookup and the close path. A dialog closed
    mid-flight cancels its own call and unhooks the bridge's
    signals, so its late answer reaches no one and the next
    dialog's own answers land on it alone.

    The hookup holds the very slot objects it connected and the
    close unhooks those: a ``QtCore.Slot``-decorated method can
    hand out a fresh wrapper per access, and a close that
    re-looked the slots up could fail to recognize its own
    hookup."""

    def hook_bridge(self, bridge) -> None:
        """The bridge's signals hooked: the subclass's two slots
        connected, the exact objects held for the close."""
        self.bridge = bridge
        self.hooks = (self.on_succeeded, self.on_failed)
        bridge.succeeded.connect(self.hooks[0])
        bridge.failed.connect(self.hooks[1])

    def done(self, result: int) -> None:
        """The dialog's close, every path's: the standing flight
        cancelled, the bridge's hands go — once, a close Qt
        re-enters finds nothing left to unhook — and the
        platform's own close under it. A wrapper whose
        construction never reached the hookup (#959's
        half-built shape) closes without touching a bridge it
        never took."""
        if self.flight is not None and not self.flight.done():
            self.flight.cancel()
        hooks = getattr(self, "hooks", None)
        if hooks is not None:
            self.hooks = None
            self.bridge.succeeded.disconnect(hooks[0])
            self.bridge.failed.disconnect(hooks[1])
        super().done(result)
