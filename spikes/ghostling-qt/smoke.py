"""Offscreen smoke test for the spike: exercises the binding, the
emulator, the key encoder, a real PTY session, and the widget without
a display (QT_QPA_PLATFORM=offscreen).

Run from this directory:

    QT_QPA_PLATFORM=offscreen python smoke.py
"""

from __future__ import annotations

import ctypes
import os
import sys
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import ghostty  # noqa: E402


def screen_text(
    lib, terminal, render_state, row_iterator, row_cells
) -> list[str]:
    lib.render_state_update(render_state, terminal)
    ok, iterator = lib.get(
        lib.render_state_get,
        render_state,
        ghostty.RS_ROW_ITERATOR,
        ctypes.c_void_p(row_iterator),
    )
    assert ok and iterator.value
    lines: list[str] = []
    codepoints = (ctypes.c_uint32 * 16)()
    while lib.row_iterator_next(iterator.value):
        ok, cells = lib.get(
            lib.row_get,
            iterator.value,
            ghostty.ROW_CELLS,
            ctypes.c_void_p(row_cells),
        )
        assert ok and cells.value
        chars: list[str] = []
        while lib.row_cells_next(cells.value):
            ok, length = lib.get(
                lib.row_cells_get,
                cells,
                ghostty.CELLS_GRAPHEMES_LEN,
                ctypes.c_uint32(),
            )
            assert ok
            if length.value:
                lib.row_cells_get(
                    cells.value,
                    ghostty.CELLS_GRAPHEMES_BUF,
                    ctypes.cast(codepoints, ctypes.c_void_p),
                )
                chars.append(
                    "".join(chr(codepoints[i]) for i in range(length.value))
                )
        lines.append("".join(chars))
    return lines


def test_emulator() -> None:
    lib = ghostty.Lib()
    print("libghostty-vt version:", lib.build_version())
    terminal = lib.new_handle(lib.terminal_new, 80, 24)
    render_state = lib.new_handle(lib.render_state_new)
    row_iterator = lib.new_handle(lib.row_iterator_new)
    row_cells = lib.new_handle(lib.row_cells_new)
    lib.vt_write(terminal, b"hello \x1b[31mred\x1b[0m tail")
    lib.vt_write(terminal, b"\r\nline two \x1b[1mbold\x1b[0m")
    lines = screen_text(lib, terminal, render_state, row_iterator, row_cells)
    assert lines[0].startswith("hello red tail"), lines[0]
    assert lines[1].startswith("line two bold"), lines[1]
    print("emulator: text with styles parsed ok")

    # The SGR-red run resolves to an explicit 24-bit cell color.
    ok, iterator = lib.get(
        lib.render_state_get,
        render_state,
        ghostty.RS_ROW_ITERATOR,
        ctypes.c_void_p(row_iterator),
    )
    assert ok and iterator.value
    codepoints = (ctypes.c_uint32 * 16)()
    reds = []
    while lib.row_iterator_next(iterator.value):
        ok, cells = lib.get(
            lib.row_get,
            iterator.value,
            ghostty.ROW_CELLS,
            ctypes.c_void_p(row_cells),
        )
        assert ok and cells.value
        while lib.row_cells_next(cells.value):
            ok, length = lib.get(
                lib.row_cells_get,
                cells,
                ghostty.CELLS_GRAPHEMES_LEN,
                ctypes.c_uint32(),
            )
            assert ok
            if not length.value:
                continue
            lib.row_cells_get(
                cells,
                ghostty.CELLS_GRAPHEMES_BUF,
                ctypes.cast(codepoints, ctypes.c_void_p),
            )
            text = "".join(chr(codepoints[i]) for i in range(length.value))
            if text not in "red":
                continue
            ok, fg = lib.get(
                lib.row_cells_get,
                cells,
                ghostty.CELLS_FG_COLOR,
                ghostty.ColorRgb(),
            )
            if ok:
                reds.append((text, fg.r, fg.g, fg.b))
    for text, red, green, blue in reds:
        assert red > 180 and red > green and red > blue, reds
    assert len(reds) == 3, reds
    print("emulator: cell color resolution ok")

    for handle, free in [
        (row_cells, lib.row_cells_free),
        (row_iterator, lib.row_iterator_free),
        (render_state, lib.render_state_free),
        (terminal, lib.terminal_free),
    ]:
        free(handle)


