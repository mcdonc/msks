"""The Terminal and ssh subtab (#1226): the launcher that opens a
workspace shell in a new terminal window, and the ssh options
remembered for every ssh session msks runs. Both keys take a
string or a list in the file (#314, #385); the field shows the
string form — a list-form value shell-joined into it, the same
round trip the resolution gives the environment — and a changed
field saves the string form. A list-form value the operator leaves
alone is no edit, and the file keeps its list.

This module imports PySide6 at module scope, the screens' own
contract: nothing imports it at CLI parse time (#927).
"""

from __future__ import annotations

from .fields import Panel
from .vocabulary import SHELL_LABEL


class ShellPanel(Panel):
    """The Terminal and ssh subtab (#1226): two text fields, the
    string forms of the two command keys."""

    def __init__(self, page, parent=None) -> None:
        super().__init__(page, SHELL_LABEL, parent)
        self.text("terminal_open_cmd")
        self.text("ssh_options")
        self.load()
