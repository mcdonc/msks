"""Client-side GUI screens (PySide6; the daemon never imports these).

The package presents its three parts: the entry the
``msks gui`` command runs (:mod:`.app`), the asyncio worker
thread the REST calls run on (:mod:`.bridge`), and the screen
actions over the transport (:mod:`.data`). Phase 1 of the GUI
client (#924) ships the shell: the window and the screens land
with the later phases of the plan.
"""

from . import (  # noqa: F401 — the package presents its three parts
    app,
    bridge,
    data,
)
