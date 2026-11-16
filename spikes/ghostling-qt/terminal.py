"""The terminal widget: a cell grid rendered with QPainter from
libghostty-vt's render state, mirroring Ghostling's renderer with Qt
in place of Raylib.

Supported: text with 8/16/256/truecolor, styles (bold, italic,
inverse, faint), the cursor, scrollback with a scrollbar, resize with
reflow, the Kitty keyboard protocol (through the key encoder), mouse
reporting (through the mouse encoder), focus reporting, and OSC title
changes. Out of scope for this spike: Kitty graphics, text selection,
bracketed paste, and scrollbar dragging.
"""

from __future__ import annotations

import ctypes
import os
import string
from pathlib import Path

import ghostty
from PySide6.QtCore import (
    QPointF,
    QRect,
    QSize,
    QSocketNotifier,
    Qt,
    QTimer,
    Signal,
)
from PySide6.QtGui import (
    QColor,
    QFont,
    QFontDatabase,
    QFontInfo,
    QFontMetrics,
    QPainter,
)
from PySide6.QtWidgets import QApplication, QWidget
from session import PtySession

# Widgets registered for effects callbacks, keyed by the userdata id.
_REGISTRY: dict[int, TerminalWidget] = {}


def _qt_key_to_ghostty() -> dict[int, int]:
    """Map Qt.Key codes to GhosttyKey codes."""
    mapping: dict[int, int] = {}
    for letter in string.ascii_uppercase:
        mapping[int(getattr(Qt.Key, "Key_" + letter))] = ghostty.KEY[letter]
    for digit in string.digits:
        mapping[Qt.Key(int("0" + digit))] = ghostty.KEY["DIGIT_" + digit]
    for n in range(1, 26):
        mapping[int(getattr(Qt.Key, f"Key_F{n}"))] = ghostty.KEY[f"F{n}"]
    named = {
        Qt.Key.Key_Return: "ENTER",
        Qt.Key.Key_Enter: "ENTER",
        Qt.Key.Key_Tab: "TAB",
        Qt.Key.Key_Backspace: "BACKSPACE",
        Qt.Key.Key_Delete: "DELETE",
        Qt.Key.Key_Escape: "ESCAPE",
        Qt.Key.Key_Space: "SPACE",
        Qt.Key.Key_Up: "ARROW_UP",
        Qt.Key.Key_Down: "ARROW_DOWN",
        Qt.Key.Key_Left: "ARROW_LEFT",
        Qt.Key.Key_Right: "ARROW_RIGHT",
        Qt.Key.Key_Home: "HOME",
        Qt.Key.Key_End: "END",
        Qt.Key.Key_PageUp: "PAGE_UP",
        Qt.Key.Key_PageDown: "PAGE_DOWN",
        Qt.Key.Key_Insert: "INSERT",
        Qt.Key.Key_Minus: "MINUS",
        Qt.Key.Key_Equal: "EQUAL",
        Qt.Key.Key_BracketLeft: "BRACKET_LEFT",
        Qt.Key.Key_BracketRight: "BRACKET_RIGHT",
        Qt.Key.Key_Backslash: "BACKSLASH",
        Qt.Key.Key_Semicolon: "SEMICOLON",
        Qt.Key.Key_Apostrophe: "QUOTE",
        Qt.Key.Key_Comma: "COMMA",
        Qt.Key.Key_Period: "PERIOD",
        Qt.Key.Key_Slash: "SLASH",
        Qt.Key.Key_QuoteLeft: "BACKQUOTE",
        Qt.Key.Key_CapsLock: "CAPS_LOCK",
        Qt.Key.Key_ScrollLock: "SCROLL_LOCK",
        Qt.Key.Key_Pause: "PAUSE",
        Qt.Key.Key_Print: "PRINT_SCREEN",
        Qt.Key.Key_Menu: "CONTEXT_MENU",
        # Shifted-symbol codes: on some platforms (notably Wayland)
        # Shift+; arrives as Key_Colon rather than Key_Semicolon with
        # a shift modifier, so the shifted symbols need their own
        # entries, mapped to the base key whose unshifted codepoint the
        # encoder expects.
        Qt.Key.Key_Colon: "SEMICOLON",
        Qt.Key.Key_AsciiTilde: "BACKQUOTE",
        Qt.Key.Key_Bar: "BACKSLASH",
        Qt.Key.Key_QuoteDbl: "QUOTE",
        Qt.Key.Key_Less: "COMMA",
        Qt.Key.Key_Greater: "PERIOD",
        Qt.Key.Key_Question: "SLASH",
        Qt.Key.Key_BraceLeft: "BRACKET_LEFT",
        Qt.Key.Key_BraceRight: "BRACKET_RIGHT",
        Qt.Key.Key_Underscore: "MINUS",
        Qt.Key.Key_Plus: "EQUAL",
    }
    for qt, name in named.items():
        mapping[int(qt)] = ghostty.KEY[name]
    return mapping


