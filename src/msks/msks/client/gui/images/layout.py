"""The images panes' column arithmetic (phase 4.6 of #924,
#964): the layout the two group tables take against their
panes' widths — the catalog pane's columns sharing the spare so
the group fills its pane's whole width, and the published
pane's widths: a spare pane keeps every whole value and spreads
the room across the size, the hash, the build date, and the
marker; a tight pane takes its character budget from the
description first (its middle ellipsis shrinking to a handful
of characters), then the hash, then the ref and the build date,
the size and the marker giving ground only after those are
spent — so the size and the marker read whole in every pane a
real window shows, and no horizontal scrollbar hides a row's
cells whatever the pane's width. A column at or above its
natural width keeps whole text: the natural measurement already
carries the padding the cell renders within.

Plain widget arithmetic, no tab state: the tab module
(:mod:`msks.client.gui.images.tab`) hands over the tables and
the models, this module hands back the layout.

This module imports PySide6 at module scope — the package's
init imports none of it at parse time (#927).
"""

from PySide6 import QtCore

from ..listtable import ListTable, fill_columns
from .cells import (
    BUILT_COLUMN,
    CELL_MARGIN,
    DESC_COLUMN,
    HASH_COLUMN,
    MARKER_COLUMN,
    REF_COLUMN,
    ROW_INDEX_ROLE,
    SIZE_COLUMN,
    published_cells,
)

#: The description's floor (#964): the few characters an
#: ellipsized line keeps — a tight pane's character budget
#: comes from the description first, and a handful of characters
#: keep the row's place in the reading.
DESC_FLOOR_CHARS = 4


def size_columns(catalog: ListTable, published: ListTable, models) -> None:
    """Both panes' columns against their widths (#964): the
    catalog pane's columns share the spare and the published
    pane takes its budget in the giving order. The published
    pass pauses the sorting for its text rewrites — a cell the
    eliding rewrites would re-sort mid-swap and scramble the
    rows — and resumes after, the indicator re-applied."""
    size_catalog_columns(catalog)
    published.setSortingEnabled(False)
    size_published_columns(published, models)
    published.setSortingEnabled(True)


def size_catalog_columns(catalog: ListTable) -> None:
    """The catalog group's columns (#964): every column at
    least as wide as its longest value — the header's own
    label included — and the spare beyond that split evenly
    across the columns, an odd cell to the earlier ones, the
    listing's own arithmetic (#347) so the group consumes
    the pane's whole horizontal space — the shared leaf's
    :func:`fill_columns` (#997), the secrets tables' beside
    it."""
    fill_columns(catalog)


def size_published_columns(table: ListTable, models) -> None:
    """The published group's columns (#964): a spare pane keeps
    every whole value and spreads the room across the size, the
    hash, the build date, and the marker; a pane without room
    takes its character budget from the description first —
    down to a handful of characters — then the hash, then the
    ref and the build date, the size and the marker giving
    ground only after those are spent: the size and the marker
    read whole in every pane a real window shows, and no
    horizontal scrollbar hides a row's cells whatever the
    pane's width."""
    restore_whole_texts(table, models)
    table.resizeColumnsToContents()
    widths = [
        table.columnWidth(column) for column in range(table.columnCount())
    ]
    spare = table_spare(table)
    if spare > 0:
        share_the_spare(
            table,
            (SIZE_COLUMN, HASH_COLUMN, BUILT_COLUMN, MARKER_COLUMN),
            spare,
        )
    else:
        spare = shrink_to(
            table, widths, DESC_COLUMN, description_floor(table), spare
        )
        spare = shrink_to(
            table,
            widths,
            HASH_COLUMN,
            column_floor(table, HASH_COLUMN),
            spare,
        )
        spare = stage_deficit(table, (REF_COLUMN, BUILT_COLUMN), spare)
        stage_deficit(table, (SIZE_COLUMN, MARKER_COLUMN), spare)
    fit_elided(table, models, widths)


def share_the_spare(
    table: ListTable, columns: tuple[int, ...], spare: int
) -> None:
    """The spare pane's sharing (#964): the description keeps
    its whole value and the spare spreads evenly across the
    size, the hash, the build date, and the marker — the pane
    fills its width with its facts instead of parking the room
    in the description's empty tail."""
    share, extra = divmod(spare, len(columns))
    for index, column in enumerate(columns):
        table.setColumnWidth(
            column,
            table.columnWidth(column) + share + (1 if index < extra else 0),
        )


def description_floor(table: ListTable) -> int:
    """The description's floor (#964): a handful of characters
    and the cell margin — the TUI page's own answer lets the
    column drop outright when the pane has no room, and the few
    characters keep the row's place in the reading while the
    budget comes from here first."""
    metrics = table.fontMetrics()
    return metrics.horizontalAdvance("m" * DESC_FLOOR_CHARS) + CELL_MARGIN


