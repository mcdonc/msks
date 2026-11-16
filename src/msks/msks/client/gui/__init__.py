"""Client-side GUI screens (PySide6; the daemon never imports these).

The package presents its parts: the entry the ``msks gui``
command runs (:mod:`.app`) and the screen actions over the
transport (:mod:`.data`, PySide6-free). The asyncio worker
(:mod:`.bridge`) is pinned by nothing here — it imports the Qt
binding at module scope, and this package's own init runs at
CLI parse time on every command (``wscommands`` imports
``.app`` through it), so pinning it would break a base
install's every command (#927). ``.app`` imports it lazily on
the window path, which is the reachability the gate counts.
Phase 2 of the GUI client (#924) fills the data layer and the
bridge in; the screens land with the later phases of the plan.
"""

from . import (  # noqa: F401 — reachability pin for the gate; the
    # bridge reaches through app.py's lazy window-path import.
    app,
    data,
)
