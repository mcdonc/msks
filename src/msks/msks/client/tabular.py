"""The client's aligned output (#271): listings and command help.

Rich measures every cell — the header row included — and fits each
column's width to the values present, so a long value widens its
column for every row instead of shifting one row's later columns
off the grid. The width arithmetic is rich's, measured per render;
the client's own code carries none of it.
"""

import argparse
import io

from rich.console import Console
from rich.table import Table
from rich.text import Text

# Listing rows never fold: the render width sits far past any value
# a listing carries. Help wraps instead (see ListingHelpFormatter).
UNFOLDED = 4096


def verbatim_rows(table: Table, rows: list[list[str]]) -> None:
    """Add the rows with each cell as literal text: a value the
    guest controls (an egress destination, an image's fields) is
    data, never rich markup to parse — the consent TUI escapes the
    same fields for the same reason."""
    for row in rows:
        table.add_row(*(Text(cell) for cell in row))


def listing_table(headers: list[str] | None, rows: list[list[str]]) -> Table:
    """The rows as one measured table — the header row included when
    ``headers`` names the columns, absent when it is ``None`` (a
    record's label/value pairs)."""
    table = Table(
        box=None,
        header_style="",
        pad_edge=False,
        show_header=headers is not None,
    )
    for header in headers or [""] * (len(rows[0]) if rows else 0):
        table.add_column(header, overflow="fold")
    verbatim_rows(table, rows)
    return table


def rendered(table: Table, width: int) -> str:
    """The table as text at a width, each line right-trimmed."""
    console = Console(file=io.StringIO(), width=width)
    console.print(table)
    lines = console.file.getvalue().splitlines()
    return "\n".join(line.rstrip() for line in lines)


def listing_text(
    headers: list[str] | None,
    rows: list[list[str]],
    width: int = UNFOLDED,
) -> str:
    """The rows as one aligned block.

    A rowless listing renders nothing, the CLI's contract for its
    empty tables. ``width`` bounds the render; the default keeps
    every listing row on one line.
    """
    if not rows:
        return ""
    return rendered(listing_table(headers, rows), width)


def action_rows(formatter: argparse.HelpFormatter, action) -> list[list[str]]:
    """One help entry per row: the invocation beside its help.

    A subparsers action's entries are its choices — the command
    list argparse names under the metavar.
    """
    entries = list(formatter._iter_indented_subactions(action)) or [action]
    rows = []
    for entry in entries:
        invocation = formatter._decolor(
            formatter._format_action_invocation(entry)
        )
        help_text = formatter._expand_help(entry) if entry.help else ""
        rows.append([f"  {invocation}", help_text])
    return rows


def help_table(rows: list[list[str]]) -> Table:
    """The two-column help table: the invocation column holds its
    width, the help column folds at its edge (a long path or URL
    keeps every character, argparse's own break-long-words
    posture)."""
    table = Table(box=None, pad_edge=False, show_header=False)
    table.add_column(no_wrap=True)
    table.add_column(ratio=1, overflow="fold")
    verbatim_rows(table, rows)
    return table


def section_parts(formatter: argparse.HelpFormatter, section):
    """A section's entry rows and its description lines."""
    rows: list[list[str]] = []
    description: list[str] = []
    for func, args in section.items:
        if func == formatter._format_action:
            rows.extend(action_rows(formatter, args[0]))
        else:
            # A group description, at the section's indent.
            description.extend(
                f"  {line}" for line in func(*args).strip("\n").splitlines()
            )
    return rows, description


def section_table(formatter: argparse.HelpFormatter, section) -> str:
    """One help section: heading, description, then the entries as
    one measured two-column table."""
    rows, description = section_parts(formatter, section)
    if not rows and not description:
        return ""
    suppressed = (
        section.heading is None or section.heading is argparse.SUPPRESS
    )
    heading = [] if suppressed else [f"{section.heading}:"]
    body = rendered(help_table(rows), formatter._width)
    return "\n".join([*heading, *description, body])


class ListingHelpFormatter(argparse.HelpFormatter):
    """Help whose sections are the same measured tables (#271).

    Argparse's usage line, description, and section headings stay
    as they are; each section's entries — commands, arguments,
    options — render beside their help at one measured offset, the
    help wrapping inside its column. Relies on argparse's stable
    formatter internals (the section items argparse itself builds).
    """

    def format_help(self) -> str:
        parts: list[str] = []
        for func, args in self._root_section.items:
            section = getattr(func, "__self__", None)
            text = (
                section_table(self, section)
                if isinstance(section, argparse.HelpFormatter._Section)
                else func(*args)
            )
            if text:
                parts.append(text.strip("\n"))
        return "\n\n".join(parts) + "\n"


def command_parser(**kwargs) -> argparse.ArgumentParser:
    """A parser with the table help — the factory every subcommand
    set builds its parsers from, so the whole tree renders alike."""
    kwargs.setdefault("formatter_class", ListingHelpFormatter)
    return argparse.ArgumentParser(**kwargs)
