"""The ctypes binding to libghostty-vt (#950, phase 4.5 of #924):
the terminal emulation core of Ghostty over its C ABI — the
port of the spike's binding (#935), pinned to the ABI the
vendored build carries. The module imports nothing from PySide6
(it is ctypes over the C ABI) and nothing from msks; the
terminal widget (#952) is its consumer.

The library is the official build the ``pyghostty`` wheel
bundles (the library alone — its Python API goes unused): the
venv's copy under ``pyghostty/_lib``, the ``GHOSTTY_VT_LIB``
override when it is set, the system loader's
``libghostty-vt.so`` — ``.dylib`` under macOS's loader —
otherwise. The wheel snapshot is a pre-1.0
``0.1.0-dev`` build; when a 1.x wheel ships, the base
dependency re-pins and this binding moves to the stable
ABI.

The spike's ctypes findings (#935's README) are folded in:
every struct field is mirrored exactly, padding members
included — the scroll-viewport union without its padding array
shrinks below the memory-class threshold, ctypes passes it in
registers, and the library traps reading the stack; iterator
handles are in/out parameters, so the handle slot is pre-filled
(:meth:`Lib.new_handle`); and struct-returning callbacks are
not expressible in ctypes, so the XTVERSION effect stays
unregistered — the library answers ``CSI > q`` itself.
"""

from __future__ import annotations

import ctypes
import os
import sys
from importlib.util import find_spec
from pathlib import Path

SUCCESS = 0
NO_VALUE = -4
OUT_OF_SPACE = -3

# GhosttyKey codes, in declaration order (the values are
# positional from zero; GHOSTTY_KEY_MAX_VALUE is excluded). The
# count is asserted so an accidental edit of the list cannot
# silently shift every value.
KEY_NAMES = (
    "UNIDENTIFIED BACKQUOTE BACKSLASH BRACKET_LEFT BRACKET_RIGHT COMMA "
    "DIGIT_0 DIGIT_1 DIGIT_2 DIGIT_3 DIGIT_4 DIGIT_5 DIGIT_6 DIGIT_7 "
    "DIGIT_8 DIGIT_9 EQUAL INTL_BACKSLASH INTL_RO INTL_YEN A B C D E F G "
    "H I J K L M N O P Q R S T U V W X Y Z MINUS PERIOD QUOTE SEMICOLON "
    "SLASH ALT_LEFT ALT_RIGHT BACKSPACE CAPS_LOCK CONTEXT_MENU "
    "CONTROL_LEFT CONTROL_RIGHT ENTER META_LEFT META_RIGHT SHIFT_LEFT "
    "SHIFT_RIGHT SPACE TAB CONVERT KANA_MODE NON_CONVERT DELETE END HELP "
    "HOME INSERT PAGE_DOWN PAGE_UP ARROW_DOWN ARROW_LEFT ARROW_RIGHT "
    "ARROW_UP NUM_LOCK NUMPAD_0 NUMPAD_1 NUMPAD_2 NUMPAD_3 NUMPAD_4 "
    "NUMPAD_5 NUMPAD_6 NUMPAD_7 NUMPAD_8 NUMPAD_9 NUMPAD_ADD "
    "NUMPAD_BACKSPACE NUMPAD_CLEAR NUMPAD_CLEAR_ENTRY NUMPAD_COMMA "
    "NUMPAD_DECIMAL NUMPAD_DIVIDE NUMPAD_ENTER NUMPAD_EQUAL "
    "NUMPAD_MEMORY_ADD NUMPAD_MEMORY_CLEAR NUMPAD_MEMORY_RECALL "
    "NUMPAD_MEMORY_STORE NUMPAD_MEMORY_SUBTRACT NUMPAD_MULTIPLY "
    "NUMPAD_PAREN_LEFT NUMPAD_PAREN_RIGHT NUMPAD_SUBTRACT "
    "NUMPAD_SEPARATOR NUMPAD_UP NUMPAD_DOWN NUMPAD_RIGHT NUMPAD_LEFT "
    "NUMPAD_BEGIN NUMPAD_HOME NUMPAD_END NUMPAD_INSERT NUMPAD_DELETE "
    "NUMPAD_PAGE_UP NUMPAD_PAGE_DOWN ESCAPE F1 F2 F3 F4 F5 F6 F7 F8 F9 "
    "F10 F11 F12 F13 F14 F15 F16 F17 F18 F19 F20 F21 F22 F23 F24 F25 FN "
    "FN_LOCK PRINT_SCREEN SCROLL_LOCK PAUSE BROWSER_BACK "
    "BROWSER_FAVORITES BROWSER_FORWARD BROWSER_HOME BROWSER_REFRESH "
    "BROWSER_SEARCH BROWSER_STOP EJECT LAUNCH_APP_1 LAUNCH_APP_2 "
    "LAUNCH_MAIL MEDIA_PLAY_PAUSE MEDIA_SELECT MEDIA_STOP "
    "MEDIA_TRACK_NEXT MEDIA_TRACK_PREVIOUS POWER SLEEP AUDIO_VOLUME_DOWN "
    "AUDIO_VOLUME_MUTE AUDIO_VOLUME_UP WAKE_UP COPY CUT PASTE"
).split()
assert len(KEY_NAMES) == 176, len(KEY_NAMES)
KEY = {name: code for code, name in enumerate(KEY_NAMES)}

