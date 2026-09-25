"""The tabular helpers (#271): the measured listings. The help
screens render through typer's own rich formatting (#315)."""

from msks.client.tabular import listing_text


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


def test_listing_text_renders_markup_verbatim() -> None:
    """A cell the guest controls is data, never rich markup: the
    consent surfaces print bracketed destinations verbatim (the
    TUI escapes the same fields for the same reason)."""
    text = listing_text(
        ["id", "destination"],
        [["a1", "x[/]y:443"], ["b2", "x[bold]y.z (all ports)"]],
    )
    assert "x[/]y:443" in text
    assert "x[bold]y.z (all ports)" in text


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
