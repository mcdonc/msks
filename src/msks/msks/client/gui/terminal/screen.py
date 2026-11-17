"""The terminal screen widget (phase 4.5 of #924, #952).

A ``QWidget`` that owns a libghostty-vt terminal, its render
state, and a PTY session: the emulator parses the child's
bytes, the painter draws the render state, and the input
encoders send the child what the keyboard and mouse say. The
widget keeps no terminal state of its own — colors, styles,
the cursor, the scrollback, and the modes all belong to the
library, and the child's lifecycle to the session.
"""

from __future__ import annotations

import ctypes

from PySide6.QtCore import QSize, QSocketNotifier, Qt, QTimer, Signal
from PySide6.QtGui import (
    QCloseEvent,
    QFocusEvent,
    QKeyEvent,
    QMouseEvent,
    QPainter,
    QResizeEvent,
    QWheelEvent,
)
from PySide6.QtWidgets import QWidget

from .. import ghostty as gt
from .. import termsession as ts
from . import callbacks, fonts, input, paint

# The scrollback the terminal keeps (rows).
SCROLLBACK_LINES = 10_000


def is_paging(modifiers, key) -> bool:
    """Shift alone with PgUp/PgDn: a local page, not the
    program's key."""
    shift = Qt.KeyboardModifier.ShiftModifier
    control = Qt.KeyboardModifier.ControlModifier
    page_keys = (Qt.Key.Key_PageUp, Qt.Key.Key_PageDown)
    return bool(
        modifiers & shift and not modifiers & control and key in page_keys
    )


# Wheel rows per notch — six felt right next to Konsole's
# default three on the hardware at hand.
WHEEL_ROWS_PER_NOTCH = 6