# Modifier bits (GhosttyMods, a uint16 bitmask).
MOD_SHIFT = 1 << 0
MOD_CTRL = 1 << 1
MOD_ALT = 1 << 2
MOD_SUPER = 1 << 3

ACTION_RELEASE, ACTION_PRESS, ACTION_REPEAT = 0, 1, 2

MOUSE_PRESS, MOUSE_RELEASE, MOUSE_MOTION = 0, 1, 2
MOUSE_LEFT, MOUSE_RIGHT, MOUSE_MIDDLE = 1, 2, 3
MOUSE_FOUR, MOUSE_FIVE = 4, 5

#: The system loader's bare name (#950): the last resort when
#: neither the override nor the wheel answers — a dylib under
#: macOS's loader, a .so elsewhere.
SYSTEM_LIBRARY = (
    "libghostty-vt.dylib" if sys.platform == "darwin" else "libghostty-vt.so"
)

#: The build-info selector for the version string (#950) — the
#: one selector below that carried no name of its own.
BUILD_INFO_VERSION_STRING = 5

# ghostty_terminal_get data selectors.
DATA_MODE = 37
DATA_MOUSE_TRACKING = 11
DATA_SCROLLBAR = 9
DATA_TITLE = 12
DATA_SELECTION = 31

# GhosttyColorScheme values (color_scheme.h): the light/dark
# answer the widget reports to the child's CSI ? 996 n query
# once the window path applied a scheme's colors (#1006).
COLOR_SCHEME_LIGHT = 0
COLOR_SCHEME_DARK = 1

# ghostty_terminal_set options.
OPT_USERDATA = 0
OPT_WRITE_PTY = 1
OPT_XTVERSION = 4
OPT_TITLE_CHANGED = 5
OPT_SIZE = 6
OPT_COLOR_SCHEME = 7
OPT_DEVICE_ATTRIBUTES = 8
# The color defaults (guide-colors, "Setting your theme"):
# the default layer a program's OSC 10/11/4 overrides sit
# above — fg, bg, and cursor take one ``ColorRgb``, the
# palette the whole 256-entry array.
OPT_COLOR_FOREGROUND = 11
OPT_COLOR_BACKGROUND = 12
OPT_COLOR_CURSOR = 13
OPT_COLOR_PALETTE = 14
OPT_SCROLLBACK_MAX_LINES = 28
OPT_SELECTION = 21
OPT_BELL = 2
OPT_CLIPBOARD_WRITE = 26

# OSC 52 clipboard-write results.
CLIPBOARD_WRITE_SUCCESS = 0
CLIPBOARD_WRITE_UNSUPPORTED = 2

# Render state selectors.
RS_DIRTY = 3
RS_ROW_ITERATOR = 4
RS_CURSOR_VISIBLE = 11
RS_CURSOR_VIEWPORT_HAS_VALUE = 14
RS_CURSOR_VIEWPORT_X = 15
RS_CURSOR_VIEWPORT_Y = 16
RS_COLORS = 19
ROW_CELLS = 3
ROW_DIRTY = 1
# Grid-row data selectors (the grid API's row, a plain u64
# value a ref answers — no handle to free; not the
# render-state row above): the soft-wrap flag the URL scan
# stitches wrapped rows with (#1100).
GRID_ROW_WRAP = 1
CELLS_STYLE = 2
CELLS_GRAPHEMES_LEN = 3
CELLS_GRAPHEMES_BUF = 4
CELLS_BG_COLOR = 5
CELLS_FG_COLOR = 6
CELLS_SELECTED = 7

# Selection gesture event types, options, and data.
SEL_PRESS, SEL_RELEASE, SEL_DRAG = 0, 1, 2
SEL_EVENT_OPT_REF = 0
SEL_EVENT_OPT_POSITION = 1
SEL_EVENT_OPT_GEOMETRY = 8
SEL_DATA_DRAGGED = 1

