"""The flash line.

The one-line notices that age out (hold warnings, sightings) and
the markup-safe text they render through.
"""

from __future__ import annotations

import re
import time

from rich.markup import escape
from textual.content import Content

from ..consent import SecretEvent

#: How long a flashed message owns the status line.
FLASH_TTL = 5.0

#: The close code the events websocket uses for a refused token.


class FlashLine:
    """A message that owns a status line until its TTL lapses —
    the consent app's flash, lifted one level (#309) so every
    screen's status line shows it: the tree app's standing line
    and the pushed pages' own lines."""

    def __init__(self) -> None:
        self.msg = ""
        self.until = 0.0

    def set(self, message: str) -> None:
        """Give the status line to a message for FLASH_TTL
        seconds."""
        self.msg = message
        self.until = time.time() + FLASH_TTL

    def text(self, default: str | Content) -> str | Content:
        """The flash while it lives, else ``default``."""
        if self.until > time.time():
            return self.msg
        return default


def hold_flash(request) -> str:
    """The consent-line flash a first-seen hold takes (#454): the
    destination with the key in — the page pushes nothing on a
    hold's arrival, so this line is the attention the hold gets
    (the header's count beside it), the sighting flash's shape.
    The host rides through ``flash_safe``: the consent line parses
    markup at update time, and a truncated closing tag in a
    destination would raise there (#318's rule).
    """
    port = (
        " (all ports)" if request.dest_port == 0 else f":{request.dest_port}"
    )
    return f"egress to decide: {flash_safe(request.dest_host)}{port} — press e"


def sighting_flash(event: SecretEvent) -> str:
    """The status line an off-allowlist sighting takes while the
    queue (or picker) is on top: the exfil signal surfaces on every
    screen of the app, not only the audit view."""
    return (
        f"! sighting: {escape(event.workspace_id)}/{escape(event.name)}"
        f" → {escape(event.host or '?')}"
    )


def flash_safe(text: str) -> str:
    """Escape free text for a status-line flash. ``escape`` covers
    complete markup tags but passes a truncated closing tag through
    bare (``unknown workspace [/dev``) — and the status line's
    Static parses markup at update time, so a bare ``[/`` still
    raises. The second pass backslash-escapes that prefix too,
    leaving already-escaped tags untouched. Newlines collapse to
    spaces first: the status lines are one row tall, and a message
    cut between two of its own lines would crop with no ellipsis
    marking the cut."""
    flat = " ".join(text.splitlines())
    return re.sub(r"(?<!\\)\[/", r"\\\[/", escape(flat))


def panel_safe(text: str) -> str:
    """Escape free text for a panel's body, verbatim on screen:
    the truncated-tag second pass emits Textual's own single
    backslash, so a refusal echoing a bracket renders it with no
    stray backslash beside it — where the status line's own pass
    doubles it (a one-row surface whose wart predates the panel).
    The line breaks stay: a panel wraps its lines, so a refusal's
    own breaks ride through instead of collapsing to spaces."""
    return re.sub(r"(?<!\\)\[/", r"\\[/", escape(text))
