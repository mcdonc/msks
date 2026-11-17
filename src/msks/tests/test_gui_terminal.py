"""The terminal widget package's suite (phase 4.5 of #924,
#952): the cell grid painted offscreen, the input encoders,
the font rules, the resize path, the scrollback, and the
session lifecycle — real children on real ptys, no display.

The Qt names and the package modules import inside the ``qt``
fixture, never at module scope: the runner's controller
process imports this file at collection time and has no
business loading the binding (test_gui_bridge's docstring
states the trap).
"""

import ctypes
import os
import string
import subprocess
import sys
import textwrap
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from msks.client.gui import ghostty as gt

#: The session's fork fires CPython's multi-threaded-fork
#: DeprecationWarning: Qt's platform threads make the test
#: process multi-threaded, and the child's pre-exec work is
#: the minimal close/session/ctty/dup/exec ladder — the same
#: shape stdlib pty.fork runs — so the warning is noise here,
#: not a deadlock the suite can hit (#951's suite filters the
#: same).
pytestmark = pytest.mark.filterwarnings(
    "ignore:This process.*multi-threaded.*fork:DeprecationWarning"
)

#: A child that announces itself and then waits for its parent
#: to hang up: the stand-in for the ssh child a tab really
#: spawns.
SLEEPER = textwrap.dedent(
    """
    import time
    print("ready", flush=True)
    time.sleep(30)
    """
)

#: The readback child: it echoes its stdin verbatim, so the
#: input pins read exactly the bytes the terminal wrote (the
#: pty's own kernel echo renders escapes in caret notation,
#: so the fixture turns it off and the child answers raw).
ECHOER = textwrap.dedent(
    """
    import sys
    print("ready", flush=True)
    while True:
        data = sys.stdin.buffer.read1(4096)
        if not data:
            break
        sys.stdout.buffer.write(data)
        sys.stdout.buffer.flush()
    """
)

#: A child that answers its terminal size whenever it gets a
#: line: the resize pin reads the child's own view of the
#: window, before and after a resize.
SIZER = textwrap.dedent(
    """
    import array, fcntl, sys, termios
    print("ready", flush=True)
    for line in sys.stdin:
        buf = array.array("H", [0, 0, 0, 0])
        fcntl.ioctl(0, termios.TIOCGWINSZ, buf)
        print("cols=%d rows=%d" % (buf[1], buf[0]), flush=True)
    """
)


@pytest.fixture()
def qt(tmp_path: Path):
    """The offscreen ``QApplication`` the widget needs — the
    host's fontconfig intact, because the font pins read the
    database and the rasterizer — and the package's modules
    imported on the worker side only."""
    os.environ["QT_QPA_PLATFORM"] = "offscreen"
    from msks.client.gui.terminal import fonts, input, paint
    from PySide6 import QtCore, QtGui, QtWidgets

    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication(
        ["msks-test"]
    )
    return SimpleNamespace(
        app=app,
        QtCore=QtCore,
        QtGui=QtGui,
        QtWidgets=QtWidgets,
        fonts=fonts,
        input=input,
        paint=paint,
    )


@pytest.fixture()
def screen(qt):
    from msks.client.gui.terminal import TerminalWidget

    widget = TerminalWidget([sys.executable, "-c", ECHOER])
    hush_echo(widget)
    drain_until(widget, b"ready")
    yield widget
    shutdown(widget)


def hush_echo(widget) -> None:
    """Make the pty raw enough that the echo child answers with
    the exact bytes: the kernel echo off (it renders escapes in
    caret notation) and canonical mode off (it would hold every
    write until a newline)."""
    import termios

    attrs = termios.tcgetattr(widget.session.master_fd)
    attrs[3] &= ~(termios.ECHO | termios.ECHOCTL | termios.ICANON)
    termios.tcsetattr(widget.session.master_fd, termios.TCSANOW, attrs)


def shutdown(widget) -> None:
    """Drive the widget's close path directly: ``close()`` on a
    widget Qt never showed sends no close event, and the
    teardown must run whatever the window state."""
    from PySide6.QtGui import QCloseEvent

    widget.closeEvent(QCloseEvent())


def drain_until(widget, marker: bytes, timeout: float = 5.0) -> bytes:
    """Read the master until the marker shows up (the pty's
    echo answers what the terminal writes), feeding the
    emulator as we go so the widget's state stays current."""
    deadline = time.monotonic() + timeout
    seen = b""
    while time.monotonic() < deadline:
        chunk = widget.session.read_available()
        if chunk:
            seen += chunk
            widget.lib.vt_write(widget.terminal, chunk)
            widget.refresh()
            if marker in seen:
                return seen
        else:
            time.sleep(0.02)
    return seen


