"""The workspace page's action row.

The page's action vocabulary — one PageAction per kind, the
label and note each status carries, and the content shapes the
row renders — beside the clone-name modal.
"""

from __future__ import annotations

import asyncio
from typing import NamedTuple

from rich.markup import escape
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.content import Content, Span
from textual.screen import ModalScreen
from textual.widgets import Input, Static

from ..rows import muted_style

#: The power verbs' dimming rule (#367): the statuses that
#: make each verb pointless — the row dims with its reason
#: while the workspace sits in one of them, and Enter names the
#: reason instead of calling the daemon. Stop dims beside a
#: stopped workspace and through a stop's graceful window (#789:
#: the row reads ``stopping`` until the request lands). Every
#: other status leaves both rows live: the daemon owns the
#: vocabulary (a workspace may read paused, starting, created,
#: or a watcher word), and a verb it would still take stays
#: offered — a refusal names itself on the page's consent line.
#: The shell action runs in every state
#: (#479): the ssh child it opens boots a stopped workspace
#: itself, so the row never dims — the power pair alone carries
#: the rule.
DIMMED_WHEN = {
    "start": ("running",),
    "stop": ("stopped", "stopping"),
}

#: The workspace page's shell action (#341) — a page action, not a
#: flow: the tree keeps running while the window owns its own
#: terminal.
ACTION_SHELL_WINDOW = "shell-window"

#: The workspace page's consent action (#358, #454): the egress
#: consent page — the holds queue and the in-effect verdicts on
#: one full-screen visit, opened by hand; holds surface passively
#: while it is closed (the header's count, the consent line's
#: flash). The consent page's ``m`` owns the egress-mode switch
#: (#344) — the picker's one path.
ACTION_CONSENT = "egress-consent"


class PageAction(NamedTuple):
    """One fixed action row of the workspace page (#309, #367,
    #752): its kind, the name it paints, the muted description
    behind the em dash (empty when the name stands alone), and
    the group it belongs to — use, configure, or power — whose
    name rides the muted caption row above the group's rows."""

    kind: str
    name: str
    desc: str
    group: str


def action_note(kind: str, status: str) -> str | None:
    """The reason a power row stands dimmed (#367, #789), or None
    while its verb runs: a status that makes it pointless."""
    if status in DIMMED_WHEN.get(kind, ()):
        return f"workspace is {status}"
    return None


def action_label(kind: str) -> str:
    """The row's painted name for its kind — the subject a skip
    flash names (#470 L5): the same words both screens read."""
    return next(
        (spec.name for spec in PAGE_ACTIONS if spec.kind == kind), kind
    )


def dimmed_action_content(
    name: str, note: str, marker: str, muted: str
) -> Content:
    """A dimmed power row's paint (#367): the whole row muted,
    its reason riding in the description's place."""
    text = f"{marker} {name} — {note}"
    offset = 2 + len(name)
    return Content(
        text,
        [Span(2, offset, muted), Span(offset + 3, len(text), muted)],
    )


def live_action_content(
    name: str, desc: str, marker: str, muted: str, primary: bool
) -> Content:
    """A live action row's paint (#367, #470 W1/S3, #752): the
    name bold on the page's primary rows alone — the action the
    current status permits — the description muted behind an em
    dash (every live row carries one, #470 W2). Focus carries its
    own cues: the list's highlight bar and the marker cell, so
    bold answers one question only."""
    text = f"{marker} {name} — {desc}"
    offset = 2 + len(name)
    spans = []
    if primary:
        spans.append(Span(2, offset, "$text bold"))
    spans.append(Span(offset + 3, len(text), muted))
    return Content(text, spans)


def primary_action(kind: str, status: str) -> bool:
    """Whether the row is the page's primary (#470 W1): the
    shell in every state (its child boots a stopped workspace
    itself, #479), the start verb while the workspace sits
    stopped — the emphasis follows the rows the state invites."""
    if kind == ACTION_SHELL_WINDOW:
        return True
    return kind == "start" and status == "stopped"


def action_content(
    spec: PageAction, status: str, theme_variables: dict | None, focused: bool
) -> Content:
    """One action row's paint (#367, #470 W1/S3, #752): a marker
    cell on the row Enter acts on — the highlighted row (the
    highlight bar stays the list's own cue; the marker keeps the
    row legible where a theme's bar reads weakly) — beside the
    row's tone-painted content. A power row the status dims
    (:data:`DIMMED_WHEN`) mutes the whole row behind its reason;
    every other row rides :func:`live_action_content`, its bold
    on the rows the status permits (:func:`primary_action`)
    alone. The cells ride a Content's plain text, so a
    markup-carrying name cannot shift the spans."""
    marker = "▸" if focused else " "
    muted = muted_style(theme_variables or {})
    note = action_note(spec.kind, status)
    if note is not None:
        return dimmed_action_content(spec.name, note, marker, muted)
    primary = primary_action(spec.kind, status)
    return live_action_content(spec.name, spec.desc, marker, muted, primary)


#: The workspace page's fixed actions (#309, #752), top to bottom
#: in three groups — use, configure, power — each group's name
#: riding the muted caption row above its rows (#367's blank-line
#: separation carried by the captions, #752).
PAGE_ACTIONS = (
    PageAction(
        ACTION_SHELL_WINDOW,
        "Open a shell",
        "ssh in this terminal",
        "use",
    ),
    PageAction(
        ACTION_CONSENT,
        "Egress",
        "decide holds and review rules",
        "configure",
    ),
    PageAction("edit", "Edit settings", "sizes and topology", "configure"),
    PageAction(
        "mounts",
        "Mounts",
        "host shares at guest paths",
        "configure",
    ),
    PageAction(
        "proxies",
        "Proxies",
        "persisted -L and -R port forwards",
        "configure",
    ),
    PageAction(
        "clone",
        "Clone",
        "copy to a new workspace",
        "configure",
    ),
    PageAction("start", "Start", "boot the workspace", "power"),
    PageAction("stop", "Stop", "power it off", "power"),
)


class CloneNameScreen(ModalScreen[str | None]):
    """The clone's name prompt (#773): one Input — Enter submits
    the name (the daemon owns the charset rule), Escape cancels.
    The callback given at construction runs as a task with the
    name (or None), the picker screens' shape."""

    BINDINGS = [Binding("escape", "cancel", "Cancel", show=False)]

    def __init__(self, source: str, submitted) -> None:
        super().__init__()
        self.source = source
        self.submitted = submitted

    def compose(self) -> ComposeResult:
        with Vertical(id="clone-prompt"):
            yield Static(
                f"clone {escape(self.source)} into:", id="clone-question"
            )
            yield Input(placeholder="new workspace name", id="clone-input")

    def on_mount(self) -> None:
        self.query_one("#clone-input", Input).focus()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        name = event.value.strip()
        if not name:
            return
        self.dismiss_with(name)

    def action_cancel(self) -> None:
        self.dismiss_with(None)

    def dismiss_with(self, name: str | None) -> None:
        """Dismiss and hand the name to the callback."""
        self.dismiss()
        self._submit_task = asyncio.create_task(self.submitted(name))
