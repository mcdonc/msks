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
    fonts.register_faces()  # after the application exists
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
    caret notation), canonical mode off (it would hold every
    write until a newline), and input CR→NL off (a pasted
    carriage return must survive to the child, or the paste
    pins could not read the bytes the terminal wrote)."""
    import termios

    attrs = termios.tcgetattr(widget.session.master_fd)
    attrs[3] &= ~(termios.ECHO | termios.ECHOCTL | termios.ICANON)
    attrs[0] &= ~termios.ICRNL
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
    emulator as we go so the widget's state stays current.

    The loop is the master's only reader: it disarms the
    widget's socket notifier for its duration, because one
    event-loop pump fires ``read_master``, whose
    ``session.drain()`` consumes the bytes into the emulator
    where this loop cannot see them — the macOS job's flake
    (#1074). A pump between the write under test and this
    call can still steal the bytes before the loop starts, so
    nothing may pump that gap either."""
    armed = widget.notifier.isEnabled()
    widget.notifier.setEnabled(False)
    try:
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
    finally:
        # A child that died mid-drain closed the master (the
        # read path releases the fd); arming a notifier on it
        # would be the dead-master hazard child_gone exists to
        # prevent, so the notifier stays down with it.
        if widget.session.master_fd is not None:
            widget.notifier.setEnabled(armed)


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


def test_the_bundled_faces_sit_in_the_database(qt, monkeypatch, tmp_path):
    """Registration seats JetBrains Mono even on a host whose
    font database starts empty (a CI offscreen platform) — the
    one-face guarantee everywhere. A broken wheel whose faces
    directory is missing registers nothing and stays quiet."""
    from pathlib import Path

    families = set(qt.QtGui.QFontDatabase.families())
    assert "JetBrains Mono" in families
    monkeypatch.setattr(qt.fonts, "FACES", Path(tmp_path) / "nowhere")
    monkeypatch.setattr(qt.fonts, "_FACES_SEATED", False)
    qt.fonts.register_faces()  # no crash, nothing new seated
    assert "JetBrains Mono" in set(qt.QtGui.QFontDatabase.families())


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


def test_the_em_falls_back_to_the_logical_estimate(qt, monkeypatch):
    """A font Qt has not realized answers pixelSize -1: the em
    falls back to the 96-dpi logical estimate."""
    fonts, QtGui = qt.fonts, qt.QtGui
    font = fonts.make_font()
    realized = QtGui.QFontInfo(font).pixelSize()

    class Unrealized:
        def __init__(self, _font):
            pass

        @staticmethod
        def pixelSize():
            return -1

    monkeypatch.setattr(fonts, "QFontInfo", Unrealized)
    assert fonts.em_pixels(font) == round(font.pointSizeF() * 96 / 72)
    assert realized >= 0  # the real path answers the realized em


def test_the_row_height_caps_at_twelve_tenths_em(qt):
    """The cell height is ascent plus descent, trimmed to 1.2
    em — the line-height terminals ship. The em is the M
    advance: QFontInfo.pixelSize() answers -1 for a face Qt
    has not realized, and the cap would collapse the row."""
    fonts, QtGui = qt.fonts, qt.QtGui
    font = fonts.make_font()
    metrics = fonts.measure(font)
    em = fonts.em_pixels(font)
    assert metrics.cell_height == min(
        QtGui.QFontMetrics(font).height(), round(em * fonts.ROW_HEIGHT_EM)
    )
    assert metrics.cell_height > 1


def test_the_family_falls_back_to_fixed_pitch_then_the_hint(qt, monkeypatch):
    """Nothing preferred installed resolves the database's own
    first fixed-pitch family; a database without even that
    resolves the generic hint — the floor, never an error, and
    never a name the database does not hold."""
    fonts = qt.fonts

    class Database:
        def __init__(self, names, fixed):
            self._names = names
            self._fixed = fixed

        def families(self):
            return self._names

        def isFixedPitch(self, name):
            return name in self._fixed

    monkeypatch.setattr(
        fonts,
        "QFontDatabase",
        Database(["Comic Sans", "Prose Mono"], {"Prose Mono"}),
    )
    assert fonts.resolve_family() == "Prose Mono"

    monkeypatch.setattr(
        fonts, "QFontDatabase", Database(["Comic Sans"], set())
    )
    assert fonts.resolve_family() == "Monospace"

    monkeypatch.setattr(
        fonts,
        "QFontDatabase",
        Database(["Fira Code", "Comic Sans"], set()),
    )
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


# --- paint damage (#1104) ------------------------------------------------


def test_one_typed_character_repaints_its_row_alone(screen, qt):
    """The paint's damage set is the library's own row flags
    (#1104): the first grab builds the cell cache and paints
    every row, and the next character repaints the row its
    cursor sits in alone — the whole-grid paint a typed
    character once paid is the echo lag the issue chased."""
    vt(screen, b"\x1b[2J\x1b[Hhello\r\nworld")
    screen.grab()
    assert screen.painted_rows == list(range(screen.rows))
    vt(screen, b"x")  # the cursor sits on row 1
    screen.grab()
    assert screen.painted_rows == [1]


def test_the_cursor_moves_without_row_damage(screen, qt):
    """A cursor move flags no rows (probed): the paint blits
    the cache whole — erasing the cursor the last frame drew
    above it — and draws the cursor at its new cell, with the
    cells themselves untouched."""
    vt(screen, b"\x1b[2J\x1b[Hhello")
    before = screen.grab().toImage()
    vt(screen, b"\x1b[3C")
    after = screen.grab().toImage()
    assert screen.painted_rows == []
    assert after != before


def test_a_viewport_scroll_repaints_every_row(screen, qt):
    """The library's row flags stay clean through a viewport
    scroll (probed); the cache compares the scrollbar's
    offset and repaints every row when the viewport moved —
    the rows the viewport now shows are different buffer
    lines."""
    vt(screen, b"\x1b[2J\x1b[H" + b"line\r\n" * 40)  # history to page into
    screen.grab()
    screen.scroll_rows(-3)
    screen.grab()
    assert screen.painted_rows == list(range(screen.rows))


def test_a_pixel_resize_rebuilds_the_cache(screen, qt):
    """A resize rebuilds the cache for the new pixels and
    paints every row into it (the library flags them all,
    probed): a stale cache would show the old grid's rows
    beside the new grid's."""
    vt(screen, b"\x1b[2J\x1b[Hhello")
    screen.grab()
    size = screen.size() + qt.QtCore.QSize(120, 40)
    screen.resize(size)
    screen.resizeEvent(qt.QtGui.QResizeEvent(size, screen.size()))
    screen.grab()
    assert screen.painted_rows == list(range(screen.rows))


def test_a_changed_find_pass_repaints_every_row(screen, qt):
    """The find underlays paint beneath the cells, into the
    cache (#1104): a search whose rectangles arrive with the
    rows clean repaints every row, or the tint would age in
    the cache while the needle steps."""
    from msks.client.gui.terminal import find

    vt(screen, b"\x1b[2J\x1b[Hneedle here")
    screen.grab()
    find.open(screen)
    find.run(screen, "needle")
    screen.grab()
    assert screen.painted_rows == list(range(screen.rows))
    find.close(screen)


