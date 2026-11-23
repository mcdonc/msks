"""The cell painter (phase 4.5 of #924, #952).

Every paint walks the render state's row iterator and draws
each damaged cell from the library's own answers: the RGB
colors, the style flags, the cursor rectangle, the scrollbar
thumb, the exit banner, and the bell flash. The widget keeps
no palette of its own — what the render state says is what
paints.

The cells paint into an offscreen pixmap — the cell cache
(#1104) — and every paint blits the cache whole over the
widget. A typed character repaints its row alone: the damage
set is the library's per-row dirty flags, consumed as each
row paints. The blit erases the last frame's overlays, so the
cursor, the scrollbar, the exit banner, and the bell flash
draw fresh above it every paint and never age in the cache.
Four things repaint every row: a cache built for different
pixels or cell metrics, a changed palette (a scheme flip's
terminal_set moves the render state's colors; the base fill
must take them), a viewport that moved (the library flags no
rows for a viewport scroll, probed), and a find pass whose
match rectangles moved without the rows changing underneath
them.
"""

from __future__ import annotations

import ctypes

from PySide6.QtCore import QPointF, QRect, Qt
from PySide6.QtGui import QColor, QPainter, QPixmap

from .. import ghostty as gt
from . import find
from .fonts import font_key
from .links import URL_RE

# The per-cell scratch the painter reads graphemes into — a
# cell's cluster never exceeds the library's 16-codepoint
# buffer.
CLUSTER_CAP = 16


def rgb(color: gt.ColorRgb) -> QColor:
    """The library's RGB triple as a Qt color."""
    return QColor(color.r, color.g, color.b)


def palette(screen):
    """The render state's palette when it has one: a render
    state never updated answers none."""
    colors = gt.RenderStateColors()
    colors.size = ctypes.sizeof(colors)
    ok = screen.lib.get(
        screen.lib.render_state_get,
        screen.render_state,
        gt.RS_COLORS,
        colors,
    )[0]
    return colors if ok else None


def paint_event(screen, painter: QPainter) -> None:
    """One paint of the grid: the damaged rows into the cell
    cache, the cache blitted whole over the widget, and the
    overlays drawn fresh above the blit."""
    colors = palette(screen)
    if colors is None:
        return
    rects_by_row = underlay_map(screen)
    cache = sync_cache(screen, colors, rects_by_row)
    try:
        paint_cells(screen, cache, colors, rects_by_row)
    finally:
        cache.end()
    painter.drawPixmap(0, 0, screen.cell_cache)
    paint_cursor(screen, painter, colors)
    paint_scrollbar(screen, painter)
    paint_exit_banner(screen, painter)
    if screen.bell_flash:
        paint_bell_flash(screen, painter)


def sync_cache(screen, colors, rects_by_row) -> QPainter:
    """The painter over the cell cache, after deciding how
    much of it repaints: a rebuilt cache (other pixels or
    cell metrics, or a moved palette), a viewport that moved,
    or a find pass whose rectangles moved each repaint every
    row; otherwise the damage stays the library's per-row
    flags alone."""
    full = rebuild_cache(screen, colors)
    offset = scrollbar_offset(screen)
    if screen.cache_scroll != offset:
        full = True
        screen.cache_scroll = offset
    if screen.cache_underlays != rects_by_row:
        full = True
        screen.cache_underlays = rects_by_row
    screen.cache_full = full
    screen.painted_rows = []
    return QPainter(screen.cell_cache)


def rebuild_cache(screen, colors) -> bool:
    """Whether the cache rebuilds before this paint, leaving
    a fresh pixmap behind: one built for other pixels or cell
    metrics (a zoom on a maximized window, a hidden tab's
    font change), or one whose base fill carries a stale
    background — a scheme flip's terminal_set moves the
    render state's palette. The fill is the base the cells
    without their own background show through, and the fringe
    below and right of the grid."""
    key = (
        screen.width(),
        screen.height(),
        screen.metrics.cell_width,
        screen.metrics.cell_height,
        screen.cols,
        screen.rows,
    )
    rebuild = screen.cell_cache is None or screen.cache_key != key
    scheme = bytes(colors)
    if screen.cache_palette != scheme:
        screen.cache_palette = scheme
        rebuild = True  # the base fill carries the new background
    if rebuild:
        screen.cell_cache = QPixmap(max(1, key[0]), max(1, key[1]))
        screen.cell_cache.fill(rgb(colors.background))
        screen.cache_key = key
    return rebuild


