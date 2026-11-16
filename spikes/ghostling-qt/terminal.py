"""The terminal widget: a cell grid rendered with QPainter from
libghostty-vt's render state, mirroring Ghostling's renderer with Qt
in place of Raylib.

Supported: text with 8/16/256/truecolor, styles, the cursor, scrollback
with a scrollbar, resize with reflow, the Kitty keyboard protocol,
mouse reporting, focus reporting, OSC title changes, grid-anchored
selection (drag, word, line, autoscroll), clipboard copy/paste with
bracketed paste, OSC 52 writes, hyperlinks (OSC 8 and plain-text
URLs), find with highlights, font zoom, and a visual bell. Still out
of scope: Kitty graphics and scrollbar dragging.
"""

from __future__ import annotations

import ctypes
import os
import re
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
    QUrl,
    Signal,
)
from PySide6.QtGui import (
    QClipboard,
    QColor,
    QDesktopServices,
    QFont,
    QFontDatabase,
    QFontInfo,
    QFontMetrics,
    QPainter,
)
from PySide6.QtWidgets import QApplication, QLabel, QLineEdit, QMenu, QWidget
from session import PtySession

# Widgets registered for effects callbacks, keyed by the userdata id.
_REGISTRY: dict[int, TerminalWidget] = {}


def _qt_key_to_ghostty() -> dict[int, int]:
    """Map Qt.Key codes to GhosttyKey codes."""
    mapping: dict[int, int] = {}
    for letter in string.ascii_uppercase:
        mapping[int(getattr(Qt.Key, "Key_" + letter))] = ghostty.KEY[letter]
    for digit in string.digits:
        mapping[int(getattr(Qt.Key, f"Key_{digit}"))] = ghostty.KEY[
            "DIGIT_" + digit
        ]
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

# Plain-text URL spans found per row during paint — the fallback when
# a cell carries no OSC 8 link. One column per character is close
# enough for the ASCII URLs this catches.
_URL_RE = re.compile(r"(?:https?://|ftp://|www\.)[^\s<>\"']+[a-zA-Z0-9/]")

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


@ghostty.BELL_FN
def _bell(_terminal, userdata):
    """BEL: flash the terminal visually instead of ringing."""
    widget = _widget(userdata)
    if widget is not None:
        widget._flash()


