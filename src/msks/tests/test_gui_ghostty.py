"""The libghostty-vt ctypes binding (#950): the port of the
spike's binding (#935) into the client package. The struct
mirrors pin against the C ABI — a bad mirror fails loudly
before the library traps on it — and, where the library is
installed (the pyghostty wheel the gui extra pins), the binding
drives the render state, the key and mouse encoders, and the
selection gesture: the APIs the terminal widget needs.
"""

import ast
import ctypes
from pathlib import Path

import pytest
from msks.client.gui import ghostty as gt

#: The struct sizes the C ABI dictates (#935's mirrors, pinned
#: so a bad field list — a dropped padding member above all —
#: fails here, at the test's run, not in a Zig trap inside the
#: library.
ABI_SIZES = {
    "ColorRgb": 3,
    "StyleColorValue": 8,
    "StyleColor": 16,
    "Style": 72,
    "RenderStateColors": 792,
    "Scrollbar": 24,
    "SizeReportSize": 12,
    # 4, not 8: GhosttyMode is uint16 (compile-verified:
    # sizeof 4, value at offset 2). #952 found the mirror
    # carrying a uint32 mode field — same 8-byte size the old
    # pin recorded, with the answered bool landing in padding,
    # so every mode query read False.
    "ModeConfig": 4,
    "GhosttyString": 16,
    "DeviceAttributes": 160,
    "MousePosition": 8,
    "MouseEncoderSize": 40,
    "ScrollViewportValue": 16,
    "ScrollViewport": 24,
    "GridRef": 24,
    "Selection": 64,
    "SelectionFormatOptions": 24,
    "SelectWordOptions": 48,
    "SelectLineOptions": 56,
    "ClipboardContent": 32,
    "SelectionBuffer": 24,
    "ClipboardWriteReply": 16,
    "ClipboardWrite": 72,
    "PointCoordinate": 8,
    "PointValue": 16,
    "Point": 24,
    "SurfacePosition": 16,
    "SelectionGestureGeometry": 16,
}

#: A few GhosttyKey codes at their positional values — the
#: list's order IS the ABI, and these pins catch a silent
#: shift of it.
KEY_CODES = {
    "UNIDENTIFIED": 0,
    "A": 20,
    "ENTER": 58,
    "SPACE": 63,
    "TAB": 64,
    "ARROW_UP": 78,
    "ESCAPE": 120,
    "F1": 121,
}


def library_present() -> bool:
    """Whether the library the binding's own discovery resolves
    loads on this host (#950): the wheel, an override, or a
    system build — the drive tests skip without it, and the
    skip matches what ``Lib()`` would actually find, not the
    wheel's mere presence."""
    try:
        gt.Lib()
    except OSError, AttributeError:
        return False
    return True


LIBRARY_PRESENT = library_present()


def test_the_struct_mirrors_match_the_abi() -> None:
    """Every mirror at its C size (#935, #950): a struct that
    shrank — the padding member a mirror dropped — fails here,
    by name, before the library reads garbage off the stack."""
    for name, size in ABI_SIZES.items():
        struct = getattr(gt, name)
        assert ctypes.sizeof(struct) == size, name
    # Field offsets too, for the struct whose #952 bug was
    # layout, not size: the answered value must land where the
    # mirror reads it (compile-verified against the header).
    assert gt.ModeConfig.mode.offset == 0
    assert gt.ModeConfig.value.offset == 2


def test_the_key_codes_hold_their_positions() -> None:
    """The key list's order is the ABI (#950): the pinned codes
    catch a name inserted or removed ahead of them, and the
    count assertion in the module holds the list's tail."""
    for name, code in KEY_CODES.items():
        assert gt.KEY[name] == code, name
    assert len(gt.KEY_NAMES) == 176
    assert gt.KEY_NAMES[0] == "UNIDENTIFIED"
    assert gt.KEY_NAMES[-1] == "PASTE"


def test_the_module_imports_no_qt() -> None:
    """The binding is ctypes over the C ABI (#950): nothing in
    it imports PySide6 — the terminal widget layer owns Qt."""
    tree = ast.parse(Path(gt.__file__).read_text(), filename=str(gt.__file__))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            names = [node.module or ""]
        else:
            continue
        assert not any(
            name == "PySide6" or name.startswith("PySide6.") for name in names
        )


