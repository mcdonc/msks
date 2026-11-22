"""The strip pages' shared feed wiring (#1060): the serve poke
and the drop flash a frame-driven page takes beside its own
frame filter — the pushed frames are a page's freshness, so the
page that owns the window re-reads when the feed serves and
flashes when it drops. The two slots are the whole contract: a
page mixes this in beside its QWidget base and connects the
bridge's ``feed_served`` and ``feed_dropped`` signals to them.
The workspaces shell keeps its own wiring in
:mod:`msks.client.gui.workspace`: its screens re-read per frame
family, and #946 wrote their drop and recovery tells.

This module imports PySide6 at module scope — the screens' own
contract: nothing imports it at CLI parse time, and the screens'
modules load it on the window path.
"""

from PySide6 import QtCore


class FeedPage:
    """A strip page's feed-driven reads (#1019, #1060): the page
    re-reads whole when the feed serves and flashes when it
    drops, both only while it owns the window — a hidden page
    re-reads at its next show, whatever moved while it stood
    unseen. The drop's recovery carries no flash of its own: the
    serve poke fires on every connection's first frame,
    reconnects included, and the re-read's own status line tells
    the story. The host page supplies ``refresh``, ``flash``,
    and the QWidget it mixes beside."""

    @QtCore.Slot()
    def feed_served(self) -> None:
        """The feed's serve poke (#1019): frames published before
        the connection stood never landed, so the page that owns
        the window re-reads whole — one gathered flight, the
        startup race and every reconnect's gap covered alike."""
        if self.isVisible():
            self.refresh()

    @QtCore.Slot(str)
    def feed_dropped(self, reason: str) -> None:
        """The events feed's drop (#946): the page that owns the
        window flashes it — the pushed frames are the page's
        freshness, and the outage's heartbeat is the operator's
        tell that updates stopped."""
        if self.isVisible():
            self.flash(f"the events feed dropped — {reason}")
