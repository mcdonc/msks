"""The sentinel panel.

The masked-sentinel reveal and copy modal the secrets screen
pushes for a row's fresh or standing sentinel.
"""

from __future__ import annotations

from rich.markup import escape
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.css.query import NoMatches
from textual.screen import ModalScreen
from textual.widgets import Button, Footer, Static

from .cells import (
    SCOPED_SENTINEL,
    WIDE_SENTINEL,
    coverage_text,
    osc52_copy,
    sentinel_reach,
)


def masked_sentinel(sentinel: str) -> str:
    """The sentinel's masked display: the prefix stays — it
    names the reach, holds no bearer power — and the body renders
    one bullet per character, so the panel opens without putting
    the token on screen."""
    if not sentinel:
        return ""
    if sentinel.startswith(SCOPED_SENTINEL):
        prefix = SCOPED_SENTINEL
    elif sentinel.startswith(WIDE_SENTINEL):
        prefix = WIDE_SENTINEL
    else:
        return "\u2022" * len(sentinel)
    return prefix + "\u2022" * (len(sentinel) - len(prefix))


class SentinelPanel(ModalScreen):
    """The sentinel's panel (#393, #440): the mint's reply opens
    it, and Enter (or a click) on a listing row opens it again
    over the on-demand fetch (#440) — the daemon serves the row's
    sentinel to a token holder whenever asked, so the panel opens
    on demand, not only at mint (the value is the operator's own
    and rides no reply — #423).
    The sentinel opens masked — its prefix names the reach, the
    body renders as bullets — and the Show action (`s`) reveals
    and hides it again: the panel stands where a shoulder could
    read it. `c` (or the Copy button) writes the sentinel to the
    terminal's clipboard over OSC 52 — the copy path a terminal
    that honors the sequence answers, over ssh included; the copy
    needs no reveal, and a terminal that honors nothing leaves
    the sentinel on the panel until it closes. Closing clears the
    panel's text: the sentinel leaves no trace in the widget tree
    behind it."""

    BINDINGS = [
        Binding("s", "show", "Show"),
        Binding("c", "copy", "Copy"),
        Binding("q", "close", "Close"),
        Binding("escape", "close", "Close", show=False),
    ]

    def __init__(self, row: dict) -> None:
        super().__init__()
        self.row = row
        self.revealed = False

    def compose(self) -> ComposeResult:
        with Vertical(id="sentinel-panel"):
            yield Static(
                f"{escape(coverage_text(self.row))}/"
                f"{escape(self.row['name'])}",
                id="panel-note",
            )
            yield Static("sentinel:", id="panel-label")
            yield Static(
                masked_sentinel(self.row.get("sentinel") or ""),
                id="panel-sentinel",
            )
            yield Static(
                f"reach: {sentinel_reach(self.row)}", id="panel-reach"
            )
            yield Static(
                "s reveals the sentinel — this display ends with the panel",
                id="panel-rule",
            )
            with Horizontal(id="panel-buttons"):
                yield Button(
                    "Show", id="do-show", variant="primary", compact=True
                )
                yield Button("Copy", id="do-copy", compact=True)
                yield Button("Close", id="do-close", compact=True)
        yield Footer()

    def on_mount(self) -> None:
        self.query_one("#do-show", Button).focus()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "do-show":
            self.action_show()
        elif event.button.id == "do-copy":
            self.action_copy()
        else:
            self.action_close()

    def action_show(self) -> None:
        """Reveal or hide the sentinel: the panel swaps the masked
        body for the real one (and back), so the token reaches
        the screen only while the operator asked for it."""
        self.revealed = not self.revealed
        self.query_one("#panel-sentinel", Static).update(
            self.row.get("sentinel") or ""
            if self.revealed
            else masked_sentinel(self.row.get("sentinel") or "")
        )
        self.query_one("#do-show", Button).label = (
            "Hide" if self.revealed else "Show"
        )

    def action_copy(self) -> None:
        """The OSC 52 copy (#393): the sequence rides the driver
        beside the frame — masked or revealed, the real sentinel
        is what lands — and the note names what happened."""
        copied = osc52_copy(self.app, self.row.get("sentinel") or "")
        self.query_one("#panel-note", Static).update(
            "copied to the clipboard — where the terminal honors OSC 52"
            if copied
            else "the copy did not land — no terminal to write through"
        )

    def action_close(self) -> None:
        """Close: the panel's text clears first (#393) — the
        sentinel leaves the widget tree with the display."""
        for widget_id in (
            "panel-note",
            "panel-label",
            "panel-sentinel",
            "panel-reach",
            "panel-rule",
        ):
            try:
                self.query_one(f"#{widget_id}", Static).update("")
            except NoMatches:
                pass  # teardown unmounted this line first
        self.row = {}
        self.dismiss()
