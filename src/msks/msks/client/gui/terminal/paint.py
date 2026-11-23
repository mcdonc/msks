"""The cell painter (phase 4.5 of #924, #952).

Every paint walks the render state's row iterator and draws
each damaged cell from the library's own answers: the RGB
colors, the style flags, the cursor rectangle, the scrollbar
thumb, the exit banner, and the bell flash. The widget keeps
no palette of its own — what the render state says is what
paints.

The cells paint straight onto the widget (#1113): one paint
takes the rows the plan named — the library's per-row dirty
flags, the rows the overlays must repaint, and any row the
update region exposes — and a taken row paints its background
first, so no pixel of an earlier frame can survive beside the
new one. No offscreen pixmap outlives a paint: the device
pixel ratio and the backing store are Qt's own mapping, so a
window on a scaled display, on KDE Wayland's split fractional
scaling, or moved between displays that scale differently
paints the same logical rows onto whatever device it meets —
the stale-band and ghosted-glyph classes the cell cache
chased (#1104, #1110, #1113) cannot arise.
"""

from __future__ import annotations

import ctypes

from PySide6.QtCore import QPointF, QRect, Qt
from PySide6.QtGui import QColor, QPainter, QRegion

from .. import ghostty as gt
from . import find, links
from .fonts import font_key

# The scrollbar's lane, reserved right of the grid: the thumb
# never covers a cell, and erasing it never erases a glyph.
LANE_W = 8

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


def plan_paint(screen) -> QRegion:
    """The update region for the paint the refreshed render
    state owes: every row when a full pass is due — the
    palette, the scroll offset, the find underlays, the pixel
    or cell geometry changed — otherwise the rows the library
    flags dirty plus the rows the overlays must repaint. The
    verdict and the rows land on the screen for the paint to
    take, so ``paint_event`` and the region it is handed agree
    on what repaints. Called after every emulator sync, and it
    consumes the flags it reads, so the paint never walks
    them a second time."""
    colors = palette(screen)
    if colors is None:
        return unplanned(screen)
    full = full_due(screen, colors, underlay_map(screen))
    rows = consume_damage(screen, colors, full)
    return planned_region(screen, full, rows)


def unplanned(screen) -> QRegion:
    """The plan a render state without a palette owes:
    nothing. A paint of it draws nothing either (probed)."""
    screen.repaint_full = False
    screen.repaint_rows = set()
    screen.repaint_region = QRegion()
    return screen.repaint_region


def planned_region(screen, full: bool, rows: set[int]) -> QRegion:
    """The plan's verdict and rows moved onto the screen, and
    the update region they name: the whole widget for a full
    pass, a strip per row otherwise — united with whatever an
    earlier plan of the same coalesced paint already asked
    for."""
    screen.repaint_full = full or screen.repaint_full
    if screen.repaint_full:
        screen.repaint_rows = set()
        screen.repaint_region = QRegion(
            QRect(0, 0, screen.width(), screen.height())
        )
    else:
        screen.repaint_rows = screen.repaint_rows | rows
        screen.repaint_region = rows_region(screen, rows).united(
            screen.repaint_region
        )
    return screen.repaint_region


def consume_damage(screen, colors, full: bool) -> set[int]:
    """The damage the plan takes from the library, consumed
    once per cycle (#935's iterator lessons keep the walk to
    one): a full pass supersedes every flag, a partial plan
    takes the rows the flags name plus the rows the overlays
    must repaint. A flag clears only through its row's cells —
    row_set without the row's ROW_CELLS read first leaves the
    flag set (probed) — so every clear loads its row."""
    rows: set[int] = set()
    ok, iterator = screen.lib.get(
        screen.lib.render_state_get,
        screen.render_state,
        gt.RS_ROW_ITERATOR,
        ctypes.c_void_p(screen.row_iterator),
    )
    if not (ok and iterator.value):  # no state reaches this arm
        return rows  # pragma: no cover — probed: a state with a
        # palette always holds its rows
    if full:
        clear_all_rows(screen, iterator)
        return rows
    rows = take_dirty_rows(screen, iterator)
    return rows | overlay_rows(
        screen,
        cursor_rect(
            screen,
            colors,
            (screen.metrics.cell_width, screen.metrics.cell_height),
        ),
    )


