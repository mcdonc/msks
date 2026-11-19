"""The images tab package (phase 4.6 of #924, #964): the TUI
image page's counterpart under the window's second tab.

The package presents its parts: the tab widget
(:mod:`.tab` — the two labeled groups, the flights and the
fills over the bridge, the import dialog, the detail, the
actions) and the panes' plain cells (:mod:`.cells` — the
column sets, the cell texts, the sort ranks, the channel URL).
The submodules import PySide6 at module scope, so this init
imports none of them at parse time: ``gui``'s own init runs at
CLI parse on every command and a base install has no Qt
(#927). The names arrive lazily through ``__getattr__`` — the
terminal package's own pattern, the function-scope imports
below the reachability edge the import-graph gate counts.
"""

from __future__ import annotations

__all__ = ["ImagesTab", "channel_url"]


def __getattr__(name: str):
    """Present the package's parts lazily: importing
    ``msks.client.gui.images`` alone loads no Qt; touching
    ``ImagesTab`` loads the tab module and its tree, the
    channel URL the cells module."""
    if name == "ImagesTab":
        # allow-deferred-import — the Qt-bearing tab loads only
        # when a window path (or a test) asks for it, never at
        # CLI parse time (#927).
        from . import tab

        return tab.ImagesTab
    if name == "channel_url":
        # allow-deferred-import — the same door, the plain
        # cells module behind it.
        from .cells import channel_url

        return channel_url
    raise AttributeError(f"module {__name__!r} has no {name!r}")
