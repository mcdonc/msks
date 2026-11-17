"""The terminal's input encoders (phase 4.5 of #924, #952).

Keys, mouse buttons, the wheel, and focus changes encode
through the library's encoders — legacy sequences, application
cursor keys, the Kitty keyboard protocol — and what the child
reads comes back from the library, never spelled here.
"""

from __future__ import annotations

import ctypes
import string

from PySide6.QtCore import QPointF, Qt

from .. import ghostty as gt

# Qt key codes to the library's key codes, built once at
# import: the letters, the digits, the function keys, and the
# named keys — plus the shifted-symbol codes, because some
# platforms (Wayland notably) deliver Shift+; as the shifted
# codepoint's key with a shift modifier, so each maps to the
# base key whose unshifted codepoint the encoder expects.
_QT_KEYS: dict[int, int] = {}


def letter_map() -> dict[int, int]:
    """The letters A-Z at their Qt codes."""
    return {
        int(getattr(Qt.Key, "Key_" + letter)): gt.KEY[letter]
        for letter in string.ascii_uppercase
    }


def digit_map() -> dict[int, int]:
    """The digits at Qt codes 48-57 — never at 0-9."""
    return {
        int(getattr(Qt.Key, f"Key_{digit}")): gt.KEY["DIGIT_" + digit]
        for digit in string.digits
    }


def function_map() -> dict[int, int]:
    """The function keys F1-F25."""
    return {
        int(getattr(Qt.Key, f"Key_F{n}")): gt.KEY[f"F{n}"]
        for n in range(1, 26)
    }


def build_qt_keys() -> dict[int, int]:
    """The whole Qt-to-library key table in one dict."""
    build = {**letter_map(), **digit_map(), **function_map()}
    for qt, name in NAMED_KEYS.items():
        build[int(qt)] = gt.KEY[name]
    return build


def qt_keys() -> dict[int, int]:
    """The Qt-to-library key table, built once and cached."""
    if not _QT_KEYS:
        _QT_KEYS.update(build_qt_keys())
    return _QT_KEYS


