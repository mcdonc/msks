"""The modal screens and the edge list.

The duration picker both questions reuse, the mode picker, the
confirm, the failure panel, and the focus-preserving ListView.
"""

from __future__ import annotations

import asyncio

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Footer, ListView, OptionList, Static

from ..consent import (
    DURATION_DEFAULT,
    DURATIONS,
    EGRESS_MODES,
)
from .flash import panel_safe


class PickerScreen(ModalScreen[str | None]):
    """One pick from a short, static list: Enter picks, Escape or q
    cancels, and the current choice starts highlighted. The chosen
    string (or None) goes to the callback given at construction.
    The mode picker (#280), the duration picker, and the audit
    view's filters (#390) all ride this one shape."""

    BINDINGS = [
        Binding("q", "cancel", show=False),
        Binding("escape", "cancel", "Cancel", show=False),
    ]

    def __init__(self, options, current, picked) -> None:
        super().__init__()
        self.options = tuple(options)
        self.current = current
        self.picked = picked

    def compose(self) -> ComposeResult:
        yield OptionList(*self.options, id="pick-options")

    def on_mount(self) -> None:
        options = self.query_one("#pick-options", OptionList)
        options.focus()
        # A current that names no option leaves nothing highlighted:
        # the widget pre-highlights its first row at construction,
        # and a bare Enter must not take a pre-selected choice the
        # operator never made — the mode picker's no-rules window
        # would otherwise pre-select ``allow`` (#465 review).
        options.highlighted = (
            list(self.options).index(self.current)
            if self.current in self.options
            else None
        )

    def on_option_list_option_selected(
        self, event: OptionList.OptionSelected
    ) -> None:
        self.dismiss_with(str(event.option.prompt))

    def action_cancel(self) -> None:
        self.dismiss_with(None)

    def dismiss_with(self, pick: str | None) -> None:
        """Dismiss and hand the pick to the callback (async — the
        exchange runs as a task, so the modal closes without
        waiting on it)."""
        self.dismiss()
        # Referenced: an unreferenced task can be collected mid-await.
        self._pick_task = asyncio.create_task(self.picked(pick))


class ModeScreen(PickerScreen):
    """The mode picker (#280): Enter picks, Escape or q cancels.
    The chosen mode (or None) goes to the callback given at
    construction."""

    def __init__(self, current: str, picked) -> None:
        super().__init__(EGRESS_MODES, current, picked)


class ConfirmScreen(ModalScreen[bool]):
    """A yes/no question (#280): ``y``/Enter answers True, ``n``/
    ``q``/Escape answers False. The callback given at construction
    runs as a task with the answer."""

    BINDINGS = [
        Binding("y", "yes", "Confirm"),
        Binding("enter", "yes", "Confirm", show=False),
        Binding("n", "no", "Cancel"),
        Binding("q", "no", "Cancel", show=False),
        Binding("escape", "no", "Cancel", show=False),
    ]

    def __init__(self, question: str, answered) -> None:
        super().__init__()
        self.question = question
        self.answered = answered

    def compose(self) -> ComposeResult:
        yield Static(self.question, id="question")

    def action_yes(self) -> None:
        self.dismiss_with(True)

    def action_no(self) -> None:
        self.dismiss_with(False)

    def dismiss_with(self, answer: bool) -> None:
        """Dismiss and hand the answer to the callback."""
        self.dismiss()
        self._pick_task = asyncio.create_task(self.answered(answer))


