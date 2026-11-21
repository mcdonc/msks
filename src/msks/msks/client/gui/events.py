"""The workspace Events tab (phase 5.9 of #924, #989): the
page's recent-frames window with the room a shared pane never
had — each row keeps the receipt stamp, the kind, and the
summary the presenter builds
(:func:`msks.client.gui.detail.event_line`), newest last, in a
scroll that holds the window's tail. The tab holds no
flights and reads no daemon state: the page's pushed-frame
path (#946) appends to it directly.

This module imports PySide6 at module scope — the screens' own
contract: nothing imports it at CLI parse time, and the
workspace page (itself lazily imported) owns the import.
"""

import time

from PySide6 import QtCore, QtWidgets

from .detail import EVENTS_LIMIT, event_line
from .forms import muted_note

#: The receipt stamp's shape (#989): the local clock's own
#: time-of-day — the row reads as a log line, the day the
#: buffer never crosses at twenty rows.
STAMP_FORMAT = "%H:%M:%S"


def stamp_text(epoch: float) -> str:
    """One receipt stamp as the row reads it (#989): the local
    clock's time-of-day at the moment the frame landed — the
    daemon's live stream carries no stamp of its own."""
    return time.strftime(STAMP_FORMAT, time.localtime(epoch))


def row_text(frame: dict, epoch: float) -> str:
    """One frame as the tab's row (#989): the stamp, the kind,
    and the summary the presenter builds — the row reads as a
    log line."""
    kind = frame.get("event") or "?"
    return f"{stamp_text(epoch)}  {kind}  {event_line(frame)}"


class EventsTab(QtWidgets.QWidget):
    """The page's recent-events window (#989): the buffered
    frames as rows, newest last, the tail held in view — each
    row using the pane's whole width, a long summary wrapping
    (#1025). The page's open clears it; every pushed frame
    appends."""

    def __init__(self) -> None:
        super().__init__()
        self.rows = QtWidgets.QListWidget(self)
        # The rows use the pane's whole width (#1025): a long
        # summary wraps to the next line instead of growing a
        # horizontal scroll. Wrappable text never shows the
        # scroll; a single token longer than the pane — a digest,
        # a URL — brings it back, so its tail stays reachable.
        self.rows.setWordWrap(True)
        self.rows.setHorizontalScrollBarPolicy(
            QtCore.Qt.ScrollBarPolicy.ScrollBarAsNeeded
        )
        self.empty = muted_note("no events yet")
        layout = QtWidgets.QVBoxLayout(self)
        layout.addWidget(self.rows)
        layout.addWidget(self.empty)
        self.sync_empty()

    def sync_empty(self) -> None:
        """The empty stand-in (#989): honest while no frame has
        landed."""
        self.rows.setVisible(bool(self.rows.count()))
        self.empty.setVisible(not self.rows.count())

    def clear(self) -> None:
        """The open's reset (#989): a fresh workspace's window
        starts empty."""
        self.rows.clear()
        self.sync_empty()

    def push(self, frame: dict) -> None:
        """One pushed frame as a row (#989): appended newest
        last, the window capped at the presenter's own limit —
        the oldest row leaves first — and the tail held in
        view."""
        self.rows.addItem(row_text(frame, time.time()))
        while self.rows.count() > EVENTS_LIMIT:
            self.rows.takeItem(0)
        self.rows.scrollToBottom()
        self.sync_empty()