def scrollbar_offset(screen):
    """The viewport's offset in the scrollback: the number a
    paint compares against the last paint's number to catch a
    viewport scroll, which the library's row flags leave
    clean."""
    ok, bar = screen.lib.get(
        screen.lib.terminal_get,
        screen.terminal,
        gt.DATA_SCROLLBAR,
        gt.Scrollbar(),
    )
    return bar.offset if ok else None


def paint_cells(screen, painter: QPainter, colors, rects_by_row) -> None:
    """Walk the rows the render state holds and paint the
    damaged ones into the cache, consuming each painted row's
    dirty flag. A row outside the damage set keeps both its
    pixels and its URL spans in the cache. The walk also
    records each painted row's plain text into the URL scan,
    and paints the find bar's match underlays beneath the
    cells."""
    ok, iterator = screen.lib.get(
        screen.lib.render_state_get,
        screen.render_state,
        gt.RS_ROW_ITERATOR,
        ctypes.c_void_p(screen.row_iterator),
    )
    # A state with a palette always holds its rows (probed: a
    # never-updated state answers no colors; an updated one
    # answers both). # pragma: no branch — the no-iterator arm
    # has no state that reaches it.
    if ok and iterator.value:  # pragma: no branch
        style = gt.Style()
        style.size = ctypes.sizeof(style)
        codepoints = (ctypes.c_uint32 * CLUSTER_CAP)()
        screen.link_spans = walk_rows(
            screen, painter, colors, style, codepoints, iterator, rects_by_row
        )


def walk_rows(
    screen, painter, colors, style, codepoints, iterator, rects_by_row
):
    """The viewport walk: a row paints when it carries damage
    or the pass takes every row, and the answer is the trimmed
    per-row URL span list — a row outside the damage set keeps
    the spans its cached pixels still show."""
    spans = screen.link_spans
    count = 0
    y = 0
    while screen.lib.row_iterator_next(iterator.value):
        _, cells = screen.lib.get(
            screen.lib.row_get,
            iterator.value,
            gt.ROW_CELLS,
            ctypes.c_void_p(screen.row_cells),
        )
        if screen.cache_full or row_dirty(screen, iterator.value):
            paint_damaged_row(
                screen,
                painter,
                colors,
                style,
                codepoints,
                iterator.value,
                cells,
                spans,
                rects_by_row,
                count,
                y,
            )
        count += 1
        y += screen.metrics.cell_height
    return spans[:count]


def paint_damaged_row(
    screen,
    painter,
    colors,
    style,
    codepoints,
    iterator: int,
    cells,
    spans,
    rects_by_row,
    count: int,
    y: int,
) -> None:
    """One damaged row's contribution to the cache: the find
    underlays beneath it, its cells, its URL span entry, the
    consumption of its dirty flag, and its place in the paint
    record the damage pins read."""
    paint_underlays(screen, painter, rects_by_row.get(count, []), y)
    while len(spans) <= count:
        spans.append([])
    spans[count] = paint_row(
        screen, painter, colors, style, codepoints, cells, y
    )
    clean = ctypes.c_bool(False)
    screen.lib.row_set(iterator, gt.ROW_DIRTY, ctypes.byref(clean))
    screen.painted_rows.append(count)


def row_dirty(screen, iterator: int) -> bool:
    """Whether the library flags one viewport row as damaged,
    read without consuming it."""
    ok, dirty = screen.lib.get(
        screen.lib.row_get, iterator, gt.ROW_DIRTY, ctypes.c_bool()
    )
    return bool(ok and dirty.value)


def underlay_map(screen) -> dict[int, list[tuple[int, int, bool]]]:
    """The find bar's viewport rectangles grouped by row, for
    painting beneath the cells and for the cache's comparison
    against the last paint's map."""
    rects_by_row: dict[int, list[tuple[int, int, bool]]] = {}
    for row, x, width, is_selected in find.viewport_rects(screen):
        rects_by_row.setdefault(row, []).append((x, width, is_selected))
    return rects_by_row


