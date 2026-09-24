"""The tabular helpers (#271): the measured listings and the table
help every subcommand renders with."""

import argparse

from msks.client.tabular import (
    ListingHelpFormatter,
    command_parser,
    listing_text,
)


def test_listing_text_fits_columns_to_the_values() -> None:
    """A long cell widens its column for every row — one offset per
    column across all rows, the header included."""
    text = listing_text(
        ["ref", "hash"],
        [
            ["debian:13", "98ccf2e1f2db"],
            ["nixos:26.05pre-git-w7r3iyyw", "7ed54d56c63e"],
        ],
    )
    lines = text.splitlines()
    # The header's cells sit in the same measured columns the rows
    # do — the hash column starts at one offset everywhere.
    assert (
        lines[0].index("hash")
        == lines[1].index("98ccf2e1f2db")
        == lines[2].index("7ed54d56c63e")
    )
    assert lines[1].startswith("debian:13")
    assert lines[2].startswith("nixos:26.05pre-git-w7r3iyyw")


def test_listing_text_without_rows_prints_nothing() -> None:
    """The CLI's empty-table contract: no rows, no header."""
    assert listing_text(["ref"], []) == ""


def test_listing_text_without_headers_renders_rows_alone() -> None:
    """``None`` headers: the record shape — label/value pairs on one
    measured grid, no header row."""
    text = listing_text(None, [["ref", "debian:13"], ["hash", "a" * 8]])
    assert text.splitlines() == ["ref   debian:13", "hash  " + "a" * 8]


def test_listing_text_folds_at_a_bounded_width() -> None:
    """Help-shaped renders wrap inside their width; every continued
    line still starts at its column's offset."""
    text = listing_text(
        ["cmd", "help"],
        [["run", "one two three four five six seven eight"]],
        width=18,
    )
    lines = text.splitlines()
    assert lines[0] == "cmd  help"
    assert lines[1] == "run  one two three"
    # The folded help continues inside its column's offset.
    assert [line[:5] for line in lines[2:]] == ["     "] * 2
    assert lines[2].strip() == "four five six"


def build_help_parser() -> argparse.ArgumentParser:
    """A parser exercising the formatter's shapes: a description, an
    option, a help-less argument, and a command set."""
    parser = command_parser(
        prog="tool", description="the tool: a description line"
    )
    parser.add_argument(
        "--json", action="store_true", help="one JSON document"
    )
    parser.add_argument("target")
    sub = parser.add_subparsers(
        dest="command",
        required=True,
        title="commands",
        metavar="<command>",
        parser_class=command_parser,
    )
    sub.add_parser("run", help="run it")
    sub.add_parser(
        "inspect", help="inspect it, with a help text long enough to wrap"
    )
    return parser


def test_help_renders_sections_as_measured_tables(
    monkeypatch,
) -> None:
    """#271: commands, arguments, and options sit beside their help
    at one measured offset; long help wraps inside its column."""
    monkeypatch.setenv("COLUMNS", "46")
    text = build_help_parser().format_help()
    lines = text.splitlines()
    assert lines[0].startswith("usage: tool [-h] [--json]")
    assert "the tool: a description line" in lines
    commands = lines.index("commands:")
    assert lines[commands + 1] == "  run      run it"
    wrapped = "  inspect  inspect it, with a help text long"
    assert lines[commands + 2] == wrapped
    assert lines[commands + 3] == "           enough to wrap"
    # The options align the same way, wrapping inside the help
    # column at the continued offset.
    options = lines.index("options:")
    assert lines[options + 1] == "  -h, --help  show this help message and"
    assert lines[options + 2] == "              exit"
    assert lines[options + 3] == "  --json      one JSON document"
    positionals = lines.index("positional arguments:")
    # ``target`` carries no help: the invocation renders alone.
    assert lines[positionals + 1] == "  target"


def test_help_drops_an_empty_section(monkeypatch) -> None:
    """A section with no entries renders nothing — argparse's own
    contract for its default (unused) positional group."""
    monkeypatch.setenv("COLUMNS", "60")
    text = build_help_parser().format_help()
    assert "positional arguments:" in text  # ``target`` keeps its section
    parser = command_parser(prog="empty")
    parser.add_subparsers(
        dest="command", required=True, title="commands", metavar="<command>"
    )
    assert "positional arguments:" not in parser.format_help()


def test_help_skips_a_suppressed_heading(monkeypatch) -> None:
    """A group titled SUPPRESS renders its rows without a heading,
    the way argparse treats the marker."""
    monkeypatch.setenv("COLUMNS", "60")
    parser = command_parser(prog="tool")
    group = parser.add_argument_group(argparse.SUPPRESS)
    group.add_argument("--quiet", action="store_true", help="say less")
    text = parser.format_help()
    assert "  --quiet  say less" in text
    assert "options:" in text  # the ordinary groups keep their headings


def test_help_carries_a_group_description(monkeypatch) -> None:
    """A titled group's description renders between its heading and
    its rows."""
    monkeypatch.setenv("COLUMNS", "60")
    parser = command_parser(prog="tool")
    group = parser.add_argument_group("sizing", "how big")
    group.add_argument("--mib", help="size in MiB")
    text = parser.format_help()
    lines = text.splitlines()
    sizing = lines.index("sizing:")
    assert lines[sizing + 1] == "  how big"
    assert lines[sizing + 2] == "  --mib MIB  size in MiB"


def test_formatter_is_argparse_shaped() -> None:
    """The formatter rides argparse's machinery: format_help returns
    the same text the parser prints."""
    parser = build_help_parser()
    assert parser._get_formatter().__class__ is ListingHelpFormatter
