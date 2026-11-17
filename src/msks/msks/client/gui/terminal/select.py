"""Grid-anchored selection and the clipboard (phase 4.5 of
#924, #953).

Selection lives in the library, anchored to grid coordinates:
it survives viewport scrolling and reflow because the library
re-resolves the anchor set against the grid on every read. The
gestures follow the library's selection-gesture protocol —
press, drag (the only event that produces a selection), and
release (which publishes to the PRIMARY selection, or clears
on a click without a drag). Clipboard paths route through Qt's
clipboard: copy from the library's plain-text formatter, paste
wrapped in bracketed paste when the program enabled mode 2004
and with newlines as carriage returns otherwise, and the
PRIMARY selection fed by the X11 select-then-middle-click
convention.
"""

from __future__ import annotations

import ctypes

from PySide6.QtCore import QPointF
from PySide6.QtGui import QClipboard
from PySide6.QtWidgets import QApplication

from .. import ghostty as gt
from .input import cell_at, mode_enabled, mouse_tracking

# The double/triple-click window: a second press on the same
# cell inside it counts as a word select, a third as a line
# select.
MULTI_CLICK_MS = 400


def ref_under(screen, position: QPointF):
    """The grid ref under a viewport pixel position, when the
    library resolves one."""
    col, row = cell_at(screen, position)
    point = gt.Point(
        tag=gt.POINT_VIEWPORT,
        value=gt.PointValue(coordinate=gt.PointCoordinate(x=col, y=row)),
    )
    ref = gt.GridRef()
    ref.size = ctypes.sizeof(ref)
    if (
        screen.lib.terminal_grid_ref(screen.terminal, point, ctypes.byref(ref))
        # The library answers a ref for every clamped in-grid
        # position (probed, blank cells included); the guard
        # holds for a terminal torn down mid-gesture.
        != gt.SUCCESS  # pragma: no cover
    ):
        return None
    return ref


def selection(screen):
    """The library's active selection, or None."""
    selection = gt.Selection()
    selection.size = ctypes.sizeof(selection)
    ok, _ = screen.lib.get(
        screen.lib.terminal_get,
        screen.terminal,
        gt.DATA_SELECTION,
        selection,
    )
    return selection if ok and selection.size else None


def apply(screen, selection) -> None:
    """Make a selection the terminal's own, so the render
    state flags its cells for the inverted paint."""
    screen.lib.terminal_set(
        screen.terminal, gt.OPT_SELECTION, ctypes.byref(selection)
    )
    screen.sel_dragged = True
    screen.refresh()


def clear(screen) -> None:
    """Drop the selection (a click without a drag)."""
    screen.lib.terminal_set(screen.terminal, gt.OPT_SELECTION, None)
    screen.refresh()


def word_at(screen, position: QPointF) -> None:
    """Double-click: select the word under the cursor through
    the library's word selector."""
    ref = ref_under(screen, position)
    if ref is None:  # pragma: no cover — ref_under's guard
        return
    options = gt.SelectWordOptions(
        size=ctypes.sizeof(gt.SelectWordOptions), ref=ref
    )
    picked = gt.Selection()
    picked.size = ctypes.sizeof(picked)
    if (
        screen.lib.terminal_select_word(
            screen.terminal, ctypes.byref(options), ctypes.byref(picked)
        )
        # SUCCESS even for a blank cell (probed: an empty
        # selection comes back); failure needs a dead terminal.
        == gt.SUCCESS
    ):
        apply(screen, picked)  # pragma: no branch


def line_at(screen, position: QPointF) -> None:
    """Triple-click: select the line under the cursor through
    the library's line selector."""
    ref = ref_under(screen, position)
    if ref is None:  # pragma: no cover — ref_under's guard
        return
    options = gt.SelectLineOptions(
        size=ctypes.sizeof(gt.SelectLineOptions), ref=ref
    )
    picked = gt.Selection()
    picked.size = ctypes.sizeof(picked)
    if (
        screen.lib.terminal_select_line(
            screen.terminal, ctypes.byref(options), ctypes.byref(picked)
        )
        # Same probe as the word selector: SUCCESS on every
        # live ref, an empty selection on a blank line.
        == gt.SUCCESS
    ):
        apply(screen, picked)  # pragma: no branch