def paint_underlays(screen, painter: QPainter, rects, y: int) -> None:
    """The find bar's match underlays beneath one row: the
    selected match in orange above the rest in blue."""
    for rx, rw, is_selected in rects:
        painter.fillRect(
            rx,
            y,
            rw,
            screen.metrics.cell_height,
            QColor(255, 170, 0, 130)
            if is_selected
            else QColor(120, 170, 255, 70),
        )


def paint_row(screen, painter: QPainter, colors, style, codepoints, cells, y):
    """One row's cells left to right; answers the row's URL
    spans from the text the cells carried."""
    x = 0
    row_chars: list[str] = []
    while screen.lib.row_cells_next(cells.value):
        paint_cell(screen, painter, colors, style, codepoints, x, y, row_chars)
        x += screen.metrics.cell_width
    return [
        (m.start(), m.end(), m.group(0))
        for m in URL_RE.finditer("".join(row_chars))
    ]


def cell_colors(screen, colors, cells):
    """A cell's foreground and background as Qt colors, with
    whether the cell carries a paint of its own — a set
    background, the inverse style, or a library-flagged
    selection each make one."""
    lib = screen.lib
    ok_fg, fg = lib.get(
        lib.row_cells_get, cells, gt.CELLS_FG_COLOR, gt.ColorRgb()
    )
    ok_bg, bg = lib.get(
        lib.row_cells_get, cells, gt.CELLS_BG_COLOR, gt.ColorRgb()
    )
    foreground = rgb(fg if ok_fg else colors.foreground)
    background = rgb(bg if ok_bg else colors.background)
    selected = cell_selected(lib, cells)
    if selected:
        foreground, background = background, foreground
    return foreground, background, ok_bg or selected


def cell_selected(lib, cells) -> bool:
    """Whether the library flags the cell as inside the
    active selection."""
    ok, selected = lib.get(
        lib.row_cells_get, cells, gt.CELLS_SELECTED, ctypes.c_bool()
    )
    return bool(ok and selected.value)


def paint_cell(
    screen,
    painter: QPainter,
    colors,
    style,
    codepoints,
    x: int,
    y: int,
    row_chars: list[str],
) -> None:
    """One cell: its background fill, then the cluster's
    glyphs baseline-anchored at the cell's left edge in the
    style's variant of the one face. The inverse style and
    the selection each swap the cell's foreground and
    background."""
    lib = screen.lib
    cells = screen.row_cells
    ok, length = lib.get(
        lib.row_cells_get, cells, gt.CELLS_GRAPHEMES_LEN, ctypes.c_uint32()
    )
    foreground, background, has_bg = cell_colors(screen, colors, cells)
    if not ok or length.value == 0:
        fill_cell(screen, painter, x, y, background, has_bg)
        row_chars.append(" ")
        return
    cell_style = read_style(lib, cells, style)
    if cell_style.inverse:
        foreground, background = background, foreground
        has_bg = True
    fill_cell(screen, painter, x, y, background, has_bg)
    row_chars.append(cluster_text(lib, cells, codepoints) or " ")
    draw_cluster(screen, painter, foreground, cell_style, codepoints, x, y)


def fill_cell(
    screen, painter: QPainter, x: int, y: int, color, needed: bool
) -> None:
    """One cell rect of background, when the cell needs one."""
    if not needed:
        return
    painter.fillRect(
        x, y, screen.metrics.cell_width, screen.metrics.cell_height, color
    )


def read_style(lib, cells, style):
    """The cell's style flags, read into the reused struct."""
    style.size = ctypes.sizeof(style)
    lib.row_cells_get(cells, gt.CELLS_STYLE, ctypes.byref(style))
    return style


def draw_cluster(
    screen, painter: QPainter, foreground, style, codepoints, x: int, y: int
) -> None:
    """The cluster's glyphs, in the style's variant of the one
    face and its faintness."""
    lib = screen.lib
    cells = screen.row_cells
    text = cluster_text(lib, cells, codepoints)
    if style.faint:
        foreground.setAlpha(120)
    painter.setFont(screen.fonts[font_key(style.bold, style.italic)])
    painter.setPen(foreground)
    painter.drawText(QPointF(x, y + screen.metrics.ascent), text)