def test_a_scheme_flip_repaints_every_row_and_the_pixels(screen, qt):
    """The scheme flip's own path — ``terminal_set`` of the
    base colors, what ``colors.apply`` runs — flags every row
    (probed) and moves the render state's palette: the whole
    grid repaints, and the cache's base fill takes the new
    background, so the painted pixels follow — a blank row
    reads the new scheme's background, not the one the cache
    was built with (review round one: the rows repainted over
    a stale base, black-on-black under a light scheme)."""
    from msks.client.gui.terminal import colors

    vt(screen, b"\x1b[2J\x1b[Hsome text\r\n\r\n\r\nmore")
    screen.grab()
    colors.apply(screen, gt.COLOR_SCHEME_LIGHT)
    image = screen.grab().toImage()
    assert screen.painted_rows == list(range(screen.rows))
    cw, ch = screen.metrics.cell_width, screen.metrics.cell_height
    pixel = image.pixelColor(cw * 2, ch + ch // 2)  # blank row 1
    assert (pixel.red(), pixel.green(), pixel.blue()) == (255, 255, 255)


def test_a_program_retint_repaints_the_pixels(screen, qt):
    """A program's own default-color move — OSC 11's background
    with OSC 10's foreground flushing the sync — moves the
    render state's palette like the scheme flip does, and the
    same comparison carries it: every row repaints over a base
    fill taken from the new background."""
    vt(screen, b"\x1b[2J\x1b[Hsome text\r\n\r\n\r\nmore")
    screen.grab()
    vt(screen, b"\x1b]11;rgb:00/00/cc\x07\x1b]10;rgb:ff/ff/00\x07")
    image = screen.grab().toImage()
    assert screen.painted_rows == list(range(screen.rows))
    cw, ch = screen.metrics.cell_width, screen.metrics.cell_height
    pixel = image.pixelColor(cw * 2, ch + ch // 2)  # blank row 1
    assert (pixel.red(), pixel.green(), pixel.blue()) == (0, 0, 204)


def test_a_metrics_change_at_the_same_pixels_rebuilds_the_cache(screen, qt):
    """A font change at the same widget pixels — the zoom on a
    maximized window, a hidden tab's ``set_face`` — keeps the
    cache key's pixel half still, so the key carries the cell
    metrics too: the cache rebuilds and repaints every row,
    and the strip below the smaller grid carries the
    background instead of the old frame's last row (review
    round one: the rows repainted beside their stale
    neighbor)."""
    from msks.client.gui.terminal.paint import palette

    vt(screen, b"\x1b[2J\x1b[Hsome text")
    size = qt.QtCore.QSize(400, 300)
    screen.resize(size)
    screen.resizeEvent(qt.QtGui.QResizeEvent(size, screen.size()))
    screen.grab()  # the cache builds for this grid and font
    larger = screen.font.pointSize()
    screen.set_face(larger - 4)  # smaller cells at the same pixels
    background = palette(screen).background
    image = screen.grab().toImage()
    assert screen.painted_rows == list(range(screen.rows))
    strip_y = screen.rows * screen.metrics.cell_height + 2
    assert strip_y < screen.height()  # smaller cells always leave it
    pixel = image.pixelColor(screen.metrics.cell_width * 2, strip_y)
    assert (pixel.red(), pixel.green(), pixel.blue()) == (
        background.r,
        background.g,
        background.b,
    )


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


def test_an_over_cap_cluster_draws_blank(screen, qt):
    """A combining-mark run longer than the 16-codepoint
    buffer fills one cell: the cluster cannot ride the buffer,
    the cell draws blank, and the paint does not crash."""
    cluster = "e" + "\u0308" * 20
    vt(screen, ("\x1b[2J\x1b[H" + cluster).encode())
    ok, iterator = screen.lib.get(
        screen.lib.render_state_get,
        screen.render_state,
        gt.RS_ROW_ITERATOR,
        ctypes.c_void_p(screen.row_iterator),
    )
    assert ok
    lengths = []
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
            if length.value:
                lengths.append(length.value)
    assert max(lengths) > qt.paint.CLUSTER_CAP
    image = screen.grab().toImage()
    assert image.size() == screen.size()
    # The over-cap cell carries no glyph ink: its pixels match
    # an untouched cell's background.
    width, height = screen.metrics.cell_width, screen.metrics.cell_height
    blank = image.pixelColor(width * 5, height * 2)
    assert image.pixelColor(width // 2, height // 2) == blank


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


def test_tab_reaches_the_child_and_keeps_focus(qt):
    """The bug's own path (#1066): Qt's ``QWidget.event``
    reroutes a Tab into a focus move before the press reaches
    the encoder, and the shell's completion loses the key. A
    terminal in a window with a focusable sibling that follows
    it in tab order keeps both the key and the focus — the pty
    reads the tab byte, the refusal answers the move. The
    other arm of the refusal — a child's Tab passed through
    ``super()`` — is the find bar's own pin below."""
    from msks.client.gui.terminal import TerminalWidget

    host = qt.QtWidgets.QWidget()
    host.show()
    # The window must be the app's active one (#1066): on some
    # platforms' offscreen runs a shown window never activates,
    # its focus widget answers hasFocus False, and the refusal
    # would hand the Tab to the traversal instead of the pty.
    host.activateWindow()
    widget = TerminalWidget([sys.executable, "-c", ECHOER], parent=host)
    # A focusable after the terminal in tab order (creation
    # order): a Tab from the terminal has somewhere to go, the
    # swallow pre-fix.
    button = qt.QtWidgets.QPushButton(host)
    button.setFocusPolicy(qt.QtCore.Qt.FocusPolicy.StrongFocus)
    try:
        hush_echo(widget)
        drain_until(widget, b"ready")
        widget.show()
        widget.setFocus()
        qt.app.processEvents()
        assert widget.hasFocus()  # the refusal answers from here
        # The interception's own entry: the event through Qt's
        # full delivery, not the key handler alone. The delivery
        # is synchronous, so no pump is needed here — and none
        # may stand between this write and the drain below: the
        # helper's docstring names the race one pump opens (#1074).
        qt.app.sendEvent(widget, key_event(qt, qt.QtCore.Qt.Key.Key_Tab))
        assert b"\t" in drain_until(widget, b"\t")
        assert widget.hasFocus()
        assert widget.focusNextPrevChild(True) is False
    finally:
        shutdown(widget)
        host.close()


def test_shift_tab_encodes_as_the_shifted_tab(screen, qt):
    """Shift+Tab arrives as Key_Backtab with empty text
    (#1066): the table maps it to the TAB key and the shift
    modifier stays in the mask, so the child reads a shifted
    tab — the legacy backtab sequence reverse completion
    binds, and the Kitty protocol's shifted tab."""
    Qt = qt.QtCore.Qt
    assert qt.input.qt_keys()[int(Qt.Key.Key_Backtab)] == gt.KEY["TAB"]
    backtab = key_event(
        qt, Qt.Key.Key_Backtab, "", Qt.KeyboardModifier.ShiftModifier
    )
    screen.keyPressEvent(backtab)
    assert b"\x1b[Z" in drain_until(screen, b"\x1b[Z")
    vt(screen, b"\x1b[>1u")  # disambiguate escape codes
    screen.keyPressEvent(backtab)
    assert b"\x1b[9;2u" in drain_until(screen, b"u")


def test_tab_still_moves_focus_out_of_the_find_bar(qt):
    """The refusal's other arm (#1066): Qt's default traversal
    delegates ``focusNextPrevChild`` up the parent chain, so
    the terminal answers for its children too — the find
    bar's edit is one. The guard releases the question to the
    base class there: focus walks out of the bar to the
    widgets around, and the bar's Tab does not become the
    terminal's tab byte."""
    from msks.client.gui.terminal import TerminalWidget, find

    host = qt.QtWidgets.QWidget()
    host.show()
    button = qt.QtWidgets.QPushButton(host)
    button.setFocusPolicy(qt.QtCore.Qt.FocusPolicy.StrongFocus)
    widget = TerminalWidget([sys.executable, "-c", SLEEPER], parent=host)
    try:
        drain_until(widget, b"ready")
        widget.resize(600, 400)
        widget.show()
        widget.setFocus()
        qt.app.processEvents()
        find.open(widget)
        qt.app.processEvents()
        edit = widget.find_edit
        assert edit.hasFocus()
        qt.app.sendEvent(edit, key_event(qt, qt.QtCore.Qt.Key.Key_Tab))
        qt.app.processEvents()
        assert not edit.hasFocus()
        time.sleep(0.1)  # a byte on the wire needs a moment
        assert widget.session.read_available() in (None, b"")
    finally:
        if widget.search:
            find.close(widget)
        shutdown(widget)
        host.close()


def test_the_focus_refusal_answers_qts_own_tab_delivery(qt):
    """The refusal's own pin (#1066): the offscreen platform
    moves no focus for the terminal's Tab, so the base class
    answers False here too and a bare call cannot tell the
    override from it. The override itself can: it stands on
    the class, Qt's Tab delivery reaches it while the
    terminal holds focus and it refuses without the base, and
    a child's Tab reaches it unfocused and passes through to
    the base — the traversal the find bar's pin shows whole."""
    from msks.client.gui.terminal import TerminalWidget, find

    real = TerminalWidget.__dict__.get("focusNextPrevChild")
    assert real is not None  # the override, not the base's
    calls = []

    def spy(self, forward):
        focused = self.hasFocus()  # before the base may move it
        result = real(self, forward)
        calls.append((focused, forward, result))
        return result

    TerminalWidget.focusNextPrevChild = spy
    host = qt.QtWidgets.QWidget()
    host.show()
    host.activateWindow()  # a real focus holder, every
    # platform — the refusal's arm answers from hasFocus
    widget = TerminalWidget([sys.executable, "-c", SLEEPER], parent=host)
    button = qt.QtWidgets.QPushButton(host)
    button.setFocusPolicy(qt.QtCore.Qt.FocusPolicy.StrongFocus)
    try:
        drain_until(widget, b"ready")
        widget.resize(600, 400)  # the bar's overlay geometry
        widget.show()
        widget.setFocus()
        qt.app.processEvents()
        assert widget.hasFocus()
        qt.app.sendEvent(widget, key_event(qt, qt.QtCore.Qt.Key.Key_Tab))
        qt.app.processEvents()
        assert (True, True, False) in calls
        find.open(widget)
        qt.app.processEvents()
        qt.app.sendEvent(
            widget.find_edit, key_event(qt, qt.QtCore.Qt.Key.Key_Tab)
        )
        qt.app.processEvents()
        assert (False, True, True) in calls
    finally:
        TerminalWidget.focusNextPrevChild = real
        if widget.search:
            find.close(widget)
        shutdown(widget)
        host.close()


def test_shortcut_override_claims_only_the_bare_escape(screen, qt):
    """The override's own bounds (#1099): Qt's shortcut
    dispatch sends a ``ShortcutOverride`` to the focus widget
    before a matching shortcut consumes the press, and the
    accept — the answer Qt reads off the event's accepted
    flag — yields the shortcut. The claim names the bare Esc
    alone: a modified Esc or another key stays unaccepted, so
    no other shortcut the window stands loses its key."""
    QEvent = qt.QtCore.QEvent
    Qt = qt.QtCore.Qt

    def override(key, modifiers):
        return qt.QtGui.QKeyEvent(
            QEvent.Type.ShortcutOverride, int(key), modifiers, ""
        )

    bare = override(Qt.Key.Key_Escape, Qt.KeyboardModifier.NoModifier)
    assert screen.event(bare) is True
    assert bare.isAccepted()
    for other in (
        override(Qt.Key.Key_Escape, Qt.KeyboardModifier.ControlModifier),
        override(Qt.Key.Key_Escape, Qt.KeyboardModifier.ShiftModifier),
        override(Qt.Key.Key_A, Qt.KeyboardModifier.NoModifier),
    ):
        assert screen.event(other) is True  # the base's answer
        assert not other.isAccepted()


def test_escape_reaches_the_child_past_a_back_shortcut(qt):
    """The bug's own path (#1099): the workspace page stands a
    widget-scoped shortcut on the bare Esc — its Back — and
    the match consumes the press before the encoder, so the
    program inside never reads the key. A terminal under the
    same shortcut keeps the press: Qt's override question
    answers first, the shortcut stays quiet, and the pty
    reads the escape byte. The delivery is the shortcut
    system's own — the press goes through ``sendEvent``, the
    dispatch a real platform serves."""
    from msks.client.gui.terminal import TerminalWidget

    host = qt.QtWidgets.QWidget()
    host.show()
    host.activateWindow()  # a shortcut answers in the active window
    fired = []
    escape = qt.QtGui.QShortcut(
        qt.QtGui.QKeySequence(qt.QtCore.Qt.Key.Key_Escape), host
    )
    escape.activated.connect(lambda: fired.append(1))
    widget = TerminalWidget([sys.executable, "-c", ECHOER], parent=host)
    try:
        hush_echo(widget)
        drain_until(widget, b"ready")
        widget.show()
        widget.setFocus()
        qt.app.processEvents()
        qt.app.sendEvent(widget, key_event(qt, qt.QtCore.Qt.Key.Key_Escape))
        assert b"\x1b" in drain_until(widget, b"\x1b")
        assert fired == []
    finally:
        shutdown(widget)
        host.close()


def test_escape_closes_the_find_bar_not_the_page(qt):
    """The accept's other arm (#1099): the find bar's edit is
    the terminal's child, and an override a focused child
    ignores propagates to the parent — the accept answers it
    the same way, so the page's Back shortcut stays quiet and
    the bar's own Esc handler closes the bar. Before the fix
    the shortcut answered first, and Back navigated away with
    the bar still open."""
    from msks.client.gui.terminal import TerminalWidget, find

    host = qt.QtWidgets.QWidget()
    host.show()
    host.activateWindow()
    fired = []
    escape = qt.QtGui.QShortcut(
        qt.QtGui.QKeySequence(qt.QtCore.Qt.Key.Key_Escape), host
    )
    escape.activated.connect(lambda: fired.append(1))
    widget = TerminalWidget([sys.executable, "-c", SLEEPER], parent=host)
    try:
        drain_until(widget, b"ready")
        widget.resize(600, 400)  # the bar's overlay geometry
        widget.show()
        widget.setFocus()
        qt.app.processEvents()
        find.open(widget)
        qt.app.processEvents()
        assert widget.find_edit.hasFocus()
        qt.app.sendEvent(
            widget.find_edit, key_event(qt, qt.QtCore.Qt.Key.Key_Escape)
        )
        qt.app.processEvents()
        assert widget.search == 0  # the bar's own Esc closed it
        assert widget.find_edit is None  # close's teardown ran
        assert fired == []
    finally:
        if widget.search:
            find.close(widget)
        shutdown(widget)
        host.close()


def test_escape_still_answers_back_off_the_terminal(qt):
    """The shortcut's own arm (#1099): the refusal yields the
    key only while the terminal holds the focus it was sent
    to. A press that lands on the chrome — the Back button
    the page anchors — still fires the shortcut."""
    from msks.client.gui.terminal import TerminalWidget

    host = qt.QtWidgets.QWidget()
    host.show()
    host.activateWindow()
    fired = []
    escape = qt.QtGui.QShortcut(
        qt.QtGui.QKeySequence(qt.QtCore.Qt.Key.Key_Escape), host
    )
    escape.activated.connect(lambda: fired.append(1))
    button = qt.QtWidgets.QPushButton(host)
    button.setFocusPolicy(qt.QtCore.Qt.FocusPolicy.StrongFocus)
    widget = TerminalWidget([sys.executable, "-c", SLEEPER], parent=host)
    try:
        drain_until(widget, b"ready")
        widget.show()
        qt.app.processEvents()
        qt.app.sendEvent(button, key_event(qt, qt.QtCore.Qt.Key.Key_Escape))
        assert fired == [1]
    finally:
        shutdown(widget)
        host.close()


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


def test_an_auto_repeat_release_sends_nothing(screen, qt):
    """A held key's platform repeats arrive as presses; the
    repeat's release is dropped before the encoder — under
    Kitty event types, where an unguarded release would encode
    \x1b[97:1:3u and spam the program inside."""
    vt(screen, b"\x1b[>3u")
    repeat_release = qt.QtGui.QKeyEvent(
        qt.QtCore.QEvent.Type.KeyRelease,
        int(qt.QtCore.Qt.Key.Key_A),
        qt.QtCore.Qt.KeyboardModifier.NoModifier,
        "a",
        True,
        1,
    )
    assert repeat_release.isAutoRepeat()
    screen.keyReleaseEvent(repeat_release)
    time.sleep(0.1)
    assert screen.session.read_available() in (None, b"")


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


def mouse_event(qt, kind, pos, button, buttons, modifiers=None):
    return qt.QtGui.QMouseEvent(
        kind,
        qt.QtCore.QPointF(*pos),
        qt.QtCore.QPointF(*pos),
        button,
        buttons,
        modifiers or qt.QtCore.Qt.KeyboardModifier.NoModifier,
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
    offset answers where the viewport sits, and the thumb
    paints where the offset puts it."""
    for index in range(40):
        vt(screen, f"line {index}\r\n".encode())
    before = scrollbar(screen)
    at_bottom = screen.grab().toImage()
    screen.wheelEvent(wheel_event(qt, 120))  # one notch up
    qt.app.processEvents()
    after = scrollbar(screen)
    assert after[2] < before[2]
    paged_up = screen.grab().toImage()  # the thumb moved
    assert paged_up != at_bottom
    screen.wheelEvent(wheel_event(qt, -120))  # one notch down
    qt.app.processEvents()
    assert scrollbar(screen)[2] > after[2]  # back toward bottom
    qt.input.snap_to_bottom(screen)
    total, length, offset = scrollbar(screen)
    assert offset == total - length


def test_the_wheel_feeds_a_tracking_child(screen, qt):
    """A child that enables mouse reporting owns the wheel:
    button four arrives as a press/release pair."""
    vt(screen, b"\x1b[?1000h\x1b[?1006h")
    screen.wheelEvent(wheel_event(qt, 120))
    # Drain to the RELEASE byte (the lowercase m): the press
    # marker alone can precede the release through the echo
    # child, and a drain on the press would race the pair.
    seen = drain_until(screen, b"m")
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


def test_a_focused_screen_closes_without_a_focus_read(screen, qt):
    """The focus guard at teardown (#1034): closing frees the
    emulator handles, and the hide the close performs delivers
    the focus-out after them — the closed screen answers no
    focus report, and the report's terminal read never happens.
    The unguarded read was the use-after-free that took the
    process down on every close of a focused terminal; this
    test's own survival is the pin (a regression segfaults the
    suite, the crash the issue chased), with the closed flag
    read beside it so the guard's branch shows in coverage."""
    vt(screen, b"\x1b[?1004h")  # the mode a report would answer
    screen.resize(600, 400)
    screen.show()
    screen.setFocus()
    qt.app.processEvents()
    assert screen.close()  # the real close: frees, then hides
    qt.app.processEvents()  # the hide's focus-out, delivered
    assert screen._closed
    # The driven arm (#1034): the same events the hide
    # delivers, sent straight to the handler, so the pin holds
    # even where the platform's focus chain hands the close
    # nothing — the focus-out the crash ran on, and the
    # focus-in its guard shares the reason with.
    screen.focusOutEvent(qt.QtGui.QFocusEvent(qt.QtCore.QEvent.Type.FocusOut))
    screen.focusInEvent(qt.QtGui.QFocusEvent(qt.QtCore.QEvent.Type.FocusIn))
    # The resize arm (#1034, review round one): the same
    # contract holds the layout's late resize — a closed widget
    # a layout still owns sizes no grid on the freed terminal.
    screen.resizeEvent(
        qt.QtGui.QResizeEvent(qt.QtCore.QSize(10, 10), qt.QtCore.QSize(20, 20))
    )
    # The session was hung up on inside the close: nothing the
    # guard skipped could have been written anyway.
    time.sleep(0.1)
    assert screen.session.read_available() in (None, b"")


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


def test_the_widget_holds_no_font_keys(screen, qt):
    """The font keys left the widget (#976): the zoom lives at
    the application level now — a widget-level Ctrl+= or
    Ctrl+0 steps nothing."""
    Qt = qt.QtCore.Qt
    start = screen.font.pointSize()
    grow = key_event(
        qt, Qt.Key.Key_Equal, "=", Qt.KeyboardModifier.ControlModifier
    )
    screen.keyPressEvent(grow)
    assert screen.font.pointSize() == start
    reset = key_event(
        qt, Qt.Key.Key_0, "0", Qt.KeyboardModifier.ControlModifier
    )
    screen.keyPressEvent(reset)
    assert screen.font.pointSize() == start


def test_a_bigger_font_keeps_the_grid_and_grows_the_window(screen, qt):
    """Zooming the face keeps the cell count and grows the
    enclosing window by the same delta the grid took (#976):
    the window answers the new cell metrics, not the bare
    grid — the chrome the window carries around the terminal
    keeps its share of the pixels."""
    host = qt.QtWidgets.QWidget()
    host.show()
    from msks.client.gui.terminal import TerminalWidget

    widget = TerminalWidget([sys.executable, "-c", SLEEPER], parent=host)
    try:
        drain_until(widget, b"ready")
        widget.resize(widget.sizeHint())  # laid out at its grid
        widget.show()  # a raw child of a shown parent stays
        # hidden until it is shown itself — the window resizes
        # only around a terminal it shows
        cols, rows = widget.cols, widget.rows
        before = host.size()
        old_grid = widget.sizeHint()
        old_cell = widget.metrics.cell_height
        widget.apply_font_size(widget.font.pointSize() + 2)
        new_grid = widget.sizeHint()
        assert widget.metrics.cell_height > old_cell
        assert (widget.cols, widget.rows) == (cols, rows)
        # The window took the grid's own growth (#976): the new
        # cell metrics less the old ones, per axis — the chrome
        # around the terminal keeps its share of the pixels.
        assert (
            host.width()
            == before.width() + new_grid.width() - old_grid.width()
        )
        assert (
            host.height()
            == before.height() + new_grid.height() - old_grid.height()
        )
    finally:
        shutdown(widget)
        host.close()


def test_a_maximized_window_answers_no_fit(screen, qt):
    """A maximized frame keeps its pixels (#976): no resize
    lands while the window is maximized — the face still
    steps, and the grid re-derives at the restore."""
    host = qt.QtWidgets.QMainWindow()
    host.show()
    from msks.client.gui.terminal import TerminalWidget

    widget = TerminalWidget([sys.executable, "-c", SLEEPER], parent=host)
    host.setCentralWidget(widget)
    try:
        drain_until(widget, b"ready")
        host.showMaximized()
        qt.app.processEvents()
        maximized = host.size()
        stepped = widget.metrics.cell_height
        widget.apply_font_size(widget.font.pointSize() + 2)
        assert widget.metrics.cell_height > stepped  # the face stepped
        assert host.size() == maximized  # the frame answered nothing
    finally:
        shutdown(widget)
        host.close()


def test_a_fit_that_cannot_reach_lands_the_pixels_grid(screen, qt):
    """A fit the window's layout cannot complete (#976): a
    capped terminal takes its window's growth only to the
    cap, the settle cannot bring the entry grid back, and the
    landing hands the emulator and the child the grid the
    pixels hold — the screen paints, the child reads, one
    grid."""
    host = qt.QtWidgets.QMainWindow()
    host.show()
    from msks.client.gui.terminal import TerminalWidget

    widget = TerminalWidget([sys.executable, "-c", SLEEPER], parent=host)
    host.setCentralWidget(widget)
    try:
        # The child's own startup line drained before the
        # layout pump: a pump with the line already landed
        # fires the notifier, which eats it into the emulator
        # and stalls this drain its whole window (#1074).
        drain_until(widget, b"ready")
        qt.app.processEvents()  # the central takes the window's pixels
        widget.setMaximumHeight(widget.height() // 2)  # the cap the
        # layout cannot lift: the fit's growth stops at it
        qt.app.processEvents()
        held = (widget.cols, widget.rows)
        widget.apply_font_size(widget.font.pointSize() + 2)
        qt.app.processEvents()
        landed = (
            max(1, widget.width() // widget.metrics.cell_width),
            max(1, widget.height() // widget.metrics.cell_height),
        )
        assert landed[1] < held[1]  # the cap held the fit short
        assert (widget.cols, widget.rows) == landed  # one grid everywhere
    finally:
        shutdown(widget)
        host.close()


def test_fit_settles_only_font_and_layout_inside_grid_guard(
    screen, qt, monkeypatch
):
    from msks.client.gui.terminal import screen as screen_mod

    events = []
    passes = []
    monkeypatch.setattr(screen, "can_fit_window", lambda window: True)
    monkeypatch.setattr(
        screen_mod.QCoreApplication,
        "sendPostedEvents",
        lambda receiver, kind: events.append((kind, screen._fitting)),
    )
    monkeypatch.setattr(
        screen,
        "fit_once",
        lambda window: passes.append(screen._fitting) or True,
    )
    held = (screen.cols, screen.rows)
    screen.fit_window()
    assert events == [
        (screen_mod.QEvent.Type.ApplicationFontChange, True),
        (screen_mod.QEvent.Type.LayoutRequest, True),
    ]
    assert passes == [True, True]
    assert not screen._fitting
    assert (screen.cols, screen.rows) == held


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


def test_final_output_and_exit_share_one_readiness_window(qt):
    """The common exit: the child's last bytes and its death
    land in one readiness burst. The drain delivers the bytes
    with the session already marked exited — the exit path
    must still run: the notifier disarms (the session closed
    the master under it), the reaper starts, and the exit
    signal fires."""
    from msks.client.gui.terminal import TerminalWidget

    exits = []
    widget = TerminalWidget(
        [sys.executable, "-c", 'print("bye"); raise SystemExit(7)']
    )
    try:
        widget.process_exited.connect(exits.append)
        # The event loop alone drives every read: the
        # notifier's activated signal calls read_master, and
        # nothing else touches the master. On the pre-fix code
        # the burst case leaves the notifier armed on a closed
        # fd and emits nothing — this loop times out red.
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline and not exits:
            qt.app.processEvents()
            time.sleep(0.01)
        assert widget.session.exited
        assert not widget.notifier.isEnabled()
        assert exits == [7]
        # Later reads on the dead master change nothing: the
        # signal fired once.
        widget.read_master()
        qt.app.processEvents()
        assert exits == [7]
    finally:
        shutdown(widget)


def test_a_burst_of_final_bytes_takes_the_exit_path(screen, qt):
    """The burst arm itself: a drain that delivers the child's
    last bytes with the session already marked exited runs the
    exit path — the arm the event-loop test reaches only when
    output and exit share one readiness window, pinned here
    whatever the timing."""

    class Burst:
        exited = True
        exit_status = 7

        def drain(self):
            return b"bye\n"

        def reap(self):
            return None

        def close(self, grace: float = 0.3):
            return None

    screen.session = Burst()
    exits = []
    screen.process_exited.connect(exits.append)
    screen.read_master()
    qt.app.processEvents()
    assert exits == [7]
    assert not screen.notifier.isEnabled()
    screen.read_master()  # once-only: nothing changes
    assert exits == [7]


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

    # The out slot arrives as the C ABI's pointer; None is the
    # NULL the no-answer arm never writes through.
    assert callbacks.color_scheme(None, None, None) is False


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


# --- selection and clipboard (#953) --------------------------------------


def test_a_drag_selects_text(screen, qt):
    """A press-drag-release sweep selects the cells between
    the anchors and the finished drag publishes the text to
    the PRIMARY selection."""
    from msks.client.gui.terminal import select

    Qt = qt.QtCore.Qt
    vt(screen, b"\x1b[2J\x1b[Hhello world")
    cw = screen.metrics.cell_width
    screen.mousePressEvent(
        mouse_event(
            qt,
            qt.QtGui.QMouseEvent.Type.MouseButtonPress,
            (2, 2),
            Qt.MouseButton.LeftButton,
            Qt.MouseButton.LeftButton,
        )
    )
    screen.mouseMoveEvent(
        mouse_event(
            qt,
            qt.QtGui.QMouseEvent.Type.MouseMove,
            (5 * cw, 2),
            Qt.MouseButton.NoButton,
            Qt.MouseButton.LeftButton,
        )
    )
    screen.mouseReleaseEvent(
        mouse_event(
            qt,
            qt.QtGui.QMouseEvent.Type.MouseButtonRelease,
            (5 * cw, 2),
            Qt.MouseButton.LeftButton,
            Qt.MouseButton.NoButton,
        )
    )
    assert select.text(screen) == "hello"
    clipboard = qt.QtWidgets.QApplication.clipboard()
    assert clipboard.text(select.primary_mode(clipboard)) == "hello"


def test_a_click_without_a_drag_clears_the_selection(screen, qt):
    """A plain click drops the selection instead of leaving
    the last drag's anchors live."""
    from msks.client.gui.terminal import select

    Qt = qt.QtCore.Qt
    vt(screen, b"\x1b[2J\x1b[Hhello world")
    cw = screen.metrics.cell_width
    screen.mousePressEvent(
        mouse_event(
            qt,
            qt.QtGui.QMouseEvent.Type.MouseButtonPress,
            (2, 2),
            Qt.MouseButton.LeftButton,
            Qt.MouseButton.LeftButton,
        )
    )
    screen.mouseMoveEvent(
        mouse_event(
            qt,
            qt.QtGui.QMouseEvent.Type.MouseMove,
            (5 * cw, 2),
            Qt.MouseButton.NoButton,
            Qt.MouseButton.LeftButton,
        )
    )
    screen.mouseReleaseEvent(
        mouse_event(
            qt,
            qt.QtGui.QMouseEvent.Type.MouseButtonRelease,
            (5 * cw, 2),
            Qt.MouseButton.LeftButton,
            Qt.MouseButton.NoButton,
        )
    )
    assert select.text(screen) == "hello"
    screen.mousePressEvent(
        mouse_event(
            qt,
            qt.QtGui.QMouseEvent.Type.MouseButtonPress,
            (cw * 8, 2),
            Qt.MouseButton.LeftButton,
            Qt.MouseButton.LeftButton,
        )
    )
    screen.mouseReleaseEvent(
        mouse_event(
            qt,
            qt.QtGui.QMouseEvent.Type.MouseButtonRelease,
            (cw * 8, 2),
            Qt.MouseButton.LeftButton,
            Qt.MouseButton.NoButton,
        )
    )
    assert select.text(screen) == ""


def test_a_double_click_selects_a_word(screen, qt):
    """Two presses on one cell inside the multi-click window
    select the word under them."""
    from msks.client.gui.terminal import select

    Qt = qt.QtCore.Qt
    vt(screen, b"\x1b[2J\x1b[Hone two three")
    pos = (screen.metrics.cell_width * 5, 2)
    for kind_press, kind_release in (
        (
            qt.QtGui.QMouseEvent.Type.MouseButtonPress,
            qt.QtGui.QMouseEvent.Type.MouseButtonRelease,
        ),
    ):
        screen.mousePressEvent(
            mouse_event(
                qt,
                kind_press,
                pos,
                Qt.MouseButton.LeftButton,
                Qt.MouseButton.LeftButton,
            )
        )
        screen.mouseReleaseEvent(
            mouse_event(
                qt,
                kind_release,
                pos,
                Qt.MouseButton.LeftButton,
                Qt.MouseButton.NoButton,
            )
        )
    # A synthetic press carries timestamp 0, so both presses
    # sit inside the window by construction.
    screen.mousePressEvent(
        mouse_event(
            qt,
            qt.QtGui.QMouseEvent.Type.MouseButtonPress,
            pos,
            Qt.MouseButton.LeftButton,
            Qt.MouseButton.LeftButton,
        )
    )
    screen.mouseReleaseEvent(
        mouse_event(
            qt,
            qt.QtGui.QMouseEvent.Type.MouseButtonRelease,
            pos,
            Qt.MouseButton.LeftButton,
            Qt.MouseButton.NoButton,
        )
    )
    assert select.text(screen) == "two"


def test_a_triple_click_selects_the_line(screen, qt):
    """Three presses on one cell select the whole line."""
    from msks.client.gui.terminal import select

    Qt = qt.QtCore.Qt
    vt(screen, b"\x1b[2J\x1b[Halpha beta\r\ngamma delta")
    pos = (screen.metrics.cell_width * 3, 2)
    for _ in range(3):
        screen.mousePressEvent(
            mouse_event(
                qt,
                qt.QtGui.QMouseEvent.Type.MouseButtonPress,
                pos,
                Qt.MouseButton.LeftButton,
                Qt.MouseButton.LeftButton,
            )
        )
        screen.mouseReleaseEvent(
            mouse_event(
                qt,
                qt.QtGui.QMouseEvent.Type.MouseButtonRelease,
                pos,
                Qt.MouseButton.LeftButton,
                Qt.MouseButton.NoButton,
            )
        )
    assert select.text(screen) == "alpha beta"


def test_selection_survives_viewport_scrolling(screen, qt):
    """Grid-anchored selection: paging the viewport leaves the
    text unchanged, whatever rows it now shows."""
    from msks.client.gui.terminal import select

    vt(screen, b"\x1b[2J\x1b[Htarget line\r\n" + b"filler\r\n" * 60)
    select.word_at(screen, qt.QtCore.QPointF(4, 2))
    before = select.text(screen)
    screen.scroll_rows(-20)
    assert select.text(screen) == before


def test_selection_survives_reflow(screen, qt):
    """A selection's anchors re-resolve after a resize: the
    same logical text stays selected."""
    from msks.client.gui.terminal import select

    vt(screen, b"\x1b[2J\x1b[Hstable words here")
    select.word_at(screen, qt.QtCore.QPointF(screen.metrics.cell_width * 3, 2))
    before = select.text(screen)
    screen.resize(
        screen.width() + screen.metrics.cell_width * 10, screen.height()
    )
    qt.app.processEvents()
    assert select.text(screen) == before


def test_copy_puts_the_selection_on_the_clipboard(screen, qt):
    """Ctrl+Shift+C (and X, a terminal cannot cut) copies the
    selection."""
    from msks.client.gui.terminal import select

    Qt = qt.QtCore.Qt
    vt(screen, b"\x1b[2J\x1b[Hclipboard words")
    select.word_at(screen, qt.QtCore.QPointF(4, 2))
    for key in (Qt.Key.Key_C, Qt.Key.Key_X):
        screen.keyPressEvent(
            key_event(
                qt,
                key,
                modifiers=Qt.KeyboardModifier.ControlModifier
                | Qt.KeyboardModifier.ShiftModifier,
            )
        )
        assert qt.QtWidgets.QApplication.clipboard().text() == "clipboard"


def test_ctrl_shift_v_pastes_the_clipboard(screen, qt):
    """Ctrl+Shift+V sends the clipboard to the child."""
    Qt = qt.QtCore.Qt
    qt.QtWidgets.QApplication.clipboard().setText("pasted")
    screen.keyPressEvent(
        key_event(
            qt,
            Qt.Key.Key_V,
            modifiers=Qt.KeyboardModifier.ControlModifier
            | Qt.KeyboardModifier.ShiftModifier,
        )
    )
    assert drain_until(screen, b"pasted").endswith(b"pasted")


def test_paste_wraps_in_bracketed_mode(screen, qt):
    """A program that enabled mode 2004 receives its paste
    wrapped in the bracket markers, newlines intact."""
    from msks.client.gui.terminal import select

    vt(screen, b"\x1b[?2004h")
    select.paste_text(screen, "a\nb")
    seen = drain_until(screen, b"201~")
    # The pty's output post-processing (ONLCR) renders the
    # child's newlines as CRLF on the master side.
    assert seen.replace(b"\r\n", b"\n").endswith(b"\x1b[200~a\nb\x1b[201~")


def test_plain_paste_converts_newlines(screen, qt):
    """Without bracketed paste, newlines become carriage
    returns — a shell executes the line a carriage return
    ends."""
    from msks.client.gui.terminal import select

    vt(screen, b"\x1b[?2004l")
    select.paste_text(screen, "a\nb")
    seen = drain_until(screen, b"\r")
    # The pty's output post-processing (ONLCR) renders the
    # child's newlines as CRLF on the master side; input
    # CR→NL stays hushed, so the carriage return arrives
    # exactly as written.
    assert seen.replace(b"\r\n", b"\n") == b"a\rb"


def test_middle_click_pastes_primary(screen, qt):
    """The PRIMARY selection pastes back on middle-click —
    the X11 convention."""
    from msks.client.gui.terminal import select

    Qt = qt.QtCore.Qt
    clipboard = qt.QtWidgets.QApplication.clipboard()
    clipboard.setText("primary payload", select.primary_mode(clipboard))
    screen.mousePressEvent(
        mouse_event(
            qt,
            qt.QtGui.QMouseEvent.Type.MouseButtonPress,
            (10, 10),
            Qt.MouseButton.MiddleButton,
            Qt.MouseButton.MiddleButton,
        )
    )
    assert drain_until(screen, b"payload").endswith(b"primary payload")


def test_osc52_sets_the_clipboard(screen, qt):
    """An OSC 52 write from the program inside reaches the
    desktop clipboard; a selection-location write reaches
    PRIMARY instead."""
    import base64

    from msks.client.gui.terminal import select

    vt(screen, b"\x1b]52;c;" + base64.b64encode(b"from inside") + b"\x1b\\")
    clipboard = qt.QtWidgets.QApplication.clipboard()
    assert clipboard.text() == "from inside"
    vt(screen, b"\x1b]52;s;" + base64.b64encode(b"to primary") + b"\x1b\\")
    assert clipboard.text(select.primary_mode(clipboard)) == "to primary"


def test_a_non_text_osc52_payload_answers_unsupported(qt):
    """A clipboard write carrying no text payload answers
    UNSUPPORTED — the payload parser names the reason."""
    from msks.client.gui.terminal import callbacks

    write = SimpleNamespace(
        contents_len=1,
        contents=[
            SimpleNamespace(
                mime=gt.GhosttyString(
                    ptr=pointer_to(b"image/png"),
                    len=len(b"image/png"),
                ),
                data=gt.GhosttyString(),
            )
        ],
        location=0,
    )
    payload, location = callbacks.clipboard_payload(write)
    assert payload is None
    assert location == 0


def pointer_to(data: bytes):
    """A GhosttyString's char* pointing at owned bytes."""
    ctypes_p = ctypes.create_string_buffer(data + b"\x00")
    return ctypes.cast(ctypes_p, ctypes.POINTER(ctypes.c_uint8))


def test_selection_stands_aside_under_mouse_tracking(screen, qt):
    """A program that enabled mouse reporting owns the mouse:
    presses encode to it, and no selection forms."""
    from msks.client.gui.terminal import select

    Qt = qt.QtCore.Qt
    vt(screen, b"\x1b[2J\x1b[Hwords\r\n" + b"\x1b[?1000h\x1b[?1006h")
    screen.mousePressEvent(
        mouse_event(
            qt,
            qt.QtGui.QMouseEvent.Type.MouseButtonPress,
            (2, 2),
            Qt.MouseButton.LeftButton,
            Qt.MouseButton.LeftButton,
        )
    )
    screen.mouseMoveEvent(
        mouse_event(
            qt,
            qt.QtGui.QMouseEvent.Type.MouseMove,
            (60, 2),
            Qt.MouseButton.NoButton,
            Qt.MouseButton.LeftButton,
        )
    )
    screen.mouseReleaseEvent(
        mouse_event(
            qt,
            qt.QtGui.QMouseEvent.Type.MouseButtonRelease,
            (60, 2),
            Qt.MouseButton.LeftButton,
            Qt.MouseButton.NoButton,
        )
    )
    assert select.text(screen) == ""
    assert drain_until(screen, b"M")  # the press reached the child


def test_selected_cells_paint_inverted(screen, qt):
    """The library's selection flags repaint the covered
    cells with their colors swapped."""

    Qt = qt.QtCore.Qt
    vt(screen, b"\x1b[2J\x1b[Hxxxx")
    cw, ch = screen.metrics.cell_width, screen.metrics.cell_height
    before = screen.grab().toImage().pixelColor(cw // 2, ch // 2)
    screen.mousePressEvent(
        mouse_event(
            qt,
            qt.QtGui.QMouseEvent.Type.MouseButtonPress,
            (2, 2),
            Qt.MouseButton.LeftButton,
            Qt.MouseButton.LeftButton,
        )
    )
    screen.mouseMoveEvent(
        mouse_event(
            qt,
            qt.QtGui.QMouseEvent.Type.MouseMove,
            (4 * cw, 2),
            Qt.MouseButton.NoButton,
            Qt.MouseButton.LeftButton,
        )
    )
    screen.mouseReleaseEvent(
        mouse_event(
            qt,
            qt.QtGui.QMouseEvent.Type.MouseButtonRelease,
            (4 * cw, 2),
            Qt.MouseButton.LeftButton,
            Qt.MouseButton.NoButton,
        )
    )
    after = screen.grab().toImage().pixelColor(cw // 2, ch // 2)
    assert after != before


def test_the_context_menu_offers_copy_paste_and_links(screen, qt):
    """The menu dims Copy without a selection and Paste
    without clipboard text, and adds Copy Link Address over
    a link."""
    vt(screen, b"\x1b[2J\x1b[Hsee https://example.com/x now")
    screen.grab().toImage()  # the paint pass fills the URL scan
    qt.QtWidgets.QApplication.clipboard().clear()
    menu = screen.build_context_menu(None)
    labels = [action.text() for action in menu.actions()]
    assert "Copy\tCtrl+Shift+C" in labels
    assert not menu.actions()[0].isEnabled()  # nothing selected
    link_pos = qt.QtCore.QPointF(screen.metrics.cell_width * 6, 2)
    menu = screen.build_context_menu(link_pos)
    assert menu.actions()[0].text() == "Copy Link Address"


# --- hyperlinks (#953) ----------------------------------------------------


def test_plain_text_urls_record_spans(screen, qt):
    """The painter's per-row scan records plain-text URL
    spans; the link under a position answers its text."""
    from msks.client.gui.terminal import links

    vt(screen, b"\x1b[2J\x1b[Hgo to https://example.com/now please")
    screen.grab().toImage()  # the paint pass runs the scan
    cw, ch = screen.metrics.cell_width, screen.metrics.cell_height
    assert (
        links.under(screen, qt.QtCore.QPointF(cw * 7, ch // 2))
        == "https://example.com/now"
    )
    assert links.under(screen, qt.QtCore.QPointF(cw, ch // 2)) is None


def test_osc8_links_answer_their_uri(screen, qt):
    """OSC 8-marked cells answer the URI the program set,
    ahead of any plain-text scan."""
    from msks.client.gui.terminal import links

    vt(
        screen,
        b"\x1b[2J\x1b[H\x1b]8;;https://example.com/osc8\x1b\\link"
        b"\x1b]8;;\x1b\\",
    )
    cw, ch = screen.metrics.cell_width, screen.metrics.cell_height
    assert (
        links.under(screen, qt.QtCore.QPointF(cw, ch // 2))
        == "https://example.com/osc8"
    )


def test_the_hand_cursor_follows_links(screen, qt):
    """Motion with no buttons pressed turns the pointing hand
    on over a link and off away from it."""
    Qt = qt.QtCore.Qt
    vt(screen, b"\x1b[2J\x1b[Hhttps://example.com/x")
    screen.grab().toImage()
    cw, ch = screen.metrics.cell_width, screen.metrics.cell_height
    screen.mouseMoveEvent(
        mouse_event(
            qt,
            qt.QtGui.QMouseEvent.Type.MouseMove,
            (cw * 3, ch // 2),
            Qt.MouseButton.NoButton,
            Qt.MouseButton.NoButton,
        )
    )
    assert screen.hand_cursor
    screen.mouseMoveEvent(
        mouse_event(
            qt,
            qt.QtGui.QMouseEvent.Type.MouseMove,
            (cw, ch * 5),
            Qt.MouseButton.NoButton,
            Qt.MouseButton.NoButton,
        )
    )
    assert not screen.hand_cursor


def test_ctrl_click_opens_browser_links_only(screen, qt, monkeypatch):
    """Ctrl+Click opens a link through the desktop handler;
    non-browser schemes stay unopened."""
    from msks.client.gui.terminal import links

    vt(screen, b"\x1b[2J\x1b[Hhttps://example.com/ok")
    screen.grab().toImage()
    opened = []
    monkeypatch.setattr(
        links.QDesktopServices,
        "openUrl",
        lambda url: opened.append(url.toString()) or True,
    )
    cw, ch = screen.metrics.cell_width, screen.metrics.cell_height
    assert links.open_at(screen, qt.QtCore.QPointF(cw * 2, ch // 2))
    assert opened == ["https://example.com/ok"]
    vt(screen, b"\x1b[2J\x1b[H\x1b]8;;file:///etc/passwd\x1b\\x\x1b]8;;\x1b\\")
    screen.grab().toImage()  # the scan re-runs over the cleared row
    assert not links.open_at(
        screen, qt.QtCore.QPointF(cw // 2, ch // 2)
    )  # the linked cell itself: a non-browser scheme
    assert opened == ["https://example.com/ok"]


def test_www_urls_promote_to_https():
    """A bare www. host opens as https."""
    from msks.client.gui.terminal.links import openable

    assert openable("www.example.com/x") == "https://www.example.com/x"
    assert openable("ftp://example.com/f") == "ftp://example.com/f"
    assert openable("gopher://example.com") is None


# --- find (#953) ----------------------------------------------------------


def test_the_find_bar_opens_and_closes(screen, qt):
    """Ctrl+Shift+F opens the bar with a search handle;
    Escape closes it and frees the handle."""

    Qt = qt.QtCore.Qt
    screen.keyPressEvent(
        key_event(
            qt,
            Qt.Key.Key_F,
            modifiers=Qt.KeyboardModifier.ControlModifier
            | Qt.KeyboardModifier.ShiftModifier,
        )
    )
    assert screen.search
    assert screen.find_edit is not None
    screen.eventFilter(screen.find_edit, key_event(qt, Qt.Key.Key_Escape))
    assert not screen.search
    assert screen.find_edit is None


def test_a_changed_needle_reruns_the_search(screen, qt):
    """Typing in the bar runs the search live: the counter
    reads the total and the viewport holds match rects."""
    from msks.client.gui.terminal import find

    vt(screen, b"\x1b[2J\x1b[Hneedle here\r\nneedle there")
    find.open(screen)
    screen.find_edit.setText("needle")
    assert screen.find_count.text() == "2 matches"
    # The paint pass lays the match underlays beneath the
    # cells. A resize clears the search's pending matches
    # (probed), and a never-shown widget's first grab carries
    # one — so settle the first grab, re-run, write fresh
    # content, and grab: a needle cell tints against a plain
    # cell on the same row.
    screen.grab().toImage()
    vt(screen, b"\x1b[2J\x1b[Hneedle here\r\nneedle there")
    image = screen.grab().toImage()
    cw, ch = screen.metrics.cell_width, screen.metrics.cell_height
    assert image.pixelColor(cw * 2, ch // 2) != image.pixelColor(
        cw * 10, ch // 2
    )


def test_return_steps_the_matches(screen, qt):
    """Return steps to the next match and the counter follows;
    the selected match highlights above the rest."""
    from msks.client.gui.terminal import find

    vt(screen, b"\x1b[2J\x1b[Hone one one")
    find.open(screen)
    screen.find_edit.setText("one")
    qt.app.processEvents()
    screen.eventFilter(
        screen.find_edit, key_event(qt, qt.QtCore.Qt.Key.Key_Return)
    )
    assert screen.find_count.text() == "1 of 3"
    screen.eventFilter(
        screen.find_edit, key_event(qt, qt.QtCore.Qt.Key.Key_Return)
    )
    assert screen.find_count.text() == "2 of 3"
    rects = find.viewport_rects(screen)
    assert [selected for _, _, _, selected in rects].count(True) == 1


def test_shift_return_steps_back(screen, qt):
    """Shift+Return steps to the previous match."""
    from msks.client.gui.terminal import find

    vt(screen, b"\x1b[2J\x1b[Hone one one")
    find.open(screen)
    screen.find_edit.setText("one")
    qt.app.processEvents()
    for _ in range(2):
        screen.eventFilter(
            screen.find_edit, key_event(qt, qt.QtCore.Qt.Key.Key_Return)
        )
    screen.eventFilter(
        screen.find_edit,
        key_event(
            qt,
            qt.QtCore.Qt.Key.Key_Return,
            modifiers=qt.QtCore.Qt.KeyboardModifier.ShiftModifier,
        ),
    )
    assert screen.find_count.text() == "1 of 3"


def test_an_empty_needle_clears_the_matches(screen, qt):
    """Clearing the bar clears the counter and the viewport
    rects."""
    from msks.client.gui.terminal import find

    vt(screen, b"\x1b[2J\x1b[Hneedle")
    find.open(screen)
    screen.find_edit.setText("needle")
    qt.app.processEvents()
    screen.find_edit.setText("")
    qt.app.processEvents()
    assert screen.find_count.text() == ""
    assert find.viewport_rects(screen) == []


def test_the_find_shortcut_toggles(screen, qt):
    """Ctrl+Shift+F closes an open bar as well as opening
    one."""

    Qt = qt.QtCore.Qt
    shortcut = key_event(
        qt,
        Qt.Key.Key_F,
        modifiers=Qt.KeyboardModifier.ControlModifier
        | Qt.KeyboardModifier.ShiftModifier,
    )
    screen.keyPressEvent(shortcut)
    assert screen.search
    screen.keyPressEvent(shortcut)
    assert not screen.search


def test_autoscroll_extends_the_selection(screen, qt):
    """A drag held at the viewport's top edge scrolls the
    buffer and extends the selection into the revealed
    rows."""
    from msks.client.gui.terminal import select

    Qt = qt.QtCore.Qt
    vt(screen, b"\x1b[2J\x1b[Htop line\r\n" + b"filler\r\n" * 40)
    screen.mousePressEvent(
        mouse_event(
            qt,
            qt.QtGui.QMouseEvent.Type.MouseButtonPress,
            (4, 2),
            Qt.MouseButton.LeftButton,
            Qt.MouseButton.LeftButton,
        )
    )
    screen.sel_dragged = True
    screen.drag_position = qt.QtCore.QPointF(4, 2)
    before = scrollbar(screen)
    select.autoscroll_tick(screen)
    total, length, offset = scrollbar(screen)
    assert offset < before[2] or before[2] == 0
    assert select.text(screen)  # the selection followed


# --- the surfaces' edge pins (#953, gap closing) --------------------------


def test_the_find_bar_refocuses_when_already_open(screen, qt):
    """Opening the bar while it is open refocuses the edit and
    selects its text instead of rebuilding the search."""
    from msks.client.gui.terminal import find

    find.open(screen)
    first = screen.search
    screen.find_edit.setText("needle")
    find.open(screen)
    assert screen.search == first
    assert screen.find_edit.selectedText() == "needle"


def test_closing_a_never_opened_bar_is_quiet(screen, qt):
    """Close with no search handle changes nothing."""
    from msks.client.gui.terminal import find

    find.close(screen)
    assert not screen.search
    assert screen.find_edit is None


def test_the_bar_ignores_non_key_events(screen, qt):
    """The filter answers False for events that are not key
    presses — the edit keeps them."""
    from msks.client.gui.terminal import find

    find.open(screen)
    focus_out = qt.QtGui.QFocusEvent(qt.QtCore.QEvent.Type.FocusOut)
    assert not screen.eventFilter(screen.find_edit, focus_out)
    assert screen.find_edit is not None


def test_run_and_step_without_a_search_do_nothing(screen, qt):
    """A dead bar's run and step are no-ops."""
    from msks.client.gui.terminal import find

    find.run(screen, "needle")
    find.step(screen, 1)


def test_a_plain_key_in_the_bar_reaches_the_edit(screen, qt):
    """A key that is neither Return nor Escape falls through to
    the edit."""
    from msks.client.gui.terminal import find

    find.open(screen)
    plain = key_event(qt, qt.QtCore.Qt.Key.Key_A)
    assert not find.filter_key(screen, plain)


def test_matches_in_history_do_not_paint(screen, qt):
    """A match pushed into history by later output stays out of
    the viewport's rects while the counter still counts it."""
    from msks.client.gui.terminal import find

    vt(screen, b"\x1b[2J\x1b[Hneedle\r\n" + b"filler\r\n" * 60)
    find.open(screen)
    screen.find_edit.setText("needle")
    qt.app.processEvents()
    assert screen.find_count.text() == "1 matches"
    assert find.viewport_rects(screen) == []


def test_viewport_rects_without_a_search_are_empty(screen, qt):
    """Viewport rects without a search answer empty."""
    from msks.client.gui.terminal import find

    assert find.viewport_rects(screen) == []


def test_an_oversized_osc8_uri_is_dropped(screen, qt):
    """A URI past the library's ~2 KiB cap never lands on the
    cell (probed: 2014 bytes answers, 2061 answers written 0)
    — the query answers no link rather than a truncated
    one."""
    from msks.client.gui.terminal import links

    uri = "https://example.com/" + "x" * 4200
    vt(
        screen,
        b"\x1b[2J\x1b[H\x1b]8;;" + uri.encode() + b"\x1b\\z\x1b]8;;\x1b\\",
    )
    cw, ch = screen.metrics.cell_width, screen.metrics.cell_height
    assert links.under(screen, qt.QtCore.QPointF(cw // 2, ch // 2)) is None
    short = "https://example.com/" + "x" * 2000
    vt(
        screen,
        b"\x1b[2J\x1b[H\x1b]8;;" + short.encode() + b"\x1b\\z\x1b]8;;\x1b\\",
    )
    assert links.under(screen, qt.QtCore.QPointF(cw // 2, ch // 2)) == short


def test_an_unresolved_ref_answers_no_uri(screen, qt):
    """A grid ref the library did not resolve answers no URI —
    the error answer (probed: a zeroed ref answers -2) is not
    an out-of-space retry."""
    from msks.client.gui.terminal import links

    ref = gt.GridRef()
    ref.size = ctypes.sizeof(ref)
    assert links.osc8_uri(screen, ref) is None


def test_open_at_without_a_link_answers_false(screen, qt):
    """Ctrl+Click over no link opens nothing — and the press
    falls through to the selection gesture."""
    from msks.client.gui.terminal import links

    Qt = qt.QtCore.Qt
    vt(screen, b"\x1b[2J\x1b[Hplain text")
    cw, ch = screen.metrics.cell_width, screen.metrics.cell_height
    assert not links.open_at(screen, qt.QtCore.QPointF(cw, ch // 2))
    screen.mousePressEvent(
        mouse_event(
            qt,
            qt.QtGui.QMouseEvent.Type.MouseButtonPress,
            (cw, ch // 2),
            Qt.MouseButton.LeftButton,
            Qt.MouseButton.LeftButton,
            qt.QtCore.Qt.KeyboardModifier.ControlModifier,
        )
    )
    assert screen.press_count == 1  # the gesture began
    screen.mouseReleaseEvent(
        mouse_event(
            qt,
            qt.QtGui.QMouseEvent.Type.MouseButtonRelease,
            (cw, ch // 2),
            Qt.MouseButton.LeftButton,
            Qt.MouseButton.NoButton,
            qt.QtCore.Qt.KeyboardModifier.ControlModifier,
        )
    )


def test_the_hand_cursor_settles(screen, qt):
    """The hand settles: a repeat request changes nothing."""
    from msks.client.gui.terminal import links

    links.set_hand(screen, False)
    assert not screen.hand_cursor


def test_ctrl_click_on_a_link_opens_and_consumes(screen, qt, monkeypatch):
    """A Ctrl+Click over a plain-text link opens it through the
    desktop handler and the press never starts a selection."""
    from msks.client.gui.terminal import links

    Qt = qt.QtCore.Qt
    vt(screen, b"\x1b[2J\x1b[Hhttps://example.com/click")
    screen.grab().toImage()  # the paint pass fills the URL scan
    opened = []
    monkeypatch.setattr(
        links.QDesktopServices,
        "openUrl",
        lambda url: opened.append(url.toString()) or True,
    )
    cw, ch = screen.metrics.cell_width, screen.metrics.cell_height
    screen.mousePressEvent(
        mouse_event(
            qt,
            qt.QtGui.QMouseEvent.Type.MouseButtonPress,
            (cw * 2, ch // 2),
            Qt.MouseButton.LeftButton,
            Qt.MouseButton.LeftButton,
            qt.QtCore.Qt.KeyboardModifier.ControlModifier,
        )
    )
    assert opened == ["https://example.com/click"]
    assert screen.press_count == 0  # no selection began


def test_a_drag_turns_the_hand_off(screen, qt):
    """Motion with a button pressed drops the hand — the drag
    owns the cursor."""
    Qt = qt.QtCore.Qt
    screen.hand_cursor = True
    screen.mouseMoveEvent(
        mouse_event(
            qt,
            qt.QtGui.QMouseEvent.Type.MouseMove,
            (40, 8),
            Qt.MouseButton.NoButton,
            Qt.MouseButton.LeftButton,
        )
    )
    assert not screen.hand_cursor


def test_a_right_press_reaches_the_encoder(screen, qt):
    """A right-button press under no tracking skips selection
    and forwards to the mouse encoder."""
    Qt = qt.QtCore.Qt
    screen.mousePressEvent(
        mouse_event(
            qt,
            qt.QtGui.QMouseEvent.Type.MouseButtonPress,
            (10, 10),
            Qt.MouseButton.RightButton,
            Qt.MouseButton.RightButton,
        )
    )
    screen.mouseReleaseEvent(
        mouse_event(
            qt,
            qt.QtGui.QMouseEvent.Type.MouseButtonRelease,
            (10, 10),
            Qt.MouseButton.RightButton,
            Qt.MouseButton.NoButton,
        )
    )
    assert screen.press_count == 0  # no selection began


def test_the_context_menu_paths(screen, qt, monkeypatch):
    """Under mouse reporting the menu stands aside; without it
    the menu runs at the press point."""
    executed = []

    class FakeMenu:
        def exec(self, pos):
            executed.append(pos)
            return None

    monkeypatch.setattr(screen, "build_context_menu", lambda pos: FakeMenu())
    event = qt.QtGui.QContextMenuEvent(
        qt.QtGui.QContextMenuEvent.Reason.Mouse,
        qt.QtCore.QPoint(10, 10),
        qt.QtCore.QPoint(10, 10),
    )
    vt(screen, b"\x1b[?1000h")
    screen.contextMenuEvent(event)
    assert not executed
    assert not event.isAccepted()
    vt(screen, b"\x1b[?1000l")
    screen.contextMenuEvent(event)
    assert executed


def test_the_menu_copy_action_carries_the_selection(screen, qt):
    """The menu's Copy action copies the selection through the
    clipboard path."""
    from msks.client.gui.terminal import select

    vt(screen, b"\x1b[2J\x1b[Hmenu words")
    select.word_at(screen, qt.QtCore.QPointF(4, 2))
    menu = screen.build_context_menu(qt.QtCore.QPointF(4, 2))
    copy_action = [a for a in menu.actions() if a.text().startswith("Copy")][0]
    copy_action.trigger()
    qt.app.processEvents()
    assert qt.QtWidgets.QApplication.clipboard().text() == "menu"


def test_a_blank_word_select_answers_empty(screen, qt):
    """Word and line selects over blank cells answer empty
    selections, not failures (probed)."""
    from msks.client.gui.terminal import select

    vt(screen, b"\x1b[2J\x1b[Halpha beta\r\n\r\n        \r\nmore")
    cw, ch = screen.metrics.cell_width, screen.metrics.cell_height
    select.word_at(screen, qt.QtCore.QPointF(cw * 3, ch + 1))
    assert select.text(screen) == ""
    select.line_at(screen, qt.QtCore.QPointF(cw * 3, ch + 1))
    assert select.text(screen) == ""


def test_a_huge_word_selection_grows_the_format_buffer(screen, qt):
    """A selection over 4 KiB formats through the grown
    buffer."""
    from msks.client.gui.terminal import select

    vt(screen, b"\x1b[2J\x1b[H" + b"z" * 6000)
    select.word_at(screen, qt.QtCore.QPointF(4, 2))
    assert len(select.text(screen)) > 4096


def test_copy_and_publish_with_no_selection_are_quiet(screen, qt):
    """Copy and PRIMARY publish over nothing leave the
    clipboard alone."""
    from msks.client.gui.terminal import select

    qt.QtWidgets.QApplication.clipboard().clear()
    select.copy(screen)
    select.publish_primary(screen)
    assert qt.QtWidgets.QApplication.clipboard().text() == ""


def test_pasting_empty_text_writes_nothing(screen, qt):
    """An empty paste writes no bytes."""
    from msks.client.gui.terminal import select

    select.paste_text(screen, "")
    time.sleep(0.1)
    assert screen.session.read_available() in (None, b"")


def test_autoscroll_at_the_bottom_edge(screen, qt):
    """A drag held at the bottom edge scrolls forward into the
    buffer's newer rows."""
    from msks.client.gui.terminal import select

    Qt = qt.QtCore.Qt
    vt(screen, b"\x1b[2J\x1b[Htop\r\n" + b"filler\r\n" * 40)
    screen.scroll_rows(-30)  # history in view
    screen.mousePressEvent(
        mouse_event(
            qt,
            qt.QtGui.QMouseEvent.Type.MouseButtonPress,
            (4, 4),
            Qt.MouseButton.LeftButton,
            Qt.MouseButton.LeftButton,
        )
    )
    screen.sel_dragged = True
    screen.drag_position = qt.QtCore.QPointF(4, screen.height() - 2)
    select.autoscroll_tick(screen)
    assert scrollbar(screen)[2] > 0  # scrolled toward the bottom


def test_autoscroll_in_the_middle_does_nothing(screen, qt):
    """A drag held in the middle of the viewport scrolls
    nothing."""
    from msks.client.gui.terminal import select

    Qt = qt.QtCore.Qt
    vt(screen, b"\x1b[2J\x1b[Htop\r\n" + b"filler\r\n" * 40)
    screen.mousePressEvent(
        mouse_event(
            qt,
            qt.QtGui.QMouseEvent.Type.MouseButtonPress,
            (4, 4),
            Qt.MouseButton.LeftButton,
            Qt.MouseButton.LeftButton,
        )
    )
    screen.sel_dragged = True
    screen.drag_position = qt.QtCore.QPointF(4, screen.height() // 2)
    before = scrollbar(screen)
    select.autoscroll_tick(screen)
    assert scrollbar(screen) == before
    screen.sel_dragged = False  # the timer's tick with no drag
    select.autoscroll_tick(screen)
    assert scrollbar(screen) == before


def test_the_autoscroll_timer_drives_the_widget(screen, qt):
    """The widget's timer delegates to the selection module."""
    Qt = qt.QtCore.Qt
    vt(screen, b"\x1b[2J\x1b[Htop\r\n" + b"filler\r\n" * 40)
    screen.mousePressEvent(
        mouse_event(
            qt,
            qt.QtGui.QMouseEvent.Type.MouseButtonPress,
            (4, 4),
            Qt.MouseButton.LeftButton,
            Qt.MouseButton.LeftButton,
        )
    )
    screen.sel_dragged = True
    screen.drag_position = qt.QtCore.QPointF(4, 2)
    before = scrollbar(screen)
    screen.autoscroll_tick()
    assert scrollbar(screen)[2] < before[2] or before[2] == 0


def test_a_non_text_osc52_write_answers_unsupported(screen, qt):
    """A clipboard write with no text payload answers
    UNSUPPORTED and leaves the clipboard alone."""
    payload = ctypes.create_string_buffer(b"image/png\x00")
    mime = gt.GhosttyString(
        ptr=ctypes.cast(payload, ctypes.POINTER(ctypes.c_uint8)),
        len=len(b"image/png"),
    )
    contents = (gt.ClipboardContent * 1)(
        gt.ClipboardContent(mime=mime, data=gt.GhosttyString())
    )
    write = gt.ClipboardWrite()
    write.size = ctypes.sizeof(write)
    write.location = 0
    write.contents = contents
    write.contents_len = 1
    write.reply = ctypes.cast(None, gt.CLIPBOARD_WRITE_REPLY_FN)
    qt.QtWidgets.QApplication.clipboard().clear()
    screen.osc52_clipboard(write)
    assert qt.QtWidgets.QApplication.clipboard().text() == ""


def test_a_clipboard_write_without_a_writer_is_ignored(qt):
    """A late callback for a freed screen (or a null write
    pointer) changes nothing."""
    from msks.client.gui.terminal import callbacks

    callbacks.clipboard_write(0, 0, None)


# --- the review's pins (#953 follow-ups) ----------------------------------


class FakePress:
    """A press carrying a real timestamp: synthetic QMouseEvents
    answer 0, and the multi-click window needs two distinct
    clocks."""

    def __init__(self, pos, ts):
        from PySide6.QtCore import QPointF

        self._pos = QPointF(*pos)
        self._ts = ts

    def position(self):
        return self._pos

    def timestamp(self):
        return self._ts


def test_the_multi_click_window_expires(qt, screen):
    """A second press on the same cell inside the window counts
    as a multi-click; the same press past the window starts a
    fresh one."""
    cw = screen.metrics.cell_width
    pos = (cw, 2)
    screen.press_gesture(FakePress(pos, 1000))
    assert screen.press_count == 1
    screen.press_gesture(FakePress(pos, 1500))  # 500 ms later
    assert screen.press_count == 1  # the window expired
    screen.press_gesture(FakePress(pos, 1600))  # 100 ms after
    assert screen.press_count == 2
    screen.press_gesture(FakePress(pos, 2000))  # exactly the window
    assert screen.press_count == 3  # the boundary counts inside


def test_the_multi_click_window_resets_on_a_new_cell(qt, screen):
    """A press on a different cell starts a fresh count inside
    the window."""
    cw = screen.metrics.cell_width
    screen.press_gesture(FakePress((cw, 2), 1000))
    screen.press_gesture(FakePress((cw * 5, 2), 1100))
    assert screen.press_count == 1


def test_the_span_upper_bound_excludes_the_next_cell(screen, qt):
    """A URL span covers its own cells and not the one after:
    the col one past the end answers no link."""
    from msks.client.gui.terminal import links

    vt(screen, b"\x1b[2J\x1b[Hhttps://example.com/x tail")
    screen.grab().toImage()  # the paint pass fills the URL scan
    cw, ch = screen.metrics.cell_width, screen.metrics.cell_height
    end = screen.link_spans[0][0][1]
    assert links.under(screen, qt.QtCore.QPointF(cw * (end - 1), ch // 2))
    assert links.under(screen, qt.QtCore.QPointF(cw * end, ch // 2)) is None


def test_the_hand_drops_when_the_pointer_leaves(screen, qt):
    """The pointing hand is an application-global override; a
    leave event drops it so the rest of the app keeps its
    cursor."""
    Qt = qt.QtCore.Qt
    vt(screen, b"\x1b[2J\x1b[Hhttps://example.com/x")
    screen.grab().toImage()  # the paint pass fills the URL scan
    cw, ch = screen.metrics.cell_width, screen.metrics.cell_height
    screen.mouseMoveEvent(
        mouse_event(
            qt,
            qt.QtGui.QMouseEvent.Type.MouseMove,
            (cw * 2, ch // 2),
            Qt.MouseButton.NoButton,
            Qt.MouseButton.NoButton,
        )
    )
    hand = qt.QtWidgets.QApplication.overrideCursor()
    assert hand is not None
    assert hand.shape() is Qt.CursorShape.PointingHandCursor
    leave = qt.QtCore.QEvent(qt.QtCore.QEvent.Type.Leave)
    screen.leaveEvent(leave)
    assert qt.QtWidgets.QApplication.overrideCursor() is None
    screen.leaveEvent(leave)  # a second leave stays quiet


def test_a_close_while_hovering_drops_the_hand(qt):
    """A tab closed while its hand cursor is on drops the
    override before the widget goes."""
    from msks.client.gui.terminal import TerminalWidget, links

    widget = TerminalWidget(
        [sys.executable, "-c", "import time; time.sleep(30)"]
    )
    try:
        links.set_hand(widget, True)  # the override is really pushed
        assert widget.hand_cursor
        shutdown(widget)
        assert qt.QtWidgets.QApplication.overrideCursor() is None
    finally:
        shutdown(widget)


def test_a_crlf_paste_carries_one_line_break(screen, qt):
    """A Windows-format paste sends one carriage return per
    paragraph, not two."""
    from msks.client.gui.terminal import select

    vt(screen, b"\x1b[?2004l")
    select.paste_text(screen, "a\r\nb")
    seen = drain_until(screen, b"\r")
    assert seen.replace(b"\r\n", b"\n") == b"a\rb"
