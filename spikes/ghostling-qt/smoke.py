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
    from PySide6.QtWidgets import QApplication
    from terminal import TerminalWidget

    app = QApplication.instance() or QApplication(sys.argv)
    # Enough trailing newlines to push the marker into scrollback.
    widget = TerminalWidget(
        [
            "/bin/sh",
            "-c",
            "printf 'widget-ok'; printf '\\r\\n%.0s' $(seq 1 20)",
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
    test_key_encoder()
    test_pty_session()
    test_widget()
    print("smoke: all checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
