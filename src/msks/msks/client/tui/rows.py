"""The workspace tree's row rendering (#309, #347–#351): the
listing's columns, the status colors, the relative created labels,
and the header/status lines' content — pure text arithmetic over
daemon rows, with no Textual and no daemon imports, so every
screen (:mod:`msks.client.tui.main_app` and its siblings) renders
through one vocabulary.
"""

import re
from datetime import UTC, datetime

from rich.cells import cell_len
from textual.content import Content, Span


def workspace_label(row: dict) -> str:
    """The row's human-facing label (#246): its name, else its id."""
    return row.get("name") or row["id"]


#: The listing's columns (#347): the header's label and the
#: column's width, in row order — every row pads each field to its
#: column's width, so the columns line up down the list. The name
#: column pays for the frame's edges (#349): at 80 columns the
#: border and the scrollbar together leave 74 cells for a row,
#: and the widest label the created column reads (#350,
#: ``yesterday``) must fit whole beside the columns before it.
LIST_COLUMNS = (
    ("NAME", 22),
    ("STATUS", 10),
    ("EGRESS", 12),
    ("IMAGE", 12),
    ("CREATED", 10),
)

#: The space between two listing columns.
COLUMN_GAP = "  "

#: The listing's column widths, in row order.
NAME_W, STATUS_W, EGRESS_W, IMAGE_W, CREATED_W = (
    width for _label, width in LIST_COLUMNS
)


def cell_prefix(text: str, width: int) -> str:
    """The longest head of ``text`` that fits ``width`` display
    cells (a wide character that would cross the budget stays
    whole; the head may land a cell short)."""
    out: list[str] = []
    used = 0
    for char in text:
        wide = cell_len(char)
        if used + wide > width:
            break
        out.append(char)
        used += wide
    return "".join(out)


def cell_pad(text: str, width: int) -> str:
    """The text left-justified to ``width`` display cells — a
    wide-character name cannot shift the columns beside it."""
    short = width - cell_len(text)
    return text + " " * short if short > 0 else text


def clip(text: str, width: int = 12) -> str:
    """One clipped column: the text, or its head and tail kept
    around a middle ellipsis when it runs wider than ``width``
    display cells (the tail carries the version half of a
    reference, the part a head-only clip eats; a wide character
    that would cross a budget stays whole, so a clipped cell may
    land a cell short and the padding fills it)."""
    if cell_len(text) <= width:
        return text
    head = (width - 1) // 2
    return (
        f"{cell_prefix(text, head)}…"
        f"{cell_prefix(text[::-1], width - 1 - head)[::-1]}"
    )


def list_header(columns=LIST_COLUMNS) -> str:
    """The listing's header row (#347): the column labels, each
    left-justified to its column's width — the offsets the rows
    pad their fields to. The secrets page's own columns ride the
    same shape (#390)."""
    return COLUMN_GAP.join(
        cell_pad(label, width) for label, width in columns
    ).rstrip()


def padded_cells(cells: tuple[str, ...], columns=LIST_COLUMNS) -> str:
    """One listing line's cells joined: each left-justified to its
    column's display width, two spaces between columns."""
    return COLUMN_GAP.join(
        cell_pad(cell, width)
        for cell, (_label, width) in zip(cells, columns, strict=True)
    )


#: The running status's color (#348) and the color every
#: other unmapped state renders in — a state the map does not
#: know takes the warning color, so a vocabulary the daemon
#: grows still stands out.
RUNNING_COLOR = "$success"

#: The color every status outside the map's two entries renders
#: in.
OTHER_STATUS_COLOR = "$warning"

#: The muted ratio a theme spells some other way than Textual's
#: own "auto 60%" falls back to.
DEFAULT_MUTED_RATIO = "60%"


def muted_style(theme_variables: dict) -> str:
    """The stopped status's color (#348): the theme's text
    variable at the theme's own muted ratio — "$text-muted"
    itself is a widget-css color ("auto 60%"), and a content
    span parses its style as a rich style, where the auto half
    does not resolve; riding ``$text`` at the same ratio renders
    near the same muted text, within a few color values (the
    auto base composes slightly differently in a span than in
    widget css). A theme that spells its muted color without a
    ratio rides the 60% Textual's own themes use."""
    parts = theme_variables.get("text-muted", "").split()
    ratio = (
        parts[1]
        if len(parts) == 2 and parts[1].endswith("%")
        else DEFAULT_MUTED_RATIO
    )
    return f"$text {ratio}"


def status_color(status: str, theme_variables: dict | None = None) -> str:
    """The status column's color (#348): a theme variable the
    render resolves against the active theme — running in the
    success color, stopped in muted text at the theme's own
    ratio, any other state in the warning color."""
    if status == "running":
        return RUNNING_COLOR
    if status == "stopped":
        return muted_style(theme_variables or {})
    return OTHER_STATUS_COLOR


def status_class(status: str) -> str:
    """The class a row's status becomes (#348): the status itself
    when it reads as one ASCII CSS word (Textual's class names
    are ASCII — a wider word would raise), else ``other``."""
    return status if status.isidentifier() and status.isascii() else "other"


def clock_now() -> datetime:
    """The wall clock the relative created labels read (#350) —
    the tests' seam for a pinned date."""
    return datetime.now().astimezone()


#: The pinned bucketing ladder (#350): a day count under the
#: bound reads as its days divided by the span, in the span's
#: unit — under 7 reads ``Nd ago``, under 30 ``Nw ago`` (13 days
#: reads ``1w ago``), under 365 ``Nmo ago``; past the ladder a
#: year count reads ``Ny ago``.
AGE_BUCKETS = (
    (7, 1, "d"),
    (30, 7, "w"),
    (365, 30, "mo"),
)