def gesture(screen, kind: int, position: QPointF) -> None:
    """Feed one selection gesture event to the library's
    gesture state machine. Every event carries the grid ref
    under the cursor and the pixel position; a drag also
    carries the surface geometry and is the only event that
    produces a selection, which is applied to the terminal so
    the render state flags the selected cells. A release
    publishes a dragged selection to PRIMARY, or clears the
    selection for a click that never dragged."""
    lib = screen.lib
    ref = ref_under(screen, position)
    if ref is None:  # pragma: no cover — ref_under's guard
        return
    event = gesture_event(screen, kind, position, ref)
    try:
        picked = gt.Selection()
        picked.size = ctypes.sizeof(picked)
        result = lib.selection_gesture_event(
            screen.selection_gesture,
            screen.terminal,
            event,
            ctypes.byref(picked),
        )
        gesture_result(screen, kind, result, picked)
    finally:
        lib.selection_gesture_event_free(event)
    if kind in (gt.SEL_DRAG, gt.SEL_RELEASE):
        screen.refresh()


def gesture_result(screen, kind: int, result: int, picked) -> None:
    """What a gesture answered: a successful drag's selection
    is applied to the terminal; a release publishes or
    clears."""
    if kind == gt.SEL_DRAG and result == gt.SUCCESS:
        screen.lib.terminal_set(
            screen.terminal, gt.OPT_SELECTION, ctypes.byref(picked)
        )
    elif kind == gt.SEL_RELEASE:
        release(screen)


def gesture_event(screen, kind: int, position, ref):
    """One assembled gesture event: the ref, the pixel
    position, and — for a drag — the surface geometry."""
    lib = screen.lib
    event = lib.new_handle(lib.selection_gesture_event_new, kind)
    lib.selection_gesture_event_set(
        event, gt.SEL_EVENT_OPT_REF, ctypes.byref(ref)
    )
    surface = gt.SurfacePosition(x=float(position.x()), y=float(position.y()))
    lib.selection_gesture_event_set(
        event, gt.SEL_EVENT_OPT_POSITION, ctypes.byref(surface)
    )
    if kind == gt.SEL_DRAG:
        geometry = gt.SelectionGestureGeometry(
            columns=screen.cols,
            cell_width=screen.metrics.cell_width,
            padding_left=0,
            screen_height=screen.height(),
        )
        lib.selection_gesture_event_set(
            event, gt.SEL_EVENT_OPT_GEOMETRY, ctypes.byref(geometry)
        )
    return event


def release(screen) -> None:
    """A finished press: publish a dragged selection to the
    PRIMARY selection; clear the selection when the press
    never dragged (unless a word or line select bypassed the
    gesture — its dragged flag knows nothing of those, so a
    multi-click press always publishes)."""
    if screen.press_count >= 2 or screen.sel_dragged:
        publish_primary(screen)
        return
    dragged = ctypes.c_bool()
    ok = screen.lib.selection_gesture_get(
        screen.selection_gesture,
        screen.terminal,
        gt.SEL_DATA_DRAGGED,
        ctypes.byref(dragged),
    )
    if ok == gt.SUCCESS and not dragged.value:
        clear(screen)
    else:
        # The dragged read fails only on a torn-down gesture
        # state (probed SUCCESS on every live one).
        publish_primary(screen)  # pragma: no cover