@ghostty.CLIPBOARD_WRITE_FN
def _clipboard_write(_terminal, userdata, write_ptr):
    """OSC 52: a program inside wants to set the clipboard."""
    widget = _widget(userdata)
    if widget is None or not write_ptr:
        return
    write = ctypes.cast(
        write_ptr, ctypes.POINTER(ghostty.ClipboardWrite)
    ).contents
    text = None
    for index in range(write.contents_len):
        content = write.contents[index]
        if content.mime.text() in (
            "",
            "text/plain",
            "text/plain;charset=utf-8",
        ):
            text = content.data.text()
            break
    reply = ghostty.ClipboardWriteReply()
    reply.size = ctypes.sizeof(reply)
    reply.remember = False
    if text is not None:
        mode = (
            QClipboard.Mode.Selection
            if write.location in (1, 2)
            else QClipboard.Mode.Clipboard
        )
        QApplication.clipboard().setText(text, mode)
        reply.result = ghostty.CLIPBOARD_WRITE_SUCCESS
    else:
        reply.result = ghostty.CLIPBOARD_WRITE_UNSUPPORTED
    if write.reply:
        write.reply(write_ptr, ctypes.byref(reply))


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
        self.cell_width = 1
        self.cell_height = 1
        self._ascent = 1
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
        self._wheel_accum = 0
        self._drag_position = QPointF(0, 0)
        self._last_press_ms = 0
        self._last_press_cell: tuple[int, int] | None = None
        self._press_count = 0
        # Per-row plain-text URL spans from the last paint:
        # row -> [(start_col, end_col, url), ...].
        self._link_spans: list[list[tuple[int, int, str]]] = []
        self._hand_cursor = False
        # Autoscroll while a selection drag overshoots the top or
        # bottom edge: a timer scrolls the viewport and re-sends the
        # drag gesture at the clamped position, and the grid-anchored
        # selection follows into the revealed lines.
        self._autoscroll = QTimer(self)
        self._autoscroll.setInterval(50)
        self._autoscroll.timeout.connect(self._autoscroll_tick)
        self.render_state = self.lib.new_handle(self.lib.render_state_new)
        self.row_iterator = self.lib.new_handle(self.lib.row_iterator_new)
        self.row_cells = self.lib.new_handle(self.lib.row_cells_new)
        self.selection_gesture = self.lib.new_handle(
            self.lib.selection_gesture_new
        )
        # Measure cells and sync the grid now that the terminal and
        # session exist (Ctrl+Plus/Minus re-run this with a new size).
        self._set_font_point_size(self.font.pointSize())

        # Visual bell: BEL flashes a border for a moment.
        self._bell_flash = False
        self._bell_timer = QTimer(self)
        self._bell_timer.setSingleShot(True)
        self._bell_timer.timeout.connect(self._bell_off)
        # Find bar: a lazy search handle plus its overlay widgets.
        self._search = 0
        self._find_edit: QLineEdit | None = None
        self._find_count: QLabel | None = None

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
                ghostty.OPT_BELL,
                ctypes.cast(_bell, ctypes.c_void_p),
            ),
            (
                ghostty.OPT_DEVICE_ATTRIBUTES,
                ctypes.cast(_device_attributes, ctypes.c_void_p),
            ),
            (
                ghostty.OPT_CLIPBOARD_WRITE,
                ctypes.cast(_clipboard_write, ctypes.c_void_p),
            ),
        ]
        for option, value in registrations:
            self.lib.terminal_set(self.terminal, option, value)

    def closeEvent(self, event) -> None:  # noqa: N802 (Qt name)
        _REGISTRY.pop(id(self), None)
        self._reaper.stop()
        self._autoscroll.stop()
        if self._search:
            self.lib.search_free(self._search)
            self._search = 0
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
        link_spans: list[list[tuple[int, int, str]]] = []
        match_rects = self._viewport_match_rects()
        rects_by_row: dict[int, list] = {}
        for row, x, width, is_selected in match_rects:
            rects_by_row.setdefault(row, []).append((x, width, is_selected))
        viewport_row = -1
        while self.lib.row_iterator_next(iterator.value):
            viewport_row += 1
            ok, cells = self.lib.get(
                self.lib.row_get,
                iterator.value,
                ghostty.ROW_CELLS,
                ctypes.c_void_p(self.row_cells),
            )
            if not ok or not cells.value:
                link_spans.append([])
                continue
            x = 0
            row_chars: list[str] = []
            for rx, rw, is_selected in rects_by_row.get(viewport_row, []):
                painter.fillRect(
                    rx,
                    y,
                    rw,
                    self.cell_height,
                    QColor(255, 170, 0, 130)
                    if is_selected
                    else QColor(120, 170, 255, 70),
                )
            while self.lib.row_cells_next(cells):
                self._paint_cell(
                    painter,
                    cells,
                    colors,
                    style,
                    codepoints,
                    x,
                    y,
                    row_chars,
                )
                x += self.cell_width
            link_spans.append(
                [
                    (m.start(), m.end(), m.group(0))
                    for m in _URL_RE.finditer("".join(row_chars))
                ]
            )
            clean = ctypes.c_bool(False)
            self.lib.row_set(iterator, ghostty.ROW_DIRTY, ctypes.byref(clean))
            y += self.cell_height
        self._link_spans = link_spans

        self._paint_cursor(painter, colors)
        self._paint_scrollbar(painter)
        self._paint_exit_banner(painter)
        if self._bell_flash:
            pen = painter.pen()
            painter.setPen(QColor(255, 170, 0, 200))
            for inset in range(0, 3):
                painter.drawRect(
                    inset,
                    inset,
                    self.width() - 2 * inset - 1,
                    self.height() - 2 * inset - 1,
                )
            painter.setPen(pen)
        painter.end()

    def _paint_cell(
        self,
        painter,
        cells,
        colors,
        style,
        codepoints,
        x: int,
        y: int,
        row_chars: list[str] | None = None,
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
            if row_chars is not None:
                row_chars.append(" ")
            return
        text = ""
        if length.value <= 16:
            lib.row_cells_get(
                cells,
                ghostty.CELLS_GRAPHEMES_BUF,
                ctypes.cast(codepoints, ctypes.c_void_p),
            )
            text = "".join(chr(codepoints[i]) for i in range(length.value))
        if row_chars is not None:
            row_chars.append(text if text else " ")
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

    def _select_word_at(self, position: QPointF) -> None:
        """Double-click: select the word under the cursor through the
        library's word selector, and apply it as the selection."""
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
        options = ghostty.SelectWordOptions(
            size=ctypes.sizeof(ghostty.SelectWordOptions), ref=ref
        )
        selection = ghostty.Selection()
        selection.size = ctypes.sizeof(selection)
        if (
            lib.terminal_select_word(
                self.terminal,
                ctypes.byref(options),
                ctypes.byref(selection),
            )
            == ghostty.SUCCESS
        ):
            lib.terminal_set(
                self.terminal,
                ghostty.OPT_SELECTION,
                ctypes.byref(selection),
            )
            self._sel_dragged = True
            self.refresh()

    def _ref_under(self, position: QPointF):
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
            self.lib.terminal_grid_ref(
                self.terminal,
                point,
                ctypes.byref(ref),
            )
            != ghostty.SUCCESS
        ):
            return None
        return ref

    def _apply_selection(self, selection) -> None:
        self.lib.terminal_set(
            self.terminal,
            ghostty.OPT_SELECTION,
            ctypes.byref(selection),
        )
        self._sel_dragged = True
        self.refresh()

    def _select_line_at(self, position: QPointF) -> None:
        """Triple-click: select the line under the cursor through the
        library's line selector."""
        ref = self._ref_under(position)
        if ref is None:
            return
        options = ghostty.SelectLineOptions(
            size=ctypes.sizeof(ghostty.SelectLineOptions), ref=ref
        )
        selection = ghostty.Selection()
        selection.size = ctypes.sizeof(selection)
        if (
            self.lib.terminal_select_line(
                self.terminal,
                ctypes.byref(options),
                ctypes.byref(selection),
            )
            == ghostty.SUCCESS
        ):
            self._apply_selection(selection)

    def _autoscroll_tick(self) -> None:
        """Scroll while the drag position sits in the edge margin, then
        extend the selection into the revealed rows. Wayland windows
        cannot grab the mouse (only popups can), so the last in-window
        drag position drives this: holding it at the top or bottom
        row keeps scrolling until the pointer moves inward."""
        if not self._sel_dragged or self._mouse_tracking():
            return
        y = self._drag_position.y()
        margin = self.cell_height
        if y < margin:
            rows = max(1, round((margin - y) / self.cell_height))
            delta = -rows
        elif y > self.height() - margin:
            rows = max(
                1, round((y - (self.height() - margin)) / self.cell_height)
            )
            delta = rows
        else:
            return
        self.lib.terminal_scroll_viewport(
            self.terminal,
            ghostty.ScrollViewport(
                ghostty.SCROLL_DELTA,
                ghostty.ScrollViewportValue(delta=delta),
            ),
        )
        self._selection_gesture(ghostty.SEL_DRAG, self._drag_position)

    MIN_PT, MAX_PT = 7, 28
    DEFAULT_PT = 15

    def _set_font_point_size(self, points: int) -> None:
        """Measure cells for the font size, push the new pixel metrics
        into the terminal and pty, and grow the window to keep the
        same grid at the new size."""
        self.font.setPointSize(points)
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
        # box is about 1.3 em. Terminals ship exactly this trim as
        # their line-height setting; 1.2 em keeps rows compact.
        em = QFontInfo(self.font).pixelSize()
        self.cell_height = max(1, min(metrics.height(), round(em * 1.2)))
        self._ascent = metrics.ascent()
        self._fonts = self._font_variants()
        self.lib.terminal_resize(
            self.terminal,
            self.cols,
            self.rows,
            self.cell_width,
            self.cell_height,
        )
        self.session.resize(
            self.cols, self.rows, self.cell_width, self.cell_height
        )
        window = self.window()
        if window is not None and window is not self:
            window.resize(self.sizeHint())
        self.refresh()

    def _zoom_font(self, direction: int) -> None:
        """Ctrl+Plus/Minus: step the font size, clamped."""
        points = self.font.pointSize() + direction
        if self.MIN_PT <= points <= self.MAX_PT:
            self._set_font_point_size(points)

    def _hyperlink_at(self, position: QPointF) -> str | None:
        """The link under the cursor: OSC 8 from the grid when the
        cell carries one, else a plain-text URL span from the last
        paint."""
        ref = self._ref_under(position)
        if ref is not None:
            for size in (4096, 1 << 16):
                buf = ctypes.create_string_buffer(size)
                written = ctypes.c_size_t()
                result = self.lib.grid_ref_hyperlink_uri(
                    ctypes.byref(ref), buf, size, ctypes.byref(written)
                )
                if result == ghostty.SUCCESS:
                    uri = buf.raw[: written.value].decode(
                        "utf-8", errors="replace"
                    )
                    if uri:
                        return uri
                    # An empty answer means no OSC 8 link here; fall
                    # through to the plain-text spans.
                    break
                if result != ghostty.OUT_OF_SPACE:
                    break
        col, row = self._cell_at(position)
        if row < len(self._link_spans):
            for start, end, url in self._link_spans[row]:
                if start <= col < end:
                    return url
        return None

    def _set_hand_cursor(self, on: bool) -> None:
        if on != self._hand_cursor:
            self._hand_cursor = on
            if on:
                QApplication.setOverrideCursor(
                    Qt.CursorShape.PointingHandCursor
                )
            else:
                QApplication.restoreOverrideCursor()

    def _open_link_at(self, position: QPointF) -> bool:
        """Open a link the user Ctrl+Clicked. Only browser schemes go
        to the desktop handler — OSC 8 URIs come from the program
        inside, so file:// and friends stay unopened."""
        url = self._hyperlink_at(position)
        if not url:
            return False
        if url.startswith("www."):
            url = "https://" + url
        if not url.startswith(("http://", "https://", "ftp://")):
            return False
        QDesktopServices.openUrl(QUrl(url))
        return True

    # --- find bar -----------------------------------------------------

    def _find_open(self) -> None:
        """Ctrl+Shift+F: create the search and the overlay widgets."""
        if self._search:
            self._find_edit.setFocus()
            self._find_edit.selectAll()
            return
        self._search = self.lib.new_handle(self.lib.search_new, self.terminal)
        self._find_edit = QLineEdit(self)
        self._find_edit.setPlaceholderText("Find")
        self._find_edit.setFixedWidth(220)
        self._find_edit.textChanged.connect(self._find_run)
        self._find_edit.installEventFilter(self)
        self._find_count = QLabel(self)
        self._layout_find_bar()
        self._find_edit.show()
        self._find_count.show()
        self._find_edit.raise_()
        self._find_count.raise_()
        self._find_edit.setFocus()

    def _find_close(self) -> None:
        if not self._search:
            return
        self.lib.search_free(self._search)
        self._search = 0
        if self._find_edit is not None:
            self._find_edit.deleteLater()
            self._find_edit = None
        if self._find_count is not None:
            self._find_count.deleteLater()
            self._find_count = None
        self.setFocus()
        self.refresh()

    def _layout_find_bar(self) -> None:
        if self._find_edit is None or self._find_count is None:
            return
        height = 28
        self._find_count.adjustSize()
        count_w = self._find_count.width() + 10
        self._find_edit.move(self.width() - 220 - count_w - 16, 8)
        self._find_edit.setFixedHeight(height)
        self._find_count.move(
            self.width() - count_w - 8, 8 + (height - 18) // 2
        )

    def _find_run(self, text: str) -> None:
        """A changed needle restarts the search; unchanged keeps results."""
        if not self._search:
            return
        if text:
            data = text.encode("utf-8")
            needle = ghostty.GhosttyString()
            needle.ptr = ctypes.cast(
                ctypes.c_char_p(data), ctypes.POINTER(ctypes.c_uint8)
            )
            needle.len = len(data)
            self.lib.search_set(
                self._search,
                ghostty.SEARCH_OPT_NEEDLE,
                ctypes.byref(needle),
            )
            self.lib.search_run(self._search)
        self._find_update_count()
        self.refresh()

    def _find_step(self, direction: int) -> None:
        if not self._search:
            return
        option = (
            ghostty.SEARCH_OPT_SELECT_NEXT
            if direction > 0
            else ghostty.SEARCH_OPT_SELECT_PREV
        )
        self.lib.search_set(self._search, option, None)
        self._find_update_count()
        self.refresh()

    def _find_update_count(self) -> None:
        if self._find_count is None:
            return
        total = ctypes.c_size_t()
        ok, _ = self.lib.get(
            self.lib.search_get,
            self._search,
            ghostty.SEARCH_DATA_TOTAL_MATCHES,
            total,
        )
        index = ctypes.c_size_t()
        ok2, _ = self.lib.get(
            self.lib.search_get,
            self._search,
            ghostty.SEARCH_DATA_SELECTED_INDEX,
            index,
        )
        if not ok or total.value == 0:
            self._find_count.setText("")
        elif ok2:
            self._find_count.setText(f"{index.value + 1} of {total.value}")
        else:
            self._find_count.setText(f"{total.value} matches")
        self._layout_find_bar()

    def eventFilter(self, obj, event) -> None:  # noqa: N802 (Qt name)
        if obj is self._find_edit and event.type() == event.Type.KeyPress:
            key = event.key()
            if key == Qt.Key.Key_Return or key == Qt.Key.Key_Enter:
                if event.modifiers() & Qt.KeyboardModifier.ShiftModifier:
                    self._find_step(-1)
                else:
                    self._find_step(1)
                return True
            if key == Qt.Key.Key_Escape:
                self._find_close()
                return True
        return super().eventFilter(obj, event)

    def _viewport_match_rects(self):
        """Search-match rectangles for painting: (row, x_px, w_px,
        selected). Fed each paint while the find bar is open."""
        if not self._search:
            return []
        lib = self.lib
        lib.search_feed(self._search)
        storage = (ghostty.Selection * 128)()
        buffer = ghostty.SelectionBuffer(ptr=storage, cap=128, len=0)
        ok, _ = lib.get(
            lib.search_get,
            self._search,
            ghostty.SEARCH_DATA_VIEWPORT_MATCHES,
            buffer,
        )
        if not ok:
            return []
        selected = ghostty.Selection()
        selected.size = ctypes.sizeof(selected)
        sel_ok, _ = lib.get(
            lib.search_get,
            self._search,
            ghostty.SEARCH_DATA_SELECTED_MATCH,
            selected,
        )

        def viewport_point(ref):
            point = ghostty.PointCoordinate()
            result = lib.terminal_point_from_grid_ref(
                self.terminal,
                ctypes.byref(ref),
                ghostty.POINT_VIEWPORT,
                ctypes.byref(point),
            )
            return point if result == ghostty.SUCCESS else None

        rects = []

        def add_match(start_ref, end_ref, is_selected):
            start = viewport_point(start_ref)
            end = viewport_point(end_ref)
            if start is None or end is None:
                return
            if start.y >= self.rows or end.y < 0:
                return
            first = max(start.y, 0)
            last = min(end.y, self.rows - 1)
            for row in range(first, last + 1):
                col0 = start.x if row == start.y else 0
                col1 = end.x if row == end.y else self.cols - 1
                rects.append(
                    (
                        row,
                        col0 * self.cell_width,
                        (col1 - col0 + 1) * self.cell_width,
                        is_selected,
                    )
                )

        for i in range(buffer.len):
            add_match(storage[i].start, storage[i].end, False)
        if sel_ok and selected.size:
            add_match(selected.start, selected.end, True)
        return rects

    def _bell_off(self) -> None:
        self._bell_flash = False
        self.update()

    def _flash(self) -> None:
        """Visual bell: a brief orange border flash; repeat BELs
        restart the timer."""
        self._bell_flash = True
        self._bell_timer.start(180)
        self.update()

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
                if self._press_count >= 2:
                    # Word and line selections bypassed the gesture, so
                    # its dragged flag knows nothing; keep them.
                    self._publish_primary()
                else:
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
                    else:
                        self._publish_primary()
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

    def _build_context_menu(self, position=None):
        """Copy is enabled while a selection exists, Paste while the
        clipboard holds text; both dim otherwise. Right-clicking a
        link adds Copy Link Address for it."""
        menu = QMenu(self)
        link = self._hyperlink_at(position) if position is not None else None
        if link:
            copy_link = menu.addAction("Copy Link Address")
            copy_link.triggered.connect(
                lambda: QApplication.clipboard().setText(link)
            )
            menu.addSeparator()
        copy_action = menu.addAction("Copy\tCtrl+Shift+C")
        copy_action.setEnabled(bool(self._selection_text()))
        copy_action.triggered.connect(self._copy_selection)
        paste_action = menu.addAction("Paste\tCtrl+Shift+V")
        paste_action.setEnabled(bool(QApplication.clipboard().text()))
        paste_action.triggered.connect(self._paste_clipboard)
        return menu

    def contextMenuEvent(self, event) -> None:  # noqa: N802 (Qt name)
        if self._mouse_tracking():
            # The program inside owns the mouse; the press was already
            # forwarded to it.
            event.ignore()
            return
        self._build_context_menu(event.pos()).exec(event.globalPos())

    def _paste_text(self, text: str) -> None:
        if not text:
            return
        if self._mode_enabled(ghostty.MODE_BRACKETED_PASTE):
            self.write(b"\x1b[200~" + text.encode("utf-8") + b"\x1b[201~")
        else:
            # Plain paste: shells execute lines, so newlines become
            # carriage returns like every terminal's raw paste.
            self.write(text.replace("\n", "\r").encode("utf-8"))

    def _paste_clipboard(self) -> None:
        self._paste_text(QApplication.clipboard().text())

    def _paste_primary(self) -> None:
        """Middle-click paste: the PRIMARY selection — the last text
        selected with the mouse anywhere, this terminal included."""
        self._paste_text(
            QApplication.clipboard().text(QClipboard.Mode.Selection)
        )

    def _publish_primary(self) -> None:
        """A finished drag publishes its text to the PRIMARY selection,
        the X11 select-then-middle-click convention."""
        text = self._selection_text()
        if text:
            QApplication.clipboard().setText(text, QClipboard.Mode.Selection)

    def _scroll_page(self, direction: int) -> None:
        """Shift+PgUp/PgDn: page the viewport through the buffer —
        one page is the viewport minus a line of overlap."""
        rows = max(1, self.rows - 1)
        delta = -rows if direction < 0 else rows
        self.lib.terminal_scroll_viewport(
            self.terminal,
            ghostty.ScrollViewport(
                ghostty.SCROLL_DELTA,
                ghostty.ScrollViewportValue(delta=delta),
            ),
        )
        self.refresh()

    def _snap_to_bottom(self) -> None:
        """Typed input returns the viewport to the bottom so the
        cursor is visible after paging up."""
        self.lib.terminal_scroll_viewport(
            self.terminal,
            ghostty.ScrollViewport(
                ghostty.SCROLL_BOTTOM,
                ghostty.ScrollViewportValue(),
            ),
        )

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
                    self._snap_to_bottom()
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
            self._snap_to_bottom()
            self.write(data)

    def keyPressEvent(self, event) -> None:  # noqa: N802 (Qt name)
        if _DEBUG_KEYS:
            print(
                f"key: code=0x{event.key():x} text={event.text()!r} "
                f"mods={event.modifiers()!r} "
                f"auto={event.isAutoRepeat()}",
                flush=True,
            )
        # Ctrl+Plus/Equal grows the font (and the window with it,
        # grid unchanged); Ctrl+Minus shrinks it; Ctrl+0 resets.
        # Numpad +/- arrive as Key_Plus/Key_Minus with a keypad
        # modifier, so the same names cover them.
        if event.modifiers() & Qt.KeyboardModifier.ControlModifier:
            if event.key() in (Qt.Key.Key_Equal, Qt.Key.Key_Plus):
                self._zoom_font(1)
                return
            if event.key() == Qt.Key.Key_Minus:
                self._zoom_font(-1)
                return
            if event.key() == Qt.Key.Key_0:
                if self.font.pointSize() != self.DEFAULT_PT:
                    self._set_font_point_size(self.DEFAULT_PT)
                return
        # Ctrl+Shift+F toggles the find bar.
        if (
            event.modifiers() & Qt.KeyboardModifier.ControlModifier
            and event.modifiers() & Qt.KeyboardModifier.ShiftModifier
            and event.key() == Qt.Key.Key_F
        ):
            if self._search:
                self._find_close()
            else:
                self._find_open()
            return
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
        # Shift+PgUp/PgDn page the buffer locally instead of going to
        # the program inside.
        if event.modifiers() & Qt.KeyboardModifier.ShiftModifier and not (
            event.modifiers() & Qt.KeyboardModifier.ControlModifier
        ):
            if event.key() == Qt.Key.Key_PageUp:
                self._scroll_page(-1)
                return
            if event.key() == Qt.Key.Key_PageDown:
                self._scroll_page(1)
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
            and event.modifiers() & Qt.KeyboardModifier.ControlModifier
            and not self._mouse_tracking()
        ):
            if self._open_link_at(event.position()):
                return
        if (
            event.button() == Qt.MouseButton.MiddleButton
            and not self._mouse_tracking()
        ):
            self._paste_primary()
            return
        if (
            event.button() == Qt.MouseButton.LeftButton
            and not self._mouse_tracking()
        ):
            now = event.timestamp()
            cell = self._cell_at(event.position())
            repeat = (
                now - self._last_press_ms <= 400
                and cell == self._last_press_cell
            )
            self._press_count = self._press_count + 1 if repeat else 1
            self._last_press_ms = now
            self._last_press_cell = cell
            self._sel_dragged = False
            self._drag_position = event.position()
            self._autoscroll.start()
            if self._press_count == 2:
                self._select_word_at(event.position())
                return
            if self._press_count >= 3:
                self._select_line_at(event.position())
                return
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
            self._autoscroll.stop()
            self._selection_gesture(ghostty.SEL_RELEASE, event.position())
        self._send_mouse(
            ghostty.MOUSE_RELEASE,
            self._button(event),
            event.position(),
            event.modifiers(),
            event.buttons(),
        )

    def mouseMoveEvent(self, event) -> None:  # noqa: N802 (Qt name)
        if not event.buttons() and not self._mouse_tracking():
            self._set_hand_cursor(
                self._hyperlink_at(event.position()) is not None
            )
        elif self._hand_cursor:
            self._set_hand_cursor(False)
        if (
            event.buttons() & Qt.MouseButton.LeftButton
            and not self._mouse_tracking()
        ):
            self._sel_dragged = True
            self._drag_position = event.position()
            self._selection_gesture(ghostty.SEL_DRAG, event.position())
        self._send_mouse(
            ghostty.MOUSE_MOTION,
            self._button(event),
            event.position(),
            event.modifiers(),
            event.buttons(),
        )

    def wheelEvent(self, event) -> None:  # noqa: N802 (Qt name)
        # High-resolution wheels and Wayland touchpads deliver many
        # small deltas instead of one 120-per-notch tick, so both angle
        # and pixel deltas accumulate across events; steps > 0 means
        # wheel up (scroll into history).
        pixels = event.pixelDelta()
        if pixels is not None and not pixels.isNull():
            self._wheel_accum += pixels.y() * 120 // max(1, self.cell_height)
        else:
            self._wheel_accum += event.angleDelta().y()
        steps = 0
        while self._wheel_accum >= 120:
            steps += 1
            self._wheel_accum -= 120
        while self._wheel_accum <= -120:
            steps -= 1
            self._wheel_accum += 120
        if _DEBUG_KEYS:
            print(
                f"wheel: angle={event.angleDelta().y()} "
                f"pixels={pixels.y() if pixels else None} "
                f"steps={steps} "
                f"tracking={self._mouse_tracking()}",
                flush=True,
            )
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
        # Six rows per notch: the classic three felt sluggish next to
        # Konsole's default on this hardware.
        rows = 6 * abs(steps)
        delta = -rows if steps > 0 else rows
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
        self._layout_find_bar()
        super().resizeEvent(event)
