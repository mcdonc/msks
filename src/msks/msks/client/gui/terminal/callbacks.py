"""The library's effects callbacks (phase 4.5 of #924, #952).

libghostty-vt answers VT queries and reports state changes
through callbacks registered on the terminal; ctypes needs
them as module-level functions, so a registry keyed by the
widget's ``id`` carries the userdata pointer back to the
screen it serves. The module imports no Qt — the callbacks
reach the widget through its own attributes only.
"""

from __future__ import annotations

import ctypes

from .. import ghostty as gt

# The live screens, keyed by the userdata id each registered
# with — the pointer the library hands back on every call.
REGISTRY: dict[int, object] = {}


def register(screen: object) -> None:
    """Seat a screen in the registry under its id — the value
    the library hands back on every callback."""
    REGISTRY[id(screen)] = screen


def unregister(screen: object) -> None:
    """Drop a closed screen from the registry."""
    REGISTRY.pop(id(screen), None)


def screen_for(userdata) -> object | None:
    """The registered screen for a userdata pointer, when one
    is seated (a freed screen's late callback finds none)."""
    if not userdata:
        return None
    return REGISTRY.get(userdata)


@gt.WRITE_PTY_FN
def write_pty(_terminal, userdata, data, length) -> None:
    """The emulator's answer bytes: what the child reads."""
    screen = screen_for(userdata)
    if screen is not None and data:
        screen.write(bytes(bytearray(data[:length])))


@gt.SIZE_FN
def report_size(_terminal, userdata, out) -> bool:
    """The terminal's window size query (CSI 14 t)."""
    screen = screen_for(userdata)
    if screen is None:
        return False
    out.contents.rows = screen.rows
    out.contents.columns = screen.cols
    out.contents.cell_width = screen.metrics.cell_width
    out.contents.cell_height = screen.metrics.cell_height
    return True


@gt.DEVICE_ATTRIBUTES_FN
def device_attributes(_terminal, _userdata, out) -> bool:
    """The primary/secondary/tertiary device attributes (CSI
    c): a VT220 with 132-column mode, selective erase, and
    ANSI color."""
    attrs = out.contents
    attrs.primary.conformance_level = gt.DA_CONFORMANCE_VT220
    features = [
        gt.DA_FEATURE_COLUMNS_132,
        gt.DA_FEATURE_SELECTIVE_ERASE,
        gt.DA_FEATURE_ANSI_COLOR,
    ]
    for index, feature in enumerate(features):
        attrs.primary.features[index] = feature
    attrs.primary.num_features = len(features)
    attrs.secondary.device_type = gt.DA_DEVICE_TYPE_VT220
    attrs.secondary.firmware_version = 1
    attrs.secondary.rom_cartridge = 0
    attrs.tertiary.unit_id = 0
    return True


@gt.TITLE_CHANGED_FN
def title_changed(_terminal, userdata) -> None:
    """OSC 0/2: the program inside retitled the tab."""
    screen = screen_for(userdata)
    if screen is None:
        return
    ok, title = screen.lib.get(
        screen.lib.terminal_get,
        screen.terminal,
        gt.DATA_TITLE,
        gt.GhosttyString(),
    )
    # The library answers a title even when none was set (the
    # empty default); a tab retitles only on real text.
    if ok and title.text():
        screen.title_changed.emit(title.text())


@gt.COLOR_SCHEME_FN
def color_scheme(_terminal, _userdata, _out) -> bool:
    """The light/dark scheme query: this widget owns no scheme
    to report — the palette lives in the library."""
    return False


@gt.BELL_FN
def bell(_terminal, userdata) -> None:
    """BEL: flash the border instead of ringing."""
    screen = screen_for(userdata)
    if screen is not None:
        screen.flash()


@gt.CLIPBOARD_WRITE_FN
def clipboard_write(_terminal, userdata, write_ptr) -> None:
    """OSC 52: a program inside wants to set the clipboard.
    The screen applies the parsed payload to Qt's clipboard
    and answers the reply — Qt lives on the screen, not here.
    """
    screen = screen_for(userdata)
    if screen is None or not write_ptr:
        return
    write = ctypes.cast(write_ptr, ctypes.POINTER(gt.ClipboardWrite)).contents
    screen.osc52_clipboard(write)


def clipboard_payload(write) -> tuple[str | None, int]:
    """An OSC 52 write's plain-text payload and location:
    the first text/plain content (the empty mime counts, the
    library's own default), else None when nothing in the
    request is text."""
    for index in range(write.contents_len):
        content = write.contents[index]
        if content.mime.text() in (
            "",
            "text/plain",
            "text/plain;charset=utf-8",
        ):
            return content.data.text(), write.location
    return None, write.location


def registrations(screen: object) -> list[tuple[int, ctypes._CData]]:
    """The option/value pairs that seat a screen's callbacks on
    its terminal: the userdata id beside every effect."""
    uid = ctypes.c_void_p(id(screen))
    return [
        (gt.OPT_USERDATA, uid),
        (gt.OPT_WRITE_PTY, ctypes.cast(write_pty, ctypes.c_void_p)),
        (gt.OPT_SIZE, ctypes.cast(report_size, ctypes.c_void_p)),
        (
            gt.OPT_DEVICE_ATTRIBUTES,
            ctypes.cast(device_attributes, ctypes.c_void_p),
        ),
        (
            gt.OPT_TITLE_CHANGED,
            ctypes.cast(title_changed, ctypes.c_void_p),
        ),
        (
            gt.OPT_COLOR_SCHEME,
            ctypes.cast(color_scheme, ctypes.c_void_p),
        ),
        (gt.OPT_BELL, ctypes.cast(bell, ctypes.c_void_p)),
        (
            gt.OPT_CLIPBOARD_WRITE,
            ctypes.cast(clipboard_write, ctypes.c_void_p),
        ),
    ]