# Unshifted codepoints (US layout). The Kitty keyboard protocol needs
# them to identify keys independent of the modifier state.
_UNSHIFTED = {ghostty.KEY["SPACE"]: ord(" ")}
for _letter in string.ascii_lowercase:
    _UNSHIFTED[ghostty.KEY[_letter.upper()]] = ord(_letter)
for _digit in string.digits:
    _UNSHIFTED[ghostty.KEY["DIGIT_" + _digit]] = ord(_digit)
for _name, _char in {
    "BACKQUOTE": "`",
    "MINUS": "-",
    "EQUAL": "=",
    "BRACKET_LEFT": "[",
    "BRACKET_RIGHT": "]",
    "BACKSLASH": "\\",
    "SEMICOLON": ";",
    "QUOTE": "'",
    "COMMA": ",",
    "PERIOD": ".",
    "SLASH": "/",
}.items():
    _UNSHIFTED[ghostty.KEY[_name]] = ord(_char)

_QT_KEYS = _qt_key_to_ghostty()

_DEBUG_KEYS = bool(os.environ.get("GHOSTLING_DEBUG_KEYS"))
_DEBUG_PAINT = bool(os.environ.get("GHOSTLING_DEBUG_PAINT"))

_QT_TO_GHOSTTY_BUTTON = {
    Qt.MouseButton.LeftButton: ghostty.MOUSE_LEFT,
    Qt.MouseButton.RightButton: ghostty.MOUSE_RIGHT,
    Qt.MouseButton.MiddleButton: ghostty.MOUSE_MIDDLE,
}


def _mods(modifiers) -> int:
    mods = 0
    if modifiers & Qt.KeyboardModifier.ShiftModifier:
        mods |= ghostty.MOD_SHIFT
    if modifiers & Qt.KeyboardModifier.ControlModifier:
        mods |= ghostty.MOD_CTRL
    if modifiers & Qt.KeyboardModifier.AltModifier:
        mods |= ghostty.MOD_ALT
    if modifiers & Qt.KeyboardModifier.MetaModifier:
        mods |= ghostty.MOD_SUPER
    return mods


def _rgb(color: ghostty.ColorRgb) -> QColor:
    return QColor(color.r, color.g, color.b)


# --- effects callbacks (invoked from inside libghostty) ----------------


def _widget(userdata) -> TerminalWidget | None:
    return _REGISTRY.get(userdata) if userdata else None


@ghostty.WRITE_PTY_FN
def _write_pty(_terminal, userdata, data, length):
    widget = _widget(userdata)
    if widget is not None and data:
        widget.write(bytes(bytearray(data[:length])))


@ghostty.SIZE_FN
def _report_size(_terminal, userdata, out):
    widget = _widget(userdata)
    if widget is None:
        return False
    out.contents.rows = widget.rows
    out.contents.columns = widget.cols
    out.contents.cell_width = widget.cell_width
    out.contents.cell_height = widget.cell_height
    return True


@ghostty.DEVICE_ATTRIBUTES_FN
def _device_attributes(_terminal, _userdata, out):
    attrs = out.contents
    attrs.primary.conformance_level = ghostty.DA_CONFORMANCE_VT220
    features = [
        ghostty.DA_FEATURE_COLUMNS_132,
        ghostty.DA_FEATURE_SELECTIVE_ERASE,
        ghostty.DA_FEATURE_ANSI_COLOR,
    ]
    for index, feature in enumerate(features):
        attrs.primary.features[index] = feature
    attrs.primary.num_features = len(features)
    attrs.secondary.device_type = ghostty.DA_DEVICE_TYPE_VT220
    attrs.secondary.firmware_version = 1
    attrs.secondary.rom_cartridge = 0
    attrs.tertiary.unit_id = 0
    return True


@ghostty.TITLE_CHANGED_FN
def _title_changed(_terminal, userdata):
    widget = _widget(userdata)
    if widget is None:
        return
    ok, title = widget.lib.get(
        widget.lib.terminal_get,
        widget.terminal,
        ghostty.DATA_TITLE,
        ghostty.GhosttyString(),
    )
    if ok:
        widget.title_changed.emit(title.text())


@ghostty.COLOR_SCHEME_FN
def _color_scheme(_terminal, _userdata, _out):
    return False