def vt(widget, data: bytes) -> None:
    """Feed VT bytes straight into the emulator (the paint and
    mode pins drive the terminal directly, not a child)."""
    widget.lib.vt_write(widget.terminal, data)
    widget.refresh()


def scrollbar(widget) -> tuple[int, int, int]:
    """The terminal's scrollbar (total, viewport, offset)."""
    ok, bar = widget.lib.get(
        widget.lib.terminal_get,
        widget.terminal,
        gt.DATA_SCROLLBAR,
        gt.Scrollbar(),
    )
    assert ok
    return bar.total, bar.len, bar.offset


def key_event(qt, key, text="", modifiers=None):
    """A synthetic key press, offscreen."""
    return qt.QtGui.QKeyEvent(
        qt.QtCore.QEvent.Type.KeyPress,
        int(key),
        modifiers or qt.QtCore.Qt.KeyboardModifier.NoModifier,
        text,
    )


# --- fonts ---------------------------------------------------------------


def test_the_face_resolves_and_measures_as_one(qt):
    """The resolved family exists in the database and the
    metrics come from the same face the rasterizer will draw
    (the klangk flterm lesson: check the rasterized family,
    not just the metrics — mcdonc/klangk#2)."""
    fonts, QtGui = qt.fonts, qt.QtGui
    font = fonts.make_font()
    assert QtGui.QFontInfo(font).family() == font.family()
    metrics = fonts.measure(font)
    measured = QtGui.QFontMetrics(font).horizontalAdvance(fonts.REPCHAR)
    assert metrics.cell_width == round(measured / len(fonts.REPCHAR))
    assert metrics.cell_width >= 1
    assert metrics.cell_height >= 1
    assert metrics.ascent == QtGui.QFontMetrics(font).ascent()


def test_the_row_height_caps_at_twelve_tenths_em(qt):
    """The cell height is ascent plus descent, trimmed to 1.2
    em — the line-height terminals ship."""
    fonts, QtGui = qt.fonts, qt.QtGui
    font = fonts.make_font()
    metrics = fonts.measure(font)
    em = QtGui.QFontInfo(font).pixelSize()
    assert metrics.cell_height == min(
        QtGui.QFontMetrics(font).height(), round(em * fonts.ROW_HEIGHT_EM)
    )


def test_the_family_falls_back_to_the_hint(qt, monkeypatch):
    """Nothing preferred installed resolves the generic hint —
    the floor, never an error."""
    fonts = qt.fonts

    class Database:
        @staticmethod
        def families():
            return ["Comic Sans"]

    monkeypatch.setattr(fonts, "QFontDatabase", Database)
    assert fonts.resolve_family() == fonts.PREFERRED[-1]

    class HasFira:
        @staticmethod
        def families():
            return ["Fira Code", "Comic Sans"]

    monkeypatch.setattr(fonts, "QFontDatabase", HasFira)
    assert fonts.resolve_family() == "Fira Code"


def test_the_variants_carry_the_four_styles(qt):
    fonts = qt.fonts
    variants = fonts.variants(fonts.make_font())
    assert set(variants) == {"normal", "bold", "italic", "bold_italic"}
    assert variants["bold"].bold()
    assert variants["italic"].italic()
    assert not variants["normal"].bold()
    assert fonts.font_key(True, True) == "bold_italic"
    assert fonts.font_key(True, False) == "bold"
    assert fonts.font_key(False, True) == "italic"
    assert fonts.font_key(False, False) == "normal"


# --- the key table -------------------------------------------------------


def test_the_qt_key_table_maps_digits_letters_and_names(qt):
    input, Qt = qt.input, qt.QtCore.Qt
    table = input.qt_keys()
    for digit in string.digits:
        assert table[48 + int(digit)] == gt.KEY[f"DIGIT_{digit}"]
    assert table[int(Qt.Key.Key_A)] == gt.KEY["A"]
    assert table[int(Qt.Key.Key_Up)] == gt.KEY["ARROW_UP"]
    assert table[int(Qt.Key.Key_Return)] == gt.KEY["ENTER"]
    # The shifted symbols map to their base keys (Wayland's
    # Shift+; arrives as Key_Colon).
    assert table[int(Qt.Key.Key_Colon)] == gt.KEY["SEMICOLON"]
    assert table[int(Qt.Key.Key_Plus)] == gt.KEY["EQUAL"]
    # Digits live at Qt codes 48-57, never at 0-9.
    assert all(table.get(code) is None for code in range(0, 10))
    assert input.qt_keys() is table  # built once, then cached


def test_the_unshifted_table_covers_the_printable_us_layout():
    from msks.client.gui.terminal import input as input_mod

    table = input_mod.UNSHIFTED
    for letter in string.ascii_lowercase:
        assert table[gt.KEY[letter.upper()]] == ord(letter)
    assert table[gt.KEY["SPACE"]] == ord(" ")
    assert table[gt.KEY["SEMICOLON"]] == ord(";")