def autoscroll_tick(screen) -> None:
    """Scroll while a drag's last in-window position sits in
    the edge margin, then extend the selection into the
    revealed rows — Wayland windows cannot grab the mouse, so
    the last position drives this instead of continuous
    events."""
    if not screen.sel_dragged or mouse_tracking(screen):
        return
    y = screen.drag_position.y()
    margin = screen.metrics.cell_height
    if y < margin:
        rows = max(1, round((margin - y) / screen.metrics.cell_height))
        delta = -rows
    elif y > screen.height() - margin:
        rows = max(
            1,
            round(
                (y - (screen.height() - margin)) / screen.metrics.cell_height
            ),
        )
        delta = rows
    else:
        return
    screen.lib.terminal_scroll_viewport(
        screen.terminal,
        gt.ScrollViewport(
            gt.SCROLL_DELTA, gt.ScrollViewportValue(delta=delta)
        ),
    )
    gesture(screen, gt.SEL_DRAG, screen.drag_position)


def text(screen) -> str:
    """The active selection as plain text, straight from the
    library's formatter (trim on, like a terminal copy)."""
    picked = selection(screen)
    if picked is None:
        return ""
    options = gt.SelectionFormatOptions(
        size=ctypes.sizeof(gt.SelectionFormatOptions),
        emit=gt.FORMATTER_PLAIN,
        unwrap=False,
        trim=True,
        selection=ctypes.pointer(picked),
    )
    size = 4096
    while size <= 1 << 20:
        buf = ctypes.create_string_buffer(size)
        written = ctypes.c_size_t()
        result = screen.lib.selection_format_buf(
            screen.terminal, options, buf, size, ctypes.byref(written)
        )
        if result == gt.SUCCESS:
            return buf.raw[: written.value].decode("utf-8", errors="replace")
        size *= 4
    # An accepted limit, not an unreachable arm: a selection
    # spanning the scrollback in multi-byte graphemes (emoji
    # art fills 10k rows past a MiB) formats to empty rather
    # than grow the buffer without bound.
    return ""  # pragma: no cover


def copy(screen) -> None:
    """Ctrl+Shift+C: the selection to the clipboard."""
    picked = text(screen)
    if picked:
        QApplication.clipboard().setText(picked)


def primary_mode(clipboard) -> int:
    """The mode that carries the X11 PRIMARY selection: the
    Selection mode where the platform has one (X11, Wayland),
    the clipboard itself otherwise (offscreen CI runners,
    Windows) — select-then-middle-click works everywhere."""
    if clipboard.supportsSelection():
        # X11 and Wayland hosts only — the offscreen CI
        # clipboard answers supportsSelection False, and the
        # suite pins the fallback there.
        return QClipboard.Mode.Selection  # pragma: no cover
    return QClipboard.Mode.Clipboard


def publish_primary(screen) -> None:
    """A finished drag publishes its text to the PRIMARY
    selection — the X11 select-then-middle-click
    convention."""
    picked = text(screen)
    if picked:
        clipboard = QApplication.clipboard()
        clipboard.setText(picked, primary_mode(clipboard))


def paste_text(screen, text: str) -> None:
    """Send pasted text to the program: wrapped in bracketed
    paste when it enabled mode 2004, else raw — and raw means
    newlines become carriage returns, because a shell executes
    the line a carriage return ends."""
    if not text:
        return
    if mode_enabled(screen, gt.MODE_BRACKETED_PASTE):
        screen.write(b"\x1b[200~" + text.encode("utf-8") + b"\x1b[201~")
    else:
        # CRLF pairs collapse first: a Windows-format paste
        # would otherwise carry an empty line per paragraph.
        raw = text.replace("\r\n", "\n").replace("\n", "\r")
        screen.write(raw.encode("utf-8"))


def paste_clipboard(screen) -> None:
    """Ctrl+Shift+V: the clipboard to the program."""
    paste_text(screen, QApplication.clipboard().text())


def paste_primary(screen) -> None:
    """Middle-click: the PRIMARY selection — the last text
    selected with the mouse anywhere, this terminal
    included."""
    clipboard = QApplication.clipboard()
    paste_text(screen, clipboard.text(primary_mode(clipboard)))
