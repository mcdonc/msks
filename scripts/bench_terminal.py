#!/usr/bin/env python3
"""Measure the GUI terminal's write throughput for large ANSI
blocks (#1193).

The motivating case is pi's fullscreen exit: the program writes
the whole session transcript as one synchronized-output block,
and the GUI shell paints it slowly enough to watch. This tool
measures the client's own pipeline stage by stage, offscreen
and deterministically, so a fix's effect reads as a number:

- pty-read     the ``PtySession`` drain loop over a ``cat``
               child that writes the corpus to the master
- emulator     ``vt_write`` alone, into a bare terminal — the
               parse share, no widget
- gui-parse    ``vt_write`` inside the widget loop (same call,
               through the widget's own handle)
- gui-plan     ``render_state_update`` plus ``plan_paint`` per
               presented chunk — the damage walk
- gui-paint    ``paint_event`` onto a kept image — the cell
               walk and the Qt drawing, painted the way the
               widget's backing store paints it

The gui stages model the read path's synchronized-output hold
(#1193): a chunk that leaves a mode-2026 block open presents
nothing, and the chunk that closes the block paints it whole —
the default corpus is one block, the shape pi's exit replay
produces. Pass ``--no-sync`` for the unwrapped flood, where
every chunk paints.

The ssh share (the child the GUI really spawns) is not measured
here: it needs a live workspace. Run the same corpus inside one
(``--emit-script`` prints a shell snippet that cats the corpus
file) to time that share by the wall clock.

The corpus is deterministic for a given ``--size``/``--cols``:
a transcript-shaped replay — one synchronized-output block of
``\\r\\x1b[2K``-prefixed lines with mixed SGR colors, the shape
pi's exit write produces. Chunks model the pty's readiness
window (``--chunk``, default 64 KiB).

Usage (from the repo root, inside the devenv shell):

    python scripts/bench_terminal.py [--size 2] [--chunk 64]
        [--cols 80] [--rows 24] [--seed 7] [--profile]
        [--emit-script] [--corpus-file PATH]

Exits 0 when every stage ran, 1 when a stage failed.
"""

from __future__ import annotations

import argparse
import cProfile
import os
import pstats
import random
import select
import sys
import tempfile
import time
from pathlib import Path

# The scrollback the widget configures (screen.py).
SCROLLBACK_LINES = 10_000

# The sync-output wrappers the corpus carries, and the SGR
# vocabulary its lines use.
SYNC_BEGIN = b"\x1b[?2026h"
SYNC_END = b"\x1b[?2026l"
CLEAR_LINE = b"\r\x1b[2K"
RESET = b"\x1b[0m"

WORDS = (
    "workspace mount proxy egress daemon socket forward consent "
    "session terminal scrollback viewport render palette cluster "
    "grapheme column row grid resize signal window layout focus "
    "select paste clipboard bracket paste image kitty sixel link"
).split()


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Measure the GUI terminal's write throughput."
    )
    parser.add_argument(
        "--size",
        type=float,
        default=2.0,
        help="corpus size in MiB (default 2)",
    )
    parser.add_argument(
        "--chunk",
        type=int,
        default=64,
        help="bytes per pipeline chunk in KiB (default 64)",
    )
    parser.add_argument(
        "--cols", type=int, default=80, help="grid columns (default 80)"
    )
    parser.add_argument(
        "--rows", type=int, default=24, help="grid rows (default 24)"
    )
    parser.add_argument(
        "--seed", type=int, default=7, help="corpus seed (default 7)"
    )
    parser.add_argument(
        "--profile",
        action="store_true",
        help="profile the gui loop and print the top functions",
    )
    parser.add_argument(
        "--emit-script",
        action="store_true",
        help="print a shell snippet that replays the corpus inside "
        "a workspace, for timing the ssh share by wall clock",
    )
    parser.add_argument(
        "--no-sync",
        action="store_true",
        help="build the corpus without the synchronized-output "
        "wrappers, for the per-chunk painting path",
    )
    parser.add_argument(
        "--corpus-file",
        type=Path,
        default=None,
        help="write the corpus to this path instead of a temp file",
    )
    return parser.parse_args(argv)


def line_sgr(rng) -> bytes:
    """One line's SGR prefix, from a seeded shape pick: plain
    on most lines, a 256-color foreground on many, a truecolor
    pair with its own background on a tenth, bold on the
    rest."""
    shape = rng.random()
    if shape < 0.55:
        return b""
    if shape < 0.85:
        return b"\x1b[38;5;%dm" % rng.randint(0, 255)
    if shape < 0.95:
        fg = (rng.randint(80, 250), rng.randint(80, 250), 0)
        bg = (rng.randint(0, 40), rng.randint(0, 40), 60)
        return b"\x1b[38;2;%d;%d;%dm\x1b[48;2;%d;%d;%dm" % (*fg, *bg)
    return b"\x1b[1m"