# Search data selectors, options, and statuses.
SEARCH_DATA_TOTAL_MATCHES = 2
SEARCH_DATA_SELECTED_INDEX = 3
SEARCH_DATA_SELECTED_MATCH = 4
SEARCH_DATA_VIEWPORT_MATCHES = 6
SEARCH_OPT_NEEDLE = 0
SEARCH_OPT_SELECT_NEXT = 1
SEARCH_OPT_SELECT_PREV = 2

# GhosttyPoint tags.
POINT_ACTIVE = 0
POINT_VIEWPORT = 1

# GhosttyFormatterFormat values.
FORMATTER_PLAIN = 0

# Mouse encoder options.
MOPT_SIZE = 2
MOPT_ANY_BUTTON_PRESSED = 3
MOPT_TRACK_LAST_CELL = 4

# GHOSTTY_MODE_FOCUS_EVENT: mode 1004, DEC private (ansi =
# false), packed by ghostty_mode_new(value, ansi) as
# value | ansi << 15.
MODE_FOCUS_EVENT = 1004
MODE_BRACKETED_PASTE = 2004

# Device attributes reported for DA1/DA2/DA3 queries (VT220,
# the answer the spike's terminal gives).
DA_CONFORMANCE_VT220 = 62
DA_FEATURE_COLUMNS_132 = 1
DA_FEATURE_SELECTIVE_ERASE = 6
DA_FEATURE_ANSI_COLOR = 22
DA_DEVICE_TYPE_VT220 = 1


class ColorRgb(ctypes.Structure):
    _fields_ = [
        ("r", ctypes.c_uint8),
        ("g", ctypes.c_uint8),
        ("b", ctypes.c_uint8),
    ]


class PaletteMask(ctypes.Structure):
    """GhosttyColorPaletteMask: 256 bits, one per palette
    index — the set bits name the entries palette generation
    keeps from the base palette."""

    _fields_ = [("bits", ctypes.c_uint64 * 4)]


class StyleColorValue(ctypes.Union):
    # The _padding member mirrors the C union's full width — a
    # mirror without it shrinks the struct and shifts the C ABI.
    _fields_ = [
        ("palette", ctypes.c_uint8),
        ("rgb", ColorRgb),
        ("_padding", ctypes.c_uint64),
    ]


class StyleColor(ctypes.Structure):
    _fields_ = [("tag", ctypes.c_int), ("value", StyleColorValue)]


class Style(ctypes.Structure):
    _fields_ = [
        ("size", ctypes.c_size_t),
        ("fg_color", StyleColor),
        ("bg_color", StyleColor),
        ("underline_color", StyleColor),
        ("bold", ctypes.c_bool),
        ("italic", ctypes.c_bool),
        ("faint", ctypes.c_bool),
        ("blink", ctypes.c_bool),
        ("inverse", ctypes.c_bool),
        ("invisible", ctypes.c_bool),
        ("strikethrough", ctypes.c_bool),
        ("overline", ctypes.c_bool),
        ("underline", ctypes.c_int),
    ]


class RenderStateColors(ctypes.Structure):
    _fields_ = [
        ("size", ctypes.c_size_t),
        ("background", ColorRgb),
        ("foreground", ColorRgb),
        ("cursor", ColorRgb),
        ("cursor_has_value", ctypes.c_bool),
        ("palette", ColorRgb * 256),
    ]


class Scrollbar(ctypes.Structure):
    _fields_ = [
        ("total", ctypes.c_uint64),
        ("offset", ctypes.c_uint64),
        ("len", ctypes.c_uint64),
    ]


class SizeReportSize(ctypes.Structure):
    _fields_ = [
        ("rows", ctypes.c_uint16),
        ("columns", ctypes.c_uint16),
        ("cell_width", ctypes.c_uint32),
        ("cell_height", ctypes.c_uint32),
    ]


class ModeConfig(ctypes.Structure):
    # GhosttyMode is uint16 and value bool: a uint16 mirror
    # puts the answered value where the C struct keeps it — a
    # uint32 field would land it in padding (#952).
    _fields_ = [
        ("mode", ctypes.c_uint16),
        ("value", ctypes.c_bool),
    ]


class GhosttyString(ctypes.Structure):
    _fields_ = [
        ("ptr", ctypes.POINTER(ctypes.c_uint8)),
        ("len", ctypes.c_size_t),
    ]

    def text(self) -> str:
        """The string as Python text, undecodable bytes
        replaced."""
        raw = ctypes.string_at(self.ptr, self.len)
        return raw.decode("utf-8", errors="replace")