class TerminalWidget(QWidget):
    """A terminal screen: libghostty-vt state plus a PTY session."""

    title_changed = Signal(str)
    process_exited = Signal(int)

    def __init__(
        self, argv: list[str] | None = None, parent: QWidget | None = None
    ) -> None:
        super().__init__(parent)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setMouseTracking(True)
        self.setAttribute(Qt.WidgetAttribute.WA_OpaquePaintEvent)

        # Font and cell metrics. Konsole's TerminalDisplay is the
        # reference: the cell width is the average advance over a
        # representative ASCII run (robust when the resolved font is
        # not truly fixed-pitch), the cell height is ascent + descent,
        # and glyphs draw baseline-anchored at the cell's left edge.
        # Centering glyphs inside the cell rect makes narrow ones
        # (i, l, 1) drift and pads the lines — the spacing klangk hit
        # as "measure with one font, render with another" (#2), so
        # resolve an explicit family before measuring, never the
        # generic hint alone.
        # Register the vendored JetBrains Mono (OFL 1.1, the face
        # Ghostty itself ships) before any font work, so the measured
        # metrics and the rendered glyphs come from the same face on
        # every host — no system font dependency. Measuring one font
        # and rendering another is the spacing bug klangk's frontend
        # fixed in its flterm terminal (mcdonc/klangk#2).
        for name in ("Regular", "Bold", "Italic", "BoldItalic"):
            path = (
                Path(__file__).resolve().parent
                / "vendor"
                / "fonts"
                / f"JetBrainsMono-{name}.ttf"
            )
            if path.exists():
                QFontDatabase.addApplicationFont(str(path))
        families = QFontDatabase.families()
        preferred = next(
            (
                name
                for name in [
                    "JetBrains Mono",
                    "UbuntuMono Nerd Font Mono",
                    "Fira Code",
                    "Cascadia Mono",
                    "DejaVu Sans Mono",
                    "Liberation Mono",
                    "Monospace",
                ]
                if name in families
            ),
            "Monospace",
        )
        self.font = QFont(preferred)
        self.font.setStyleHint(QFont.StyleHint.TypeWriter)
        # 15pt = 12x26 logical cells: the width matches the reference
        # Konsole grid exactly and the height is as close as JetBrains
        # Mono's line box allows (Konsole's face packs 24px rows; JBM
        # needs 26). Keeps the default 80x24 window in Konsole's size
        # class. Point size also scales cleanly with a window's device
        # pixel ratio on a fractionally scaled Wayland surface.
        self.font.setPointSize(15)
        metrics = QFontMetrics(self.font)
        repchar = (
            "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
            "abcdefgjijklmnopqrstuvwxyz"
            "0123456789./+@"
        )
        self.cell_width = max(
            1, round(metrics.horizontalAdvance(repchar) / len(repchar))
        )
        # Cap the row height at 1.2 em: JetBrains Mono's natural line
        # box is about 1.3 em, which makes an 80x24 window a size class
        # above the Konsole reference. Terminals ship exactly this trim
        # as their line-height setting; 1.2 em lands rows at 24 px,
        # matching the reference grid.
        em = QFontInfo(self.font).pixelSize()
        self.cell_height = max(1, min(metrics.height(), round(em * 1.2)))
        self._ascent = metrics.ascent()
        self._fonts = self._font_variants()

        self.cols, self.rows = 80, 24
        self.lib = ghostty.Lib()
        self.terminal = self.lib.new_handle(
            self.lib.terminal_new, self.cols, self.rows
        )
        scrollback = ctypes.c_size_t(10_000)
        self.lib.terminal_set(
            self.terminal,
            ghostty.OPT_SCROLLBACK_MAX_LINES,
            ctypes.byref(scrollback),
        )
        self.lib.terminal_resize(
            self.terminal,
            self.cols,
            self.rows,
            self.cell_width,
            self.cell_height,
        )
        self._register_effects()

        self.session = PtySession(
            argv, self.cols, self.rows, self.cell_width, self.cell_height
        )
        self.key_encoder = self.lib.new_handle(self.lib.key_encoder_new)
        self.key_event = self.lib.new_handle(self.lib.key_event_new)
        self.mouse_encoder = self.lib.new_handle(self.lib.mouse_encoder_new)
        self.mouse_event = self.lib.new_handle(self.lib.mouse_event_new)
        # Selection state: gestures drive libghostty's selection,
        # which is anchored to the grid, so the selection survives
        # scrolling and reflow. Apps that enable mouse tracking own
        # the mouse, and selection pauses while they do.
        self._sel_dragged = False
        self.render_state = self.lib.new_handle(self.lib.render_state_new)
        self.row_iterator = self.lib.new_handle(self.lib.row_iterator_new)
        self.row_cells = self.lib.new_handle(self.lib.row_cells_new)
        self.selection_gesture = self.lib.new_handle(
            self.lib.selection_gesture_new
        )

        self.notifier = QSocketNotifier(
            self.session.master_fd or 0, QSocketNotifier.Type.Read, self
        )
        self.notifier.activated.connect(self._on_readable)
        self._reaper = QTimer(self)
        self._reaper.timeout.connect(self._on_reap)

    # --- lifecycle ----------------------------------------------------

    def _register_effects(self) -> None:
        uid = id(self)
        _REGISTRY[uid] = self
        registrations = [
            (ghostty.OPT_USERDATA, ctypes.c_void_p(uid)),
            (ghostty.OPT_WRITE_PTY, ctypes.cast(_write_pty, ctypes.c_void_p)),
            (ghostty.OPT_SIZE, ctypes.cast(_report_size, ctypes.c_void_p)),
            (
                ghostty.OPT_TITLE_CHANGED,
                ctypes.cast(_title_changed, ctypes.c_void_p),
            ),
            (
                ghostty.OPT_COLOR_SCHEME,
                ctypes.cast(_color_scheme, ctypes.c_void_p),
            ),
            (
                ghostty.OPT_DEVICE_ATTRIBUTES,
                ctypes.cast(_device_attributes, ctypes.c_void_p),
            ),
        ]
        for option, value in registrations:
            self.lib.terminal_set(self.terminal, option, value)

    def closeEvent(self, event) -> None:  # noqa: N802 (Qt name)
        _REGISTRY.pop(id(self), None)
        self._reaper.stop()
        self.notifier.setEnabled(False)
        self.session.close()
        for free, handle in [
            (self.lib.row_cells_free, self.row_cells),
            (self.lib.row_iterator_free, self.row_iterator),
            (self.lib.render_state_free, self.render_state),
            (self.lib.mouse_event_free, self.mouse_event),
            (self.lib.mouse_encoder_free, self.mouse_encoder),
            (self.lib.key_event_free, self.key_event),
            (self.lib.key_encoder_free, self.key_encoder),
        ]:
            free(handle)
        # The gesture free takes the terminal it was created against.
        self.lib.selection_gesture_free(self.selection_gesture, self.terminal)
        self.lib.terminal_free(self.terminal)
        super().closeEvent(event)

    # --- data flow ----------------------------------------------------

    def _on_readable(self) -> None:
        data = self.session.drain()
        if data is None:
            self.notifier.setEnabled(False)
            self._reaper.start(100)
            self.refresh()
            self.process_exited.emit(self.session.exit_status or 0)
            return
        if data:
            self.lib.vt_write(self.terminal, data)
            self.refresh()

    def _on_reap(self) -> None:
        self.session.reap()
        if self.session.child_pid is None:
            self._reaper.stop()

    def write(self, data: bytes) -> None:
        self.session.write(data)

    def refresh(self) -> None:
        self.lib.render_state_update(self.render_state, self.terminal)
        self.update()

    # --- rendering ----------------------------------------------------

    def _font_variants(self) -> dict[str, QFont]:
        def variant(bold: bool, italic: bool) -> QFont:
            font = QFont(self.font)
            font.setBold(bold)
            font.setItalic(italic)
            return font

        return {
            "normal": variant(False, False),
            "bold": variant(True, False),
            "italic": variant(False, True),
            "bold_italic": variant(True, True),
        }

    def sizeHint(self) -> QSize:  # noqa: N802 (Qt name)
        return QSize(self.cell_width * self.cols, self.cell_height * self.rows)

    def paintEvent(self, event) -> None:  # noqa: N802 (Qt name)
        painter = QPainter(self)
        if _DEBUG_PAINT and not globals().get("_PAINT_PROBED", False):
            globals()["_PAINT_PROBED"] = True
            device = painter.device()
            transform = painter.combinedTransform()
            print(
                f"paint-probe: "
                f"widget_dpr={self.devicePixelRatioF():.3f} "
                f"device={type(device).__name__} "
                f"device_dpr={device.devicePixelRatio():.3f} "
                f"viewport={painter.viewport().width()}"
                f"x{painter.viewport().height()} "
                f"window={painter.window().width()}"
                f"x{painter.window().height()} "
                f"transform=({transform.m11():.3f}, "
                f"{transform.m22():.3f})",
                flush=True,
            )
        colors = ghostty.RenderStateColors()
        colors.size = ctypes.sizeof(colors)
        if not self.lib.get(
            self.lib.render_state_get,
            self.render_state,
            ghostty.RS_COLORS,
            colors,
        )[0]:
            painter.end()
            return
        background = _rgb(colors.background)
        painter.fillRect(self.rect(), background)

        ok, iterator = self.lib.get(
            self.lib.render_state_get,
            self.render_state,
            ghostty.RS_ROW_ITERATOR,
            ctypes.c_void_p(self.row_iterator),
        )
        if not ok or not iterator.value:
            self._paint_exit_banner(painter)
            painter.end()
            return

        style = ghostty.Style()
        codepoints = (ctypes.c_uint32 * 16)()
        y = 0
        while self.lib.row_iterator_next(iterator.value):
            ok, cells = self.lib.get(
                self.lib.row_get,
                iterator.value,
                ghostty.ROW_CELLS,
                ctypes.c_void_p(self.row_cells),
            )
            if not ok or not cells.value:
                continue
            x = 0
            while self.lib.row_cells_next(cells):
                self._paint_cell(
                    painter, cells, colors, style, codepoints, x, y
                )
                x += self.cell_width
            clean = ctypes.c_bool(False)
            self.lib.row_set(iterator, ghostty.ROW_DIRTY, ctypes.byref(clean))
            y += self.cell_height

        self._paint_cursor(painter, colors)
        self._paint_scrollbar(painter)
        self._paint_exit_banner(painter)
        painter.end()

    def _paint_cell(
        self, painter, cells, colors, style, codepoints, x: int, y: int
    ) -> None:
        lib = self.lib
        ok, length = lib.get(
            lib.row_cells_get,
            cells,
            ghostty.CELLS_GRAPHEMES_LEN,
            ctypes.c_uint32(),
        )
        if not ok or length.value == 0:
            ok, bg = lib.get(
                lib.row_cells_get,
                cells,
                ghostty.CELLS_BG_COLOR,
                ghostty.ColorRgb(),
            )
            if ok:
                painter.fillRect(
                    x, y, self.cell_width, self.cell_height, _rgb(bg)
                )
            return
        text = ""
        if length.value <= 16:
            lib.row_cells_get(
                cells,
                ghostty.CELLS_GRAPHEMES_BUF,
                ctypes.cast(codepoints, ctypes.c_void_p),
            )
            text = "".join(chr(codepoints[i]) for i in range(length.value))
        ok, fg = lib.get(
            lib.row_cells_get,
            cells,
            ghostty.CELLS_FG_COLOR,
            ghostty.ColorRgb(),
        )
        foreground = _rgb(fg if ok else colors.foreground)
        ok, bg = lib.get(
            lib.row_cells_get,
            cells,
            ghostty.CELLS_BG_COLOR,
            ghostty.ColorRgb(),
        )
        has_bg = ok
        background = _rgb(bg if ok else colors.background)
        style.size = ctypes.sizeof(style)
        lib.row_cells_get(cells, ghostty.CELLS_STYLE, ctypes.byref(style))
        if style.inverse:
            foreground, background = background, foreground
            has_bg = True
        ok, selected = lib.get(
            lib.row_cells_get,
            cells,
            ghostty.CELLS_SELECTED,
            ctypes.c_bool(),
        )
        if ok and selected.value:
            foreground, background = background, foreground
            has_bg = True
        if has_bg:
            painter.fillRect(
                x, y, self.cell_width, self.cell_height, background
            )
        if style.faint:
            foreground.setAlpha(120)
        # _font_key returns the variants-dict key; passing the bare
        # string to setFont would silently install it as a family name
        # and every glyph would render in the default Sans Serif.
        painter.setFont(self._fonts[self._font_key(style)])
        painter.setPen(foreground)
        painter.drawText(QPointF(x, y + self._ascent), text)

    def _font_key(self, style) -> str:
        if style.bold and style.italic:
            return "bold_italic"
        if style.bold:
            return "bold"
        if style.italic:
            return "italic"
        return "normal"

    def _paint_cursor(self, painter, colors) -> None:
        lib = self.lib
        ok, visible = lib.get(
            lib.render_state_get,
            self.render_state,
            ghostty.RS_CURSOR_VISIBLE,
            ctypes.c_bool(),
        )
        ok2, present = lib.get(
            lib.render_state_get,
            self.render_state,
            ghostty.RS_CURSOR_VIEWPORT_HAS_VALUE,
            ctypes.c_bool(),
        )
        if not (ok and ok2 and visible.value and present.value):
            return
        _, cx = lib.get(
            lib.render_state_get,
            self.render_state,
            ghostty.RS_CURSOR_VIEWPORT_X,
            ctypes.c_uint16(),
        )
        _, cy = lib.get(
            lib.render_state_get,
            self.render_state,
            ghostty.RS_CURSOR_VIEWPORT_Y,
            ctypes.c_uint16(),
        )
        color = _rgb(
            colors.cursor if colors.cursor_has_value else colors.foreground
        )
        color.setAlpha(140)
        painter.fillRect(
            cx.value * self.cell_width,
            cy.value * self.cell_height,
            self.cell_width,
            self.cell_height,
            color,
        )

    def _paint_scrollbar(self, painter) -> None:
        ok, bar = self.lib.get(
            self.lib.terminal_get,
            self.terminal,
            ghostty.DATA_SCROLLBAR,
            ghostty.Scrollbar(),
        )
        if not ok or bar.total <= bar.len:
            return
        track = max(1, self.height() - 20)
        thumb = max(10, int(track * bar.len / bar.total))
        offset = int(track * bar.offset / max(1, bar.total - bar.len))
        painter.fillRect(
            self.width() - 8, 10 + offset, 6, thumb, QColor(160, 160, 160, 130)
        )

    def _paint_exit_banner(self, painter) -> None:
        if not self.session.exited:
            return
        status = self.session.exit_status
        message = (
            f"[process exited with status {status}]"
            if status is not None
            else "[process exited]"
        )
        height = self.cell_height + 8
        rect = QRect(0, self.height() - height, self.width(), height)
        painter.fillRect(rect, QColor(0, 0, 0, 180))
        painter.setPen(QColor(255, 255, 255))
        painter.setFont(self._fonts["normal"])
        painter.drawText(rect, Qt.AlignmentFlag.AlignCenter, message)

    # --- input --------------------------------------------------------

    def _mode_enabled(self, mode_id: int) -> bool:
        mode = ghostty.ModeConfig()
        mode.mode = mode_id
        ok, mode = self.lib.get(
            self.lib.terminal_get, self.terminal, ghostty.DATA_MODE, mode
        )
        return bool(ok and mode.value)

    def _mouse_tracking(self) -> bool:
        ok, tracking = self.lib.get(
            self.lib.terminal_get,
            self.terminal,
            ghostty.DATA_MOUSE_TRACKING,
            ctypes.c_bool(),
        )
        return bool(ok and tracking.value)

    def _cell_at(self, position: QPointF) -> tuple[int, int]:
        col = max(0, min(self.cols - 1, int(position.x()) // self.cell_width))
        row = max(0, min(self.rows - 1, int(position.y()) // self.cell_height))
        return col, row

    def _selection_gesture(self, kind: int, position: QPointF) -> None:
        """Feed a selection gesture event, following the protocol of
        Ghostty's c-vt-selection-gesture example: every event carries
        the grid ref under the cursor and the pixel position; a drag
        also carries surface geometry and is the only event that
        produces a selection, which we apply to the terminal so the
        render state flags the selected cells."""
        lib = self.lib
        col, row = self._cell_at(position)
        point = ghostty.Point(
            tag=ghostty.POINT_VIEWPORT,
            value=ghostty.PointValue(
                coordinate=ghostty.PointCoordinate(x=col, y=row)
            ),
        )
        ref = ghostty.GridRef()
        ref.size = ctypes.sizeof(ref)
        if (
            lib.terminal_grid_ref(
                self.terminal,
                point,
                ctypes.byref(ref),
            )
            != ghostty.SUCCESS
        ):
            return
        event = lib.new_handle(lib.selection_gesture_event_new, kind)
        try:
            lib.selection_gesture_event_set(
                event, ghostty.SEL_EVENT_OPT_REF, ctypes.byref(ref)
            )
            surface = ghostty.SurfacePosition(
                x=float(position.x()), y=float(position.y())
            )
            lib.selection_gesture_event_set(
                event,
                ghostty.SEL_EVENT_OPT_POSITION,
                ctypes.byref(surface),
            )
            if kind == ghostty.SEL_DRAG:
                geometry = ghostty.SelectionGestureGeometry(
                    columns=self.cols,
                    cell_width=self.cell_width,
                    padding_left=0,
                    screen_height=self.height(),
                )
                lib.selection_gesture_event_set(
                    event,
                    ghostty.SEL_EVENT_OPT_GEOMETRY,
                    ctypes.byref(geometry),
                )
            selection = ghostty.Selection()
            selection.size = ctypes.sizeof(selection)
            result = lib.selection_gesture_event(
                self.selection_gesture,
                self.terminal,
                event,
                ctypes.byref(selection),
            )
            if kind == ghostty.SEL_DRAG and result == ghostty.SUCCESS:
                lib.terminal_set(
                    self.terminal,
                    ghostty.OPT_SELECTION,
                    ctypes.byref(selection),
                )
            elif kind == ghostty.SEL_RELEASE:
                dragged = ctypes.c_bool()
                ok = lib.selection_gesture_get(
                    self.selection_gesture,
                    self.terminal,
                    ghostty.SEL_DATA_DRAGGED,
                    ctypes.byref(dragged),
                )
                if ok == ghostty.SUCCESS and not dragged.value:
                    # A click without a drag clears the selection.
                    lib.terminal_set(
                        self.terminal, ghostty.OPT_SELECTION, None
                    )
            if kind in (ghostty.SEL_DRAG, ghostty.SEL_RELEASE):
                self.refresh()
        finally:
            lib.selection_gesture_event_free(event)

    def _selection_text(self) -> str:
        """The active selection as plain text, straight from the
        library's formatter (trim on, like a terminal copy)."""
        lib = self.lib
        selection = ghostty.Selection()
        selection.size = ctypes.sizeof(selection)
        ok, _ = lib.get(
            lib.terminal_get, self.terminal, ghostty.DATA_SELECTION, selection
        )
        if not ok:
            return ""
        options = ghostty.SelectionFormatOptions(
            size=ctypes.sizeof(ghostty.SelectionFormatOptions),
            emit=ghostty.FORMATTER_PLAIN,
            unwrap=False,
            trim=True,
            selection=ctypes.pointer(selection),
        )
        size = 4096
        while size <= 1 << 20:
            buf = ctypes.create_string_buffer(size)
            written = ctypes.c_size_t()
            result = lib.selection_format_buf(
                self.terminal, options, buf, size, ctypes.byref(written)
            )
            if result == ghostty.SUCCESS:
                return buf.raw[: written.value].decode(
                    "utf-8", errors="replace"
                )
            size *= 4
        return ""

    def _copy_selection(self) -> None:
        text = self._selection_text()
        if text:
            QApplication.clipboard().setText(text)

    def _paste_clipboard(self) -> None:
        text = QApplication.clipboard().text()
        if not text:
            return
        if self._mode_enabled(ghostty.MODE_BRACKETED_PASTE):
            self.write(b"\x1b[200~" + text.encode("utf-8") + b"\x1b[201~")
        else:
            # Plain paste: shells execute lines, so newlines become
            # carriage returns like every terminal's raw paste.
            self.write(text.replace("\n", "\r").encode("utf-8"))

    def _encode_key(self, event, action: int) -> None:
        key = _QT_KEYS.get(int(event.key()))
        if key is None:
            # Keys outside the table (shifted symbols the platform
            # reports as their own codes, layouts beyond the map) go to
            # the pty as their text — the same fallback Ghostling uses
            # so platform-resolved input is never silently dropped.
            if action != ghostty.ACTION_RELEASE:
                text = event.text()
                if text and all(ch >= " " for ch in text):
                    self.write(text.encode("utf-8"))
            return
        lib = self.lib
        lib.key_encoder_setopt_from_terminal(self.key_encoder, self.terminal)
        mods = _mods(event.modifiers())
        lib.key_event_set_key(self.key_event, key)
        lib.key_event_set_action(self.key_event, action)
        lib.key_event_set_mods(self.key_event, mods)
        unshifted = _UNSHIFTED.get(key, 0)
        lib.key_event_set_unshifted_codepoint(self.key_event, unshifted)
        consumed = (
            ghostty.MOD_SHIFT
            if (unshifted and mods & ghostty.MOD_SHIFT)
            else 0
        )
        lib.key_event_set_consumed_mods(self.key_event, consumed)
        text = event.text()
        if (
            action != ghostty.ACTION_RELEASE
            and text
            and all(ch >= " " for ch in text)
        ):
            encoded = text.encode("utf-8")
            lib.key_event_set_utf8(self.key_event, encoded, len(encoded))
        else:
            lib.key_event_set_utf8(self.key_event, None, 0)
        data = lib.encode_key(self.key_encoder, self.key_event)
        if data:
            self.write(data)

    def keyPressEvent(self, event) -> None:  # noqa: N802 (Qt name)
        if _DEBUG_KEYS:
            print(
                f"key: code=0x{event.key():x} text={event.text()!r} "
                f"mods={event.modifiers()!r} "
                f"auto={event.isAutoRepeat()}",
                flush=True,
            )
        # Clipboard shortcuts before encoding: copy/cut (a terminal
        # cannot cut, so cut copies) and paste.
        if (
            event.modifiers() & Qt.KeyboardModifier.ControlModifier
            and event.modifiers() & Qt.KeyboardModifier.ShiftModifier
        ):
            if event.key() in (Qt.Key.Key_C, Qt.Key.Key_X):
                self._copy_selection()
                return
            if event.key() == Qt.Key.Key_V:
                self._paste_clipboard()
                return
        action = (
            ghostty.ACTION_REPEAT
            if event.isAutoRepeat()
            else ghostty.ACTION_PRESS
        )
        self._encode_key(event, action)

    def keyReleaseEvent(self, event) -> None:  # noqa: N802 (Qt name)
        if event.isAutoRepeat():
            return
        self._encode_key(event, ghostty.ACTION_RELEASE)

    def _send_mouse(
        self,
        action: int,
        button: int | None,
        position: QPointF,
        modifiers,
        buttons,
    ) -> None:
        lib = self.lib
        lib.mouse_encoder_setopt_from_terminal(
            self.mouse_encoder, self.terminal
        )
        size = ghostty.MouseEncoderSize()
        size.size = ctypes.sizeof(size)
        size.screen_width = self.width()
        size.screen_height = self.height()
        size.cell_width = self.cell_width
        size.cell_height = self.cell_height
        lib.mouse_encoder_setopt(
            self.mouse_encoder, ghostty.MOPT_SIZE, ctypes.byref(size)
        )
        track_cell = ctypes.c_bool(True)
        lib.mouse_encoder_setopt(
            self.mouse_encoder,
            ghostty.MOPT_TRACK_LAST_CELL,
            ctypes.byref(track_cell),
        )
        any_pressed = ctypes.c_bool(bool(buttons))
        lib.mouse_encoder_setopt(
            self.mouse_encoder,
            ghostty.MOPT_ANY_BUTTON_PRESSED,
            ctypes.byref(any_pressed),
        )
        lib.mouse_event_set_mods(self.mouse_event, _mods(modifiers))
        lib.mouse_event_set_action(self.mouse_event, action)
        if button is None:
            lib.mouse_event_clear_button(self.mouse_event)
        else:
            lib.mouse_event_set_button(self.mouse_event, button)
        lib.mouse_event_set_position(
            self.mouse_event, ghostty.MousePosition(position.x(), position.y())
        )
        data = lib.encode_mouse(self.mouse_encoder, self.mouse_event)
        if data:
            self.write(data)

    @staticmethod
    def _button(event) -> int | None:
        for qt_button, ghostty_button in _QT_TO_GHOSTTY_BUTTON.items():
            if event.button() & qt_button:
                return ghostty_button
        return None

    def mousePressEvent(self, event) -> None:  # noqa: N802 (Qt name)
        if (
            event.button() == Qt.MouseButton.LeftButton
            and not self._mouse_tracking()
        ):
            self._sel_dragged = False
            self._selection_gesture(ghostty.SEL_PRESS, event.position())
        self._send_mouse(
            ghostty.MOUSE_PRESS,
            self._button(event),
            event.position(),
            event.modifiers(),
            event.buttons(),
        )

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802 (Qt name)
        if (
            event.button() == Qt.MouseButton.LeftButton
            and not self._mouse_tracking()
        ):
            self._selection_gesture(ghostty.SEL_RELEASE, event.position())
        self._send_mouse(
            ghostty.MOUSE_RELEASE,
            self._button(event),
            event.position(),
            event.modifiers(),
            event.buttons(),
        )

    def mouseMoveEvent(self, event) -> None:  # noqa: N802 (Qt name)
        if (
            event.buttons() & Qt.MouseButton.LeftButton
            and not self._mouse_tracking()
        ):
            self._sel_dragged = True
            self._selection_gesture(ghostty.SEL_DRAG, event.position())
        self._send_mouse(
            ghostty.MOUSE_MOTION,
            self._button(event),
            event.position(),
            event.modifiers(),
            event.buttons(),
        )

    def wheelEvent(self, event) -> None:  # noqa: N802 (Qt name)
        steps = event.angleDelta().y() // 120
        if steps == 0:
            return
        ok, tracking = self.lib.get(
            self.lib.terminal_get,
            self.terminal,
            ghostty.DATA_MOUSE_TRACKING,
            ctypes.c_bool(),
        )
        if ok and tracking.value:
            button = ghostty.MOUSE_FOUR if steps > 0 else ghostty.MOUSE_FIVE
            for action in (ghostty.MOUSE_PRESS, ghostty.MOUSE_RELEASE):
                self._send_mouse(
                    action,
                    button,
                    event.position(),
                    event.modifiers(),
                    event.buttons(),
                )
            return
        delta = -3 * abs(steps) if steps > 0 else 3 * abs(steps)
        scroll = ghostty.ScrollViewport(
            ghostty.SCROLL_DELTA, ghostty.ScrollViewportValue(delta=delta)
        )
        self.lib.terminal_scroll_viewport(self.terminal, scroll)
        self.refresh()

    def focusInEvent(self, event) -> None:  # noqa: N802 (Qt name)
        self._send_focus(True)
        super().focusInEvent(event)

    def focusOutEvent(self, event) -> None:  # noqa: N802 (Qt name)
        self._send_focus(False)
        super().focusOutEvent(event)

    def _send_focus(self, gained: bool) -> None:
        mode = ghostty.ModeConfig()
        mode.mode = ghostty.MODE_FOCUS_EVENT
        ok, mode = self.lib.get(
            self.lib.terminal_get, self.terminal, ghostty.DATA_MODE, mode
        )
        if not ok or not mode.value:
            return
        data = self.lib.encode_focus(gained)
        if data:
            self.write(data)

    # --- geometry -----------------------------------------------------

    def resizeEvent(self, event) -> None:  # noqa: N802 (Qt name)
        cols = max(1, self.width() // self.cell_width)
        rows = max(1, self.height() // self.cell_height)
        if (cols, rows) != (self.cols, self.rows):
            self.cols, self.rows = cols, rows
            self.lib.terminal_resize(
                self.terminal, cols, rows, self.cell_width, self.cell_height
            )
            self.session.resize(cols, rows, self.cell_width, self.cell_height)
            self.refresh()
        super().resizeEvent(event)