def wheel_spec(_name):
    """The find_spec seam's answer for an installed pyghostty
    whose ``_lib`` directory carries nothing."""
    return type(
        "Spec",
        (),
        {"submodule_search_locations": ["/nowhere/pyghostty"]},
    )()


def test_library_discovery_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Where the binding loads the library from (#950): the
    ``GHOSTTY_VT_LIB`` override first, the pyghostty wheel's
    bundled build second, the system loader's bare name last —
    and a wheel whose ``_lib`` directory carries nothing reads
    as absent, not as an error."""
    monkeypatch.setenv("GHOSTTY_VT_LIB", "/override/libghostty-vt.so")
    assert gt.library_path() == "/override/libghostty-vt.so"
    monkeypatch.delenv("GHOSTTY_VT_LIB")
    assert gt.library_path(wheel_spec) == gt.SYSTEM_LIBRARY
    assert gt.wheel_library(wheel_spec) is None
    assert gt.wheel_library(lambda _name: None) is None
    raised = ValueError("a broken distribution")

    def broken(_name):
        raise raised

    assert gt.wheel_library(broken) is None


def test_a_library_that_cannot_load_says_so() -> None:
    """The load's own failure (#950): a path that holds no
    library raises the loader's error, not a silent
    anything."""
    with pytest.raises(OSError):
        gt.Lib("/nowhere/libghostty-vt.so")


@pytest.mark.skipif(
    not LIBRARY_PRESENT,
    reason="libghostty-vt does not load on this host",
)
class TestTheDrive:
    """The binding against the real library (#950): the
    render-state iteration the widget paints from, the key and
    mouse encoders its input paths ride, and the selection
    gesture its clipboard work stands on."""

    @pytest.fixture()
    def lib(self) -> gt.Lib:
        return gt.Lib()

    @pytest.fixture()
    def terminal(self, lib: gt.Lib) -> int:
        handle = lib.new_handle(lib.terminal_new, 32, 4)
        yield handle
        lib.terminal_free(handle)

    def screen_text(self, lib: gt.Lib, terminal: int) -> list[str]:
        """The screen as lines, through the render state's row
        and cell iterators — the widget's whole painting read.
        The iterator and cell handles are in/out parameters: the
        handle slots are pre-filled, and the gets write through
        them (#935's lesson)."""
        state = lib.new_handle(lib.render_state_new)
        assert lib.render_state_update(state, terminal) == gt.SUCCESS
        # The iterator's in/out slot: the new() handle goes in,
        # the get() answers the real iterator through it.
        iterator = ctypes.c_void_p(lib.new_handle(lib.row_iterator_new, state))
        ok, iterator = lib.get(
            lib.render_state_get, state, gt.RS_ROW_ITERATOR, iterator
        )
        assert ok
        lines: list[str] = []
        codepoints = (ctypes.c_uint32 * 16)()
        while lib.row_iterator_next(iterator.value):
            cells = ctypes.c_void_p(
                lib.new_handle(lib.row_cells_new, iterator.value)
            )
            ok, _ = lib.get(lib.row_get, iterator.value, gt.ROW_CELLS, cells)
            assert ok
            chars: list[str] = []
            while lib.row_cells_next(cells.value):
                ok, length = lib.get(
                    lib.row_cells_get,
                    cells,
                    gt.CELLS_GRAPHEMES_LEN,
                    ctypes.c_uint32(),
                )
                assert ok
                if length.value:
                    assert length.value <= len(codepoints)
                    assert (
                        lib.row_cells_get(
                            cells.value,
                            gt.CELLS_GRAPHEMES_BUF,
                            ctypes.cast(codepoints, ctypes.c_void_p),
                        )
                        == gt.SUCCESS
                    )
                    chars.append(
                        "".join(
                            chr(codepoints[i]) for i in range(length.value)
                        )
                    )
            lib.row_cells_free(cells.value)
            lines.append("".join(chars))
        lib.row_iterator_free(iterator.value)
        lib.render_state_free(state)
        return lines

    def test_the_render_state_reads_the_screen(
        self, lib: gt.Lib, terminal: int
    ) -> None:
        """VT bytes in, the rendered screen out (#950): the row
        and cell iteration the widget paints from carries the
        emulator's parsed text."""
        lib.vt_write(terminal, b"hello vt")
        lines = self.screen_text(lib, terminal)
        assert lines[0].startswith("hello vt")
        lib.vt_write(terminal, b"\r\nrow two")
        lines = self.screen_text(lib, terminal)
        assert lines[1].startswith("row two")

    def test_the_key_encoder_encodes_the_widget_keys(
        self, lib: gt.Lib, terminal: int
    ) -> None:
        """The key encoder drives the widget's keyboard input
        (#950): Enter in legacy mode answers a carriage return,
        and a terminal that switched to application cursor keys
        changes what an arrow answers — the encoder reads the
        modes off the terminal itself."""
        encoder = lib.new_handle(lib.key_encoder_new)
        event = lib.new_handle(lib.key_event_new)
        try:
            lib.key_event_set_key(event, gt.KEY["ENTER"])
            lib.key_event_set_action(event, gt.ACTION_PRESS)
            assert lib.encode_key(encoder, event) == b"\r"
            lib.vt_write(terminal, b"\x1b[?1h")  # application keys
            lib.key_encoder_setopt_from_terminal(encoder, terminal)
            lib.key_event_set_key(event, gt.KEY["ARROW_UP"])
            assert lib.encode_key(encoder, event) == b"\x1bOA"
        finally:
            lib.key_event_free(event)
            lib.key_encoder_free(encoder)

    def test_the_mouse_encoder_reports_a_press(
        self, lib: gt.Lib, terminal: int
    ) -> None:
        """The mouse encoder drives the widget's mouse input
        (#950): a terminal in SGR tracking answers a left press
        at a cell with a marker-terminated report."""
        lib.vt_write(terminal, b"\x1b[?1006;1000h")  # SGR tracking
        encoder = lib.new_handle(lib.mouse_encoder_new)
        event = lib.new_handle(lib.mouse_event_new)
        try:
            lib.mouse_encoder_setopt_from_terminal(encoder, terminal)
            size = gt.MouseEncoderSize(
                size=ctypes.sizeof(gt.MouseEncoderSize),
                screen_width=800,
                screen_height=600,
                cell_width=10,
                cell_height=20,
            )
            lib.mouse_encoder_setopt(encoder, gt.MOPT_SIZE, ctypes.byref(size))
            lib.mouse_event_set_action(event, gt.MOUSE_PRESS)
            lib.mouse_event_set_button(event, gt.MOUSE_LEFT)
            lib.mouse_event_set_position(
                event, gt.MousePosition(x=35.0, y=45.0)
            )
            report = lib.encode_mouse(encoder, event)
            assert report.startswith(b"\x1b[<0;")
            assert report.endswith(b"M")
        finally:
            lib.mouse_event_free(event)
            lib.mouse_encoder_free(encoder)

    def test_the_selection_gesture_selects_a_word(
        self, lib: gt.Lib, terminal: int
    ) -> None:
        """The selection gesture drives the widget's clipboard
        work (#950): a word selection over written text answers
        a grid-anchored selection the screen can read back. The
        ref and the selection carry their own size, in and out —
        the same pre-filled-slot lesson as the iterators."""
        lib.vt_write(terminal, b"select thisword now")
        point = gt.Point(
            tag=gt.POINT_VIEWPORT,
            value=gt.PointValue(coordinate=gt.PointCoordinate(x=8, y=0)),
        )
        ref = gt.GridRef(size=ctypes.sizeof(gt.GridRef))
        assert (
            lib.terminal_grid_ref(terminal, point, ctypes.byref(ref))
            == gt.SUCCESS
        )
        options = gt.SelectWordOptions(
            size=ctypes.sizeof(gt.SelectWordOptions), ref=ref
        )
        selection = gt.Selection(size=ctypes.sizeof(gt.Selection))
        assert (
            lib.terminal_select_word(
                terminal, ctypes.byref(options), ctypes.byref(selection)
            )
            == gt.SUCCESS
        )
        assert selection.size == ctypes.sizeof(gt.Selection)

    def test_the_scroll_viewport_scrolls_and_reports(
        self, lib: gt.Lib, terminal: int
    ) -> None:
        """The scroll viewport (#950): the struct whose padding
        mirror is the port's headline trap, driven — scroll a
        written buffer and the scrollbar answers the moved
        offset."""
        for n in range(8):
            lib.vt_write(terminal, f"line {n}\r\n".encode())
        lib.vt_write(terminal, b"last line")
        ok, bar = lib.get(
            lib.terminal_get,
            terminal,
            gt.DATA_SCROLLBAR,
            gt.Scrollbar(),
        )
        assert ok
        # Nine lines in a four-row viewport, pinned at the
        # bottom.
        assert (bar.total, bar.offset, bar.len) == (9, 5, 4)
        # The value slot is unused by both tags — the union
        # wants some field; the row count moves nothing here.
        top = gt.ScrollViewport(
            tag=gt.SCROLL_TOP, value=gt.ScrollViewportValue(row=0)
        )
        assert lib.terminal_scroll_viewport(terminal, top) is None
        ok, bar = lib.get(
            lib.terminal_get,
            terminal,
            gt.DATA_SCROLLBAR,
            gt.Scrollbar(),
        )
        assert ok
        assert bar.offset == 0  # the viewport now sits at the top
        bottom = gt.ScrollViewport(
            tag=gt.SCROLL_BOTTOM, value=gt.ScrollViewportValue(row=1)
        )
        lib.terminal_scroll_viewport(terminal, bottom)
        ok, bar = lib.get(
            lib.terminal_get,
            terminal,
            gt.DATA_SCROLLBAR,
            gt.Scrollbar(),
        )
        assert ok
        assert bar.offset == 5  # and back to the bottom

    def test_the_build_names_itself(self, lib: gt.Lib) -> None:
        """The library's build version (#950): the snapshot the
        wheel carries names itself — the string the About line
        and the logs read — and a library that will not say
        reads as unknown."""
        assert lib.build_version() == "0.1.0-dev"
        refused = lib.build_info
        lib.build_info = lambda *_args: gt.NO_VALUE
        try:
            assert lib.build_version() == "unknown"
        finally:
            lib.build_info = refused

    def test_the_encoders_refusals_answer_empty(self, lib: gt.Lib) -> None:
        """The encoders' refusal arms (#950): the focus report
        encodes mode 1004's own sequences, and a call the
        library refuses answers empty bytes — never an
        exception, never a stale buffer."""
        assert lib.encode_focus(True) == b"\x1b[I"
        assert lib.encode_focus(False) == b"\x1b[O"
        names = (
            "key_encoder_encode",
            "mouse_encoder_encode",
            "focus_encode",
        )
        refused = {name: getattr(lib, name) for name in names}
        for name in names:
            setattr(lib, name, lambda *_args: gt.NO_VALUE)
        try:
            assert lib.encode_key(0, 0) == b""
            assert lib.encode_mouse(0, 0) == b""
            assert lib.encode_focus(True) == b""
        finally:
            for name, fn in refused.items():
                setattr(lib, name, fn)

    def test_a_failed_allocation_raises(self, lib: gt.Lib) -> None:
        """The constructor helper's failure (#950): an allocator
        that answers anything but success raises — the widget
        never paints from a handle the library did not give."""
        with pytest.raises(RuntimeError):
            lib.new_handle(lambda *_args: gt.NO_VALUE)


def test_the_ghostty_string_decodes() -> None:
    """GhosttyString as text (#950): the pointer-and-length pair
    the library answers titles with decodes, undecodable bytes
    replaced — no library needed for the read."""
    raw = ctypes.cast(
        ctypes.c_char_p(b"tab title"), ctypes.POINTER(ctypes.c_uint8)
    )
    assert gt.GhosttyString(ptr=raw, len=9).text() == "tab title"
    broken = ctypes.cast(
        ctypes.c_char_p(b"\xff\xfe"), ctypes.POINTER(ctypes.c_uint8)
    )
    assert gt.GhosttyString(ptr=broken, len=2).text() == "\ufffd\ufffd"