NAMED_KEYS = {
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

# The unshifted codepoints (US layout) the Kitty protocol
# identifies keys by, independent of the modifier state.
UNSHIFTED: dict[int, int] = {gt.KEY["SPACE"]: ord(" ")}
for _letter in string.ascii_lowercase:
    UNSHIFTED[gt.KEY[_letter.upper()]] = ord(_letter)
for _digit in string.digits:
    UNSHIFTED[gt.KEY["DIGIT_" + _digit]] = ord(_digit)
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
    UNSHIFTED[gt.KEY[_name]] = ord(_char)

QT_TO_LIB_BUTTON = {
    Qt.MouseButton.LeftButton: gt.MOUSE_LEFT,
    Qt.MouseButton.RightButton: gt.MOUSE_RIGHT,
    Qt.MouseButton.MiddleButton: gt.MOUSE_MIDDLE,
}


def mods(modifiers) -> int:
    """Qt modifier flags as the library's modifier mask."""
    mask = 0
    if modifiers & Qt.KeyboardModifier.ShiftModifier:
        mask |= gt.MOD_SHIFT
    if modifiers & Qt.KeyboardModifier.ControlModifier:
        mask |= gt.MOD_CTRL
    if modifiers & Qt.KeyboardModifier.AltModifier:
        mask |= gt.MOD_ALT
    if modifiers & Qt.KeyboardModifier.MetaModifier:
        mask |= gt.MOD_SUPER
    return mask


def button_for(event) -> int | None:
    """The pressed button as the library's button code, when
    the event names one."""
    for qt_button, lib_button in QT_TO_LIB_BUTTON.items():
        if event.button() & qt_button:
            return lib_button
    return None


def cell_at(screen, position: QPointF) -> tuple[int, int]:
    """The grid cell a pixel position lands in, clamped to the
    grid's edges."""
    col = max(
        0,
        min(screen.cols - 1, int(position.x()) // screen.metrics.cell_width),
    )
    row = max(
        0,
        min(screen.rows - 1, int(position.y()) // screen.metrics.cell_height),
    )
    return col, row


def encode_key(screen, event, action: int) -> None:
    """One key event through the library's encoder: the mapped
    key, the modifiers, and the text ride the event, and the
    encoded bytes go to the child. Keys outside the table —
    platform-resolved shifted symbols, layouts beyond the map —
    fall back to their text, so input is never silently
    dropped."""
    key = qt_keys().get(int(event.key()))
    if key is None:
        if action != gt.ACTION_RELEASE:
            forward_text(screen, event.text())
        return
    set_event_key(screen, event, key, action)
    set_event_text(screen, event, action)
    data = screen.lib.encode_key(screen.key_encoder, screen.key_event)
    if data:
        snap_to_bottom(screen)
        screen.write(data)


def consumed_mods(unshifted: int, mask: int) -> int:
    """The modifiers the encoder treats as consumed: a shifted
    printable key has its shift folded into the text."""
    return gt.MOD_SHIFT if (unshifted and mask & gt.MOD_SHIFT) else 0


def set_event_key(screen, event, key: int, action: int) -> None:
    """The key itself on the event: the code, the action, the
    modifiers, and the unshifted codepoint the Kitty protocol
    identifies keys by."""
    lib = screen.lib
    lib.key_encoder_setopt_from_terminal(screen.key_encoder, screen.terminal)
    mask = mods(event.modifiers())
    lib.key_event_set_key(screen.key_event, key)
    lib.key_event_set_action(screen.key_event, action)
    lib.key_event_set_mods(screen.key_event, mask)
    unshifted = UNSHIFTED.get(key, 0)
    lib.key_event_set_unshifted_codepoint(screen.key_event, unshifted)
    lib.key_event_set_consumed_mods(
        screen.key_event, consumed_mods(unshifted, mask)
    )


def set_event_text(screen, event, action: int) -> None:
    """The event's printable text rides the key event; a
    release, or text without printable characters, clears it."""
    text = event.text()
    if action != gt.ACTION_RELEASE and text and all(ch >= " " for ch in text):
        encoded = text.encode("utf-8")
        screen.lib.key_event_set_utf8(screen.key_event, encoded, len(encoded))
    else:
        screen.lib.key_event_set_utf8(screen.key_event, None, 0)


def forward_text(screen, text: str) -> None:
    """Text from outside the key table, sent raw: printable
    text only."""
    if text and all(ch >= " " for ch in text):
        snap_to_bottom(screen)
        screen.write(text.encode("utf-8"))


def send_mouse(
    screen,
    action: int,
    button: int | None,
    position: QPointF,
    modifiers,
    buttons,
) -> None:
    """One mouse event through the library's encoder, sized in
    the pixels the widget holds. The child sees it only when it
    asked for mouse reporting — the encoder answers empty
    otherwise."""
    lib = screen.lib
    lib.mouse_encoder_setopt_from_terminal(
        screen.mouse_encoder, screen.terminal
    )
    size = gt.MouseEncoderSize()
    size.size = ctypes.sizeof(size)
    size.screen_width = screen.width()
    size.screen_height = screen.height()
    size.cell_width = screen.metrics.cell_width
    size.cell_height = screen.metrics.cell_height
    lib.mouse_encoder_setopt(
        screen.mouse_encoder, gt.MOPT_SIZE, ctypes.byref(size)
    )
    track_cell = ctypes.c_bool(True)
    lib.mouse_encoder_setopt(
        screen.mouse_encoder,
        gt.MOPT_TRACK_LAST_CELL,
        ctypes.byref(track_cell),
    )
    any_pressed = ctypes.c_bool(bool(buttons))
    lib.mouse_encoder_setopt(
        screen.mouse_encoder,
        gt.MOPT_ANY_BUTTON_PRESSED,
        ctypes.byref(any_pressed),
    )
    lib.mouse_event_set_mods(screen.mouse_event, mods(modifiers))
    lib.mouse_event_set_action(screen.mouse_event, action)
    if button is None:
        lib.mouse_event_clear_button(screen.mouse_event)
    else:
        lib.mouse_event_set_button(screen.mouse_event, button)
    lib.mouse_event_set_position(
        screen.mouse_event,
        gt.MousePosition(position.x(), position.y()),
    )
    data = lib.encode_mouse(screen.mouse_encoder, screen.mouse_event)
    if data:
        screen.write(data)


def send_focus(screen, gained: bool) -> None:
    """A focus report (mode 1004's sequences), sent only when
    the program inside asked for them."""
    mode = gt.ModeConfig()
    mode.mode = gt.MODE_FOCUS_EVENT
    ok, mode = screen.lib.get(
        screen.lib.terminal_get, screen.terminal, gt.DATA_MODE, mode
    )
    if not ok or not mode.value:
        return
    # The encoder always answers mode 1004's bytes; an empty
    # answer would mean the call failed, and the write's
    # best-effort contract drops nothing here.
    screen.write(screen.lib.encode_focus(gained))


def mode_enabled(screen, mode_id: int) -> bool:
    """Whether a DEC private mode is on inside."""
    mode = gt.ModeConfig()
    mode.mode = mode_id
    ok, mode = screen.lib.get(
        screen.lib.terminal_get, screen.terminal, gt.DATA_MODE, mode
    )
    return bool(ok and mode.value)


def mouse_tracking(screen) -> bool:
    """Whether the program inside owns the mouse."""
    ok, tracking = screen.lib.get(
        screen.lib.terminal_get,
        screen.terminal,
        gt.DATA_MOUSE_TRACKING,
        ctypes.c_bool(),
    )
    return bool(ok and tracking.value)


def wheel_steps(
    accumulated: int, pixels_y: int | None, angle_y: int, cell_height: int
) -> tuple[int, int]:
    """The notches a wheel event adds: high-resolution wheels
    and touchpads deliver many small deltas, so pixel and
    angle deltas accumulate across events and a notch is every
    120 units. Positive steps mean wheel up (into history)."""
    total = accumulated
    if pixels_y:
        total += pixels_y * 120 // max(1, cell_height)
    else:
        total += angle_y
    steps = 0
    while total >= 120:
        steps += 1
        total -= 120
    while total <= -120:
        steps -= 1
        total += 120
    return steps, total


def key_action(auto_repeat: bool) -> int:
    """The library action a Qt key event carries: an
    auto-repeat press repeats."""
    return gt.ACTION_REPEAT if auto_repeat else gt.ACTION_PRESS


def snap_to_bottom(screen) -> None:
    """Typed input returns the viewport to the bottom so the
    cursor is visible after paging up."""
    screen.lib.terminal_scroll_viewport(
        screen.terminal,
        gt.ScrollViewport(gt.SCROLL_BOTTOM, gt.ScrollViewportValue()),
    )


def scroll_page(screen, direction: int) -> None:
    """Shift+PgUp/PgDn: page the viewport through the buffer —
    one page is the viewport minus a line of overlap."""
    rows = max(1, screen.rows - 1)
    delta = -rows if direction < 0 else rows
    screen.lib.terminal_scroll_viewport(
        screen.terminal,
        gt.ScrollViewport(gt.SCROLL_DELTA, gt.ScrollViewportValue(delta)),
    )
    screen.refresh()
