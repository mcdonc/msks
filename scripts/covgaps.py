#!/usr/bin/env python3
"""Print every missing coverage line and branch arc in one pass.

The 100% gate's text table mixes the gaps into a full-suite report
where an arc like ``79->80`` is easy to read past, one round at a
time. This tool reads the combined data a gated ``unit-tests`` run
leaves at the repo root (``.coverage``) and prints the complete gap
list for the requested files — every missing line, condensed into
ranges, and every missing branch arc — from ``coverage json``, and
nothing else.

Usage:
    covgaps.py [FILE...]     # default: every measured source file

A requested file absent from the data prints as never measured: no
test imported it (or the path is a typo). Exit status is 0 when
clean, 1 when any gap is reported, 2 when the coverage data cannot
be read — run ``unit-tests`` first; ``scripts/preflight.sh`` runs it
for you.

Run from the repo root: the default data file and coverage's config
discovery are both cwd-relative. The gap list describes the suite
run that wrote the data — edits made while that run was in flight
drift the line numbers.
"""

import argparse
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Print all missing coverage lines and branch arcs."
    )
    parser.add_argument(
        "files",
        nargs="*",
        help="sources to report (default: every measured file)",
    )
    parser.add_argument(
        "--data-file",
        default=".coverage",
        help="combined coverage data to read (default: .coverage)",
    )
    return parser.parse_args(argv)


def load_report(data_file: str) -> dict:
    """The parsed ``coverage json`` document for the data file."""
    fd, path = tempfile.mkstemp(suffix=".json", prefix="covgaps-")
    os.close(fd)
    try:
        subprocess.run(
            [
                sys.executable,
                "-m",
                "coverage",
                "json",
                # --fail-under=0: the report config pins 100, which makes
                # `coverage json` exit nonzero on any gap even though it
                # wrote the report — the gap list below owns that verdict.
                "--fail-under=0",
                "-q",
                f"--data-file={data_file}",
                "-o",
                path,
            ],
            check=True,
        )
        return json.loads(Path(path).read_text(encoding="utf-8"))
    finally:
        Path(path).unlink(missing_ok=True)


def flush_range(parts: list[str], start: int | None, end: int | None) -> None:
    """Append one ``198`` or ``200-205`` piece of a line range."""
    if start is None:
        return
    parts.append(str(start) if start == end else f"{start}-{end}")


def condensed(numbers: list[int]) -> str:
    """``198, 200-205`` — consecutive line numbers folded into ranges."""
    parts: list[str] = []
    start: int | None = None
    prev: int | None = None
    for n in sorted(numbers):
        if prev == n - 1:
            prev = n
        else:
            flush_range(parts, start, prev)
            start = prev = n
    flush_range(parts, start, prev)
    return ", ".join(parts)


def format_arc(arc: list[int]) -> str:
    """``79->80``; coverage marks the function exit as line 0."""
    source, target = arc
    destination = str(target) if target > 0 else "exit"
    return f"{source}->{destination}"


def key_matches(key: str, arg: str) -> bool:
    return key == arg or key.endswith("/" + arg)


def first_match(files: dict, arg: str) -> str:
    """The report key for one requested path (the arg itself when no
    measured key matches, so print_report names it as never measured)."""
    for key in sorted(files):
        if key_matches(key, arg):
            return key
    return arg


def select_keys(report: dict, wanted: list[str]) -> list[str]:
    """Map requested paths onto the report's file keys."""
    if not wanted:
        return sorted(report["files"])
    keys: list[str] = []
    for arg in wanted:
        keys.append(first_match(report["files"], arg))
    return keys


def file_gaps(entry: dict) -> list[str]:
    """The human-readable gap lines for one measured file."""
    details = []
    lines = condensed(entry["missing_lines"])
    if lines:
        details.append(f"missing lines: {lines}")
    arcs = ", ".join(format_arc(a) for a in entry["missing_branches"])
    if arcs:
        details.append(f"missing arcs : {arcs}")
    return details


def print_report(report: dict, keys: list[str]) -> int:
    """One block per file; returns the number of files with gaps."""
    total = 0
    for key in keys:
        entry = report["files"].get(key)
        if entry is None:
            print(
                f"{key}: never measured — no test imported it (or a path typo)"
            )
            total += 1
            continue
        details = file_gaps(entry)
        if not details:
            continue
        print(f"{key}:")
        for line in details:
            print(f"  {line}")
        total += 1
    return total


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv if argv is not None else sys.argv[1:])
    try:
        report = load_report(args.data_file)
    except subprocess.CalledProcessError as exc:
        print(
            f"covgaps: coverage data unreadable ({exc}) — "
            "run unit-tests first (scripts/preflight.sh does)",
            file=sys.stderr,
        )
        return 2
    keys = select_keys(report, args.files)
    gaps = print_report(report, keys)
    if gaps == 0:
        print("covgaps: no missing lines or branch arcs")
    return 1 if gaps else 0


if __name__ == "__main__":
    sys.exit(main())