def clear_all_rows(screen, iterator) -> None:
    """Every viewport row's dirty flag cleared, each through
    its row's cells."""
    clean = ctypes.c_bool(False)
    while screen.lib.row_iterator_next(iterator.value):
        _, cells = screen.lib.get(
            screen.lib.row_get,
            iterator.value,
            gt.ROW_CELLS,
            ctypes.c_void_p(screen.row_cells),
        )
        screen.lib.row_set(iterator.value, gt.ROW_DIRTY, ctypes.byref(clean))


def take_dirty_rows(screen, iterator) -> set[int]:
    """The rows the library flags dirty, each flag consumed
    through its row's cells."""
    rows: set[int] = set()
    clean = ctypes.c_bool(False)
    count = 0
    while screen.lib.row_iterator_next(iterator.value):
        _, cells = screen.lib.get(
            screen.lib.row_get,
            iterator.value,
            gt.ROW_CELLS,
            ctypes.c_void_p(screen.row_cells),
        )
        if row_dirty(screen, iterator.value):
            rows.add(count)
            screen.lib.row_set(
                iterator.value, gt.ROW_DIRTY, ctypes.byref(clean)
            )
        count += 1
    return rows


def full_due(screen, colors, rects_by_row) -> bool:
    """Whether the next paint takes every row, after moving
    the comparisons' answers to the screen: the first paint, a
    moved palette (a scheme flip or a program's own default
    colors), a viewport that moved (the library flags no rows
    for a scrollback page, probed), a find pass whose
    rectangles moved, a change of pixels or cell metrics (a
    resize, a zoom on a maximized window, a hidden tab's font
    change), or the sticky full a flash's end or an exit sets.
    The fringe below and right of the grid repaints with the
    full pass alone."""
    offset = scrollbar_offset(screen)
    answers = (
        bytes(colors),
        offset,
        rects_by_row,
        (
            screen.width(),
            screen.height(),
            screen.metrics.cell_width,
            screen.metrics.cell_height,
            screen.cols,
            screen.rows,
        ),
    )
    tracked = (
        screen.last_palette,
        screen.last_scroll,
        screen.last_underlays,
        screen.last_geometry,
    )
    screen.last_palette, screen.last_scroll = answers[0], answers[1]
    screen.last_underlays, screen.last_geometry = answers[2], answers[3]
    return screen.repaint_full or tracked != answers


def scrollbar_offset(screen):
    """The viewport's offset in the scrollback: the number a
    plan compares against the last paint's number to catch a
    viewport scroll, which the library's row flags leave
    clean."""
    ok, bar = screen.lib.get(
        screen.lib.terminal_get,
        screen.terminal,
        gt.DATA_SCROLLBAR,
        gt.Scrollbar(),
    )
    return bar.offset if ok else None


