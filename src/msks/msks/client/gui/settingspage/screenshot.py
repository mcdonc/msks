"""The Screenshot subtab (#1226): the client-held screenshot pipe's
two settings — the capture command this desktop runs, whose
presence alone enables the pipe, and the loopback port the guest's
sshd binds for it. The command shows its string form (a list-form
value shell-joined, the Terminal and ssh subtab's own rule) and the
port its digits; a digit port saves as the file's native int (the
digit string would load as text), and the loader checks the range
when a save lands.

This module imports PySide6 at module scope, the screens' own
contract: nothing imports it at CLI parse time (#927).
"""

from __future__ import annotations

from .fields import Panel
from .vocabulary import SCREENSHOT_LABEL


class ScreenshotPanel(Panel):
    """The Screenshot subtab (#1226): the capture command and the
    port."""

    def __init__(self, page, parent=None) -> None:
        super().__init__(page, SCREENSHOT_LABEL, parent)
        self.text("screenshot_command")
        self.text("screenshot_port")
        self.load()

    def edits(self) -> dict[str, str | int | None]:
        """What Apply writes (#1226): the panel's edits, a port typed
        as digits as the number the file carries it."""
        edits = super().edits()
        port = edits.get("screenshot_port")
        if isinstance(port, str) and port.isdigit():
            edits["screenshot_port"] = int(port)
        return edits