def age_label(days: int) -> str:
    """One bucket of the pinned rule (#350) over whole calendar
    days: ``today`` and ``yesterday`` cover the first two days
    (a clock that trails its stamp — skew — stays at ``today``),
    then the AGE_BUCKETS ladder, then ``Ny ago``."""
    if days <= 0:
        return "today"
    if days == 1:
        return "yesterday"
    for bound, span, unit in AGE_BUCKETS:
        if days < bound:
            return f"{days // span}{unit} ago"
    return f"{days // 365}y ago"


def parse_stamp(created_at: str) -> datetime:
    """The stamp as an aware datetime — the daemon sends naive
    UTC, so a bare stamp reads as UTC — or ``None`` when the text
    does not parse."""
    try:
        stamp = datetime.fromisoformat(created_at)
    except ValueError:
        return None
    return stamp if stamp.tzinfo else stamp.replace(tzinfo=UTC)


def created_label(created_at: str | None, now: datetime | None = None) -> str:
    """The CREATED column's relative label (#350), the pinned
    bucketing (:func:`age_label`) over whole calendar days
    between the creation and the clock. The daemon stamps in
    UTC; the label reads the creation in the operator's own
    calendar day. A stamp that is missing or unparseable reads
    ``-``."""
    stamp = parse_stamp(created_at) if created_at else None
    if stamp is None:
        return "-"
    when = clock_now() if now is None else now
    local = stamp.astimezone(when.tzinfo) if when.tzinfo else stamp
    return age_label((when.date() - local.date()).days)


def row_cells(row: dict) -> tuple[str, ...]:
    """The row's column cells (#347), each clipped to its
    column's width whatever the daemon's vocabulary grows; the
    created cell reads as a relative label (#350)."""
    return (
        clip(workspace_label(row), NAME_W),
        clip(row["status"], STATUS_W),
        clip(row.get("egress_mode") or "-", EGRESS_W),
        clip(row.get("image_hash") or "-", IMAGE_W),
        clip(created_label(row.get("created_at")), CREATED_W),
    )


def row_content(row: dict, theme_variables: dict | None = None) -> Content:
    """One listing row (#348): the padded cells with the status
    cell alone carrying its state's color. The span's style is a
    theme variable the render resolves against the active theme
    (the muted ratio reads the theme's own); the cells ride a
    Content's plain text — never parsed as markup — so a
    markup-carrying name cannot shift the columns (the name
    cell's own length fixes the span's offset: a wide-character
    name pads with fewer characters than its display width)."""
    cells = row_cells(row)
    name, status, *_ = cells
    line = padded_cells(cells).rstrip()
    offset = len(cell_pad(name, NAME_W)) + len(COLUMN_GAP)
    span = Span(
        offset,
        offset + len(status),
        status_color(row["status"], theme_variables),
    )
    return Content(line, [span])


def header_name(
    row: dict, pending: int = 0, theme_variables: dict | None = None
) -> Content:
    """The header's first line (#351): the workspace's name in
    the default foreground, its status beside it in the status
    color (the list's status coloring), and — while holds wait on
    the page's queue — the pending-egress count (#354): the
    segment leaves with the last hold. The name rides a Content's
    plain text, so a markup-carrying name cannot shift the span."""
    name = workspace_label(row)
    status = row["status"]
    text = f" {name}  ·  {status}"
    if pending:
        text += f"  ·  egress to decide: {pending}"
    offset = 1 + len(name) + len("  ·  ")
    span = Span(
        offset,
        offset + len(status),
        status_color(status, theme_variables),
    )
    return Content(text, [span])


def meta_fields(row: dict) -> tuple[str, str, str, str]:
    """The meta line's fields: the immutable id, the image
    hash's head, the host, and the created date — each with its
    honest fallback for a row that predates it."""
    created = (row.get("created_at") or "")[:10]
    return (
        row["id"],
        (row.get("image_hash") or "-")[:12],
        row.get("host") or "-",
        created or "-",
    )


def header_meta(row: dict, theme_variables: dict | None = None) -> Content:
    """The header's second line (#351): the immutable id, the
    image hash, the host, and the created date — the page paints
    the line muted, and its ``·`` separators carry the same muted
    span style the name line gives the status (#366): a theme
    variable the render resolves, the one expression both header
    lines' muted accents share. The fields ride a Content's plain
    text (the name line's rule — no escaping), and the line
    truncates at the terminal's edge (an ellipsis marks the cut)
    beside the name's own line."""
    wid, image, host, created = meta_fields(row)
    text = f" id {wid}  ·  image {image}  ·  host {host}  ·  created {created}"
    style = muted_style(theme_variables or {})
    spans = [
        Span(mark.start(), mark.end(), style)
        for mark in re.finditer("·", text)
    ]
    return Content(text, spans)


#: The daemon URL's cell budget in the status line (#349): the
#: URL is a hint, not data — the clip keeps the standing line on
#: one row at 80 columns whatever the operator's MSKSC_URL
#: carries, the middle ellipsis keeping both ends of a long URL
#: readable.
URL_W = 48


def status_content(count: int, url: str, noun: str = "workspace") -> Content:
    """The status line's standing content (#349): the row count —
    the fact that moves while the operator works — set in the bold
    default foreground, the daemon's URL riding after it in the
    line's own muted color (the span names ``$text`` so a theme's
    foreground answers, not the line's muted base). The secrets
    page's placeholder count rides the same shape (#390)."""
    plural = "" if count == 1 else "s"
    head = f" {count} {noun}{plural}"
    line = f"{head}  ·  {clip(url, URL_W)}"
    return Content(line, [Span(0, len(head), "$text bold")])