def test_key_action_repeats_only_on_repeat():
    from msks.client.gui.terminal import input as input_mod

    assert input_mod.key_action(False) == gt.ACTION_PRESS
    assert input_mod.key_action(True) == gt.ACTION_REPEAT


# --- painting ------------------------------------------------------------


def test_cells_paint_with_their_colors_offscreen(screen, qt):
    """SGR-colored text paints: the red run's glyphs land on
    the grid, straight from the render state."""
    vt(screen, b"\x1b[2J\x1b[Hplain \x1b[31mred\x1b[0m tail")
    image = screen.grab().toImage()
    assert image.size() == screen.size()
    # The red run's cells carry the 24-bit red the emulator
    # resolved — read through the same row walk the painter
    # takes.
    ok, iterator = screen.lib.get(
        screen.lib.render_state_get,
        screen.render_state,
        gt.RS_ROW_ITERATOR,
        ctypes.c_void_p(screen.row_iterator),
    )
    assert ok
    reds = []
    while screen.lib.row_iterator_next(iterator.value):
        ok, cells = screen.lib.get(
            screen.lib.row_get,
            iterator.value,
            gt.ROW_CELLS,
            ctypes.c_void_p(screen.row_cells),
        )
        while ok and screen.lib.row_cells_next(cells.value):
            ok_len, length = screen.lib.get(
                screen.lib.row_cells_get,
                cells,
                gt.CELLS_GRAPHEMES_LEN,
                ctypes.c_uint32(),
            )
            if not ok_len or not length.value:
                continue
            ok_fg, fg = screen.lib.get(
                screen.lib.row_cells_get,
                cells,
                gt.CELLS_FG_COLOR,
                gt.ColorRgb(),
            )
            if ok_fg and fg.r > 180 and fg.r > fg.g and fg.r > fg.b:
                reds.append(fg.r)
    assert len(reds) == 3