class DeviceAttributes(ctypes.Structure):
    class Primary(ctypes.Structure):
        _fields_ = [
            ("conformance_level", ctypes.c_uint16),
            ("features", ctypes.c_uint16 * 64),
            ("num_features", ctypes.c_size_t),
        ]

    class Secondary(ctypes.Structure):
        _fields_ = [
            ("device_type", ctypes.c_uint16),
            ("firmware_version", ctypes.c_uint16),
            ("rom_cartridge", ctypes.c_uint16),
        ]

    class Tertiary(ctypes.Structure):
        _fields_ = [("unit_id", ctypes.c_uint32)]

    _fields_ = [
        ("primary", Primary),
        ("secondary", Secondary),
        ("tertiary", Tertiary),
    ]


class MousePosition(ctypes.Structure):
    _fields_ = [("x", ctypes.c_float), ("y", ctypes.c_float)]


class MouseEncoderSize(ctypes.Structure):
    # Field order mirrors the C header, padding_right before
    # padding_left included.
    _fields_ = [
        ("size", ctypes.c_size_t),
        ("screen_width", ctypes.c_uint32),
        ("screen_height", ctypes.c_uint32),
        ("cell_width", ctypes.c_uint32),
        ("cell_height", ctypes.c_uint32),
        ("padding_top", ctypes.c_uint32),
        ("padding_bottom", ctypes.c_uint32),
        ("padding_right", ctypes.c_uint32),
        ("padding_left", ctypes.c_uint32),
    ]


class ScrollViewportValue(ctypes.Union):
    # The _padding member makes this 16 bytes; a mirror without
    # it shrinks the struct to 16 bytes total, ctypes passes it
    # in registers, and the library — expecting a 24-byte
    # memory-class argument — reads garbage off the stack and
    # traps (#935's finding: the SIGILL with no diagnostic).
    _fields_ = [
        ("row", ctypes.c_uint64),
        ("delta", ctypes.c_int64),
        ("_padding", ctypes.c_uint64 * 2),
    ]


class ScrollViewport(ctypes.Structure):
    _fields_ = [("tag", ctypes.c_int), ("value", ScrollViewportValue)]


class GridRef(ctypes.Structure):
    """A pinned grid location; ``node`` is opaque state the
    library uses to keep the ref stable across scrolls and
    reflows."""

    _fields_ = [
        ("size", ctypes.c_size_t),
        ("node", ctypes.c_void_p),
        ("x", ctypes.c_uint16),
        ("y", ctypes.c_uint16),
    ]


class Selection(ctypes.Structure):
    """A grid-anchored selection: the refs move with the
    content, so the selection survives viewport scrolling."""

    _fields_ = [
        ("size", ctypes.c_size_t),
        ("start", GridRef),
        ("end", GridRef),
        ("rectangle", ctypes.c_bool),
    ]


class SelectionFormatOptions(ctypes.Structure):
    _fields_ = [
        ("size", ctypes.c_size_t),
        ("emit", ctypes.c_int),
        ("unwrap", ctypes.c_bool),
        ("trim", ctypes.c_bool),
        ("selection", ctypes.POINTER(Selection)),
    ]


class SelectWordOptions(ctypes.Structure):
    """A word-selection request; null boundary codepoints mean
    the library's own defaults."""

    _fields_ = [
        ("size", ctypes.c_size_t),
        ("ref", GridRef),
        ("boundary_codepoints", ctypes.POINTER(ctypes.c_uint32)),
        ("boundary_codepoints_len", ctypes.c_size_t),
    ]


class SelectLineOptions(ctypes.Structure):
    """A line-selection request for triple-click."""

    _fields_ = [
        ("size", ctypes.c_size_t),
        ("ref", GridRef),
        ("whitespace", ctypes.POINTER(ctypes.c_uint32)),
        ("whitespace_len", ctypes.c_size_t),
        ("semantic_prompt_boundary", ctypes.c_bool),
    ]


class ClipboardContent(ctypes.Structure):
    """One clipboard payload (mime and data) of an OSC 52
    write."""

    _fields_ = [("mime", GhosttyString), ("data", GhosttyString)]


class SelectionBuffer(ctypes.Structure):
    """A caller-provided array of selections for viewport
    matches."""

    _fields_ = [
        ("ptr", ctypes.POINTER(Selection)),
        ("cap", ctypes.c_size_t),
        ("len", ctypes.c_size_t),
    ]


