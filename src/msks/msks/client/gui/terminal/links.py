"""Hyperlinks over the grid (phase 4.5 of #924, #953).

Two sources feed the pointing hand and the link click
(Ctrl+Click, Cmd+Click on macOS): OSC 8
URIs the program inside set on cells through the grid API,
and plain-text URLs the painter's scan recorded in
``screen.link_spans`` at the last paint — soft-wrapped rows
scan as one text, so a URL spanning rows answers whole from
any of them (#1100). Opening goes to the desktop browser
handler, gated to browser schemes — an OSC 8 URI comes from
the program inside, so file:// and friends stay unopened.
"""

from __future__ import annotations

import ctypes
import re
from bisect import bisect_right

from PySide6.QtCore import QPointF, Qt, QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import QApplication

from .. import ghostty as gt
from .input import cell_at
from .select import cell_ref, ref_under

# A plain-text URL: a browser scheme or a www. prefix, then
# non-delimiter characters ending in an alphanumeric — the
# classic terminal-link shape (trailing punctuation excluded).
URL_RE = re.compile(r"(?:https?://|ftp://|www\.)[^\s<>\"']+[a-zA-Z0-9/]")


def row_wraps(screen, row: int) -> bool:
    """Whether the viewport row soft-wraps onto the next: the
    stitch signal for the URL scan. The flag lives on the grid
    row — the render-state row the painter walks carries none
    — read through the row of the row's first cell."""
    ref = cell_ref(screen, 0, row)
    grid_row = ctypes.c_uint64()
    if (
        ref is None
        or screen.lib.grid_ref_row(ctypes.byref(ref), ctypes.byref(grid_row))
        != gt.SUCCESS
    ):  # pragma: no cover — cell_ref's guard: a
        # terminal torn down mid-paint resolves no ref, and a
        # live ref always answers its row (probed, wrapped and
        # unwrapped rows alike)
        return False
    wrap = ctypes.c_bool()
    result = screen.lib.grid_row_get(
        grid_row.value, gt.GRID_ROW_WRAP, ctypes.byref(wrap)
    )
    return result == gt.SUCCESS and wrap.value


def scan(rows: list[list[str]], wraps: list[bool]):
    """The viewport's URL spans, one span list per row, from
    the rows' per-cell text and wrap flags. Soft-wrapped rows
    scan as one text, and every row a URL touches carries its
    own cells' slice with the whole URL, so a click on any
    fragment opens the full link. Two limits stay: a URL
    whose start scrolled off the top keeps no scheme in the
    joined text and matches nothing, and a match reaching the
    final cell of a line still wrapping past the bottom is
    dropped whole — it may continue below, and opening a
    truncated URL is the bug this scan exists to fix."""
    spans: list[list[tuple[int, int, str]]] = [[] for _ in rows]
    start = 0
    while start < len(rows):
        end = start
        while end + 1 < len(rows) and wraps[end]:
            end += 1
        scan_line(spans, rows, start, end, wraps[end])
        start = end + 1
    return spans


def scan_line(spans, rows, start: int, end: int, open_ended: bool) -> None:
    """One soft-wrapped logical line's URL matches, recorded
    as each touched row's slice of cells. ``open_ended``: the
    line wraps on past the viewport's last row, so a match
    that reaches the line's final cell may continue below
    it — dropped, not truncated."""
    cells: list[str] = []
    starts: list[tuple[int, int]] = []  # each row, its first cell
    for row in range(start, end + 1):
        starts.append((row, len(cells)))
        cells.extend(rows[row])
    offsets = cell_offsets(cells)
    for match in URL_RE.finditer("".join(cells)):
        first, last = match_cells(offsets, match)
        if not (open_ended and last == len(cells) - 1):
            record(spans, rows, starts, first, last, match.group(0))


def match_cells(offsets: list[int], match) -> tuple[int, int]:
    """The first and last cell a URL match covers, from its
    character span over the joined text."""
    return (
        bisect_right(offsets, match.start()) - 1,
        bisect_right(offsets, match.end() - 1) - 1,
    )


def record(spans, rows, starts, first: int, last: int, url: str) -> None:
    """The matched URL's slice on each row it touches, in that
    row's own cell columns."""
    for row, base in starts:
        lo, hi = max(first, base), min(last + 1, base + len(rows[row]))
        if lo < hi:
            spans[row].append((lo - base, hi - base, url))


def cell_offsets(cells: list[str]) -> list[int]:
    """Each cell's offset into the joined text: a cell's
    cluster can carry several characters, so a match's
    characters map back to cells through these."""
    offsets: list[int] = []
    at = 0
    for cell in cells:
        offsets.append(at)
        at += len(cell)
    return offsets


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
    if result != gt.SUCCESS:  # an unresolved ref (the test
        # seam drives a zeroed one; it answers -2, not space)
        return None
    uri = buf.raw[: written.value].decode("utf-8", errors="replace")
    return uri or None


def span_uri(screen, position: QPointF) -> str | None:
    """A plain-text URL span under the position, from the
    painter's last scan."""
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
    """The link click (Ctrl+Click, Cmd+Click on macOS): open
    the link under the position in the
    desktop browser. Answers whether a link opened."""
    url = under(screen, position)
    if not url:
        return False
    url = openable(url)
    if url is None:
        return False
    return QDesktopServices.openUrl(QUrl(url))