def overlay_rows(screen, cursor) -> set[int]:
    """The rows the overlays must repaint beyond the library's
    damage: the translucent cursor redraws over fresh pixels
    every paint, so the rows it covers now and covered last
    paint repaint, and a dead child's exit banner redraws over
    the rows it spans."""
    rows: set[int] = set()
    cell_h = screen.metrics.cell_height
    if cursor is not None:
        rows.add(cursor.y() // cell_h)
    if screen.last_cursor is not None:
        rows.add(screen.last_cursor.y() // cell_h)
    if screen.session.exited:
        rows.add(max(0, screen.rows - 1))  # the banner's strip
    return rows


def rows_region(screen, rows) -> QRegion:
    """The update region of a set of rows: whole-width strips,
    one per row."""
    cell_h = screen.metrics.cell_height
    region = QRegion()
    for count in sorted(rows):
        region = region.united(
            QRect(0, count * cell_h, screen.width(), cell_h)
        )
    return region


def paint_event(screen, painter: QPainter, region: QRegion) -> None:
    """One paint of the grid: the planned, damaged, and
    exposed rows straight onto the widget — each with its
    background first — then the fringe and the overlays drawn
    fresh above them."""
    colors = palette(screen)
    if colors is None:
        return
    screen.painted_rows = []
    rects_by_row = underlay_map(screen)
    full = take_full(screen, colors, rects_by_row)
    cursor = cursor_rect(
        screen,
        colors,
        (screen.metrics.cell_width, screen.metrics.cell_height),
    )
    rows = screen.repaint_rows | overlay_rows(screen, cursor)
    paint_cells(screen, painter, colors, rects_by_row, region, full, rows)
    paint_past_grid(screen, painter, colors, region, full)
    consume_plan(screen)
    paint_overlays(screen, painter, colors, cursor)


def take_full(screen, colors, rects_by_row) -> bool:
    """Whether this paint takes every row: the plan's stored
    verdict, or a paint that arrived before any plan did. A
    full paint moves the trackers a plan would have
    moved."""
    full = screen.repaint_full or screen.last_palette is None
    if full:
        full_due(screen, colors, rects_by_row)
    return full


def paint_past_grid(screen, painter, colors, region, full) -> None:
    """The strips the grid does not reach — the fringe below
    and right of it, the scrollbar's lane among them — filled
    with the background on a full pass, or when this paint's
    region reaches into them."""
    if full or fringe_exposed(screen, region):
        paint_fringe(screen, painter, colors)


def consume_plan(screen) -> None:
    """The plan this paint consumed: nothing stays queued for
    the next paint to take twice."""
    screen.repaint_full = False
    screen.repaint_rows = set()
    screen.repaint_region = QRegion()


def paint_overlays(screen, painter, colors, cursor) -> None:
    """The overlays, drawn fresh above the rows every paint:
    the translucent cursor over the row it covers, the
    scrollbar's thumb in its lane, a dead child's exit
    banner, and the bell flash's border."""
    paint_cursor(screen, painter, colors, cursor)
    screen.last_cursor = cursor
    paint_scrollbar(screen, painter, colors)
    paint_exit_banner(screen, painter)
    if screen.bell_flash:
        paint_bell_flash(screen, painter)


def fringe_exposed(screen, region: QRegion) -> bool:
    """Whether this paint's region reaches past the grid, into
    the fringe the full pass fills."""
    if region.isEmpty():
        return False
    box = region.boundingRect()
    return (
        box.bottom() >= screen.rows * screen.metrics.cell_height
        or box.right() >= screen.cols * screen.metrics.cell_width
    )


def paint_cells(
    screen, painter, colors, rects_by_row, region, full, rows
) -> None:
    """Walk the rows the render state holds and paint the
    damaged ones onto the widget, consuming each painted row's
    dirty flag. A row outside the damage set keeps its pixels
    and its per-cell text. The walk also reads every row's
    wrap flag, and the URL scan stitches the cached texts
    into the span list — soft-wrapped rows scan as one text
    (#1100)."""
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
            screen,
            painter,
            colors,
            style,
            codepoints,
            iterator,
            rects_by_row,
            region,
            full,
            rows,
        )


def walk_rows(
    screen,
    painter,
    colors,
    style,
    codepoints,
    iterator,
    rects_by_row,
    region,
    full,
    rows,
):
    """The viewport walk: a row paints when the plan named
    it or when the update region exposes it — an exposure owes
    every pixel it shows, a resize or a screen move included.
    The damage set itself was consumed at the plan, so the
    walk paints the plan's rows and the region's rows alone.
    The answer is the per-row URL span list the scan derives
    — a row outside the paint keeps the per-cell text its
    pixels still show, and its spans come back from the same
    text."""
    rows_text = screen.link_rows
    wraps: list[bool] = []
    count = 0
    y = 0
    cell_w, cell_h = screen.metrics.cell_width, screen.metrics.cell_height
    while screen.lib.row_iterator_next(iterator.value):
        _, cells = screen.lib.get(
            screen.lib.row_get,
            iterator.value,
            gt.ROW_CELLS,
            ctypes.c_void_p(screen.row_cells),
        )
        rect = QRect(0, y, screen.cols * cell_w, cell_h)
        if full or count in rows or region.intersects(rect):
            paint_damaged_row(
                screen,
                painter,
                colors,
                style,
                codepoints,
                cells,
                rows_text,
                rects_by_row,
                count,
                y,
            )
        wraps.append(links.row_wraps(screen, count))
        count += 1
        y += cell_h
    del rows_text[count:]
    return links.scan(rows_text, wraps)


def paint_damaged_row(
    screen,
    painter,
    colors,
    style,
    codepoints,
    cells,
    rows,
    rects_by_row,
    count: int,
    y: int,
) -> None:
    """One damaged row's contribution to the widget: the row's
    background first — every pixel the row covers starts
    clean, so a line of scrolled or rewritten text leaves none
    of the frame before it — then the find underlays beneath
    it, its cells, its per-cell text into the scan's row
    cache, the consumption of its dirty flag, and its place in
    the paint record the damage pins read."""
    painter.fillRect(
        0,
        y,
        screen.cols * screen.metrics.cell_width,
        screen.metrics.cell_height,
        rgb(colors.background),
    )
    paint_underlays(screen, painter, rects_by_row.get(count, []), y)
    while len(rows) <= count:
        rows.append([])
    rows[count] = paint_row(
        screen, painter, colors, style, codepoints, cells, y
    )
    screen.painted_rows.append(count)


def row_dirty(screen, iterator: int) -> bool:
    """Whether the library flags one viewport row as damaged,
    read without consuming it — the plan consumes the flags it
    reads."""
    ok, dirty = screen.lib.get(
        screen.lib.row_get, iterator, gt.ROW_DIRTY, ctypes.c_bool()
    )
    return bool(ok and dirty.value)


def underlay_map(screen) -> dict[int, list[tuple[int, int, bool]]]:
    """The find bar's viewport rectangles grouped by row, for
    painting beneath the cells and for the plan's comparison
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


def paint_row(screen, painter, colors, style, codepoints, cells, y):
    """One row's cells left to right; answers the row's
    per-cell text for the URL scan."""
    x = 0
    row_chars: list[str] = []
    while screen.lib.row_cells_next(cells.value):
        paint_cell(screen, painter, colors, style, codepoints, x, y, row_chars)
        x += screen.metrics.cell_width
    return row_chars


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
    """One cell rect of background, when the cell needs one:
    the row's own fill already carries the base background."""
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
    screen, painter, foreground, style, codepoints, x: int, y: int
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
        screen.lib.render_state_get,
        screen.render_state,
        gt.RS_CURSOR_VISIBLE,
        ctypes.c_bool(),
    )
    ok2, present = lib.get(
        screen.lib.render_state_get,
        screen.render_state,
        gt.RS_CURSOR_VIEWPORT_HAS_VALUE,
        ctypes.c_bool(),
    )
    if not (ok and ok2 and visible.value and present.value):
        return None
    _, cx = lib.get(
        screen.lib.render_state_get,
        screen.render_state,
        gt.RS_CURSOR_VIEWPORT_X,
        ctypes.c_uint16(),
    )
    _, cy = lib.get(
        screen.lib.render_state_get,
        screen.render_state,
        gt.RS_CURSOR_VIEWPORT_Y,
        ctypes.c_uint16(),
    )
    width, height = cell
    return QRect(cx.value * width, cy.value * height, width, height)


