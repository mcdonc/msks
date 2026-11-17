"""Find through the library's search API (phase 4.5 of
#924, #953).

The overlay bar (a line edit and a match counter, laid out
top-right) drives a library search handle: a changed needle
restarts the search live, Return and Shift+Return step
through matches, and the counter reads the total and the
selected index straight from the search state. Each paint
while the bar is open feeds the search and asks for the
viewport's match rectangles — the current match highlighted
above the rest.
"""

from __future__ import annotations

import ctypes

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QLabel, QLineEdit

from .. import ghostty as gt

# The overlay bar's geometry: the edit's width and the bar's
# height and insets.
EDIT_WIDTH = 220
BAR_HEIGHT = 28
BAR_INSET = 8


def open(screen) -> None:
    """Ctrl+Shift+F: create the search handle and the overlay
    widgets, or refocus the bar when it is already open."""
    if screen.search:
        screen.find_edit.setFocus()
        screen.find_edit.selectAll()
        return
    screen.search = screen.lib.new_handle(
        screen.lib.search_new, screen.terminal
    )
    screen.find_edit = QLineEdit(screen)
    screen.find_edit.setPlaceholderText("Find")
    screen.find_edit.setFixedWidth(EDIT_WIDTH)
    screen.find_edit.textChanged.connect(lambda text: run(screen, text))
    screen.find_edit.installEventFilter(screen)
    screen.find_count = QLabel(screen)
    layout(screen)
    screen.find_edit.show()
    screen.find_count.show()
    screen.find_edit.raise_()
    screen.find_count.raise_()
    screen.find_edit.setFocus()


def close(screen) -> None:
    """Escape: free the search handle, drop the overlay
    widgets, and hand focus back to the grid."""
    if not screen.search:
        return
    screen.lib.search_free(screen.search)
    screen.search = 0
    if screen.find_edit is not None:  # pragma: no branch
        # The bar's widgets exist together (open creates both)
        # and die together, and a closed bar never reaches this
        # far (the search handle went first) — the guards keep
        # the teardown honest, not reachable.
        screen.find_edit.deleteLater()
        screen.find_edit = None
    if screen.find_count is not None:  # pragma: no branch
        screen.find_count.deleteLater()
        screen.find_count = None
    screen.setFocus()
    screen.refresh()


def layout(screen) -> None:
    """Place the bar: the edit with its counter to the right,
    both at the top-right inset."""
    if screen.find_edit is None or screen.find_count is None:
        return
    screen.find_count.adjustSize()
    count_w = screen.find_count.width() + 10
    screen.find_edit.move(
        screen.width() - EDIT_WIDTH - count_w - 16, BAR_INSET
    )
    screen.find_edit.setFixedHeight(BAR_HEIGHT)
    screen.find_count.move(
        screen.width() - count_w - BAR_INSET,
        BAR_INSET + (BAR_HEIGHT - 18) // 2,
    )


def run(screen, text: str) -> None:
    """A changed needle restarts the search; an empty one
    keeps the state clear."""
    if not screen.search:
        return
    if text:
        data = text.encode("utf-8")
        needle = gt.GhosttyString()
        needle.ptr = ctypes.cast(
            ctypes.c_char_p(data), ctypes.POINTER(ctypes.c_uint8)
        )
        needle.len = len(data)
        screen.lib.search_set(
            screen.search, gt.SEARCH_OPT_NEEDLE, ctypes.byref(needle)
        )
        screen.lib.search_run(screen.search)
    else:
        # An emptied needle resets: the library keeps the last
        # run's matches otherwise, so the handle is rebuilt
        # fresh.
        screen.lib.search_free(screen.search)
        screen.search = screen.lib.new_handle(
            screen.lib.search_new, screen.terminal
        )
    update_count(screen)
    screen.refresh()


def step(screen, direction: int) -> None:
    """Return steps to the next match; Shift+Return to the
    previous one."""
    if not screen.search:
        return
    option = (
        gt.SEARCH_OPT_SELECT_NEXT
        if direction > 0
        else gt.SEARCH_OPT_SELECT_PREV
    )
    screen.lib.search_set(screen.search, option, None)
    update_count(screen)
    screen.refresh()


def filter_key(screen, event) -> bool:
    """The bar's own keys: Return steps, Shift+Return steps
    back, Escape closes. Answers whether the key was
    consumed."""
    if event.type() != event.Type.KeyPress:
        return False
    key = event.key()
    if key in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
        if event.modifiers() & Qt.KeyboardModifier.ShiftModifier:
            step(screen, -1)
        else:
            step(screen, 1)
        return True
    if key == Qt.Key.Key_Escape:
        close(screen)
        return True
    return False


