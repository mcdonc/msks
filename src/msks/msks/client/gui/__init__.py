"""Client-side GUI screens (PySide6; the daemon never imports these).

The package presents its parts: the entry the ``msks gui``
command runs (:mod:`.app`), the screen actions over the
transport (:mod:`.data`, PySide6-free), and the workspace
list's presenter (:mod:`.rows`, phase 3 of #924 — plain row
models, PySide6-free; the widget that renders them lands with
the later phases of the plan). The asyncio worker
(:mod:`.bridge`) is pinned by nothing here — it imports the Qt
binding at module scope, and this package's own init runs at
CLI parse time on every command (``wscommands`` imports
``.app`` through it), so pinning it would break a base
install's every command (#927). ``.app`` imports it lazily on
the window path, which is the reachability the gate counts.
"""

from . import (  # noqa: F401 — reachability pins for the gate; the
    # bridge reaches through app.py's lazy window-path import.
    app,
    data,
    rows,
)