def line_text(rng, cols: int) -> bytes:
    """One line's text: joined vocabulary words, occasionally
    long enough to wrap the grid."""
    words = [rng.choice(WORDS) for _ in range(rng.randint(4, cols // 6))]
    text = " ".join(words).encode()
    if rng.random() < 0.1:  # the occasional wrapped line
        joined = "-".join(words).encode()
        text = text + b" " + joined
    return text


def build_corpus(
    size_bytes: int, cols: int, seed: int, sync: bool = True
) -> bytes:
    """The transcript-shaped replay block: one synchronized-
    output wrapper over ``\\r\\x1b[2K``-prefixed lines, each
    line's text drawn from a seeded pick of plain, colored,
    back-filled, and bold shapes — the mix pi's exit write
    produces, at a byte budget."""
    rng = random.Random(seed)
    parts: list[bytes] = [SYNC_BEGIN] if sync else []
    budget = size_bytes - len(SYNC_BEGIN) - len(SYNC_END)
    line_no = 0
    while budget > 0:
        line = CLEAR_LINE + line_sgr(rng) + line_text(rng, cols) + RESET
        parts.append(line)
        budget -= len(line) + 2
        line_no += 1
        if line_no > 1:
            parts.append(b"\r\n")
    if sync:
        parts.append(SYNC_END)
    return b"".join(parts)


def chunked(data: bytes, chunk: int) -> list[bytes]:
    """The pipeline's chunks: the readiness window's worth of
    bytes at a time."""
    return [data[i : i + chunk] for i in range(0, len(data), chunk)]


def bench_pty(corpus: bytes, path: Path) -> dict:
    """The drain loop's share: a ``cat`` child writes the corpus
    to the pty, the session reads it off the master the way the
    widget's notifier path does."""
    from msks.client.gui.termsession import PtySession

    session = PtySession(["cat", str(path)])
    total = 0
    drains = 0
    started = time.perf_counter()
    try:
        while not session.exited:
            ready, _, _ = select.select([session.master_fd], [], [], 5.0)
            if not ready:
                break
            data = session.drain()
            if data is None:
                break
            total += len(data)
            drains += 1
    finally:
        session.close()
    return {
        "name": "pty-read",
        "bytes": total,
        "seconds": time.perf_counter() - started,
        "notes": f"{drains} drains",
    }


def bench_emulator(corpus: bytes, chunks: list[bytes], grid) -> dict:
    """The parse share alone: ``vt_write`` into a bare terminal
    with the widget's own grid and scrollback, no widget."""
    from msks.client.gui import ghostty as gt

    lib = gt.Lib()
    terminal = lib.new_handle(lib.terminal_new, grid[0], grid[1])
    import ctypes

    scrollback = ctypes.c_size_t(SCROLLBACK_LINES)
    lib.terminal_set(
        terminal, gt.OPT_SCROLLBACK_MAX_LINES, ctypes.byref(scrollback)
    )
    started = time.perf_counter()
    for chunk in chunks:
        lib.vt_write(terminal, chunk)
    return {
        "name": "emulator",
        "bytes": len(corpus),
        "seconds": time.perf_counter() - started,
        "notes": f"{len(chunks)} vt_write calls",
    }


def bench_widget(args):
    """The offscreen widget the gui stages feed: the
    application, the bundled faces, and a sleeper child that
    writes once and never again — no event loop runs, so the
    feeder owns every byte the emulator takes."""
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from msks.client.gui.terminal import TerminalWidget, paint
    from msks.client.gui.terminal import fonts as fonts_mod
    from PySide6 import QtCore, QtWidgets

    if QtWidgets.QApplication.instance() is None:
        QtWidgets.QApplication(["msks-bench"])
    fonts_mod.register_faces()  # after the application exists
    sleeper = "import time\nprint('ready', flush=True)\ntime.sleep(600)\n"
    widget = TerminalWidget([sys.executable, "-c", sleeper])
    widget.resize_grid(args.cols, args.rows)
    width, height = widget.metrics.size(widget.cols, widget.rows)
    widget.resize(QtCore.QSize(width + paint.LANE_W, height))
    return widget


def feed_pipeline(widget, painter, chunks, profile):
    """The widget loop over the chunks, timed stage by stage:
    the parse, and the plan and paint a chunk that leaves no
    synchronized-output block open owes. A chunk that leaves
    one open holds presentation, exactly as ``read_master``
    does (#1193). The profile, when one was asked for, wraps
    the loop. Answers the parse, plan, and paint seconds, the
    paint count, the painted-row count, and the held-chunk
    count."""
    from msks.client.gui import ghostty as gt
    from msks.client.gui.terminal import input, paint

    parse_s = plan_s = paint_s = 0.0
    paints = painted_rows = holds = 0
    if profile is not None:
        profile.enable()
    for chunk in chunks:
        t0 = time.perf_counter()
        widget.lib.vt_write(widget.terminal, chunk)
        t1 = time.perf_counter()
        parse_s += t1 - t0
        if input.mode_enabled(widget, gt.MODE_SYNCHRONIZED_OUTPUT):
            holds += 1
            continue
        widget.refresh()
        t2 = time.perf_counter()
        plan_s += t2 - t1
        paint.paint_event(widget, painter, widget.repaint_region)
        t3 = time.perf_counter()
        paint_s += t3 - t2
        paints += 1
        painted_rows += len(widget.painted_rows)
    if profile is not None:
        profile.disable()
    return parse_s, plan_s, paint_s, paints, painted_rows, holds


def pipeline_stages(
    corpus: bytes,
    parse_s: float,
    plan_s: float,
    paint_s: float,
    paints: int,
    painted_rows: int,
    holds: int,
) -> list[dict]:
    """The gui stages' report rows."""
    stage = lambda name, seconds, notes: {  # noqa: E731
        "name": name,
        "bytes": len(corpus),
        "seconds": seconds,
        "notes": notes,
    }
    return [
        stage(
            "gui-parse",
            parse_s,
            f"{paints + holds} vt_write calls in the widget loop",
        ),
        stage("gui-plan", plan_s, f"{paints} render_state_update + plans"),
        stage(
            "gui-paint",
            paint_s,
            f"{paints} paints, {painted_rows / max(1, paints):.0f} rows/paint",
        ),
        stage(
            "gui-total",
            parse_s + plan_s + paint_s,
            f"{holds} held chunks, pty and ssh not included",
        ),
    ]


def bench_gui(corpus: bytes, chunks: list[bytes], args) -> list[dict]:
    """The widget pipeline, one presentation per unheld chunk:
    the parse, the plan, and the paint timed apart, painted
    onto one kept image the way the widget's backing store
    paints it."""
    widget = bench_widget(args)
    profile = cProfile.Profile() if args.profile else None
    painter = None
    try:
        from PySide6.QtGui import QImage, QPainter

        image = QImage(
            widget.width(), widget.height(), QImage.Format.Format_ARGB32
        )
        painter = QPainter(image)
        timings = feed_pipeline(widget, painter, chunks, profile)
    finally:
        if painter is not None:
            painter.end()
        from PySide6.QtGui import QCloseEvent

        widget.closeEvent(QCloseEvent())
    if profile is not None:
        _print_profile(profile)
    return pipeline_stages(corpus, *timings)


def _print_profile(profile) -> None:
    """The gui loop's top functions by cumulative time."""
    stats = pstats.Stats(profile)
    stats.sort_stats("cumulative")
    print("\n--- profile: the gui loop, top 18 by cumulative time ---")
    stats.print_stats(18)


def mib(stats: dict) -> float:
    """A stage's throughput in MiB/s."""
    return stats["bytes"] / (1024 * 1024) / max(stats["seconds"], 1e-9)


def report(stats: dict) -> None:
    """One stage's line in the report."""
    print(
        f"{stats['name']:<12} {stats['seconds']:8.3f} s"
        f" {mib(stats):8.1f} MiB/s   {stats['notes']}"
    )


def main(argv: list[str]) -> int:
    args = parse_args(argv)
    size = int(args.size * 1024 * 1024)
    corpus = build_corpus(size, args.cols, args.seed, not args.no_sync)
    chunks = chunked(corpus, args.chunk * 1024)
    lines = corpus.count(b"\r\n")
    path = args.corpus_file or Path(
        tempfile.NamedTemporaryFile(
            prefix="msks-bench-corpus-", suffix=".ansi", delete=False
        ).name
    )
    path.write_bytes(corpus)
    try:
        print(
            f"corpus: {len(corpus) / (1024 * 1024):.1f} MiB"
            f" ({lines} lines), grid {args.cols}x{args.rows},"
            f" chunk {args.chunk} KiB ({len(chunks)} chunks),"
            f" seed {args.seed}"
        )
        print(f"corpus file: {path}")
        if args.emit_script:
            print(
                "\nreplay inside a workspace (time the wall clock):\n"
                f"  time cat {path}\n"
                "Run it in a GUI shell tab; compare against a local\n"
                "terminal to isolate the ssh share."
            )
        print()
        rows = []
        rows.append(bench_pty(corpus, path))
        rows.append(bench_emulator(corpus, chunks, (args.cols, args.rows)))
        rows.extend(bench_gui(corpus, chunks, args))
        for row in rows:
            report(row)
        return 0
    finally:
        if args.corpus_file is None:
            path.unlink(missing_ok=True)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