def update_count(screen) -> None:
    """The counter label: ``n of total`` for the selected
    match, the bare total otherwise, empty for no matches."""
    if screen.find_count is None:  # pragma: no cover — the
        # bar's widgets live exactly as long as its search
        # handle; close frees the handle first.
        return
    total = ctypes.c_size_t()
    ok, _ = screen.lib.get(
        screen.lib.search_get,
        screen.search,
        gt.SEARCH_DATA_TOTAL_MATCHES,
        total,
    )
    index = ctypes.c_size_t()
    ok2, _ = screen.lib.get(
        screen.lib.search_get,
        screen.search,
        gt.SEARCH_DATA_SELECTED_INDEX,
        index,
    )
    if not ok or total.value == 0:
        screen.find_count.setText("")
    elif ok2:
        screen.find_count.setText(f"{index.value + 1} of {total.value}")
    else:
        screen.find_count.setText(f"{total.value} matches")
    layout(screen)


def viewport_rects(screen) -> list[tuple[int, int, int, bool]]:
    """The viewport's search-match rectangles for painting:
    (row, x_px, w_px, is_selected), clipped to the viewport's
    rows and spanning full rows for a match that crosses
    them. The feed answers once per search run — the first
    paint after the run carries the underlays, and the next
    feed before another run answers none."""

    if not screen.search:
        return []
    lib = screen.lib
    lib.search_feed(screen.search)
    storage = (gt.Selection * 128)()
    buffer = gt.SelectionBuffer(ptr=storage, cap=128, len=0)
    ok, _ = lib.get(
        lib.search_get,
        screen.search,
        gt.SEARCH_DATA_VIEWPORT_MATCHES,
        buffer,
    )
    if not ok:  # pragma: no cover — the viewport read on a
        # live search always answers (probed); failure needs a
        # freed handle, which run/step guard against.
        return []
    rects: list[tuple[int, int, int, bool]] = []
    for index in range(buffer.len):
        add_match(
            screen, storage[index].start, storage[index].end, False, rects
        )
    selected = read_selected(screen)
    if selected is not None:
        add_match(screen, selected.start, selected.end, True, rects)
    return rects


def read_selected(screen):
    """The search's selected match as a selection, when the
    search holds one."""
    selected = gt.Selection()
    selected.size = ctypes.sizeof(selected)
    ok, _ = screen.lib.get(
        screen.lib.search_get,
        screen.search,
        gt.SEARCH_DATA_SELECTED_MATCH,
        selected,
    )
    return selected if ok and selected.size else None


def viewport_point(screen, ref):
    """A grid ref's coordinates in the viewport, when the
    library resolves them."""
    point = gt.PointCoordinate()
    result = screen.lib.terminal_point_from_grid_ref(
        screen.terminal,
        ctypes.byref(ref),
        gt.POINT_VIEWPORT,
        ctypes.byref(point),
    )
    return point if result == gt.SUCCESS else None


def add_match(screen, start_ref, end_ref, is_selected: bool, rects) -> None:
    """One match's rectangles: the row span between its refs,
    a full-row rect per row it crosses, clipped to the
    viewport."""
    start = viewport_point(screen, start_ref)
    end = viewport_point(screen, end_ref)
    for row, col0, col1 in match_cells(screen, start, end):
        rects.append(
            (
                row,
                col0 * screen.metrics.cell_width,
                (col1 - col0 + 1) * screen.metrics.cell_width,
                is_selected,
            )
        )


def match_cells(screen, start, end):
    """A match's cells as (row, first column, last column)
    triples: the row span between its viewport points, full
    rows between the end columns, clipped to the viewport."""
    rows = clipped_rows(screen, start, end)
    return [
        (row, row_span_start(start, row), row_span_end(screen, end, row))
        for row in rows
    ]


def clipped_rows(screen, start, end):
    """The viewport rows a match spans: its start row through
    its end row, clipped to the viewport; empty when either
    end resolves to no point (point_from_grid_ref answers
    SUCCESS for history rows too, probed — off-viewport
    coordinates come back and the clip handles them) or the
    match sits fully off-viewport (the buffer carries
    viewport matches only, probed; that needs a scroll
    between the feed and this read)."""
    if start is None or end is None:  # pragma: no cover
        return []
    if start.y >= screen.rows or end.y < 0:  # pragma: no cover
        return []
    return range(max(start.y, 0), min(end.y, screen.rows - 1) + 1)


def row_span_start(start, row: int) -> int:
    """The match's first column on one row: its start column
    on the row it starts, column zero on the rows below."""
    return start.x if row == start.y else 0


def row_span_end(screen, end, row: int) -> int:
    """The match's last column on one row: its end column on
    the row it ends, the last column on the rows above."""
    return end.x if row == end.y else screen.cols - 1
