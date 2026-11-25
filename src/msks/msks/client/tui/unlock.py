"""The TUI's unlock modal (#515, #504, #1147).

The flow itself is the client's shared half
(:mod:`msks.client.unlock`): the tree offers it at start (the
app's mount worker) and from the secrets page's ``u`` action, and
the configured identity file's private half decrypts the pending
rows the daemon reports locked. What lives here is the tree's
own prompt seam — the one passphrase modal a session asks
(:class:`PassphraseScreen`) and the outcome line's escape for the
status line's markup — the pieces the GUI does not share, its
prompt being a Qt dialog over the window's own thread.

The flows serialize on the app's turnstile and share one prompt
seam: whichever surface asks, the ask spends the session's
prompt, and the automatic re-run answers a spent prompt as a
cancel (#920).
"""

from pathlib import Path

from rich.markup import escape
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.css.query import NoMatches
from textual.screen import ModalScreen
from textual.widgets import Button, Footer, Input, Static


class PassphraseScreen(ModalScreen[str | None]):
    """The session's one passphrase prompt (#515): the identity
    file's name above one masked field. Enter answers with the
    typed passphrase; Escape or Cancel answers None and the rows
    stay locked (named, not retried)."""

    CSS = """
    PassphraseScreen { align: center middle; }
    #pass-form { width: 56; height: auto; background: $panel;
                 border: round $primary; padding: 1 2; }
    #pass-note { color: $text-muted; margin-bottom: 1;
                 text-wrap: nowrap; text-overflow: ellipsis; }
    .pass-row { height: 1; margin-bottom: 1; }
    .pass-label { width: 12; color: $text-muted; }
    #pass-field { width: 1fr; }
    #pass-buttons { height: auto; align-horizontal: center;
                    margin-top: 1; }
    #pass-buttons Button { margin: 0 2; }
    """

    BINDINGS = [
        Binding("escape", "cancel", "Cancel"),
        Binding("q", "cancel", "Cancel", show=False),
    ]

    def __init__(self, path: Path) -> None:
        super().__init__()
        self.path = path

    def compose(self) -> ComposeResult:
        with Vertical(id="pass-form"):
            # The note must fit the form's 50-column text width
            # (width 56 less the border and padding) for a
            # realistic key name — the #894 lesson: fixed words
            # leave room for the variable name, or the note's
            # point dies to the ellipsis (#916). The name leads
            # and names its passphrase's purpose: secrets
            # unlock, 20 columns of name budget.
            yield Static(
                f"{escape(self.path.name)} passphrase for secrets unlock",
                id="pass-note",
            )
            with Horizontal(classes="pass-row"):
                yield Input(password=True, id="pass-field", compact=True)
            with Horizontal(id="pass-buttons"):
                yield Button(
                    "Unlock", id="pass-ok", variant="primary", compact=True
                )
                yield Button("Cancel", id="pass-cancel", compact=True)
        yield Footer()

    def on_mount(self) -> None:
        """The masked field holds the focus; Enter submits it."""
        self.query_one("#pass-field", Input).focus()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        """Enter answers with the typed passphrase."""
        self.dismiss_clearing(event.value)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        """The Unlock button submits; Cancel answers None."""
        if event.button.id == "pass-ok":
            self.dismiss_clearing(self.query_one("#pass-field", Input).value)
        else:
            self.dismiss_clearing(None)

    def action_cancel(self) -> None:
        self.dismiss_clearing(None)

    def dismiss_clearing(self, answer: str | None) -> None:
        """Dismiss with the field cleared first — the typed
        passphrase leaves the widget tree with the screen
        (MintScreen's discipline: nothing typed stays behind)."""
        try:
            self.query_one("#pass-field", Input).value = ""
        except NoMatches:
            pass  # teardown already unmounted the field
        self.dismiss(answer)


def unlock_flash(message: str) -> str:
    """One outcome line escaped for the status line's markup
    parse — a key file's path carries operator-chosen bytes."""
    return escape(message)