def paint_cursor(screen, painter, colors, rect) -> None:
    """The cursor cell, translucent over the fresh row
    beneath it — the row repaints every paint the cursor
    occupies, so the block never compounds on itself."""
    if rect is None:
        return
    color = rgb(
        colors.cursor if colors.cursor_has_value else colors.foreground
    )
    color.setAlpha(140)
    painter.fillRect(rect, color)


def paint_fringe(screen, painter: QPainter, colors) -> None:
    """The strips the grid does not reach — below its last
    row and right of its last column, the scrollbar's lane
    among them — filled with the background, so a resize or a
    font change never leaves the earlier frame's edge on the
    widget."""
    background = rgb(colors.background)
    grid_h = screen.rows * screen.metrics.cell_height
    if grid_h < screen.height():
        painter.fillRect(
            0, grid_h, screen.width(), screen.height() - grid_h, background
        )
    grid_w = screen.cols * screen.metrics.cell_width
    if grid_w < screen.width():
        painter.fillRect(
            grid_w, 0, screen.width() - grid_w, screen.height(), background
        )


def paint_scrollbar(screen, painter, colors) -> None:
    """The thumb over the scrollback, in the lane the grid
    reserves right of its cells: the lane fills with the
    background first — erasing the last thumb — so the thumb
    never ages and never covers a glyph."""
    lane_x = screen.width() - LANE_W
    painter.fillRect(
        lane_x, 0, LANE_W, screen.height(), rgb(colors.background)
    )
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
        screen.width() - LANE_W,
        10 + offset,
        6,
        thumb,
        QColor(160, 160, 160, 130),
    )


def paint_exit_banner(screen, painter) -> None:
    """The banner a dead child leaves: the exit status across
    the bottom, over the frozen grid — the strip's row repaints
    with every paint while the child is gone, so the banner
    draws over fresh pixels."""
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


def paint_bell_flash(screen, painter) -> None:
    """The visual bell: a triple orange border for the flash's
    180 ms; the flash's end takes a full pass that erases
    it."""
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
