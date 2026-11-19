"""The secrets tab package (phase 4.6 of #924, #966, #997):
the TUI secrets page's counterpart under the window's third
tab.

The package presents its parts: the tab widget
(:mod:`.tab` — the placeholder listing and the audit group,
full width, the flights and the fills over the bridge), the
mint form (:mod:`.mint` — the dialog the Mint action opens),
and the panes' plain cells (:mod:`.cells` — the column sets,
the sort-rank items, the mint form's local checks). The
sentinel pane is gone from the page (#997): the sentinel
stays the CLI's and the TUI's own surface. The submodules
import PySide6 at module scope, so this init imports none of
them at parse time: ``gui``'s own init runs at CLI parse on
every command and a base install has no Qt (#927). The names
arrive lazily through ``__getattr__`` — the images package's
own pattern.
"""

from __future__ import annotations

__all__ = ["SecretsTab"]


def __getattr__(name: str):
    """Present the package's parts lazily: importing
    ``msks.client.gui.secrets`` alone loads no Qt; touching
    ``SecretsTab`` loads the tab module and its tree."""
    if name == "SecretsTab":
        # allow-deferred-import — the Qt-bearing tab loads only
        # when a window path (or a test) asks for it, never at
        # CLI parse time (#927).
        from . import tab

        return tab.SecretsTab
    raise AttributeError(f"module {__name__!r} has no {name!r}")
