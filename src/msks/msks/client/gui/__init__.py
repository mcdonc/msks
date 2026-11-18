"""Client-side GUI screens (PySide6; the daemon never imports these).

The package presents its parts: the entry the ``msks gui``
command runs (:mod:`.app`), the screen actions over the
transport (:mod:`.data`, PySide6-free), the workspace list's
presenter (:mod:`.rows`, phase 3 of #924 — plain row models,
PySide6-free), and the workspace detail view's presenter
(:mod:`.detail`, phase 4 — plain section models,
PySide6-free), the live events feed (:mod:`.feed`, phase 4 of
#924 — plain asyncio over the shared dial seams, no Qt in it),
and the libghostty-vt ctypes binding (:mod:`.ghostty`,
phase 4.5 — #950: pure ctypes over the C ABI, no Qt in it),
the PTY session behind a terminal tab (:mod:`.termsession`,
phase 4.5 — #951: a child on a pty, pure stdlib, no Qt in it),
and the shell tabs that host the terminals inside the detail
page (:mod:`.shells`, #954: the session hub and the tab strip),
and the terminal widget package (:mod:`.terminal`, phase 4.5
— #952 and #953: the cell-grid screen over the render state,
with grid-anchored selection, the clipboard paths, OSC 8 and
plain-text hyperlinks, and find; its own init imports no Qt,
so it parses safe here while its widgets wait for #954's tab
wiring to reach them), and the images tab's presenter
(:mod:`.imagerows`, #963 — the catalog and published groups
as plain row models, PySide6-free).
The screen widgets
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
    # the bridge and the screen modules reach through app.py's
    # lazy window-path import; the feed rides the same path.
    app,
    data,
    detail,
    feed,
    ghostty,
    imagerows,
    rows,
    terminal,
    termsession,
)
