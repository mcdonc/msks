"""The images tab's plain cells (phase 4.6 of #924, #964):
the panes' arithmetic over the presenter's row models — the
column sets and their indexes, the cell texts, the sort ranks a
date or size cell orders by, the header and flash texts, and
the channel URL both clients share. Plain data and plain
widgets' glue, no tab state in it: the tab module
(:mod:`msks.client.gui.images.tab`) owns the flights and the
fills, and this module owns what they render.

This module imports PySide6 at module scope — the package's
init imports none of it at parse time (#927), the terminal
package's own pattern.
"""

from datetime import datetime

from PySide6 import QtCore, QtWidgets

from ...config import DEFAULT_IMAGE_MANIFEST_URL
from ..imagerows import CatalogRow, PublishedRow

#: The model's place on every row's first cell (#964): the
#: selection and the actions read the row's model through it,
#: whatever a rebuild or a sort did to the row slots.
ROW_INDEX_ROLE = QtCore.Qt.ItemDataRole.UserRole

#: The row's own identity on its first cell (#964): the hash
#: for a catalog row, the url for a published one — the carry
#: keys on the row the operator held, not its slot and not its
#: ref (a rebuilt archive shares the ref under another hash).
ROW_KEY_ROLE = QtCore.Qt.ItemDataRole.UserRole + 1

#: A cell's sort rank (#964): the moment or whole-MiB count a
#: rank cell orders by — a label's spelling and its order
#: disagree (the dates across months, the sizes across digits).
RANK_ROLE = QtCore.Qt.ItemDataRole.UserRole + 2

#: The catalog table's columns (#964) — the TUI group's own
#: cells: the ref, the default marker, the hash prefix, and the
#: import date.
CATALOG_COLUMNS = ("REF", "DEFAULT", "HASH", "IMPORTED")

#: The catalog's import-date column (#964): a rank cell — the
#: parsed moment, a dash the oldest.
IMPORTED_COLUMN = 3

#: The published table's columns (#964) — the TUI group's own
#: cells: the ref, the baked description, the download size,
#: the hash prefix, the build date, and the membership marker.
PUBLISHED_COLUMNS = (
    "REF",
    "DESCRIPTION",
    "SIZE",
    "HASH",
    "BUILT",
    "MARKER",
)

#: The published table's rank columns (#964): the size by its
#: whole MiB, the build date by its moment.
REF_COLUMN = 0
SIZE_COLUMN = 2
BUILT_COLUMN = 4
MARKER_COLUMN = 5

#: The first group's label (#565, the GUI's read).
CATALOG_GROUP = "In the catalog"

#: The second group's label (#565).
PUBLISHED_GROUP = "Published"

#: The cell margin the column sizing pays (#939's own): the
#: budget an ellipsized cell's text renders within.
CELL_MARGIN = 8

#: The published table's description column (#964): the
#: flexible one — it absorbs the spare and gives ground first.
DESC_COLUMN = 1

#: The published table's hash column (#964): the second to
#: give ground, after the description's floor.
HASH_COLUMN = 3


def channel_url(conf) -> str:
    """The channel manifest's URL (#964): the configured
    override when one resolved, else the default the TUI page
    reads — one source both clients share."""
    return (
        getattr(conf, "image_manifest_url", None) or DEFAULT_IMAGE_MANIFEST_URL
    )


def published_cells(model: PublishedRow) -> tuple[str, ...]:
    """One published model's cells in column order (#964): the
    whole values the sizing measures and the eliding reads —
    one source for the fill, the restore, and the fit."""
    return (
        model.ref,
        model.description,
        model.size,
        model.hash_prefix,
        model.built,
        model.marker,
    )


def imported_rank(cell: str) -> float:
    """The import-date cell's sort rank (#964): the parsed
    moment — the label's spelling and the date order disagree
    across months and years — with a dash reading as the oldest
    and sinking on the newest-first read."""
    try:
        return datetime.fromisoformat(cell).timestamp()
    except ValueError:
        return 0.0


def size_rank(cell: str) -> int:
    """The size cell's sort rank (#964): the whole MiB — the
    labels' text order disagrees (``99`` sorts after ``100``)
    — with a dash reading as the smallest."""
    try:
        return int(cell.split()[0])
    except ValueError:
        return -1


def built_rank(cell: str) -> float:
    """The build-date cell's sort rank (#964): the parsed date,
    a dash the oldest — the import rank's own rule over the
    date-half stamps."""
    try:
        return datetime.fromisoformat(cell).timestamp()
    except ValueError:
        return 0.0


class RankItem(QtWidgets.QTableWidgetItem):
    """One cell that sorts by its carried rank (#964), never by
    its label: the dates order by their moments and the sizes
    by their whole MiB, a dash sinking with the oldest and the
    smallest rank the floor reads."""

    def __init__(self, text: str, rank) -> None:
        super().__init__(text)
        self.setData(RANK_ROLE, rank)

    def __lt__(self, other: QtWidgets.QTableWidgetItem) -> bool:
        return self.data(RANK_ROLE) < other.data(RANK_ROLE)


def ranked_item(cell: str, rank) -> QtWidgets.QTableWidgetItem:
    """One read-only cell, ranked or plain (#964): a rank sorts
    by the carried value, None by the cell's own text — the ref,
    the description, the hash, the marker, all words already."""
    if rank is None:
        return read_only_item(QtWidgets.QTableWidgetItem(cell))
    return read_only_item(RankItem(cell, rank))


def read_only_item(
    item: QtWidgets.QTableWidgetItem,
) -> QtWidgets.QTableWidgetItem:
    """One cell that renders but never edits (#939's rule, the
    images tab's own read): the groups are views over the
    daemon's and the channel's state, and an edit would paint a
    value only the display holds."""
    item.setFlags(item.flags() & ~QtCore.Qt.ItemFlag.ItemIsEditable)
    return item


def default_cell(model: CatalogRow) -> str:
    """The default column's text (#964): the designation the
    marker column carries, a dash on every other row."""
    return "default" if model.default else "-"


def importing_label(ref: str, elapsed_s: int) -> str:
    """The import dialog's line (#964): the TUI importing
    modal's own — the chosen ref, what the daemon does with the
    archive, and the elapsed wait."""
    return (
        f"importing {ref} — the daemon downloads and verifies "
        f"the archive… {elapsed_s}s"
    )


def imported_text(record: dict) -> str:
    """The import's landing flash (#964): the registered
    reference and its short hash, the CLI import's own line."""
    return f"imported {record['ref']} ({record['hash'][:12]})"


def counts_text(catalog: list, published: list) -> str:
    """The standing status after a refresh (#964): the two
    groups' counts — or, with both empty, the TUI page's own
    advice to import once from a channel URL."""
    if not catalog and not published:
        return (
            "the catalog and the channel both carry nothing — "
            "import from a channel URL once one is published"
        )
    return f"{len(catalog)} in the catalog · {len(published)} published"
