"""The Files subtab (#1226): the paths and the manifest the client
reads around its own config — the cache root, the state root, the
ssh identity and its public half, and the published channel's
manifest URL. The two directories and the three files carry
**Browse…** buttons beside their fields (the platform's own
pickers, :func:`msks.client.gui.settingspage.fields.pick_file` and
:func:`pick_directory`); a path types free as well.

This module imports PySide6 at module scope, the screens' own
contract: nothing imports it at CLI parse time (#927).
"""

from __future__ import annotations

from .fields import Panel
from .vocabulary import FILES_LABEL


class FilesPanel(Panel):
    """The Files subtab (#1226): five text fields, three with a
    file picker and two with a directory picker."""

    def __init__(self, page, parent=None) -> None:
        super().__init__(page, FILES_LABEL, parent)
        self.text("cache_dir", browse="dir")
        self.text("state_dir", browse="dir")
        self.text("identity_file", browse="file")
        self.text("pubkey_file", browse="file")
        self.text("image_manifest_url")
        self.load()
