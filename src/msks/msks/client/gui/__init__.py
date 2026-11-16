"""Client-side GUI screens (PySide6; the daemon never imports these).

The package presents its parts: the entry the ``msks gui``
command runs (:mod:`.app`), the screen actions over the
transport (:mod:`.data`, PySide6-free), the workspace list's
presenter (:mod:`.rows`, phase 3 of #924 — plain row models,
PySide6-free), and the workspace detail view's presenter
(:mod:`.detail`, phase 4 — plain section models,
PySide6-free). The screen widgets
(:mod:`.workspaces`, #939 — the list; :mod:`.workspace`, #945
— the detail page and the shell that swaps the two) and the
asyncio worker (:mod:`.bridge`) are pinned by nothing here — both import the
Qt binding at module scope, and this package's own init runs
at CLI parse time on every command (``wscommands`` imports
``.app`` through it), so pinning either would break a base
install's every command (#927). ``.app`` imports both lazily
on the window path, which is the reachability the gate counts.
"""

from . import (  # noqa: F401 — reachability pins for the gate;
    # the bridge and the screen module both reach through
    # app.py's lazy window-path import.
    app,
    data,
    detail,
    rows,
)