class FailurePanel(ModalScreen):
    """A failed create or mint (#426): the daemon's refusal in a
    panel that waits for dismissal — Enter, Escape, ``q``, or the
    Close button — where a flash would drop the detail after
    five seconds and a form note would squeeze it into one line.
    The title names the verb and the identity the form submitted;
    the body carries the daemon's detail verbatim, wrapping at
    the panel's edge."""

    BINDINGS = [
        Binding("enter", "close", "Close", show=False),
        Binding("q", "close", "Close"),
        Binding("escape", "close", "Close", show=False),
    ]

    def __init__(self, verb: str, identity: str | None, detail: str) -> None:
        super().__init__()
        self.verb = verb
        self.identity = identity
        self.detail = detail

    def heading(self) -> str:
        """The panel's title line: the verb that failed and the
        identity the form submitted (a store check names no
        identity — its refusal belongs to no one body), escaped
        for the markup parse the way the body is — a name
        carrying a truncated closing tag renders literally, not
        crashes the panel. A method, not an attribute — a
        Screen's own ``title`` starts as None and would shadow
        one."""
        if self.identity is None:
            return f"{self.verb} failed"
        return f"{self.verb} failed: {panel_safe(self.identity)}"

    def compose(self) -> ComposeResult:
        with Vertical(id="failure-panel"):
            yield Static(self.heading(), id="failure-title")
            yield Static(panel_safe(self.detail), id="failure-detail")
            with Horizontal(id="failure-buttons"):
                yield Button(
                    "Close", id="do-close", variant="primary", compact=True
                )
        yield Footer()

    def on_mount(self) -> None:
        self.query_one("#do-close", Button).focus()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.action_close()

    def action_close(self) -> None:
        """Dismiss — the panel has no timeout; it leaves when the
        operator closes it."""
        self.dismiss()


class DurationScreen(PickerScreen):
    """The duration picker: Enter picks, Escape or q cancels. The
    chosen duration (or None) goes to the callback given at
    construction. The verdict picker rides the consent durations
    with ``tilrestart`` highlighted; the secrets page's renew
    picker (#390) rides TTL-appropriate choices with its own
    default."""

    def __init__(
        self,
        picked,
        choices: tuple[str, ...] = DURATIONS,
        default: str = DURATION_DEFAULT,
    ) -> None:
        super().__init__(choices, default, picked)


class EdgeListView(ListView):
    """A :class:`~textual.widgets.ListView` whose arrow keys leave
    the list at its edges (#454): the consent page stacks two
    lists, and the arrows alone must walk both in reading order —
    the spatial-navigation rule, the mint form's picker-edge
    pattern carried to the page's lists. The interior rows take
    the stock cursor walk; an edge hands the walk to the callback
    given at construction (nothing happens at an edge with no
    handoff — the walk just stops, as it does at the page's own
    top and bottom)."""

    BINDINGS = [
        Binding("up", "edge_previous", show=False),
        Binding("down", "edge_next", show=False),
    ]

    def __init__(self, *children, leave_up=None, leave_down=None, **kwargs):
        super().__init__(*children, **kwargs)
        self.leave_up = leave_up
        self.leave_down = leave_down

    def at_top(self) -> bool:
        """Whether the walk leaves upward from here: no rows,
        nothing highlighted, or the first row highlighted."""
        return not self.children or self.index in (None, 0)

    def at_bottom(self) -> bool:
        """Whether the walk leaves downward from here: no rows,
        nothing highlighted, or the last row highlighted."""
        return not self.children or self.index == len(self.children) - 1

    def action_edge_previous(self) -> None:
        """Up: the interior walks the rows, the top edge hands the
        walk to the surface above."""
        if self.at_top():
            if self.leave_up is not None:
                self.leave_up()
        else:
            self.action_cursor_up()

    def action_edge_next(self) -> None:
        """Down: the interior walks the rows, the bottom edge
        hands the walk to the surface below."""
        if self.at_bottom():
            if self.leave_down is not None:
                self.leave_down()
        else:
            self.action_cursor_down()


#: The verdict keys' standing hint (#470 C2): the letters the
#: queue answers, named beside the queue itself — the footer's
#: ellipsis variant cannot teach its own meaning.

#: The verdict keys' standing hint (#470 C2): the letters the
#: queue answers, named beside the queue itself — the footer's
#: ellipsis variant cannot teach its own meaning.
HOLDS_HINT = "a allow · d deny · shift or enter picks a duration · x revoke"