class ClipboardWriteReply(ctypes.Structure):
    _fields_ = [
        ("size", ctypes.c_size_t),
        ("result", ctypes.c_int),
        ("remember", ctypes.c_bool),
    ]


CLIPBOARD_WRITE_REPLY_FN = ctypes.CFUNCTYPE(
    None, ctypes.c_void_p, ctypes.c_void_p
)


class ClipboardWrite(ctypes.Structure):
    """An OSC 52 write request; the embedder replies
    asynchronously through the embedded function pointer."""

    _fields_ = [
        ("size", ctypes.c_size_t),
        ("location", ctypes.c_int),
        ("contents", ctypes.POINTER(ClipboardContent)),
        ("contents_len", ctypes.c_size_t),
        ("name", GhosttyString),
        ("granted", ctypes.c_bool),
        ("can_remember", ctypes.c_bool),
        ("ctx", ctypes.c_void_p),
        ("reply", CLIPBOARD_WRITE_REPLY_FN),
    ]


class PointCoordinate(ctypes.Structure):
    _fields_ = [("x", ctypes.c_uint16), ("y", ctypes.c_uint32)]


class PointValue(ctypes.Union):
    _fields_ = [
        ("coordinate", PointCoordinate),
        ("_padding", ctypes.c_uint64 * 2),
    ]


class Point(ctypes.Structure):
    _fields_ = [("tag", ctypes.c_int), ("value", PointValue)]


class SurfacePosition(ctypes.Structure):
    """Pixel coordinates, what OPT_POSITION expects."""

    _fields_ = [("x", ctypes.c_double), ("y", ctypes.c_double)]


class SelectionGestureGeometry(ctypes.Structure):
    """Surface geometry a drag needs to map pixels to cells."""

    _fields_ = [
        ("columns", ctypes.c_uint32),
        ("cell_width", ctypes.c_uint32),
        ("padding_left", ctypes.c_uint32),
        ("screen_height", ctypes.c_uint32),
    ]


SCROLL_TOP = 0
SCROLL_BOTTOM = 1
SCROLL_DELTA = 2

# Effects callbacks: the terminal invokes these to answer VT
# queries and report state changes; each receives the userdata
# pointer registered with OPT_USERDATA. The XTVERSION effect is
# deliberately absent — its callback returns GhosttyString by
# value, a struct-returning callback ctypes cannot express
# (#935); the library answers CSI > q itself.
WRITE_PTY_FN = ctypes.CFUNCTYPE(
    None,
    ctypes.c_void_p,
    ctypes.c_void_p,
    ctypes.POINTER(ctypes.c_uint8),
    ctypes.c_size_t,
)
SIZE_FN = ctypes.CFUNCTYPE(
    ctypes.c_bool,
    ctypes.c_void_p,
    ctypes.c_void_p,
    ctypes.POINTER(SizeReportSize),
)
DEVICE_ATTRIBUTES_FN = ctypes.CFUNCTYPE(
    ctypes.c_bool,
    ctypes.c_void_p,
    ctypes.c_void_p,
    ctypes.POINTER(DeviceAttributes),
)
TITLE_CHANGED_FN = ctypes.CFUNCTYPE(None, ctypes.c_void_p, ctypes.c_void_p)
COLOR_SCHEME_FN = ctypes.CFUNCTYPE(
    ctypes.c_bool,
    ctypes.c_void_p,
    ctypes.c_void_p,
    ctypes.POINTER(ctypes.c_int),
)
BELL_FN = ctypes.CFUNCTYPE(None, ctypes.c_void_p, ctypes.c_void_p)
CLIPBOARD_WRITE_FN = ctypes.CFUNCTYPE(
    None, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p
)


def wheel_library(spec=None) -> str | None:
    """The library the installed pyghostty wheel bundles, when
    one is installed: the official build under its ``_lib``
    directory. ``spec`` is the find_spec seam, overridable in
    the tests."""
    try:
        found = (spec or find_spec)("pyghostty")
    except ImportError, ValueError:
        return None
    if found is None or not found.submodule_search_locations:
        return None
    root = Path(list(found.submodule_search_locations)[0]) / "_lib"
    return bundled_library(root)


def bundled_library(root: Path) -> str | None:
    """The official build under one wheel root: ``.so`` on
    Linux, ``.dylib`` on macOS."""
    for name in ("libghostty-vt.so", "libghostty-vt.dylib"):
        candidate = root / name
        if candidate.exists():
            return str(candidate)
    return None