def shrink_to(
    table: ListTable, widths: list[int], column: int, floor: int, spare: int
) -> int:
    """One elidable column's ground (#964): down to the floor
    it was handed and no further, whatever remains of the
    deficit."""
    give = min(widths[column] - floor, -spare)
    table.setColumnWidth(column, widths[column] - give)
    return spare + give


def stage_deficit(
    table: ListTable, columns: tuple[int, ...], spare: int
) -> int:
    """One stage's columns against the deficit (#964): a pixel
    at a time off the stage's widest column still above the
    header's own minimum section — the columns stay balanced,
    every pixel of the deficit lands, and the ref and the build
    date give their ground before the size and the marker ever
    do."""
    floor = table.horizontalHeader().minimumSectionSize()
    for _ in range(-spare):
        giver = widest_giver(table, columns, floor)
        if giver is None:
            break
        table.setColumnWidth(giver, table.columnWidth(giver) - 1)
        spare += 1
    return spare


def widest_giver(
    table: ListTable, columns: tuple[int, ...], floor: int
) -> int | None:
    """The stage's widest column still above the floor (#964):
    the next to give its pixel — None when the whole stage sits
    at its floor."""
    widest = None
    for column in columns:
        width = table.columnWidth(column)
        if width > floor and (
            widest is None or width > table.columnWidth(widest)
        ):
            widest = column
    return widest


def row_model(table: ListTable, models, row: int):
    """The model a table row shows (#964): read through the
    row's index role — a sort moves rows and models apart, and
    the text passes follow the row, never the list's order."""
    item = table.item(row, 0)
    if item is None:
        return None
    return models[item.data(ROW_INDEX_ROLE)]


def restore_whole_texts(table: ListTable, models) -> None:
    """The elidable cells' whole values back (#964): the sizing
    pass measures them afresh — an elided display from a
    narrower pane must not shrink the natural width a wider one
    measures."""
    for row in range(table.rowCount()):
        model = row_model(table, models, row)
        if model is None:
            continue
        for column, value in enumerate(published_cells(model)):
            item = table.item(row, column)
            if item is not None:
                item.setText(value)


def table_spare(table: ListTable) -> int:
    """The pane's width beyond the columns' current total
    (#964): what the sizing has to give or take."""
    return table.viewport().width() - sum(
        table.columnWidth(column) for column in range(table.columnCount())
    )


def column_floor(table: ListTable, column: int) -> int:
    """A shrinking column's floor (#964): its header's own
    label and the cell margin — a column never reads below the
    word that names it, measured in the header's own bold
    face."""
    metrics = table.horizontalHeader().fontMetrics()
    label = table.model().headerData(column, QtCore.Qt.Orientation.Horizontal)
    return metrics.horizontalAdvance(str(label)) + CELL_MARGIN


def fit_elided(table: ListTable, models, naturals: list[int]) -> None:
    """The published cells' display (#964): a column at or
    above its natural width reads its whole values — the
    natural measurement already carries the padding the cell
    renders within, and a budget shaved below it would ellipsize
    every cell that fits. A column below its natural width
    ellipsizes in the middle within the width it ended with,
    the budget paying only the padding the natural carried.
    The whole values stay on the models, so a wider pane
    restores them."""
    metrics = table.fontMetrics()
    for column in range(table.columnCount()):
        budget = elide_budget(table, metrics, column, naturals[column], models)
        for row in range(table.rowCount()):
            model = row_model(table, models, row)
            if model is None:
                continue
            item = table.item(row, column)
            if item is None:
                continue
            value = published_cells(model)[column]
            item.setText(
                metrics.elidedText(
                    value,
                    QtCore.Qt.TextElideMode.ElideMiddle,
                    budget,
                )
            )


def elide_budget(
    table: ListTable, metrics, column: int, natural: int, models
) -> int:
    """The column's elide budget (#964): the column's whole
    width when the whole value fits by construction — the
    natural measurement already carries the padding the cell
    renders within — and the padding-paid width below it."""
    width = table.columnWidth(column)
    if width >= natural:
        return width
    return width - column_overhead(table, metrics, column, natural, models)


def column_overhead(
    table: ListTable, metrics, column: int, natural: int, models
) -> int:
    """The column's own padding (#964): the natural width
    resizeColumnsToContents gave it beyond its longest whole
    value or its header — the part of the width no text may
    spend, so an elide budget of the column's whole width still
    shows a value that fits."""
    longest = max(
        (
            metrics.horizontalAdvance(published_cells(model)[column])
            for model in models
        ),
        default=0,
    )
    label = table.model().headerData(column, QtCore.Qt.Orientation.Horizontal)
    return max(
        natural - max(longest, metrics.horizontalAdvance(str(label))),
        0,
    )
