"""The GUI dialogs' shared seams (#962, #986, #1014): the
pieces the create and edit dialogs hold in common — the
whole-number rule their fields check locally, the size forms
the dialog's size fields read (#1164), the close path that
cancels a standing flight and unhooks the bridge, and the
placement rule that holds every popup on the main window's
center. Plain Python, no Qt in it: the mixins ride whatever
``QDialog`` subclass takes them, and the rules read as plain
calls for the coverage gate.
"""

import re

from ..volumes import MIB, human_bytes

#: The size fields the dialogs read in the human forms (#1164):
#: memory and the two volumes, the body's MiB counts.
SIZE_FIELDS = frozenset({"mem_mib", "root_mib", "home_mib"})

#: The forms a size field accepts (#1164): a whole number of MiB
#: as the TUI's own form reads it, or the number with a unit —
#: G/GiB (×1024 MiB) and M/MiB (×1) — a decimal allowed only
#: before a unit, and only when it resolves to a whole number
#: of MiB (`0.5G` is 512; a bare `1.5` has no unit to resolve
#: it, and refuses as the whole-number rule always refused it).
#: The number needs its leading digit: `.5G` refuses with `1.G`
#: and `8 G`. ASCII digits alone, the whole-number rule's own
#: hazard: unicode digits a paste can carry refuse here too.
#: `GB` and `MB` refuse on purpose: they name 10⁹ and 10⁶
#: bytes, different sizes than the daemon's MiB count.
SIZE_FORM = re.compile(r"^([0-9]+(?:\.[0-9]+)?)([GgMm](?:[Ii][Bb])?)?$")

#: The suffixes' multipliers on MiB, lowercased.
SIZE_UNITS = {"g": 1024, "gib": 1024, "m": 1, "mib": 1}

#: The note a refused size answers with (#1164): the accepted
#: forms, in the words the operator types them.
SIZE_NOTE = "sizes read as MiB, or with a G or M suffix: 8G, 512M"


def whole_number(value: str) -> bool:
    """Whether a form value is a whole number (#309's own rule):
    ASCII digits only — ``int()`` refuses some unicode digits
    ``isdigit()`` accepts, and a paste can carry them."""
    return value.isascii() and value.isdigit()


def parse_size(value: str) -> int | None:
    """A size field's value in MiB (#1164), or None when the
    form refuses: a whole number of MiB, or a number with a
    G/GiB or M/MiB suffix, a decimal before the unit resolving
    to a whole number of MiB through integer arithmetic — a
    product with a fraction (`0.3G`, 307.2 MiB) refuses rather
    than rounding the daemon's count, at any number of digits."""
    match = SIZE_FORM.match(value)
    if match is None:
        return None
    unit = SIZE_UNITS.get((match[2] or "").lower(), 1)
    whole, dot, fraction = match[1].partition(".")
    if dot and not match[2]:
        return None
    return whole_mib(int(whole) * unit, fraction, unit)


def whole_mib(scaled: int, fraction: str, unit: int) -> int | None:
    """The integer part's MiB plus the fraction's share of the
    unit (#1164), None when that share is not a whole MiB."""
    if not fraction:
        return scaled
    share = int(fraction) * unit
    scale = 10 ** len(fraction)
    if share % scale:
        return None
    return scaled + share // scale


def size_error(field: str) -> str:
    """The note a refused size field answers with (#1164): the
    field's name and the accepted forms."""
    return f"{field}: {SIZE_NOTE}"


def read_size(field: str, value: str) -> tuple[int | None, str | None]:
    """A size field's value as the body carries it (#1164), or
    its note: the MiB count when the form reads, the note in
    its place when it refuses — the one read both dialogs'
    size branches take."""
    mib = parse_size(value)
    return mib, None if mib is not None else size_error(field)


def exact_human(mib: int) -> str | None:
    """A size's human spelling when it reads back to the same
    MiB count (#1164) — `8G`, `1.5G`, `512M` — and None for a
    size the rendering's tenths round (`8193` renders `8G`, one
    MiB short): every spelling the dialogs show or seed a size
    in goes through here, so no shown size hides a MiB."""
    text = human_bytes(mib * MIB)
    return text if parse_size(text) == mib else None


def mib_text(mib: int) -> str:
    """A size in MiB as the create dialog's summary line spells
    it (#1164): the client's one human rendering — 8G, 1.5G,
    512M — with the binary unit written out (8 GiB, 1.5 GiB,
    512 MiB), and the plain MiB count for a size the rendering
    would round (8193 MiB): the line's job is to confirm what
    the operator typed."""
    human = exact_human(mib)
    if human is None:
        return f"{mib} MiB"
    return f"{human[:-1]} {human[-1]}iB"


def seed_size(mib: int) -> str:
    """A row's size as its field's seed text (#1164): the human
    spelling when it reads back to the same number of MiB —
    8G, 1.5G, 1000M — and the plain MiB count otherwise, so the
    editor's same-size check and the root's floor read the row
    exactly."""
    return exact_human(mib) or f"{mib}M"


def center_on_parent(dialog) -> None:
    """One dialog placed on its parent window's center (#1014):
    the dialog, sized to its own layout first, moves until its
    center is the parent's window center — the pre-show geometry
    carries no frame margins, so a decorating window manager's
    frame grows around the placed spot. A parentless dialog
    stays where it is — nothing names a window to sit on.
    Plain widget calls throughout: the seam rides whatever
    ``QDialog`` subclass takes it, no binding import at parse
    time."""
    opener = dialog.parentWidget()
    if opener is None:
        return
    dialog.adjustSize()
    frame = dialog.frameGeometry()
    frame.moveCenter(opener.window().geometry().center())
    dialog.move(frame.topLeft())


class Centered:
    """A dialog that opens on its parent window's center
    (#1014): a dialog the caller parents to a widget of the
    main window is transient for it, so most window managers
    place it over the app — but placement stays the manager's,
    and a manager may answer with a screen corner. The mixin
    rides whatever ``QDialog`` subclass takes it: the parent
    argument lands on the subclass's own ``super().__init__``,
    and the centering runs once, on the ``exec`` every popup
    opens through, before the modal loop starts."""

    def exec(self) -> int:
        """The dialog's open, centered first (#1014): the frame
        moves onto the opener's window, then the platform's own
        modal loop under it."""
        center_on_parent(self)
        return super().exec()


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