def test_painted_text_lands_in_the_right_cells(screen, qt):
    """A paint fills the grid and draws the glyphs: a row of
    M-glyphs puts ink inside its cells and none in the row
    below."""
    vt(screen, b"\x1b[2J\x1b[HMMMMMMMM")
    image = screen.grab().toImage()
    x = screen.metrics.cell_width // 2
    top = image.pixelColor(x, screen.metrics.cell_height // 2)
    below = image.pixelColor(x, screen.metrics.cell_height * 3 // 2)
    assert top != below, "row 0 carries glyph ink"


def test_the_inverse_and_faint_styles_paint(screen, qt):
    """Inverse swaps the cell's colors and faint dims the ink —
    both paint without changing the grid — and an empty cell
    still fills its explicit background."""
    plain = screen.grab().toImage()
    vt(
        screen,
        b"\x1b[2J\x1b[H\x1b[7;2minverse faint\x1b[0m\r\n\x1b[41m\x1b[K",
    )
    styled = screen.grab().toImage()
    assert styled != plain


def test_the_cursor_paints_over_the_grid(screen, qt):
    """The cursor cell paints translucent: the grid with it
    shown differs from the grid with it hidden."""
    vt(screen, b"\x1b[2J\x1b[H\x1b[?25lhidden")
    hidden = screen.grab().toImage()
    vt(screen, b"\x1b[?25h")
    shown = screen.grab().toImage()
    assert hidden != shown


def test_the_bell_flash_paints_and_clears(screen, qt):
    vt(screen, b"\x07")
    assert screen.bell_flash is True
    flashing = screen.grab().toImage()
    screen.bell_off()
    assert screen.bell_flash is False
    assert flashing != screen.grab().toImage()


def test_the_exit_banner_paints_over_a_dead_child(qt):
    from msks.client.gui.terminal import TerminalWidget

    widget = TerminalWidget([sys.executable, "-c", "raise SystemExit(3)"])
    try:
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline and not widget.session.exited:
            widget.read_master()
            time.sleep(0.02)
        assert widget.session.exited
        widget.refresh()
        assert widget.grab().toImage().size() == widget.size()
    finally:
        shutdown(widget)


def test_paint_without_a_palette_draws_nothing(screen, qt):
    """A render state that answers no palette paints nothing —
    the C API's NO_VALUE contract, pinned on a stub whose get
    answers False: a real fresh state's first answer flips
    between processes (probed both ways), so the stub carries
    the contract, not the timing."""

    class Paletteless:
        """A screen whose render state answers no palette."""

        class lib:
            render_state_get = None  # the stub's get ignores it

            @staticmethod
            def get(fn, handle, selector, buf):
                return False, buf

        render_state = 0

    from msks.client.gui.terminal import paint as paint_mod

    painter = qt.QtGui.QPainter(screen)
    try:
        paint_mod.paint_event(Paletteless(), painter)  # draws nothing
    finally:
        painter.end()


def test_paint_on_a_fresh_render_state_is_clean(screen, qt):
    """A render state never updated paints without a crash —
    whatever it answers for the palette, the grid stays
    whole."""
    saved = screen.render_state
    fresh = screen.lib.new_handle(screen.lib.render_state_new)
    screen.render_state = fresh
    try:
        assert screen.grab().toImage().size() == screen.size()
    finally:
        screen.render_state = saved
        screen.lib.render_state_free(fresh)


# --- input ---------------------------------------------------------------


def test_arrow_keys_encode_legacy_and_application(screen, qt):
    Qt = qt.QtCore.Qt
    screen.keyPressEvent(key_event(qt, Qt.Key.Key_Up))
    assert b"\x1b[A" in drain_until(screen, b"\x1b[A")
    vt(screen, b"\x1b[?1h")  # DECCKM: application cursors
    screen.keyPressEvent(key_event(qt, Qt.Key.Key_Up))
    assert b"\x1bOA" in drain_until(screen, b"\x1bOA")


def test_plain_text_encodes_as_itself(screen, qt):
    screen.keyPressEvent(key_event(qt, qt.QtCore.Qt.Key.Key_A, "a"))
    assert b"a" in drain_until(screen, b"a")


def test_control_keys_encode_through_the_modifier_mask(qt):
    """Ctrl+C is the interrupt byte: the encoder spells it
    from the key and the mask, and the pty's line discipline
    turns it into SIGINT for the child — the interrupt a real
    terminal delivers."""
    from msks.client.gui.terminal import TerminalWidget

    widget = TerminalWidget([sys.executable, "-c", SLEEPER])
    try:
        drain_until(widget, b"ready")
        Qt = qt.QtCore.Qt
        widget.keyPressEvent(
            key_event(
                qt,
                Qt.Key.Key_C,
                "\x03",
                Qt.KeyboardModifier.ControlModifier,
            )
        )
        deadline = time.monotonic() + 5.0
        while (
            time.monotonic() < deadline and widget.session.exit_status is None
        ):
            widget.session.reap()
            time.sleep(0.02)
        # 128 + SIGINT: the pty's line discipline delivered the
        # interrupt to the child.
        assert widget.session.exit_status == 130
    finally:
        shutdown(widget)


def test_a_release_without_the_kitty_protocol_sends_nothing(screen, qt):
    """Legacy releases encode to nothing — the encoder's
    refusal arm."""
    screen.keyPressEvent(key_event(qt, qt.QtCore.Qt.Key.Key_A, "a"))
    drain_until(screen, b"a")
    release = qt.QtGui.QKeyEvent(
        qt.QtCore.QEvent.Type.KeyRelease,
        int(qt.QtCore.Qt.Key.Key_A),
        qt.QtCore.Qt.KeyboardModifier.NoModifier,
        "a",
    )
    screen.keyReleaseEvent(release)
    time.sleep(0.1)
    assert screen.session.read_available() in (None, b"")


def test_kitty_protocol_press_and_release(screen, qt):
    """With the Kitty flags pushed, a modified key encodes its
    unicode key code and modifiers (and a release carries the
    release action) instead of the legacy byte."""
    Qt = qt.QtCore.Qt
    vt(screen, b"\x1b[>3u")  # disambiguate + report event types
    screen.keyPressEvent(
        key_event(
            qt,
            Qt.Key.Key_A,
            "\x01",
            Qt.KeyboardModifier.ControlModifier,
        )
    )
    seen = drain_until(screen, b"97")
    assert b"\x1b[97;5u" in seen, seen
    release = qt.QtGui.QKeyEvent(
        qt.QtCore.QEvent.Type.KeyRelease,
        int(Qt.Key.Key_A),
        qt.QtCore.Qt.KeyboardModifier.ControlModifier,
        "\x01",
    )
    screen.keyReleaseEvent(release)
    seen = drain_until(screen, b":3")
    assert b"\x1b[97;5:3u" in seen, seen


def test_keys_outside_the_table_fall_back_to_their_text(screen, qt):
    """A key the table holds nothing for sends its printable
    text — input is never silently dropped; a release of one
    sends nothing, and neither does unprintable text."""
    exotic = qt.QtCore.Qt.Key.Key_ydiaeresis
    assert int(exotic) not in qt.input.qt_keys()
    screen.keyPressEvent(key_event(qt, exotic, "ÿ"))
    assert "ÿ".encode() in drain_until(screen, "ÿ".encode())
    release = qt.QtGui.QKeyEvent(
        qt.QtCore.QEvent.Type.KeyRelease,
        int(exotic),
        qt.QtCore.Qt.KeyboardModifier.NoModifier,
        "ÿ",
    )
    screen.keyReleaseEvent(release)
    time.sleep(0.1)
    assert screen.session.read_available() in (None, b"")
    # Unprintable text from an unmapped key drops too.
    screen.keyPressEvent(key_event(qt, exotic, "\x01"))
    time.sleep(0.1)
    assert screen.session.read_available() in (None, b"")


def test_modifiers_ride_the_mask(screen, qt):
    Qt = qt.QtCore.Qt
    event = key_event(
        qt, Qt.Key.Key_A, "a", Qt.KeyboardModifier.ControlModifier
    )
    assert qt.input.mods(event.modifiers()) == gt.MOD_CTRL
    both = key_event(
        qt,
        Qt.Key.Key_A,
        "a",
        Qt.KeyboardModifier.ControlModifier
        | Qt.KeyboardModifier.ShiftModifier,
    )
    assert qt.input.mods(both.modifiers()) == gt.MOD_CTRL | gt.MOD_SHIFT
    alted = key_event(
        qt,
        qt.QtCore.Qt.Key.Key_A,
        "a",
        qt.QtCore.Qt.KeyboardModifier.AltModifier,
    )
    assert qt.input.mods(alted.modifiers()) == gt.MOD_ALT
    metaed = key_event(
        qt,
        qt.QtCore.Qt.Key.Key_A,
        "a",
        qt.QtCore.Qt.KeyboardModifier.MetaModifier,
    )
    assert qt.input.mods(metaed.modifiers()) == gt.MOD_SUPER


def test_button_for_maps_the_three_buttons(qt):
    class Fake:
        def __init__(self, button):
            self._button = button

        def button(self):
            return self._button

    Qt = qt.QtCore.Qt
    assert qt.input.button_for(Fake(Qt.MouseButton.LeftButton)) == (
        gt.MOUSE_LEFT
    )
    assert qt.input.button_for(Fake(Qt.MouseButton.RightButton)) == (
        gt.MOUSE_RIGHT
    )
    assert qt.input.button_for(Fake(Qt.MouseButton.MiddleButton)) == (
        gt.MOUSE_MIDDLE
    )
    assert qt.input.button_for(Fake(Qt.MouseButton.NoButton)) is None


def mouse_event(qt, kind, pos, button, buttons):
    return qt.QtGui.QMouseEvent(
        kind,
        qt.QtCore.QPointF(*pos),
        qt.QtCore.QPointF(*pos),
        button,
        buttons,
        qt.QtCore.Qt.KeyboardModifier.NoModifier,
    )


def test_the_mouse_encodes_sgr_presses(screen, qt):
    Qt = qt.QtCore.Qt
    vt(screen, b"\x1b[?1000h\x1b[?1006h")  # press/release, SGR
    cell = (
        screen.metrics.cell_width * 4 + 2,
        screen.metrics.cell_height * 2 + 2,
    )
    screen.mousePressEvent(
        mouse_event(
            qt,
            qt.QtGui.QMouseEvent.Type.MouseButtonPress,
            cell,
            Qt.MouseButton.LeftButton,
            Qt.MouseButton.LeftButton,
        )
    )
    assert b"\x1b[<0;5;3M" in drain_until(screen, b"M")
    screen.mouseReleaseEvent(
        mouse_event(
            qt,
            qt.QtGui.QMouseEvent.Type.MouseButtonRelease,
            cell,
            qt.QtCore.Qt.MouseButton.LeftButton,
            qt.QtCore.Qt.MouseButton.LeftButton,
        )
    )
    assert b"\x1b[<0;5;3m" in drain_until(screen, b"m")


def test_motion_without_tracking_sends_nothing(screen, qt):
    """With the child not tracking the mouse, motion encodes
    to nothing — the encoder refuses it."""
    Qt = qt.QtCore.Qt
    screen.mouseMoveEvent(
        mouse_event(
            qt,
            qt.QtGui.QMouseEvent.Type.MouseMove,
            (5, 5),
            Qt.MouseButton.NoButton,
            Qt.MouseButton.NoButton,
        )
    )
    time.sleep(0.1)
    assert screen.session.read_available() in (None, b"")


def wheel_event(qt, dy: int):
    return qt.QtGui.QWheelEvent(
        qt.QtCore.QPointF(10, 10),
        qt.QtCore.QPointF(10, 10),
        qt.QtCore.QPoint(),
        qt.QtCore.QPoint(0, dy),
        qt.QtCore.Qt.MouseButton.NoButton,
        qt.QtCore.Qt.KeyboardModifier.NoModifier,
        qt.QtCore.Qt.ScrollPhase.NoScrollPhase,
        False,
    )


def test_the_wheel_pages_the_buffer(screen, qt):
    """Wheel up walks into the scrollback; the scrollbar's
    offset answers where the viewport sits."""
    for index in range(40):
        vt(screen, f"line {index}\r\n".encode())
    before = scrollbar(screen)
    screen.wheelEvent(wheel_event(qt, 120))  # one notch up
    after = scrollbar(screen)
    assert after[2] < before[2]
    with_thumb = screen.grab().toImage()  # the thumb paints
    screen.wheelEvent(wheel_event(qt, -120))  # one notch down
    assert scrollbar(screen)[2] == before[2]
    assert screen.grab().toImage() != with_thumb


def test_the_wheel_feeds_a_tracking_child(screen, qt):
    """A child that enables mouse reporting owns the wheel:
    button four arrives as a press/release pair."""
    vt(screen, b"\x1b[?1000h\x1b[?1006h")
    screen.wheelEvent(wheel_event(qt, 120))
    seen = drain_until(screen, b"\x1b[<64;")
    assert seen.count(b"\x1b[<64;") == 2


def test_the_wheel_accumulates_fractions(screen, qt):
    """Two half-notches make one step, sub-notch leftovers
    stay accumulated."""
    assert scrollbar(screen)[2] == 0
    screen.wheelEvent(wheel_event(qt, 60))
    assert scrollbar(screen)[2] == 0
    for index in range(30):
        vt(screen, f"row {index}\r\n".encode())
    before = scrollbar(screen)[2]
    screen.wheelEvent(wheel_event(qt, 60))
    assert scrollbar(screen)[2] < before


def test_wheel_steps_accumulate_and_scale():
    from msks.client.gui.terminal import input as input_mod

    assert input_mod.wheel_steps(0, None, 120, 26) == (1, 0)
    assert input_mod.wheel_steps(0, None, 40, 26) == (0, 40)
    assert input_mod.wheel_steps(80, None, 40, 26) == (1, 0)
    assert input_mod.wheel_steps(0, 26, 0, 26) == (1, 0)
    assert input_mod.wheel_steps(0, -26, 0, 26) == (-1, 0)
    assert input_mod.wheel_steps(0, 0, 0, 26) == (0, 0)
    assert input_mod.wheel_steps(-80, None, -40, 26) == (-1, 0)


def test_focus_reports_follow_the_mode(screen, qt):
    input_mod = qt.input
    vt(screen, b"\x1b[?1004h")  # DECSET 1004
    input_mod.send_focus(screen, True)
    assert b"\x1b[I" in drain_until(screen, b"\x1b[I")
    input_mod.send_focus(screen, False)
    assert b"\x1b[O" in drain_until(screen, b"\x1b[O")
    vt(screen, b"\x1b[?1004l")
    input_mod.send_focus(screen, True)
    time.sleep(0.1)
    assert screen.session.read_available() in (None, b"")


def test_the_widget_sends_focus_on_qt_events(screen, qt):
    vt(screen, b"\x1b[?1004h")
    screen.focusInEvent(qt.QtGui.QFocusEvent(qt.QtCore.QEvent.Type.FocusIn))
    assert b"\x1b[I" in drain_until(screen, b"\x1b[I")
    screen.focusOutEvent(qt.QtGui.QFocusEvent(qt.QtCore.QEvent.Type.FocusOut))
    assert b"\x1b[O" in drain_until(screen, b"\x1b[O")


# --- modes and paging ----------------------------------------------------


def test_shift_paging_walks_the_viewport(screen, qt):
    for index in range(60):
        vt(screen, f"line {index}\r\n".encode())
    before = scrollbar(screen)
    Qt = qt.QtCore.Qt
    page_up = key_event(
        qt, Qt.Key.Key_PageUp, "", Qt.KeyboardModifier.ShiftModifier
    )
    screen.keyPressEvent(page_up)
    assert scrollbar(screen)[2] < before[2]
    page_down = key_event(
        qt, Qt.Key.Key_PageDown, "", Qt.KeyboardModifier.ShiftModifier
    )
    screen.keyPressEvent(page_down)
    assert scrollbar(screen)[2] == before[2]


def test_mode_enabled_answers_the_library(screen, qt):
    assert qt.input.mode_enabled(screen, gt.MODE_FOCUS_EVENT) is False
    vt(screen, b"\x1b[?1004h")
    assert qt.input.mode_enabled(screen, gt.MODE_FOCUS_EVENT) is True


def test_mouse_tracking_follows_the_childs_modes(screen, qt):
    assert qt.input.mouse_tracking(screen) is False
    vt(screen, b"\x1b[?1000h")
    assert qt.input.mouse_tracking(screen) is True


# --- resize and fonts ----------------------------------------------------


def test_a_resize_reaches_the_child(qt):
    from msks.client.gui.terminal import TerminalWidget

    widget = TerminalWidget([sys.executable, "-c", SIZER])
    try:
        drain_until(widget, b"ready")
        widget.write(b"\n")
        assert b"cols=80" in drain_until(widget, b"cols=80")
        widget.resize_grid(100, 30)
        widget.write(b"\n")
        assert b"cols=100 rows=30" in drain_until(widget, b"cols=100 rows=30")
    finally:
        shutdown(widget)


def test_a_resize_event_recomputes_the_grid(screen, qt):
    """A window resize re-derives the grid from the pixels —
    the event arrives through Qt's own delivery when shown,
    and directly here for the deterministic pin."""
    size = qt.QtCore.QSize(
        screen.metrics.cell_width * 40,
        screen.metrics.cell_height * 10,
    )
    old = qt.QtCore.QSize(screen.width(), screen.height())
    screen.resize(size)
    screen.resizeEvent(qt.QtGui.QResizeEvent(size, old))
    assert (screen.cols, screen.rows) == (40, 10)


def test_font_stepping_zooms_and_clamps(screen, qt):
    Qt = qt.QtCore.Qt
    start = screen.font.pointSize()
    grow = key_event(
        qt, Qt.Key.Key_Equal, "=", Qt.KeyboardModifier.ControlModifier
    )
    screen.keyPressEvent(grow)
    assert screen.font.pointSize() == start + 1
    shrink = key_event(
        qt, Qt.Key.Key_Minus, "-", Qt.KeyboardModifier.ControlModifier
    )
    screen.keyPressEvent(shrink)
    assert screen.font.pointSize() == start
    screen.apply_font_size(qt.fonts.MAX_PT)
    screen.keyPressEvent(grow)
    assert screen.font.pointSize() == qt.fonts.MAX_PT
    reset = key_event(
        qt, Qt.Key.Key_0, "0", Qt.KeyboardModifier.ControlModifier
    )
    screen.keyPressEvent(reset)
    assert screen.font.pointSize() == qt.fonts.DEFAULT_PT
    screen.keyPressEvent(reset)  # already default: skipped
    assert screen.font.pointSize() == qt.fonts.DEFAULT_PT


def test_a_bigger_font_keeps_the_grid_and_grows_the_window(screen, qt):
    """Zooming the face keeps the cell count and hands the
    enclosing window the new pixel size."""
    host = qt.QtWidgets.QWidget()
    from msks.client.gui.terminal import TerminalWidget

    widget = TerminalWidget([sys.executable, "-c", SLEEPER], parent=host)
    try:
        drain_until(widget, b"ready")
        cols, rows = widget.cols, widget.rows
        before = widget.metrics.cell_height
        widget.apply_font_size(widget.font.pointSize() + 2)
        assert widget.metrics.cell_height > before
        assert (widget.cols, widget.rows) == (cols, rows)
        assert host.size() == widget.sizeHint()
    finally:
        shutdown(widget)


def test_the_size_report_answers_the_widgets_grid(screen, qt):
    """The library's window-size query (CSI 14 t) reads the
    widget's own geometry."""
    from msks.client.gui.terminal import callbacks

    out = gt.SizeReportSize()
    ok = callbacks.report_size(None, id(screen), ctypes.pointer(out))
    assert ok
    assert out.rows == screen.rows
    assert out.columns == screen.cols
    assert out.cell_width == screen.metrics.cell_width
    assert out.cell_height == screen.metrics.cell_height


# --- effects and lifecycle -----------------------------------------------


def test_an_osc_title_emits_the_signal(screen, qt):
    titles = []
    screen.title_changed.connect(titles.append)
    vt(screen, b"\x1b]2;my title\x07")
    qt.app.processEvents()
    assert titles == ["my title"]


def test_a_titleless_child_answers_nothing(screen, qt):
    """The title effect on a terminal with no title set is
    the library's no-value answer — no signal."""
    titles = []
    screen.title_changed.connect(titles.append)
    from msks.client.gui.terminal import callbacks

    callbacks.title_changed(None, id(screen))
    qt.app.processEvents()
    assert titles == []


def test_read_master_parses_the_childs_answer(screen, qt):
    """The notifier's read path drives the whole loop: the
    terminal writes, the child answers on the pty, read_master
    parses the answer into the emulator, and the effect fires —
    an OSC title set through the round trip lands as a
    signal."""
    titles = []
    screen.title_changed.connect(titles.append)
    screen.write(b"\x1b]2;via the wire\x07")
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline and not titles:
        screen.read_master()
        qt.app.processEvents()
        time.sleep(0.02)
    assert titles == ["via the wire"]


def test_the_emulators_answers_ride_write_pty(screen, qt):
    """A device query answers through the write-pty effect: the
    emulator's DA1 reply goes to the child like a key press
    would."""
    vt(screen, b"\x1b[c")  # DA1
    seen = drain_until(screen, b"c")
    assert b"\x1b[?" in seen and seen.endswith(b"c")


def test_the_reaper_stands_down_when_the_child_is_gone(qt):
    """The reaper collects the dead child and stops; a live
    child keeps it quiet."""
    from msks.client.gui.terminal import TerminalWidget

    widget = TerminalWidget([sys.executable, "-c", SLEEPER])
    try:
        drain_until(widget, b"ready")
        widget.reap_child()  # alive: nothing collected
        assert widget.session.exit_status is None
    finally:
        shutdown(widget)
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline and widget.session.exit_status is None:
        widget.session.reap()
        time.sleep(0.02)
    widget.reap_child()  # dead: the notifier stands down
    assert not widget.notifier.isEnabled()


def test_the_registry_seats_and_unseats_screens(screen, qt):
    from msks.client.gui.terminal import callbacks

    assert callbacks.screen_for(id(screen)) is screen
    screen.close()
    assert callbacks.screen_for(id(screen)) is None


def test_a_late_callback_after_close_is_a_noop(screen, qt):
    screen.close()
    from msks.client.gui.terminal import callbacks

    assert callbacks.write_pty(None, id(screen), None, 0) is None
    assert callbacks.write_pty(None, None, None, 0) is None
    assert callbacks.report_size(None, id(screen), None) is False
    assert callbacks.bell(None, id(screen)) is None
    assert callbacks.title_changed(None, id(screen)) is None


def test_device_attributes_answer_vt220():
    from msks.client.gui.terminal import callbacks

    out = gt.DeviceAttributes()
    assert callbacks.device_attributes(None, None, ctypes.pointer(out))
    assert out.primary.conformance_level == gt.DA_CONFORMANCE_VT220
    assert out.primary.num_features == 3
    assert out.secondary.device_type == gt.DA_DEVICE_TYPE_VT220


def test_color_scheme_answers_no_scheme():
    from msks.client.gui.terminal import callbacks

    assert callbacks.color_scheme(None, None, 0) is False


def test_the_child_exit_reaches_the_widget(qt):
    from msks.client.gui.terminal import TerminalWidget

    exits = []
    widget = TerminalWidget([sys.executable, "-c", "raise SystemExit(7)"])
    try:
        widget.process_exited.connect(exits.append)
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline and not exits:
            widget.read_master()
            qt.app.processEvents()
            time.sleep(0.02)
        assert exits == [7]
    finally:
        shutdown(widget)


def test_close_tears_the_session_and_the_handles(qt):
    from msks.client.gui.terminal import TerminalWidget

    widget = TerminalWidget([sys.executable, "-c", SLEEPER])
    session = widget.session
    shutdown(widget)
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline and session.exit_status is None:
        session.reap()
        time.sleep(0.02)
    assert session.exit_status is not None  # HUP or escalated kill
    shutdown(widget)  # a second one tears nothing twice


def test_the_package_presents_the_widget_lazily():
    """A fresh interpreter importing the package root loads no
    Qt — the CLI-parse safety #927 pins — and the attribute
    hands over the widget."""
    code = textwrap.dedent(
        """
        import sys
        import msks.client.gui.terminal as t
        assert 'PySide6' not in sys.modules
        assert t.TerminalWidget is not None
        assert 'PySide6' in sys.modules
        """
    )
    done = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        env={**os.environ, "QT_QPA_PLATFORM": "offscreen"},
        check=False,
    )
    assert done.returncode == 0, done.stderr


def test_an_unknown_package_attribute_refuses():
    code = textwrap.dedent(
        """
        import msks.client.gui.terminal as t
        try:
            t.nope
        except AttributeError:
            pass
        else:
            raise SystemExit('refused nothing')
        """
    )
    done = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        env={**os.environ, "QT_QPA_PLATFORM": "offscreen"},
        check=False,
    )
    assert done.returncode == 0, done.stderr


# --- the painter's own helpers -------------------------------------------


def test_rgb_converts_the_libraries_triple(qt):
    color = gt.ColorRgb(r=1, g=2, b=3)
    converted = qt.paint.rgb(color)
    assert converted.red() == 1
    assert converted.green() == 2
    assert converted.blue() == 3


def test_cell_at_clamps_to_the_grid(screen, qt):
    QPointF = qt.QtCore.QPointF
    assert qt.input.cell_at(screen, QPointF(0, 0)) == (0, 0)
    far = QPointF(screen.width() * 3, screen.height() * 3)
    assert qt.input.cell_at(screen, far) == (screen.cols - 1, screen.rows - 1)