def library_path(spec=None) -> str:
    """Where the binding loads the library from (#950): the
    ``GHOSTTY_VT_LIB`` override first, the pyghostty wheel's
    bundled build second, the system loader's bare name last."""
    override = os.environ.get("GHOSTTY_VT_LIB")
    if override:
        return override
    return wheel_library(spec) or SYSTEM_LIBRARY


class Lib:
    """libghostty-vt loaded through ctypes, with typed
    prototypes. Handles are opaque ``c_void_p`` values; callers
    keep them alive for as long as the library holds them."""

    def __init__(self, path: str | None = None) -> None:
        self.lib = ctypes.CDLL(path or library_path())
        self.build_prototypes()

    def prototype(self, name, restype, *argtypes):
        """One exported symbol as a typed function."""
        fn = getattr(self.lib, name)
        fn.restype = restype
        fn.argtypes = argtypes
        return fn

    def build_prototypes(self) -> None:
        """Every export the widget drives, typed over the
        opaque-handle API."""
        v, u16, u32, sz = (
            ctypes.c_void_p,
            ctypes.c_uint16,
            ctypes.c_uint32,
            ctypes.c_size_t,
        )
        i, b, r = ctypes.c_int, ctypes.c_bool, ctypes.c_int
        self.terminal_new = self.prototype(
            "ghostty_terminal_new", r, v, ctypes.POINTER(v), u16, u16
        )
        self.terminal_free = self.prototype("ghostty_terminal_free", None, v)
        self.terminal_set = self.prototype("ghostty_terminal_set", r, v, i, v)
        self.terminal_get = self.prototype("ghostty_terminal_get", r, v, i, v)
        self.terminal_resize = self.prototype(
            "ghostty_terminal_resize", r, v, u16, u16, u32, u32
        )
        self.terminal_vt_write = self.prototype(
            "ghostty_terminal_vt_write",
            None,
            v,
            ctypes.POINTER(ctypes.c_uint8),
            sz,
        )
        # void in the C ABI — the header's word, not a result
        # code: a restype of int would read a register the
        # function never set.
        self.terminal_scroll_viewport = self.prototype(
            "ghostty_terminal_scroll_viewport", None, v, ScrollViewport
        )
        # The color helpers (guide-colors): the built-in
        # 256-color palette, and the cube-and-ramp derivation
        # that matches a palette to a background and
        # foreground (#1006). Both void in the C ABI.
        self.color_palette_default = self.prototype(
            "ghostty_color_palette_default", None, v
        )
        self.color_palette_generate = self.prototype(
            "ghostty_color_palette_generate",
            None,
            v,
            v,
            v,
            v,
            ctypes.c_bool,
            v,
        )
        self.color_contrast = self.prototype(
            "ghostty_color_contrast", ctypes.c_double, v, v
        )
        self.render_state_new = self.prototype(
            "ghostty_render_state_new", r, v, ctypes.POINTER(v)
        )
        self.render_state_free = self.prototype(
            "ghostty_render_state_free", None, v
        )
        self.render_state_update = self.prototype(
            "ghostty_render_state_update", r, v, v
        )
        self.render_state_get = self.prototype(
            "ghostty_render_state_get", r, v, i, v
        )
        self.render_state_set = self.prototype(
            "ghostty_render_state_set", r, v, i, v
        )
        self.row_iterator_new = self.prototype(
            "ghostty_render_state_row_iterator_new",
            r,
            v,
            ctypes.POINTER(v),
        )
        self.row_iterator_free = self.prototype(
            "ghostty_render_state_row_iterator_free", None, v
        )
        self.row_iterator_next = self.prototype(
            "ghostty_render_state_row_iterator_next", b, v
        )
        self.row_get = self.prototype(
            "ghostty_render_state_row_get", r, v, i, v
        )
        self.row_set = self.prototype(
            "ghostty_render_state_row_set", r, v, i, v
        )
        self.row_cells_new = self.prototype(
            "ghostty_render_state_row_cells_new", r, v, ctypes.POINTER(v)
        )
        self.row_cells_free = self.prototype(
            "ghostty_render_state_row_cells_free", None, v
        )
        self.row_cells_next = self.prototype(
            "ghostty_render_state_row_cells_next", b, v
        )
        self.row_cells_get = self.prototype(
            "ghostty_render_state_row_cells_get", r, v, i, v
        )
        self.key_encoder_new = self.prototype(
            "ghostty_key_encoder_new", r, v, ctypes.POINTER(v)
        )
        self.key_encoder_free = self.prototype(
            "ghostty_key_encoder_free", None, v
        )
        self.key_encoder_encode = self.prototype(
            "ghostty_key_encoder_encode",
            r,
            v,
            v,
            ctypes.c_char_p,
            sz,
            ctypes.POINTER(sz),
        )
        self.key_encoder_setopt_from_terminal = self.prototype(
            "ghostty_key_encoder_setopt_from_terminal", None, v, v
        )
        self.key_event_new = self.prototype(
            "ghostty_key_event_new", r, v, ctypes.POINTER(v)
        )
        self.key_event_free = self.prototype("ghostty_key_event_free", None, v)
        for name in ("set_key", "set_action", "set_mods", "set_consumed_mods"):
            setattr(
                self,
                "key_event_" + name,
                self.prototype("ghostty_key_event_" + name, None, v, u16),
            )
        self.key_event_set_unshifted_codepoint = self.prototype(
            "ghostty_key_event_set_unshifted_codepoint",
            None,
            v,
            ctypes.c_uint32,
        )
        self.key_event_set_utf8 = self.prototype(
            "ghostty_key_event_set_utf8", None, v, ctypes.c_char_p, sz
        )
        self.mouse_encoder_new = self.prototype(
            "ghostty_mouse_encoder_new", r, v, ctypes.POINTER(v)
        )
        self.mouse_encoder_free = self.prototype(
            "ghostty_mouse_encoder_free", None, v
        )
        self.mouse_encoder_encode = self.prototype(
            "ghostty_mouse_encoder_encode",
            r,
            v,
            v,
            ctypes.c_char_p,
            sz,
            ctypes.POINTER(sz),
        )
        # void in the C ABI, like terminal_scroll_viewport
        # above.
        self.mouse_encoder_setopt = self.prototype(
            "ghostty_mouse_encoder_setopt", None, v, i, v
        )
        self.mouse_encoder_setopt_from_terminal = self.prototype(
            "ghostty_mouse_encoder_setopt_from_terminal", None, v, v
        )
        self.mouse_event_new = self.prototype(
            "ghostty_mouse_event_new", r, v, ctypes.POINTER(v)
        )
        self.mouse_event_free = self.prototype(
            "ghostty_mouse_event_free", None, v
        )
        self.mouse_event_set_action = self.prototype(
            "ghostty_mouse_event_set_action", None, v, i
        )
        self.mouse_event_set_button = self.prototype(
            "ghostty_mouse_event_set_button", None, v, i
        )
        self.mouse_event_clear_button = self.prototype(
            "ghostty_mouse_event_clear_button", None, v
        )
        self.mouse_event_set_mods = self.prototype(
            "ghostty_mouse_event_set_mods", None, v, u16
        )
        self.mouse_event_set_position = self.prototype(
            "ghostty_mouse_event_set_position", None, v, MousePosition
        )
        self.focus_encode = self.prototype(
            "ghostty_focus_encode",
            r,
            i,
            ctypes.c_char_p,
            sz,
            ctypes.POINTER(sz),
        )
        self.terminal_grid_ref = self.prototype(
            "ghostty_terminal_grid_ref",
            r,
            v,
            Point,
            ctypes.POINTER(GridRef),
        )
        self.grid_ref_hyperlink_uri = self.prototype(
            "ghostty_grid_ref_hyperlink_uri",
            r,
            ctypes.POINTER(GridRef),
            ctypes.c_char_p,
            sz,
            ctypes.POINTER(sz),
        )
        self.grid_ref_row = self.prototype(
            "ghostty_grid_ref_row",
            r,
            ctypes.POINTER(GridRef),
            ctypes.POINTER(ctypes.c_uint64),
        )
        self.grid_row_get = self.prototype(
            "ghostty_row_get", r, ctypes.c_uint64, i, v
        )
        self.selection_gesture_new = self.prototype(
            "ghostty_selection_gesture_new", r, v, ctypes.POINTER(v)
        )
        self.selection_gesture_free = self.prototype(
            "ghostty_selection_gesture_free", None, v, v
        )
        self.selection_gesture_reset = self.prototype(
            "ghostty_selection_gesture_reset", None, v, v
        )
        self.selection_gesture_get = self.prototype(
            "ghostty_selection_gesture_get", r, v, v, i, v
        )
        self.selection_gesture_event_new = self.prototype(
            "ghostty_selection_gesture_event_new",
            r,
            v,
            ctypes.POINTER(v),
            i,
        )
        self.selection_gesture_event_free = self.prototype(
            "ghostty_selection_gesture_event_free", None, v
        )
        self.selection_gesture_event_set = self.prototype(
            "ghostty_selection_gesture_event_set", r, v, i, v
        )
        self.selection_gesture_event = self.prototype(
            "ghostty_selection_gesture_event",
            r,
            v,
            v,
            v,
            ctypes.POINTER(Selection),
        )
        self.selection_format_buf = self.prototype(
            "ghostty_terminal_selection_format_buf",
            r,
            v,
            SelectionFormatOptions,
            ctypes.c_char_p,
            sz,
            ctypes.POINTER(sz),
        )
        self.terminal_select_word = self.prototype(
            "ghostty_terminal_select_word",
            r,
            v,
            ctypes.POINTER(SelectWordOptions),
            ctypes.POINTER(Selection),
        )
        self.terminal_select_line = self.prototype(
            "ghostty_terminal_select_line",
            r,
            v,
            ctypes.POINTER(SelectLineOptions),
            ctypes.POINTER(Selection),
        )
        self.search_new = self.prototype(
            "ghostty_search_new", r, v, ctypes.POINTER(v), v
        )
        self.search_free = self.prototype("ghostty_search_free", None, v)
        self.search_run = self.prototype("ghostty_search_run", r, v)
        self.search_feed = self.prototype("ghostty_search_feed", r, v)
        self.search_set = self.prototype("ghostty_search_set", r, v, i, v)
        self.search_get = self.prototype("ghostty_search_get", r, v, i, v)
        self.terminal_point_from_grid_ref = self.prototype(
            "ghostty_terminal_point_from_grid_ref",
            r,
            v,
            ctypes.POINTER(GridRef),
            i,
            ctypes.POINTER(PointCoordinate),
        )
        self.build_info = self.prototype("ghostty_build_info", r, i, v)

    def get(self, fn, handle, selector, buf):
        """Call one ``*_get`` function with ``buf`` as the out
        parameter — the iterator handles among them are in/out
        parameters, so the caller passes a pre-filled handle
        slot (#935). Returns ``(ok, buf)``; ok is False when the
        library reports NO_VALUE or another failure."""
        ok = fn(handle, selector, ctypes.byref(buf)) == SUCCESS
        return ok, buf

    def new_handle(self, fn, *args) -> int:
        """Call one ``*_new`` constructor; ``args`` follows the
        out handle (terminal_new takes cols/rows, the rest take
        nothing). The handle slot is pre-filled — the spike's
        iterator lesson (#935): the library writes through the
        slot, and an unfilled one reads whatever was there."""
        handle = ctypes.c_void_p()
        if fn(None, ctypes.byref(handle), *args) != SUCCESS:
            raise RuntimeError("libghostty-vt allocation failed")
        return handle.value or 0

    def encode_key(self, encoder: int, event: int) -> bytes:
        """One key event as the bytes the child reads; an empty
        answer is the encoder's refusal."""
        buf = ctypes.create_string_buffer(128)
        written = ctypes.c_size_t()
        result = self.key_encoder_encode(
            encoder, event, buf, len(buf), ctypes.byref(written)
        )
        if result != SUCCESS:
            return b""
        return buf.raw[: written.value]

    def encode_mouse(self, encoder: int, event: int) -> bytes:
        """One mouse event as the bytes the child reads; an
        empty answer is the encoder's refusal."""
        buf = ctypes.create_string_buffer(128)
        written = ctypes.c_size_t()
        result = self.mouse_encoder_encode(
            encoder, event, buf, len(buf), ctypes.byref(written)
        )
        if result != SUCCESS:
            return b""
        return buf.raw[: written.value]

    def encode_focus(self, gained: bool) -> bytes:
        """One focus report as the bytes the child reads (mode
        1004's sequences)."""
        buf = ctypes.create_string_buffer(8)
        written = ctypes.c_size_t()
        result = self.focus_encode(
            0 if gained else 1, buf, len(buf), ctypes.byref(written)
        )
        if result != SUCCESS:
            return b""
        return buf.raw[: written.value]

    def vt_write(self, terminal: int, data: bytes) -> None:
        """Feed VT bytes to the terminal: the emulator's
        input."""
        self.terminal_vt_write(
            terminal,
            ctypes.cast(ctypes.c_char_p(data), ctypes.POINTER(ctypes.c_uint8)),
            len(data),
        )

    def build_version(self) -> str:
        """The library's own build version, for the About line
        and the logs; ``unknown`` when the library will not
        say."""
        out = ctypes.c_char_p()
        if (
            self.build_info(
                BUILD_INFO_VERSION_STRING,
                ctypes.cast(ctypes.byref(out), ctypes.c_void_p),
            )
            == SUCCESS
            and out.value
        ):
            return out.value.decode()
        return "unknown"
