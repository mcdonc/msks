"""The TUI's unlock flow (#515, #504).

The tree offers the unlock itself: at start (the app's mount
worker) and from the secrets page's ``u`` action, the configured
identity file's private half decrypts the pending rows the daemon
reports locked, and the plaintext rides the authenticated API
back. The flow is one shared coroutine both surfaces call — the
only difference is where the one passphrase prompt modal lands
and which status line flashes the outcome.

An agent-held key cannot unlock (#510): ssh-agent exposes
signing, and the unwrap needs the private scalar directly, so
the flow reads the file ``identity_file`` /
``MSKSC_IDENTITY_FILE`` names and prompts once when that file is
passphrase-encrypted — the one prompt per session the acceptance
bar names.
"""

from pathlib import Path

from rich.markup import escape
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.css.query import NoMatches
from textual.screen import ModalScreen
from textual.widgets import Button, Footer, Input, Static

from msks.agecrypto import AgeCryptoError, key_is_encrypted, load_identity

from ..ssh import configured_identity_file
from ..unlock import (
    decrypt_rows,
    locked_rows,
    read_identity_pem,
    value_items,
)


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
            yield Static(
                f"unlock: the identity file {escape(self.path.name)} is "
                "passphrase-encrypted — one prompt unlocks every "
                "row this session",
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


async def session_identity(path, ask_passphrase):
    """The configured key loaded for this session (#515): the
    pair of the unwrap-ready identity and the outcome line that
    ends the flow early — the loaded identity, or the named
    refusal (a cancelled prompt, an unreadable file, a key the
    crypto refuses). *ask_passphrase* owns the encrypted-key
    prompt: the modal in the tree, a constant in tests."""
    try:
        key_pem = read_identity_pem(path)
        answer = ""
        if key_is_encrypted(key_pem):
            answer = await ask_passphrase(path)
            if not answer:
                return None, (
                    "secrets stay locked — the unlock needs the "
                    f"key file's passphrase ({path.name})"
                )
        return load_identity(key_pem, lambda: answer), None
    except SystemExit as exc:
        return None, str(exc)
    except AgeCryptoError as exc:
        return None, f"unlock refused: {exc}"


def locked_refusal(rows: list, corrupt: list) -> str:
    """The nothing-opened outcome (#515): no stanza matched the
    key — another operator's rows — with the rows that failed to
    open named for the re-mint they need."""
    plural = "" if len(rows) == 1 else "s"
    line = (
        f"{len(rows)} placeholder row{plural} stay locked — the "
        "key opens none of them"
    )
    if corrupt:
        line += f" ({len(corrupt)} failed to open; re-mint them)"
    return line


def unlock_outline(unlocked: list, left: list, corrupt: list) -> str:
    """The flow's outcome line: the rows the upload unlocked,
    the ones whose ciphertext failed to open (a re-mint), and
    the ones that stayed ciphertext — another operator's."""
    outline = f"unlocked {len(unlocked)} placeholder row"
    outline += "s" if len(unlocked) != 1 else ""
    if corrupt:
        outline += f"; {len(corrupt)} failed to open (re-mint)"
    if left:
        outline += (
            f"; {len(left)} stay ciphertext (no stanza matches this key)"
        )
    return outline


async def upload_unlocked(data, unlocked: list) -> None:
    """The flow's upload half (#515): the decrypted values back
    over the authenticated API — the one item builder the CLI
    shares."""
    await data.unlock_secrets(value_items(unlocked))


def quiet_line(explicit: bool, named: str) -> str | None:
    """The two quiet gates' shared shape (#515): the mount-time
    flow says nothing when there is nothing to do, and an
    explicit ask — the page's ``u`` action — names the line the
    operator's question answers."""
    if not explicit:
        return None
    return named


async def unlock_flow(
    data, ask_passphrase, explicit: bool = False
) -> str | None:
    """The shared session unlock (#515): the configured identity
    against the daemon's locked rows. Returns the outcome line to
    flash, or None when there is nothing to say (no identity
    configured, or the daemon already holds every row's
    plaintext) — *explicit* turns both quiet cases into named
    lines, the page's ``u`` action answering an operator who
    asked."""
    path = configured_identity_file()
    if path is None:
        return quiet_line(
            explicit,
            "the unlock needs the workspace identity's private "
            "half — point identity_file (the client config) or "
            "MSKSC_IDENTITY_FILE at your key file",
        )
    rows = locked_rows(await data.pending_secrets())
    if not rows:
        return quiet_line(
            explicit,
            "nothing to unlock — the daemon holds every row's plaintext",
        )
    identity, refused = await session_identity(path, ask_passphrase)
    if refused is not None:
        return refused
    unlocked, left, corrupt = decrypt_rows(rows, identity)
    if not unlocked:
        return locked_refusal(rows, corrupt)
    await upload_unlocked(data, unlocked)
    return unlock_outline(unlocked, left, corrupt)


def unlock_flash(message: str) -> str:
    """One outcome line escaped for the status line's markup
    parse — a key file's path carries operator-chosen bytes."""
    return escape(message)
