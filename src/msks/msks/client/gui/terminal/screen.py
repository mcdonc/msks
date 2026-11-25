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

from PySide6.QtCore import (
    QCoreApplication,
    QEvent,
    QPointF,
    QSize,
    QSocketNotifier,
    Qt,
    QTimer,
    Signal,
)
from PySide6.QtGui import (
    QClipboard,
    QCloseEvent,
    QFocusEvent,
    QKeyEvent,
    QMouseEvent,
    QPainter,
    QRegion,
    QResizeEvent,
    QWheelEvent,
)
from PySide6.QtWidgets import QApplication, QLineEdit, QMenu, QWidget

from .. import ghostty as gt
from .. import termsession as ts
from . import callbacks, find, fonts, input, links, paint, select

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


# Wheel rows per notch (#1156): one notch moves twelve rows, so
# a 24-row pane passes one screen every two notches. The rate
# covers every notch that reaches the scrollback, touchpad
# travel included: a pixel delta of one cell height is one
# notch. A child with mouse reporting owns its wheel before
# this rate applies.
WHEEL_ROWS_PER_NOTCH = 12

#: How long the exit report waits for a child the kernel has not
#: made waitable yet (#1099 follow-up): the slave's fds close
#: before the process becomes a zombie, so the last-read burst
#: can arrive first. A child past that grace (wedged in
#: uninterruptible sleep) reports the 0 placeholder rather than
#: stalling the GUI on it.
EXIT_REPORT_GRACE_S = 0.5


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
        self.selection_gesture = self.lib.new_handle(
            self.lib.selection_gesture_new
        )
        # The paint plan (#1113): the rows the next paint takes
        # and the region it asked to update, set by the plan the
        # emulator sync computes and consumed by the paint —
        # the pixel-and-grid comparison answers, the scroll
        # offset and find rectangles the last paint compared,
        # whether the next paint takes every row, and the rows
        # the last paint took (the suite's damage pins). The
        # paint module owns the policy; these fields are its
        # state on the screen, and they exist before the first
        # refresh the constructor's apply_metrics runs.
        self.repaint_full = True
        self.repaint_rows: set[int] = set()
        self.repaint_region = QRegion()
        self.planned = False
        self.last_palette: bytes | None = None
        self.last_scroll: int | None = None
        self.last_underlays = None
        self.last_geometry: tuple[int, ...] | None = None
        self.last_cursor = None
        self.painted_rows: list[int] = []
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
        # The desktop scheme the window path applied (#1006):
        # None until one lands, then the library's own
        # light/dark constant — the answer the color-scheme
        # query reports to the child.
        self.color_scheme = None
        self.bell_timer = QTimer(self)
        self.bell_timer.setSingleShot(True)
        self.bell_timer.timeout.connect(self.bell_off)
        self._wheel_accum = 0
        self._closed = False
        self._exit_reported = False
        # The window fit's flag (#976): a pass's window resize
        # delivers a resize event synchronously, and the grid
        # re-derivation it would run mid-fit would shrink the
        # very target the next pass fits to — the fit holds the
        # entry grid and lands it once at the end.
        self._fitting = False
        # Selection, links, and find: the press state for the
        # multi-click window, the drag state for autoscroll,
        # the painter's URL scan, the hand cursor, and the
        # search handle with its overlay widgets.
        self.press_count = 0
        self.last_press_ms = 0
        self.last_press_cell: tuple[int, int] | None = None
        # Set by any motion with the button held and by a word or
        # line select: it gates autoscroll. The library's gesture
        # judges click against drag.
        self.sel_dragged = False
        self.drag_position = QPointF(0, 0)
        self.autoscroll = QTimer(self)
        self.autoscroll.setInterval(50)
        self.autoscroll.timeout.connect(self.autoscroll_tick)
        self.link_spans: list[list[tuple[int, int, str]]] = []
        # The URL scan's row cache (#1100): each viewport row's
        # per-cell text, refreshed when the painter paints that
        # row and read whole by the scan each paint — soft-wrapped
        # rows stitch across the cached texts.
        self.link_rows: list[list[str]] = []
        self.hand_cursor = False
        self.link_spans: list[list[tuple[int, int, str]]] = []
        self.search = 0
        self.find_edit: QLineEdit | None = None
        self.find_count = None
        # The paint state is whole now: the plan may read the
        # find and session fields it touches. The constructor's
        # own apply_metrics refresh ran before they existed and
        # asked for the whole widget instead.
        self.planned = True

    # --- lifecycle ----------------------------------------------------

    def closeEvent(self, event: QCloseEvent) -> None:  # noqa: N802
        if self._closed:  # a second close tears nothing twice
            super().closeEvent(event)
            return
        self._closed = True
        callbacks.unregister(self)
        if self.hand_cursor:  # an override cursor outlives the widget
            links.set_hand(self, False)
        self.reaper.stop()
        self.bell_timer.stop()
        self.autoscroll.stop()
        if self.search:
            find.close(self)
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
        self.lib.selection_gesture_free(self.selection_gesture, self.terminal)
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
        Later reads on the dead master change nothing.

        The report waits out the window where the child closed
        its slave fds but the kernel has not made it waitable
        (#1099 follow-up): a reaped-now status goes out at once,
        a child still finishing exits inside the grace and its
        real code goes out, and a wedged one reports the 0
        placeholder after the grace instead of hanging the
        report on it."""
        if self._exit_reported:
            return
        self._exit_reported = True
        self.notifier.setEnabled(False)
        self.session.reap()
        if self.session.exit_status is None:
            self.session.await_exit(EXIT_REPORT_GRACE_S)
        self.reaper.start(100)
        self.repaint_full = True  # the exit banner's strip
        self.update()
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
        schedule the paint of the rows the plan names. The
        constructor's own apply_metrics refresh lands before
        the find and session fields exist, and asks for the
        whole widget."""
        self.lib.render_state_update(self.render_state, self.terminal)
        if self.planned:
            self.update(paint.plan_paint(self))
        else:
            self.update()

    def flash(self) -> None:
        """The visual bell: a brief orange border flash; a
        repeat BEL restarts the timer."""
        self.bell_flash = True
        self.bell_timer.start(180)
        self.update()

    def autoscroll_tick(self) -> None:
        """The autoscroll watch: scroll while a drag holds the
        edge, extending the selection into the revealed
        rows."""
        select.autoscroll_tick(self)

    def osc52_clipboard(self, write) -> None:
        """An OSC 52 write from the program inside: apply the
        plain-text payload to the clipboard the request
        named (the selection locations to PRIMARY) and answer
        the library's reply — success, or unsupported when
        the request carried no text."""
        payload, location = callbacks.clipboard_payload(write)
        reply = gt.ClipboardWriteReply()
        reply.size = ctypes.sizeof(reply)
        reply.remember = False
        if payload is not None:
            clipboard = QApplication.clipboard()
            if location in (1, 2):
                mode = select.primary_mode(clipboard)
            else:
                mode = QClipboard.Mode.Clipboard
            clipboard.setText(payload, mode)
            reply.result = gt.CLIPBOARD_WRITE_SUCCESS
        else:
            reply.result = gt.CLIPBOARD_WRITE_UNSUPPORTED
        if write.reply:
            write.reply(
                ctypes.c_void_p(ctypes.addressof(write)), ctypes.byref(reply)
            )

    def bell_off(self) -> None:
        """The flash's timer end: the border goes dark, and
        the full pass that erases it follows."""
        self.bell_flash = False
        self.repaint_full = True
        self.update()

    # --- geometry and fonts -------------------------------------------

    def sizeHint(self) -> QSize:  # noqa: N802
        width, height = self.metrics.size(self.cols, self.rows)
        return QSize(width + paint.LANE_W, height)

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

    def set_face(self, points: int) -> None:
        """Run the face at a size (#976): measure it, push the
        new pixel metrics into the emulator and the pty, and
        keep the grid at the same cell count — the layout the
        zoom gives a terminal whose window must not move (a
        shell that just opened, one hidden behind another
        tab)."""
        self.font.setPointSize(points)
        self.apply_metrics()
        self.session.resize(
            self.cols,
            self.rows,
            self.metrics.cell_width,
            self.metrics.cell_height,
        )

    def apply_font_size(self, points: int) -> None:
        """Run the face at a size and resize the window around
        the new cell metrics (#976): the grid keeps its cell
        count and the window takes the same growth, so a
        visible terminal reads the same columns and rows at
        the new size."""
        self.set_face(points)
        self.fit_window()

    def fit_window(self) -> None:
        """Grow or shrink the enclosing window around the
        widget's grid (#976): two passes, the second taking
        the remainder the first left — the whole-client zoom
        writes the chrome's application font in the same step,
        and the font-metric chrome (the tab bars around the
        terminal) takes its share of the window between them.
        The fit holds the grid it entered with — the passes'
        window resizes deliver resize events that would
        re-derive the grid from pixels mid-fit and shrink the
        target — and :meth:`land_grid` lands the settled
        pixels' grid once at the end, so the child sees its
        entry columns and rows at the new cell size when the
        fit converges."""
        window = self.window()
        if not self.can_fit_window(window):
            return
        entered = (self.width(), self.height())
        self._fitting = True
        try:
            # Settle chrome metrics while resize events cannot change the
            # held grid. Deliver only font/layout events, never user input.
            QCoreApplication.sendPostedEvents(
                None, QEvent.Type.ApplicationFontChange
            )
            QCoreApplication.sendPostedEvents(None, QEvent.Type.LayoutRequest)
            for _ in range(2):
                if not self.fit_once(window):
                    return
        finally:
            self._fitting = False
            self.land_grid(entered)

    def derived_grid(self) -> tuple[int, int]:
        """The grid the widget's current pixels name (#1113):
        the scrollbar's lane sits right of the grid, so the
        columns derive from the width less the lane."""
        cols = max(1, (self.width() - paint.LANE_W) // self.metrics.cell_width)
        rows = max(1, self.height() // self.metrics.cell_height)
        return cols, rows

    def land_grid(self, entered: tuple[int, int]) -> None:
        """Land the grid the fit left (#976): the passes' own
        resize events were held off, and the settled pixels
        name the grid now — the entry grid when the fit
        converged, whatever the pixels hold when it could not.
        A widget whose pixels never moved (a window that
        carries no layout over it) keeps the grid it entered
        with whole."""
        if (self.width(), self.height()) == entered:
            return
        cols, rows = self.derived_grid()
        if (cols, rows) != (self.cols, self.rows):
            self.resize_grid(cols, rows)

    def can_fit_window(self, window) -> bool:
        """Whether fit_window may move this window (#976): a
        terminal that is its own window sizes nothing, a
        terminal the window does not show — hidden behind
        another tab, not yet laid out — sizes nothing, and a
        maximized frame keeps its pixels: no resize lands
        while it stays maximized, and the grid re-derives at
        the restore."""
        return (
            window is not self
            and self.isVisible()
            and not window.isMaximized()
        )

    def fit_once(self, window) -> bool:
        """One pass of the window's resize toward the grid
        (#976): the delta between the grid's pixel size and
        the widget's current pixels, one resize, and the
        layout settled for whoever measures next. Answers
        whether another pass can reach anything: no, when the
        pixels already answer the grid, and no, when the
        window carries no layout that moves the widget — a
        second pass would add the same delta to the window
        again and reach nothing."""
        width = (
            self.metrics.cell_width * self.cols + paint.LANE_W - self.width()
        )
        height = self.metrics.cell_height * self.rows - self.height()
        if not (width or height):
            return False
        held = (self.width(), self.height())
        window.resize(window.width() + width, window.height() + height)
        layout = window.layout()
        if layout is not None:
            layout.activate()
        return (self.width(), self.height()) != held

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
        # The guard is the teardown contract (#1034): a layout
        # that still owns a closed, hidden widget re-activates
        # and resizes it — the resize would size a grid on a
        # terminal the library already freed.
        if not self._closed:
            if not self._fitting:  # the window fit holds its grid
                cols, rows = self.derived_grid()
                if (cols, rows) != (self.cols, self.rows):
                    self.resize_grid(cols, rows)
            find.layout(self)
        super().resizeEvent(event)

    # --- painting -----------------------------------------------------

    def paintEvent(self, event) -> None:  # noqa: N802
        painter = QPainter(self)
        paint.paint_event(self, painter, event.region())
        painter.end()

    # --- keyboard -----------------------------------------------------

    def event(self, event: QEvent) -> bool:  # noqa: N802
        """The bare Esc belongs to the program inside (#1099):
        the workspace page stands a widget-scoped shortcut on
        the key — Esc goes Back — and Qt asks the focus widget
        first, with a ``ShortcutOverride`` event, whether the
        matching shortcut should yield. The accept keeps the
        press for the terminal: it reaches the encoder like
        every other key, and the shell reads its Esc. An
        override a focused child ignores — the find bar's
        edit — propagates here too, and the accept serves it
        the same way: the shortcut stays quiet and the edit's
        own Esc handler answers instead of the page's Back.
        Unlike Tab's refusal (#1066, :meth:`focusNextPrevChild`)
        the accept is not gated on ``hasFocus`` for exactly
        that reason.

        The claim names the bare Esc alone — a modified one
        or another key passes to the base, so the zoom keys
        and every other shortcut keep their answers."""
        if event.type() == QEvent.Type.ShortcutOverride:
            key = event.key()
            # Belt-and-braces: Qt only delivers the override
            # when a shortcut matches the press, and the page's
            # is unmodified — the mask answers a future
            # modified-Esc shortcut, not today's key.
            claimed = (
                Qt.KeyboardModifier.ControlModifier
                | Qt.KeyboardModifier.ShiftModifier
                | Qt.KeyboardModifier.AltModifier
                | Qt.KeyboardModifier.MetaModifier
            )
            if key == Qt.Key.Key_Escape and not event.modifiers() & claimed:
                event.accept()
                return True
        return super().event(event)

    def keyPressEvent(self, event: QKeyEvent) -> None:  # noqa: N802
        # The guard is the teardown contract (#1034): a shortcut
        # that closes the shell takes its key on the press
        # (#1163's Ctrl+Shift+W), and the release that follows
        # can reach this widget before its deleteLater runs —
        # the encoder would read a terminal the library already
        # freed.
        if self._closed:
            return
        if self.handle_shortcut(event):
            return
        action = input.key_action(event.isAutoRepeat())
        input.encode_key(self, event, action)

    def focusNextPrevChild(self, forward: bool) -> bool:  # noqa: N802
        """Tab and Shift+Tab belong to the program inside the
        terminal (#1066): Qt's default reroutes both into focus
        moves inside ``QWidget.event`` before the press reaches
        the encoder, and the shell's completion loses the key.
        Refuse the move while the terminal itself holds focus;
        a child that holds it instead (the find bar's edit)
        keeps the default traversal through ``super()``."""
        if self.hasFocus():
            return False
        return super().focusNextPrevChild(forward)

    def handle_shortcut(self, event: QKeyEvent) -> bool:
        """The widget's own keys before the encoder: buffer
        paging, the clipboard, and the find bar. ``True`` when
        the shortcut consumed the event. The font keys live at
        the application level now (#976, :mod:`..zoom`): they
        answer wherever the focus stands, not the terminal
        alone."""
        return (
            self.page_shortcut(event)
            or self.clipboard_shortcut(event)
            or self.find_shortcut(event)
        )

    def page_shortcut(self, event: QKeyEvent) -> bool:
        """Shift+PgUp/PgDn page the buffer locally instead of
        going to the program inside."""
        if not is_paging(event.modifiers(), event.key()):
            return False
        direction = -1 if event.key() == Qt.Key.Key_PageUp else 1
        input.scroll_page(self, direction)
        return True

    def clipboard_shortcut(self, event: QKeyEvent) -> bool:
        """Ctrl+Shift+C copies the selection (a terminal cannot
        cut, so X copies too); Ctrl+Shift+V pastes the
        clipboard."""
        if not (
            event.modifiers() & Qt.KeyboardModifier.ControlModifier
            and event.modifiers() & Qt.KeyboardModifier.ShiftModifier
        ):
            return False
        if event.key() in (Qt.Key.Key_C, Qt.Key.Key_X):
            select.copy(self)
            return True
        if event.key() == Qt.Key.Key_V:
            select.paste_clipboard(self)
            return True
        return False

    def find_shortcut(self, event: QKeyEvent) -> bool:
        """Ctrl+Shift+F toggles the find bar."""
        if not (
            event.modifiers() & Qt.KeyboardModifier.ControlModifier
            and event.modifiers() & Qt.KeyboardModifier.ShiftModifier
            and event.key() == Qt.Key.Key_F
        ):
            return False
        if self.search:
            find.close(self)
        else:
            find.open(self)
        return True

    def eventFilter(self, obj, event) -> bool:  # noqa: N802
        """The find bar's own keys: stepping and Escape."""
        if obj is self.find_edit and find.filter_key(self, event):
            return True
        return super().eventFilter(obj, event)

    def keyReleaseEvent(self, event: QKeyEvent) -> None:  # noqa: N802
        # A held key's platform repeats arrive as press events
        # only; the repeat's release is noise.
        if event.isAutoRepeat():
            return
        # The closed guard, the press's own (#1034).
        if self._closed:
            return
        input.encode_key(self, event, gt.ACTION_RELEASE)

    # --- mouse --------------------------------------------------------

    def mousePressEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        if not input.mouse_tracking(self) and self.press_without_tracking(
            event
        ):
            return
        input.send_mouse(
            self,
            gt.MOUSE_PRESS,
            input.button_for(event),
            event.position(),
            event.modifiers(),
            event.buttons(),
        )

    def press_without_tracking(self, event: QMouseEvent) -> bool:
        """The presses a terminal owns when the program inside
        does not report the mouse: Ctrl+Click opens a link,
        middle-click pastes PRIMARY, and a left press starts
        the selection gesture. Answers whether the press was
        consumed."""
        if self.link_press(event):
            return True
        if event.button() == Qt.MouseButton.MiddleButton:
            select.paste_primary(self)
            return True
        if event.button() == Qt.MouseButton.LeftButton:
            self.press_gesture(event)
            return True
        return False

    def link_press(self, event: QMouseEvent) -> bool:
        """A Ctrl+Click over a link opens it through the
        desktop handler; answers whether a link opened."""
        return (
            event.button() == Qt.MouseButton.LeftButton
            and bool(event.modifiers() & Qt.KeyboardModifier.ControlModifier)
            and links.open_at(self, event.position())
        )

    def press_gesture(self, event: QMouseEvent) -> bool:
        """A left press when the program inside does not own
        the mouse: count presses on one cell inside the
        multi-click window (two selects the word, three the
        line), start the autoscroll watch, and feed the press
        to the selection gesture. Answers whether the press
        was consumed for selection."""
        cell = input.cell_at(self, event.position())
        now = event.timestamp()
        repeat = (
            now - self.last_press_ms <= select.MULTI_CLICK_MS
            and cell == self.last_press_cell
        )
        self.press_count = self.press_count + 1 if repeat else 1
        self.last_press_ms = now
        self.last_press_cell = cell
        self.sel_dragged = False
        self.drag_position = event.position()
        self.autoscroll.start()
        if self.press_count == 2:
            select.word_at(self, event.position())
            return True
        if self.press_count >= 3:
            select.line_at(self, event.position())
            return True
        select.gesture(self, gt.SEL_PRESS, event.position())
        return True

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        if (
            event.button() == Qt.MouseButton.LeftButton
            and not input.mouse_tracking(self)
        ):
            self.autoscroll.stop()
            select.gesture(self, gt.SEL_RELEASE, event.position())
        input.send_mouse(
            self,
            gt.MOUSE_RELEASE,
            input.button_for(event),
            event.position(),
            event.modifiers(),
            event.buttons(),
        )

    def mouseMoveEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        if not input.mouse_tracking(self):
            if not event.buttons():
                links.set_hand(
                    self, links.under(self, event.position()) is not None
                )
            elif self.hand_cursor:
                links.set_hand(self, False)
            if event.buttons() & Qt.MouseButton.LeftButton:
                self.sel_dragged = True
                self.drag_position = event.position()
                select.gesture(self, gt.SEL_DRAG, event.position())
        input.send_mouse(
            self,
            gt.MOUSE_MOTION,
            input.button_for(event),
            event.position(),
            event.modifiers(),
            event.buttons(),
        )

    def leaveEvent(self, event) -> None:  # noqa: N802
        """The pointer left the terminal: the pointing hand is
        an application-global override cursor, and no further
        move events arrive to clear it — drop it here or the
        whole app keeps the hand."""
        if self.hand_cursor:
            links.set_hand(self, False)
        super().leaveEvent(event)

    def contextMenuEvent(self, event) -> None:  # noqa: N802
        """Right-click: Copy and Paste (dimmed when they would
        do nothing), plus Copy Link Address over a link. A
        program that owns the mouse sees the press forwarded
        instead."""
        if input.mouse_tracking(self):
            event.ignore()
            return
        self.build_context_menu(event.pos()).exec(event.globalPos())

    def build_context_menu(self, position):
        menu = QMenu(self)
        link = links.under(self, position) if position is not None else None
        if link:
            copy_link = menu.addAction("Copy Link Address")
            copy_link.triggered.connect(
                lambda: QApplication.clipboard().setText(link)
            )
            menu.addSeparator()
        copy_action = menu.addAction("Copy\tCtrl+Shift+C")
        copy_action.setEnabled(bool(select.text(self)))
        copy_action.triggered.connect(lambda: select.copy(self))
        paste_action = menu.addAction("Paste\tCtrl+Shift+V")
        paste_action.setEnabled(bool(QApplication.clipboard().text()))
        paste_action.triggered.connect(lambda: select.paste_clipboard(self))
        return menu

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
        # The guard is the teardown contract (#1034): closing
        # frees the emulator handles, and the hide the close
        # performs delivers Qt's focus events after them — an
        # unguarded report reads a terminal the library already
        # freed, and the read is the crash that took the suite
        # down on every quit that closed a focused terminal.
        if not self._closed:
            input.send_focus(self, True)
        super().focusInEvent(event)

    def focusOutEvent(self, event: QFocusEvent) -> None:  # noqa: N802
        if not self._closed:
            input.send_focus(self, False)
        super().focusOutEvent(event)