def cluster_text(lib, cells, codepoints) -> str:
    """The cell's cluster as text: the graphemes the buffer
    carries, or the empty string for a cluster longer than
    the 16-codepoint buffer (a combining-mark run — it draws
    blank, the buffer cannot carry it)."""
    ok, length = lib.get(
        lib.row_cells_get, cells, gt.CELLS_GRAPHEMES_LEN, ctypes.c_uint32()
    )
    if not ok or length.value == 0 or length.value > CLUSTER_CAP:
        return ""
    lib.row_cells_get(
        cells,
        gt.CELLS_GRAPHEMES_BUF,
        ctypes.cast(codepoints, ctypes.c_void_p),
    )
    return "".join(chr(codepoints[index]) for index in range(length.value))


def cursor_rect(screen, colors, cell: tuple[int, int]):
    """The cursor's pixel rectangle, when the render state
    shows it in the viewport."""
    lib = screen.lib
    ok, visible = lib.get(
        lib.render_state_get,
        screen.render_state,
        gt.RS_CURSOR_VISIBLE,
        ctypes.c_bool(),
    )
    ok2, present = lib.get(
        lib.render_state_get,
        screen.render_state,
        gt.RS_CURSOR_VIEWPORT_HAS_VALUE,
        ctypes.c_bool(),
    )
    if not (ok and ok2 and visible.value and present.value):
        return None
    _, cx = lib.get(
        lib.render_state_get,
        screen.render_state,
        gt.RS_CURSOR_VIEWPORT_X,
        ctypes.c_uint16(),
    )
    _, cy = lib.get(
        lib.render_state_get,
        screen.render_state,
        gt.RS_CURSOR_VIEWPORT_Y,
        ctypes.c_uint16(),
    )
    width, height = cell
    return QRect(cx.value * width, cy.value * height, width, height)


def paint_cursor(screen, painter: QPainter, colors) -> None:
    """The cursor cell, translucent over whatever sits beneath
    it."""
    rect = cursor_rect(
        screen,
        colors,
        (screen.metrics.cell_width, screen.metrics.cell_height),
    )
    if rect is None:
        return
    color = rgb(
        colors.cursor if colors.cursor_has_value else colors.foreground
    )
    color.setAlpha(140)
    painter.fillRect(rect, color)


def paint_scrollbar(screen, painter: QPainter) -> None:
    """The thumb over the scrollback: where the viewport sits
    in the buffer, drawn only when there is history."""
    ok, bar = screen.lib.get(
        screen.lib.terminal_get,
        screen.terminal,
        gt.DATA_SCROLLBAR,
        gt.Scrollbar(),
    )
    if not ok or bar.total <= bar.len:
        return
    track = max(1, screen.height() - 20)
    thumb = max(10, int(track * bar.len / bar.total))
    offset = int(track * bar.offset / max(1, bar.total - bar.len))
    painter.fillRect(
        screen.width() - 8, 10 + offset, 6, thumb, QColor(160, 160, 160, 130)
    )


def paint_exit_banner(screen, painter: QPainter) -> None:
    """The banner a dead child leaves: the exit status across
    the bottom, over the frozen grid."""
    if not screen.session.exited:
        return
    status = screen.session.exit_status
    message = (
        f"[process exited with status {status}]"
        if status is not None
        else "[process exited]"
    )
    height = screen.metrics.cell_height + 8
    rect = QRect(0, screen.height() - height, screen.width(), height)
    painter.fillRect(rect, QColor(0, 0, 0, 180))
    painter.setPen(QColor(255, 255, 255))
    painter.setFont(screen.fonts["normal"])
    painter.drawText(rect, Qt.AlignmentFlag.AlignCenter, message)


def paint_bell_flash(screen, painter: QPainter) -> None:
    """The visual bell: a triple orange border for the flash's
    180 ms."""
    pen = painter.pen()
    painter.setPen(QColor(255, 170, 0, 200))
    for inset in range(0, 3):
        painter.drawRect(
            inset,
            inset,
            screen.width() - 2 * inset - 1,
            screen.height() - 2 * inset - 1,
        )
    painter.setPen(pen)
