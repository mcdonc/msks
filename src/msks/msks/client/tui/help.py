"""The ``?`` overlay (#851): every binding the screen beneath
carries — hidden ones included — each with its action label, in a
panel over the screen. Any key dismisses it; the overlay takes no
focus, so no key traps (the spatial-navigation rule).
"""

from textual import events
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.content import Content, Span
from textual.dom import DOMNode
from textual.screen import ModalScreen
from textual.widgets import Static


def own_bindings(base) -> list[Binding]:
    """One class's declared bindings (#851): empty for the bases
    the overlay skips — a plain mixin's list never reaches the
    screen (Textual merges ``BINDINGS`` from ``DOMNode`` bases
    alone, FormWalk's rule), and the framework's own bases carry
    plumbing (the Tab focus walk, the copy keys), not the
    screen's vocabulary."""
    if not issubclass(base, DOMNode) or base.__module__.startswith("textual"):
        return []
    return Binding.make_bindings(base.__dict__.get("BINDINGS", []))


def screen_bindings(screen) -> list[Binding]:
    """The screen's bindings (#851): every ``BINDINGS`` the
    screen's own classes declare, hidden ones (``show=False``)
    included — the escape leaves and the arrow bridges the footer
    never paints — in declaration order, a subclass's key
    overriding its base's. ``screen`` is a screen instance or
    class."""
    cls = screen if isinstance(screen, type) else type(screen)
    bindings: dict[str, Binding] = {}
    for base in reversed(cls.__mro__):
        for binding in own_bindings(base):
            bindings[binding.key] = binding
    return list(bindings.values())


def bare_key(binding: Binding) -> str:
    """The key with no formatter: the binding's own display, else
    its key."""
    return binding.key_display or binding.key


def binding_label(binding: Binding) -> str:
    """The binding's action label: its description, else its
    action's name (a hidden binding may carry none)."""
    return binding.description or binding.action


def help_content(bindings: list[Binding], key_display=None) -> Content:
    """The overlay's lines (#851): one per binding — the key in
    the bold default foreground, its action label beside it,
    padded to the widest key so the labels column up. The key
    reads through the app's own footer formatter (``slash``
    renders ``/``, ``up`` renders ``↑`` — the glyphs the footer
    paints) or the binding's bare key when no formatter came.
    The lines ride a Content's plain text (the rows' rule — no
    markup parsing), so a label a future binding grows cannot
    shift the columns."""
    display = key_display or bare_key
    keys = [display(binding) for binding in bindings]
    width = max((len(key) for key in keys), default=0)
    lines: list[str] = []
    spans: list[Span] = []
    offset = 0
    for binding, key in zip(bindings, keys, strict=True):
        line = f"{key:<{width}}  {binding_label(binding)}"
        spans.append(Span(offset, offset + len(key), "$text bold"))
        lines.append(line)
        offset += len(line) + 1  # the newline the join adds
    return Content("\n".join(lines), spans)


class HelpScreen(ModalScreen):
    """The ``?`` overlay (#851): the screen beneath's bindings —
    hidden ones included — each with its action label, in a panel
    over the screen. Any key dismisses it (escape, the arrows,
    ``?`` again) and focus returns to the screen beneath; the
    overlay focuses nothing itself, so no key traps."""

    #: The overlay focuses nothing (#851): nothing focusable
    #: composes under it, and the empty selector — not None,
    #: which inherits the app's — matches no widget even if one
    #: lands (the consent page's rule).
    AUTO_FOCUS = ""

    def __init__(self, title: str, bindings: list[Binding]) -> None:
        super().__init__()
        self.help_title = title
        self.help_bindings = bindings

    def compose(self) -> ComposeResult:
        with Vertical(id="help-panel"):
            yield Static(f"keys — {self.help_title}", id="help-title")
            yield Static(
                help_content(self.help_bindings, self.app.get_key_display),
                id="help-keys",
            )
            yield Static("any key closes this overlay", id="help-note")

    def on_key(self, event: events.Key) -> None:
        """Any key closes the overlay (#851). The key stops here:
        a dismiss key that fell through would act on the screen
        beneath (a ``c`` would open the create form as the overlay
        closed)."""
        event.stop()
        event.prevent_default()
        self.dismiss()


class HelpKeys:
    """The ``?`` overlay's push (#851): the one action every full
    screen of the tree carries. The binding itself is declared on
    each screen, not here — Textual merges ``BINDINGS`` from
    ``DOMNode`` bases alone, so a plain mixin's list would never
    reach the screen (FormWalk's rule); the mixin carries the
    action and the screen's title."""

    #: The overlay's title: the screen's docs name, set by each
    #: screen that carries the mixin.
    help_title = "screen"

    def action_help(self) -> None:
        """``?``: the overlay listing every binding this screen
        carries, hidden ones included."""
        self.app.push_screen(
            HelpScreen(self.help_title, screen_bindings(self))
        )
