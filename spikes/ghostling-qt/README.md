# ghostling-qt

A minimal terminal emulator built on
[libghostty-vt](https://lib.ghostty.org/) and PySide6 — the Qt
counterpart of Ghostty's [Ghostling](https://github.com/ghostty-org/ghostling)
demo, and the phase 4.5 spike for the msks GUI client (#924).

It is a standalone plain terminal. It imports nothing from msks, it
lives outside `src/msks/` so the repo gates (import graph, lines,
coverage) do not apply, and it answers one question: how much work is
the Qt side when libghostty-vt provides the emulation? Ghostling needs
about 1,500 lines of C over Raylib; this spike is roughly 1,100 lines
of Python over Qt.

## Layout

- `ghostty.py` — the ctypes binding: enum constants, structs, typed
  prototypes, and small helpers over the opaque-handle API.
- `session.py` — a child process on a PTY (`pty.fork`, non-blocking
  master fd, `TIOCSWINSZ` on resize). Pure stdlib, no Qt.
- `terminal.py` — the widget: cell grid painted with `QPainter` from
  the render state, key/mouse encoding through libghostty's encoders,
  focus reporting, resize with reflow, scrollbar, exit banner.
- `main.py` — a window around the widget.
- `smoke.py` — offscreen checks: emulator parsing, per-cell colors,
  key encoding (legacy and application cursor modes), a real PTY
  session, and the widget under `QT_QPA_PLATFORM=offscreen`.
- `vendor/get-lib.sh` — fetches `libghostty-vt.so` by extracting it
  from the `pyghostty` wheel (the library build, not its Python
  API), and the JetBrains Mono faces (OFL 1.1, the face Ghostty
  itself ships). The font is vendored — not taken from the host —
  so metrics are deterministic everywhere and no system font
  dependency exists (the earlier host reference, Ubuntu Mono, ships
  under the Ubuntu Font Licence and is installed by default
  nowhere).
- `run.sh` — NixOS helper: builds a venv, fetches the library, and
  points `LD_LIBRARY_PATH` at the nixpkgs libs the PySide6 wheel
  expects (`libstdc++`, glib, X11, fontconfig, ...). The resolved
  paths are cached in `.libdirs`.

## Running

On NixOS:

```sh
./run.sh            # interactive $SHELL in a window
./run.sh vim        # run a command instead
./run.sh smoke      # offscreen smoke test
```

Elsewhere (Linux/macOS):

```sh
python3 -m venv .venv && .venv/bin/python -m pip install PySide6
sh vendor/get-lib.sh
.venv/bin/python smoke.py    # offscreen checks
.venv/bin/python main.py     # the terminal
```

The library is found through `GHOSTTY_VT_LIB` if set, then
`vendor/libghostty-vt.so`, then the system loader.

## What works

- Text with 8/16/256/truecolor, resolved per cell through the render
  state; bold, italic, faint, and inverse styles.
- Cell metrics follow Konsole's `TerminalDisplay`: the cell width is
  the average advance over a representative ASCII run, the height is
  ascent + descent, and glyphs draw baseline-anchored at the cell's
  left edge (never centered in the cell rect — centering makes
  narrow glyphs drift and pads the lines). The font family resolves
  explicitly, sized in pixels, so the measured grid and the rendered
  font agree — measuring one font and rendering another is exactly
  the spacing bug klangk's frontend fixed in its flterm terminal. The
  preference list starts with the reference Konsole profile's font
  (UbuntuMono Nerd Font Mono) so the density comparison on this host
  is pixel-for-pixel.
- The cursor, scrollback with a scrollbar, and wheel scrolling (or
  wheel forwarding to the application when it enables mouse
  reporting).
- Resize with reflow, and `TIOCSWINSZ` so the child sees the new size.
- Keyboard input through libghostty's key encoder: legacy sequences,
  application cursor keys, and the Kitty keyboard protocol when a
  program enables it. Release and repeat events are forwarded.
- Mouse reporting (X10/normal/button/any-event tracking, SGR and
  friends) through the mouse encoder; when an application enables
  mouse tracking, text selection stands aside.
- Text selection with the mouse through libghostty's selection
  gesture API: grid-anchored, so the selection survives viewport
  scrolling and reflow; drag from a left press (a click without a
  drag clears it), highlighted by inverting the selected cells.
  `Ctrl+Shift+C` (and `Ctrl+Shift+X` — a terminal cannot cut, so cut
  copies) puts the selection on the clipboard; `Ctrl+Shift+V`
  pastes, wrapping in bracketed paste when the program enabled mode
  2004 and converting newlines to carriage returns otherwise.
- Focus reporting (DECSET 1004) and OSC 0/2 title changes.
- Effects that answer VT queries programs send at startup (DA1/DA2/
  DA3, size reports), so vim and tmux do not hang.

## Known gaps (deliberate, spike scope)

- Selection needs autoscroll-during-drag (dragging past the top or
  bottom edge scrolls and extends — the wheel works mid-drag
  instead), double-click word selection, middle-click paste, and
  clipboard OSC 52. No Kitty graphics, no scrollbar dragging, no
  numpad-specific keys, no ligatures.
- The XTVERSION effect is not registered: ctypes cannot return a
  16-byte struct from a callback, so `CSI > q` goes unanswered.
- `vendor/` holds a library snapshot (currently a pre-1.0
  `0.1.0-dev` build bundled by pyghostty 0.1.4); re-fetch it when a
  1.x wheel is out and pin against the stable ABI.
- The vendored library's `.so` and the `.venv/` are not committed;
  `vendor/get-lib.sh` and `run.sh` recreate them.

## Findings so far (for the phase 4.5 decision)

- The render state API maps cleanly onto a `QPainter` cell grid; the
  row/cell iteration is the whole rendering loop, and colors resolve
  to RGB per cell with no palette bookkeeping on our side.
- The key encoder removes the worst part of writing a terminal: the
  escape-sequence tables, application modes, and the Kitty protocol
  all come from the library.
- Effects (the callbacks a terminal must answer) are a day of careful
  ctypes work; the write-pty, size-report, and device-attributes
  effects were enough for the smoke tests.
- Two ctypes traps to remember for the production binding: iterator
  handles are in/out parameters (pre-fill the handle slot), and
  struct-returning callbacks are not expressible. A third, nastier
  one: mirror every struct field exactly, including padding
  members. The scroll-viewport union without its `_padding`
  array was 8 bytes instead of 16, so the enclosing struct shrank
  below the memory-class threshold: ctypes passed it in registers,
  the library read garbage off the stack, and Zig's panic trap
  surfaced as SIGILL with no diagnostic. `objdump` on the exported
  symbol (reading the argument off `%rbp+0x10`) is what pinned it.

### Font rendering lessons (a full debugging session, condensed)

- **A Qt pitfall that produced every visual symptom we chased:**
  `painter.setFont()` silently accepts a plain string as a font
  family. Passing the variants-dict _key_ (`"normal"`) instead of the
  font object rendered all text in default Sans Serif at the
  application size — serifless, undersized glyphs inside cells
  measured for the real face. Diagnose font identity, not just
  metrics: `QRawFont.fromFont(...).familyName()` names the face
  actually rasterized, and dumping the widget's own render
  (`widget.grab()`) plus an isolated same-process probe localizes
  any mismatch to the app, the paint pipeline, or the environment.
- **A KDE session can silently drop a wheel-shipped Qt app onto
  XWayland:** the session exports `QT_PLUGIN_PATH` with system Qt
  paths that shadow PySide6's bundled plugins, so the wayland
  platform never loads and the app falls back to xcb, where kwin
  rescales the window. `run.sh` now unsets the variable and sets
  `QT_QPA_PLATFORM=wayland`; the metrics line prints
  `app.platformName()` so the platform is always visible.
- Cell metrics follow Konsole's `TerminalDisplay` (average advance
  over a representative ASCII run, ascent+descent height,
  baseline-anchored left-aligned drawing), sized in points against
  the reference profile. Measuring one font and rendering another
  is the spacing bug klangk's frontend fixed in its flterm terminal
  (mcdonc/klangk#2); here the same lesson applies at the `setFont`
  call.
- On this host the screen reports dpr 2.00 while the window surface
  renders at 1.50 (kwin fractional scaling); `devicePixelRatioF()`
  on the widget is the number that matches what lands on screen.
  The metrics line and `self-dump` print both, which is how the
  discrepancy was found.