def test_digit_keys() -> None:
    """Digits live at Qt codes 48-57; they once landed at 0-9 and only
    worked through the text fallback, which loses Alt/Ctrl modifiers."""
    from terminal import _QT_KEYS

    for digit in "0123456789":
        code = 48 + int(digit)
        mapped = _QT_KEYS.get(code)
        assert mapped is not None, f"key {digit} unmapped"
        import ghostty as _ghostty

        assert mapped == _ghostty.KEY[f"DIGIT_{digit}"], (
            f"key {digit} maps to {mapped}"
        )
    assert all(_QT_KEYS.get(code) is None for code in range(0, 10))
    print("keys: digit codes mapped at 48-57")


def test_key_encoder() -> None:
    lib = ghostty.Lib()
    terminal = lib.new_handle(lib.terminal_new, 80, 24)
    encoder = lib.new_handle(lib.key_encoder_new)
    event = lib.new_handle(lib.key_event_new)

    def encode(key: str, action: int = ghostty.ACTION_PRESS) -> bytes:
        lib.key_encoder_setopt_from_terminal(encoder, terminal)
        lib.key_event_set_key(event, ghostty.KEY[key])
        lib.key_event_set_action(event, action)
        lib.key_event_set_mods(event, 0)
        lib.key_event_set_unshifted_codepoint(
            event, ord("a") if key == "A" else 0
        )
        lib.key_event_set_consumed_mods(event, 0)
        lib.key_event_set_utf8(event, None, 0)
        return lib.encode_key(encoder, event)

    assert encode("ENTER") == b"\r", encode("ENTER")
    assert encode("ARROW_UP") == b"\x1b[A", encode("ARROW_UP")
    lib.vt_write(terminal, b"\x1b[?1h")  # DECCKM: application cursors
    assert encode("ARROW_UP") == b"\x1bOA", encode("ARROW_UP")
    print("key encoder: legacy and application modes ok")

    lib.key_event_free(event)
    lib.key_encoder_free(encoder)
    lib.terminal_free(terminal)


def test_pty_session() -> None:
    from session import PtySession

    lib = ghostty.Lib()
    terminal = lib.new_handle(lib.terminal_new, 80, 24)
    render_state = lib.new_handle(lib.render_state_new)
    row_iterator = lib.new_handle(lib.row_iterator_new)
    row_cells = lib.new_handle(lib.row_cells_new)

    session = PtySession(["/bin/sh", "-c", "printf 'pty-marker-1234'"], 40, 10)
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline:
        data = session.drain()
        if data is None:
            break
        time.sleep(0.01)
        if data:
            lib.vt_write(terminal, data)
    lines = screen_text(lib, terminal, render_state, row_iterator, row_cells)
    joined = "\n".join(lines)
    assert "pty-marker-1234" in joined, joined[:200]
    print("pty session: child output reached the screen")

    for handle, free in [
        (row_cells, lib.row_cells_free),
        (row_iterator, lib.row_iterator_free),
        (render_state, lib.render_state_free),
        (terminal, lib.terminal_free),
    ]:
        free(handle)
    session.close()


