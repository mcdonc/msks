"""The client's tabular output: the plain listings (#271) and
the framed capacity tables (#381).

Rich measures every cell — the header row included — and fits each
column's width to the values present, so a long value widens its
column for every row instead of shifting one row's later columns
off the grid. The width arithmetic is rich's, measured per render;
the client's own code carries none of it.

The help screens render through typer's own rich formatting
(#315); the argparse-era help tables this module carried are gone
with the parser that needed them.
"""

import io

from rich.console import Console
from rich.table import Table
from rich.text import Text

# Listing rows never fold: the render width sits far past any value
# a listing carries.
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


def framed_table(headers: list[str], rows: list[list[str]]) -> Table:
    """The rows as rich frames a table by default: box rules
    around and between the columns, the header row inside the top
    rule, padded cells — the capacity tables' look (#381). The
    render is plain text, so the rules carry the look, not ANSI
    styling."""
    table = Table()
    for header in headers:
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


def framed_text(
    headers: list[str],
    rows: list[list[str]],
    width: int = UNFOLDED,
) -> str:
    """The rows as one framed table.

    A rowless table renders nothing, the same empty contract the
    listings carry.
    """
    if not rows:
        return ""
    return rendered(framed_table(headers, rows), width)
