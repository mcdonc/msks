"""The terminal widget package: the cell-grid screen over
libghostty-vt (phase 4.5 of #924, #952 and #953).

The package presents its parts: the widget itself
(:mod:`.screen` — a ``QWidget`` owning the terminal handle, the
render state, and the PTY session), the cell painter
(:mod:`.paint` — per-cell RGB colors, styles, the cursor, and
the find underlays), the input encoders (:mod:`.input` — keys,
mouse, wheel, and focus through the library's encoders), the
font rules (:mod:`.fonts` — Konsole-style cell metrics from
one measured face), the library's effects callbacks
(:mod:`.callbacks` — including the OSC 52 clipboard write),
the selection and clipboard (:mod:`.select`, #953 — the
gesture protocol, word and line selects, PRIMARY publish,
and bracketed paste), the hyperlinks (:mod:`.links`, #953 —
OSC 8 and plain-text URL spans, the pointing hand, and
scheme-gated opening), and find (:mod:`.find`, #953 — the
overlay bar over the library's search API).

The submodules import PySide6 at module scope, so this init
imports none of them at parse time: ``gui``'s own init runs at
CLI parse on every command and a base install has no Qt
(#927). The names arrive lazily through ``__getattr__`` — the
function-scope import below is the reachability edge the
import-graph gate counts, the same standing ``gui.app``'s
lazy window-path imports give the list and detail screens —
and #954's tab wiring becomes the widget's real consumer.
"""

from __future__ import annotations

__all__ = ["TerminalWidget"]


def __getattr__(name: str):
    """Present the package's parts lazily: importing
    ``msks.client.gui.terminal`` alone loads no Qt; touching
    ``TerminalWidget`` loads the screen module and its tree."""
    if name == "TerminalWidget":
        # allow-deferred-import — the Qt-bearing screen loads
        # only when a window path (or a test) asks for the
        # widget, never at CLI parse time (#927).
        from . import screen

        return screen.TerminalWidget
    raise AttributeError(f"module {__name__!r} has no {name!r}")