class TerminalWidget(QWidget):
    """A terminal screen: the emulator, the painter, and the
    child, wired together. ``argv`` is what the tab runs — no
    default shell; a terminal tab always knows what it runs
    (#951's session contract).

    The teardown contract: closing (the close event, or
    ``closeEvent`` driven directly) is what hangs up on the
    child, frees the library handles, and unseats the registry
    entry — a tab that destroys the widget without closing it
    leaks all three (#954 must close before ``deleteLater``)."""

    title_changed = Signal(str)
    process_exited = Signal(int)

    def __init__(self, argv: list[str], parent: QWidget | None = None):
        super().__init__(parent)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setMouseTracking(True)
        self.setAttribute(Qt.WidgetAttribute.WA_OpaquePaintEvent)
        self.cols, self.rows = 80, 24
        fonts.register_faces()
        self.font = fonts.make_font()
        self.metrics = fonts.measure(self.font)
        self.fonts = fonts.variants(self.font)
        self.lib = gt.Lib()
        self.terminal = self.lib.new_handle(
            self.lib.terminal_new, self.cols, self.rows
        )
        scrollback = ctypes.c_size_t(SCROLLBACK_LINES)
        self.lib.terminal_set(
            self.terminal,
            gt.OPT_SCROLLBACK_MAX_LINES,
            ctypes.byref(scrollback),
        )
        # The registry seat first: every effect callback below
        # finds the screen through it.
        callbacks.register(self)
        for option, value in callbacks.registrations(self):
            self.lib.terminal_set(self.terminal, option, value)
        self.render_state = self.lib.new_handle(self.lib.render_state_new)
        self.row_iterator = self.lib.new_handle(self.lib.row_iterator_new)
        self.row_cells = self.lib.new_handle(self.lib.row_cells_new)
        self.key_encoder = self.lib.new_handle(self.lib.key_encoder_new)
        self.key_event = self.lib.new_handle(self.lib.key_event_new)
        self.mouse_encoder = self.lib.new_handle(self.lib.mouse_encoder_new)
        self.mouse_event = self.lib.new_handle(self.lib.mouse_event_new)
        self.apply_metrics()
        # The pty pair opened and sized before the fork, the
        # child exec'ed onto it — the session contract (#951).
        self.session = ts.PtySession(
            argv,
            self.cols,
            self.rows,
            self.metrics.cell_width,
            self.metrics.cell_height,
        )
        # The master's readability drives the parse-and-paint
        # loop; a dead master must not keep the notifier armed
        # (the session closes the fd under it on exit).
        self.notifier = QSocketNotifier(
            self.session.master_fd or 0,
            QSocketNotifier.Type.Read,
            self,
        )
        self.notifier.activated.connect(self.read_master)
        self.reaper = QTimer(self)
        self.reaper.timeout.connect(self.reap_child)
        # The visual bell: BEL flashes a border for a moment.
        self.bell_flash = False
        self.bell_timer = QTimer(self)
        self.bell_timer.setSingleShot(True)
        self.bell_timer.timeout.connect(self.bell_off)
        self._wheel_accum = 0
        self._closed = False
        self._exit_reported = False

    # --- lifecycle ----------------------------------------------------

    def closeEvent(self, event: QCloseEvent) -> None:  # noqa: N802
        if self._closed:  # a second close tears nothing twice
            super().closeEvent(event)
            return
        self._closed = True
        callbacks.unregister(self)
        self.reaper.stop()
        self.bell_timer.stop()
        self.notifier.setEnabled(False)
        self.session.close()
        for free, handle in [
            (self.lib.mouse_event_free, self.mouse_event),
            (self.lib.mouse_encoder_free, self.mouse_encoder),
            (self.lib.key_event_free, self.key_event),
            (self.lib.key_encoder_free, self.key_encoder),
            (self.lib.row_cells_free, self.row_cells),
            (self.lib.row_iterator_free, self.row_iterator),
            (self.lib.render_state_free, self.render_state),
        ]:
            free(handle)
        self.lib.terminal_free(self.terminal)
        super().closeEvent(event)

    # --- data flow ----------------------------------------------------

    def read_master(self) -> None:
        """The master has bytes (or the child is gone): parse
        them into the emulator and repaint. A child's final
        output and its exit often land in one readiness window
        — the drain delivers the bytes with the session already
        marked exited — so the exit path runs from either arm,
        never leaving the notifier armed on the closed master."""
        data = self.session.drain()
        if data is None:
            self.child_gone()
            return
        if data:
            self.lib.vt_write(self.terminal, data)
            self.refresh()
            if self.session.exited:
                self.child_gone()

    def child_gone(self) -> None:
        """The exit path, once: disarm the notifier (the
        session closed the master under it), start the reaper,
        and report the status the tab teardown listens for.
        Later reads on the dead master change nothing."""
        if self._exit_reported:
            return
        self._exit_reported = True
        self.notifier.setEnabled(False)
        # Reap first when the child is already collectable: the
        # report carries the real status, not the 0 a missed
        # WNOHANG would leave.
        self.session.reap()
        self.reaper.start(100)
        self.refresh()
        self.process_exited.emit(self.session.exit_status or 0)

    def reap_child(self) -> None:
        """Collect the dead child; once it is collected the
        reaper stands down."""
        self.session.reap()
        if self.session.child_pid is None or self.session.exited:
            self.notifier.setEnabled(False)
            self.reaper.stop()

    def write(self, data: bytes) -> None:
        """Bytes to the child: best-effort — the session drops
        what the pty will not take."""
        self.session.write(data)

    def refresh(self) -> None:
        """Sync the render state with the emulator and
        schedule a paint."""
        self.lib.render_state_update(self.render_state, self.terminal)
        self.update()

    def flash(self) -> None:
        """The visual bell: a brief orange border flash; a
        repeat BEL restarts the timer."""
        self.bell_flash = True
        self.bell_timer.start(180)
        self.update()

    def bell_off(self) -> None:
        """The flash's timer end: the border goes dark."""
        self.bell_flash = False
        self.update()

    # --- geometry and fonts -------------------------------------------

    def sizeHint(self) -> QSize:  # noqa: N802
        return QSize(*self.metrics.size(self.cols, self.rows))

    def apply_metrics(self) -> None:
        """Measure the face and push the pixel metrics into the
        emulator — the session-free half of a font change, run
        at construction before the child exists."""
        self.metrics = fonts.measure(self.font)
        self.fonts = fonts.variants(self.font)
        self.lib.terminal_resize(
            self.terminal,
            self.cols,
            self.rows,
            self.metrics.cell_width,
            self.metrics.cell_height,
        )
        self.refresh()

    def apply_font_size(self, points: int) -> None:
        """Measure the face at a size, push the new pixel
        metrics into the pty, and keep the grid at the same
        cell count (the emulator's resize already ran in
        apply_metrics)."""
        self.font.setPointSize(points)
        self.apply_metrics()
        self.session.resize(
            self.cols,
            self.rows,
            self.metrics.cell_width,
            self.metrics.cell_height,
        )
        window = self.window()
        if window is not None and window is not self:
            window.resize(self.sizeHint())

    def zoom_font(self, direction: int) -> None:
        """Ctrl+Plus/Minus: step the font size, clamped."""
        points = self.font.pointSize() + direction
        if fonts.MIN_PT <= points <= fonts.MAX_PT:
            self.apply_font_size(points)

    def resize_grid(self, cols: int, rows: int) -> None:
        """Tell the emulator and the child the new grid, in the
        current cell pixels."""
        self.cols, self.rows = cols, rows
        self.lib.terminal_resize(
            self.terminal,
            cols,
            rows,
            self.metrics.cell_width,
            self.metrics.cell_height,
        )
        self.session.resize(
            cols, rows, self.metrics.cell_width, self.metrics.cell_height
        )
        self.refresh()

    def resizeEvent(self, event: QResizeEvent) -> None:  # noqa: N802
        cols = max(1, self.width() // self.metrics.cell_width)
        rows = max(1, self.height() // self.metrics.cell_height)
        if (cols, rows) != (self.cols, self.rows):
            self.resize_grid(cols, rows)
        super().resizeEvent(event)

    # --- painting -----------------------------------------------------

    def paintEvent(self, event) -> None:  # noqa: N802
        painter = QPainter(self)
        paint.paint_event(self, painter)
        painter.end()

    # --- keyboard -----------------------------------------------------

    def keyPressEvent(self, event: QKeyEvent) -> None:  # noqa: N802
        if self.handle_shortcut(event):
            return
        action = input.key_action(event.isAutoRepeat())
        input.encode_key(self, event, action)

    def handle_shortcut(self, event: QKeyEvent) -> bool:
        """The widget's own keys before the encoder: font
        stepping and buffer paging. ``True`` when the shortcut
        consumed the event."""
        return self.font_shortcut(event) or self.page_shortcut(event)

    def font_shortcut(self, event: QKeyEvent) -> bool:
        """The font keys under Ctrl: stepping and reset."""
        if not event.modifiers() & Qt.KeyboardModifier.ControlModifier:
            return False
        return self.font_step(event) or self.font_reset(event)

    def font_step(self, event: QKeyEvent) -> bool:
        """Ctrl+Plus/Equal grows the face, Ctrl+Minus shrinks
        it (numpad keys arrive as the same names with a keypad
        modifier)."""
        if event.key() in (Qt.Key.Key_Equal, Qt.Key.Key_Plus):
            self.zoom_font(1)
            return True
        if event.key() == Qt.Key.Key_Minus:
            self.zoom_font(-1)
            return True
        return False

    def font_reset(self, event: QKeyEvent) -> bool:
        """Ctrl+0 resets the face to its default size."""
        if event.key() != Qt.Key.Key_0:
            return False
        if self.font.pointSize() != fonts.DEFAULT_PT:
            self.apply_font_size(fonts.DEFAULT_PT)
        return True

    def page_shortcut(self, event: QKeyEvent) -> bool:
        """Shift+PgUp/PgDn page the buffer locally instead of
        going to the program inside."""
        if not is_paging(event.modifiers(), event.key()):
            return False
        direction = -1 if event.key() == Qt.Key.Key_PageUp else 1
        input.scroll_page(self, direction)
        return True

    def keyReleaseEvent(self, event: QKeyEvent) -> None:  # noqa: N802
        # A held key's platform repeats arrive as press events
        # only; the repeat's release is noise.
        if event.isAutoRepeat():
            return
        input.encode_key(self, event, gt.ACTION_RELEASE)

    # --- mouse --------------------------------------------------------

    def mousePressEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        input.send_mouse(
            self,
            gt.MOUSE_PRESS,
            input.button_for(event),
            event.position(),
            event.modifiers(),
            event.buttons(),
        )

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        input.send_mouse(
            self,
            gt.MOUSE_RELEASE,
            input.button_for(event),
            event.position(),
            event.modifiers(),
            event.buttons(),
        )

    def mouseMoveEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        input.send_mouse(
            self,
            gt.MOUSE_MOTION,
            input.button_for(event),
            event.position(),
            event.modifiers(),
            event.buttons(),
        )

    def wheelEvent(self, event: QWheelEvent) -> None:  # noqa: N802
        pixels = event.pixelDelta()
        steps, self._wheel_accum = input.wheel_steps(
            self._wheel_accum,
            pixels.y() if pixels is not None else None,
            event.angleDelta().y(),
            self.metrics.cell_height,
        )
        if steps == 0:
            return
        if input.mouse_tracking(self):
            self.send_wheel_buttons(steps, event)
            return
        rows = WHEEL_ROWS_PER_NOTCH * abs(steps)
        self.scroll_rows(-rows if steps > 0 else rows)

    def send_wheel_buttons(self, steps: int, event: QWheelEvent) -> None:
        """The program owns the wheel: buttons four and five as
        press and release pairs."""
        button = gt.MOUSE_FOUR if steps > 0 else gt.MOUSE_FIVE
        for action in (gt.MOUSE_PRESS, gt.MOUSE_RELEASE):
            input.send_mouse(
                self,
                action,
                button,
                event.position(),
                event.modifiers(),
                event.buttons(),
            )

    def scroll_rows(self, delta: int) -> None:
        """Wheel rows into the buffer and repaint."""
        self.lib.terminal_scroll_viewport(
            self.terminal,
            gt.ScrollViewport(gt.SCROLL_DELTA, gt.ScrollViewportValue(delta)),
        )
        self.refresh()

    def focusInEvent(self, event: QFocusEvent) -> None:  # noqa: N802
        input.send_focus(self, True)
        super().focusInEvent(event)

    def focusOutEvent(self, event: QFocusEvent) -> None:  # noqa: N802
        input.send_focus(self, False)
        super().focusOutEvent(event)
