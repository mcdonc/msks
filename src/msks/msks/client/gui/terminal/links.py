"""Hyperlinks over the grid (phase 4.5 of #924, #953).

Two sources feed the pointing hand and Ctrl+Click: OSC 8
URIs the program inside set on cells through the grid API,
and plain-text URLs the painter's per-row scan recorded in
``screen.link_spans`` at the last paint. Opening goes to the
desktop browser handler, gated to browser schemes — an OSC 8
URI comes from the program inside, so file:// and friends
stay unopened.
"""

from __future__ import annotations

import ctypes
import re

from PySide6.QtCore import QPointF, Qt, QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import QApplication

from .. import ghostty as gt
from .input import cell_at
from .select import ref_under

# A plain-text URL: a browser scheme or a www. prefix, then
# non-delimiter characters ending in an alphanumeric — the
# classic terminal-link shape (trailing punctuation excluded).
URL_RE = re.compile(r"(?:https?://|ftp://|www\.)[^\s<>\"']+[a-zA-Z0-9/]")


def osc8_uri(screen, ref) -> str | None:
    """The OSC 8 URI a grid ref's cell carries, when it has
    one. One 4 KiB read: the library keeps URIs only up to
    about 2 KiB (probed: 2014 bytes answers written, 2061
    answers written 0) and silently drops longer ones — no
    real query can outgrow the buffer."""
    buf = ctypes.create_string_buffer(4096)
    written = ctypes.c_size_t()
    result = screen.lib.grid_ref_hyperlink_uri(
        ctypes.byref(ref), buf, 4096, ctypes.byref(written)
    )
    if result != gt.SUCCESS:  # pragma: no cover — probed -2 on
        # a zeroed ref happens only through the test seam; a
        # resolved ref always answers SUCCESS.
        return None
    uri = buf.raw[: written.value].decode("utf-8", errors="replace")
    return uri or None


def span_uri(screen, position: QPointF) -> str | None:
    """A plain-text URL span under the position, from the
    painter's last per-row scan."""
    col, row = cell_at(screen, position)
    if row < len(screen.link_spans):
        for start, end, url in screen.link_spans[row]:
            if start <= col < end:
                return url
    return None


def under(screen, position: QPointF) -> str | None:
    """The link under the cursor: OSC 8 from the grid first,
    else a plain-text URL span."""
    ref = ref_under(screen, position)
    if ref is not None:  # pragma: no branch — ref_under
        # resolves every live position (its own guard holds
        # for a torn-down terminal).
        uri = osc8_uri(screen, ref)
        if uri:
            return uri
    return span_uri(screen, position)


def set_hand(screen, on: bool) -> None:
    """The pointing hand over a link, as an override cursor
    so the arrow returns when the pointer leaves it."""
    if on == screen.hand_cursor:
        return
    screen.hand_cursor = on
    if on:
        QApplication.setOverrideCursor(Qt.CursorShape.PointingHandCursor)
    else:
        QApplication.restoreOverrideCursor()


def openable(url: str) -> str | None:
    """The URL as one the desktop handler may open: browser
    schemes only, with a bare www. host promoted to https."""
    if url.startswith("www."):
        url = "https://" + url
    if url.lower().startswith(("http://", "https://", "ftp://")):
        return url
    return None


def open_at(screen, position: QPointF) -> bool:
    """Ctrl+Click: open the link under the position in the
    desktop browser. Answers whether a link opened."""
    url = under(screen, position)
    if not url:
        return False
    url = openable(url)
    if url is None:
        return False
    return QDesktopServices.openUrl(QUrl(url))