def test_widget() -> None:
    from PySide6.QtCore import QPointF, QTimer
    from PySide6.QtGui import QClipboard
    from PySide6.QtWidgets import QApplication
    from terminal import TerminalWidget

    app = QApplication.instance() or QApplication(sys.argv)
    # Enough numbered lines to fill the viewport and push the marker
    # into scrollback, with real text on every line for multi-row
    # selection checks.
    widget = TerminalWidget(
        [
            "/bin/sh",
            "-c",
            "printf 'widget-ok'; printf '\\r\\n%.0s' $(seq 1 20); "
            "i=1; while [ $i -le 30 ]; do printf 'filler %02d\\r\\n' "
            "$i; i=$((i+1)); done",
        ]
    )
    widget.resize(400, 300)
    widget.show()

    failures: list[str] = []

    # Capture widget writes for the paste check; the selection checks
    # run in finish(), after the event loop has rendered content.
    sent: list[bytes] = []
    widget.write = sent.append

    def finish() -> None:
        try:
            widget.grab()
            # The marker sits in scrollback after its trailing
            # newlines; scroll to the top so it is on screen.
            widget.lib.terminal_scroll_viewport(
                widget.terminal,
                ghostty.ScrollViewport(
                    ghostty.SCROLL_TOP,
                    ghostty.ScrollViewportValue(),
                ),
            )
            widget.refresh()
            text = "\n".join(
                screen_text(
                    widget.lib,
                    widget.terminal,
                    widget.render_state,
                    widget.row_iterator,
                    widget.row_cells,
                )
            )
            if "widget-ok" not in text:
                failures.append("screen text missing widget-ok: " + text[:120])
            else:
                print("widget: offscreen render and screen text ok")
            # Selection through the gesture API, grid-anchored: select
            # row 0, scroll the viewport, and the selection must still
            # read the same text (viewport coords would have slid).
            cw, ch = widget.cell_width, widget.cell_height
            widget._sel_dragged = True
            widget._selection_gesture(
                ghostty.SEL_PRESS, QPointF(cw * 0.5, ch * 0.5)
            )
            widget._selection_gesture(
                ghostty.SEL_DRAG, QPointF(cw * 8.9, ch * 0.5)
            )
            widget._selection_gesture(
                ghostty.SEL_RELEASE, QPointF(cw * 8.9, ch * 0.5)
            )
            selected = widget._selection_text()
            if selected != "widget-ok":
                failures.append(f"selection text was {selected!r}")
            else:
                print("selection: grid-anchored text captured")
            down = ghostty.ScrollViewport(
                ghostty.SCROLL_DELTA,
                ghostty.ScrollViewportValue(delta=2),
            )
            widget.lib.terminal_scroll_viewport(widget.terminal, down)
            widget.refresh()
            after = widget._selection_text()
            if after != "widget-ok":
                failures.append(
                    f"selection lost its anchor after scroll: {after!r}"
                )
            else:
                print("selection: survives viewport scroll")
            widget._copy_selection()
            if QApplication.clipboard().text() != "widget-ok":
                failures.append("copy did not reach the clipboard")
            QApplication.clipboard().setText("echo pasted\nnext")
            widget._paste_clipboard()
            if b"echo pasted\rnext" not in sent:
                failures.append(f"paste sent {sent!r}")
            else:
                print("paste: newline became carriage return")
            # Autoscroll: from the bottom, a drag held past the top
            # edge scrolls and extends the selection across the
            # revealed rows.
            widget.lib.terminal_scroll_viewport(
                widget.terminal,
                ghostty.ScrollViewport(
                    ghostty.SCROLL_BOTTOM,
                    ghostty.ScrollViewportValue(),
                ),
            )
            widget.refresh()
            last_row = widget.rows - 2
            widget._sel_dragged = True
            widget._drag_position = QPointF(cw * 2.5, ch * (last_row + 0.5))
            widget._selection_gesture(ghostty.SEL_PRESS, widget._drag_position)
            widget._drag_position = QPointF(cw * 5.5, -ch * 0.9)
            widget._selection_gesture(ghostty.SEL_DRAG, QPointF(cw * 5.5, 0.5))
            widget._autoscroll_tick()
            widget._autoscroll_tick()
            widget._selection_gesture(
                ghostty.SEL_RELEASE, widget._drag_position
            )
            extended = widget._selection_text()
            if "\n" not in extended or "filler" not in extended:
                failures.append(
                    f"autoscroll did not extend selection: {extended!r}"
                )
            else:
                lines = extended.strip().splitlines()
                print(
                    f"selection: autoscroll extended across {len(lines)} rows"
                )
            # Middle-click: the finished drag published its text to
            # the PRIMARY selection, and pasting from PRIMARY delivers
            # it back to the pty.
            primary = QApplication.clipboard().text(QClipboard.Mode.Selection)
            if "filler" not in primary:
                failures.append(
                    f"primary selection not published: {primary!r}"
                )
            else:
                print("selection: published to PRIMARY")
            sent.clear()
            widget._paste_primary()
            if not any(b"filler" in chunk for chunk in sent):
                failures.append(f"middle-click paste sent {sent!r}")
            else:
                print("paste: middle-click pastes PRIMARY")
            # Word and line selection through the library's
            # selectors (double-click and triple-click).
            widget._press_count = 2
            widget._select_word_at(QPointF(cw * 3.5, ch * (widget.rows - 1.5)))
            # The release that follows a real double-click must keep
            # the word selected (it bypassed the drag gesture).
            widget._selection_gesture(
                ghostty.SEL_RELEASE,
                QPointF(cw * 3.5, ch * (widget.rows - 1.5)),
            )
            word = widget._selection_text()
            if "filler" not in word.split():
                failures.append(f"word selection was {word!r}")
            else:
                print(f"selection: double-click word ({word.strip()!r})")
            widget._press_count = 3
            widget._select_line_at(QPointF(cw * 3.5, ch * (widget.rows - 1.5)))
            widget._selection_gesture(
                ghostty.SEL_RELEASE,
                QPointF(cw * 3.5, ch * (widget.rows - 1.5)),
            )
            line = widget._selection_text()
            if "filler" not in line or not any(ch.isdigit() for ch in line):
                failures.append(f"line selection was {line!r}")
            else:
                print(f"selection: triple-click line ({line.strip()!r})")
            # Context menu enablement: Copy follows the selection,
            # Paste follows the clipboard.
            QApplication.clipboard().setText("menu paste text")
            actions = widget._build_context_menu().actions()
            copy_on = [
                a.isEnabled() for a in actions if a.text().startswith("Copy")
            ]
            paste_on = [
                a.isEnabled() for a in actions if a.text().startswith("Paste")
            ]
            if copy_on != [True] or paste_on != [True]:
                failures.append(f"menu with selection: {copy_on} {paste_on}")
            else:
                print("menu: copy and paste enabled")
            widget.lib.terminal_set(
                widget.terminal, ghostty.OPT_SELECTION, None
            )
            QApplication.clipboard().setText("")
            actions = widget._build_context_menu().actions()
            copy_off = [
                a.isEnabled() for a in actions if a.text().startswith("Copy")
            ]
            paste_off = [
                a.isEnabled() for a in actions if a.text().startswith("Paste")
            ]
            if copy_off != [False] or paste_off != [False]:
                failures.append(
                    f"menu without selection: {copy_off} {paste_off}"
                )
            else:
                print("menu: copy and paste dimmed")
            # Shift+PgUp/PgDn page the buffer locally; the scrollbar
            # offset proves the paging without a real key event source.
            from PySide6.QtCore import QEvent
            from PySide6.QtCore import Qt as _Qt
            from PySide6.QtGui import QKeyEvent

            def send_key(key):
                ev = QKeyEvent(
                    QEvent.Type.KeyPress,
                    key,
                    _Qt.KeyboardModifier.ShiftModifier,
                    "",
                )
                QApplication.sendEvent(widget, ev)

            def scrollbar_offset():
                bar = ghostty.Scrollbar()
                ok, _ = widget.lib.get(
                    widget.lib.terminal_get,
                    widget.terminal,
                    ghostty.DATA_SCROLLBAR,
                    bar,
                )
                return bar.offset if ok else -1

            widget.lib.terminal_scroll_viewport(
                widget.terminal,
                ghostty.ScrollViewport(
                    ghostty.SCROLL_TOP, ghostty.ScrollViewportValue()
                ),
            )
            widget.refresh()
            top_offset = scrollbar_offset()
            send_key(int(_Qt.Key.Key_PageDown))
            paged_offset = scrollbar_offset()
            send_key(int(_Qt.Key.Key_PageUp))
            back_offset = scrollbar_offset()
            if not (top_offset < paged_offset and back_offset <= paged_offset):
                failures.append(
                    f"paging did not move the viewport: "
                    f"{top_offset} -> {paged_offset} -> {back_offset}"
                )
            else:
                print("keys: shift-pgdn/pgup page the buffer")
            # OSC 52: a program inside sets the clipboard.
            import base64

            payload = base64.b64encode(b"osc52 clipboard text").decode()
            QApplication.clipboard().setText("")
            widget.lib.vt_write(
                widget.terminal,
                f"\x1b]52;c;{payload}\x07".encode(),
            )
            if QApplication.clipboard().text() != "osc52 clipboard text":
                failures.append(
                    "osc52 write did not reach the clipboard: "
                    + repr(QApplication.clipboard().text())
                )
            else:
                print("clipboard: osc52 write reached the clipboard")
            # Font zoom: cells grow with the size, and shrink back.
            pt0 = widget.font.pointSize()
            cw0 = widget.cell_width
            widget._zoom_font(1)
            if not (
                widget.font.pointSize() == pt0 + 1 and widget.cell_width > cw0
            ):
                failures.append(
                    f"zoom in: pt {widget.font.pointSize()} "
                    f"cell {widget.cell_width}"
                )
            else:
                print(f"zoom: cells {cw0} -> {widget.cell_width}")
            widget._zoom_font(-1)
            if widget.font.pointSize() != pt0 or (widget.cell_width != cw0):
                failures.append(
                    f"zoom out: pt {widget.font.pointSize()} "
                    f"cell {widget.cell_width}"
                )
            widget._zoom_font(1)
            widget._set_font_point_size(widget.DEFAULT_PT)
            if widget.font.pointSize() != widget.DEFAULT_PT:
                failures.append("zoom reset did not restore default")
            else:
                print("zoom: reset to default")
            # Hyperlinks: OSC 8 from the grid, plain-text URL spans
            # from the paint pass, and the menu's Copy Link Address.
            last = ch * (widget.rows - 0.5)
            widget.lib.terminal_scroll_viewport(
                widget.terminal,
                ghostty.ScrollViewport(
                    ghostty.SCROLL_BOTTOM, ghostty.ScrollViewportValue()
                ),
            )
            widget.refresh()
            widget.lib.vt_write(
                widget.terminal,
                b"\x1b]8;;https://example.com/osc8\x1b\\linked"
                b"\x1b]8;;\x1b\\ https://example.com/plain ",
            )
            widget.refresh()
            widget.grab()  # run a paint so the URL spans exist
            uri8 = widget._hyperlink_at(QPointF(cw * 1.5, last))
            if uri8 != "https://example.com/osc8":
                failures.append(f"osc8 lookup was {uri8!r}")
            else:
                print("links: osc8 uri from the grid")
            plain = widget._hyperlink_at(QPointF(cw * 15.5, last))
            if plain != "https://example.com/plain":
                failures.append(
                    f"plain url lookup was {plain!r}; "
                    f"spans={widget._link_spans}"
                )
            else:
                print("links: plain-text url from paint spans")
            actions = widget._build_context_menu(
                QPointF(cw * 1.5, last)
            ).actions()
            link_actions = [
                a for a in actions if a.text() == "Copy Link Address"
            ]
            if len(link_actions) != 1 or not link_actions[0].isEnabled():
                failures.append("copy-link menu item missing or dimmed")
            else:
                QApplication.clipboard().setText("")
                link_actions[0].trigger()
                if QApplication.clipboard().text() != (
                    "https://example.com/osc8"
                ):
                    failures.append("copy-link did not reach clipboard")
                else:
                    print("links: copy link address")
            # Find bar: search runs, stepping selects matches, the
            # counter reads "k of N", and viewport matches highlight.
            widget._find_open()
            widget._find_run("filler")
            total = ctypes.c_size_t()
            ok, _ = widget.lib.get(
                widget.lib.search_get,
                widget._search,
                ghostty.SEARCH_DATA_TOTAL_MATCHES,
                total,
            )
            if not ok or total.value != 30:
                failures.append(f"find total was {total.value}")
            else:
                print("find: 30 matches")
            widget._find_step(1)
            if widget._find_count.text() != "1 of 30":
                failures.append(
                    f"find counter was {widget._find_count.text()!r}"
                )
            else:
                print("find: counter 1 of 30")
            widget._find_step(1)
            if widget._find_count.text() != "2 of 30":
                failures.append(
                    f"find counter was {widget._find_count.text()!r}"
                )
            widget._find_step(-1)
            if widget._find_count.text() != "1 of 30":
                failures.append(
                    f"find counter was {widget._find_count.text()!r}"
                )
            rects = widget._viewport_match_rects()
            if not rects:
                failures.append("find produced no viewport rects")
            else:
                print(f"find: {len(rects)} highlight rects")
            widget._find_close()
            # Visual bell: BEL flashes the border and clears itself.
            widget.lib.vt_write(widget.terminal, b"\x07")
            if not widget._bell_flash:
                failures.append("BEL did not start the flash")
            else:
                deadline = time.monotonic() + 1.0
                while widget._bell_flash and time.monotonic() < deadline:
                    QApplication.processEvents()
                    time.sleep(0.02)
                if widget._bell_flash:
                    failures.append("flash never cleared")
                else:
                    print("bell: visual flash fired and cleared")
        except Exception as exc:  # noqa: BLE001 - report and stop
            failures.append(f"widget check raised: {exc}")
        finally:
            widget.close()
            app.quit()

    QTimer.singleShot(1000, finish)
    app.exec()
    assert widget.session.exited
    assert not failures, failures


def main() -> int:
    test_emulator()
    test_digit_keys()
    test_key_encoder()
    test_pty_session()
    test_widget()
    print("smoke: all checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
