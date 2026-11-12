"""The workspace tree's row rendering (#309, #347–#351): the
listing's columns, the status colors, the relative created labels,
the header/status lines' content, and the detail strip's lines
(#849) — pure text arithmetic over daemon rows: no Textual
widgets, no screens, and no daemon imports, so every screen
module renders through one vocabulary.
"""

import re
from datetime import UTC, datetime

from rich.cells import cell_len
from textual.content import Content, Span

from ..volumes import MIB, human_bytes


def workspace_label(row: dict) -> str:
    """The row's human-facing label (#246): its name, else its id."""
    return row.get("name") or row["id"]


#: The listing's columns (#347): the header's label and the
#: column's width, in row order — every row pads each field to its
#: column's width, so the columns line up down the list. The widths
#: are the fixed minimums the 80-column frame budgets: at that width
#: the border and the scrollbar together leave 74 cells for a row,
#: and the widest label the created column reads (#350,
#: ``yesterday``) must fit whole beside the columns before it. A
#: wider terminal widens the growing columns past these minimums
#: (:func:`expanded_columns`).
LIST_COLUMNS = (
    ("NAME", 22),
    ("STATUS", 10),
    ("EGRESS", 12),
    ("IMAGE", 12),
    ("CREATED", 10),
)

#: The sort direction arrows the active column's header carries
#: (#854): down for the descending read, up for the ascending
#: one. Only the active column carries one.
SORT_ARROW_DESCENDING = " ↓"
SORT_ARROW_ASCENDING = " ↑"

#: The STATUS column's sort order (#854): the lifecycle order an
#: operator scans in, the vocabulary's own sequence — a status
#: outside the vocabulary sorts after it, by id.
STATUS_SORT_ORDER = (
    "created",
    "starting",
    "running",
    "paused",
    "stopping",
    "stopped",
    "absent",
    "unknown",
)

#: The STATUS column's rank per status value (#854).
STATUS_SORT_RANK = {
    status: rank for rank, status in enumerate(STATUS_SORT_ORDER)
}

#: The columns whose first pick takes the descending read (#854):
#: CREATED's natural order is newest-first (#470 L1); every other
#: column starts ascending.
NATURAL_DESCENDING = frozenset({"CREATED"})

#: The listing's standing sort (#854): CREATED newest-first, the
#: order the tree opened with — the default sort, the header's
#: first arrow, and the state a new session starts in.
DEFAULT_SORT = ("CREATED", True)

#: The space between two listing columns.
COLUMN_GAP = "  "


#: The cells a listing row's frame eats before its first column
#: (#347, #349): the round border's two edges, the row's two-cell
#: padding, and the scrollbar's two — at an 80-column terminal the
#: 74 cells that remain are exactly the fixed columns' span, so
#: both listings (the workspaces', the secrets') compute their
#: widened budgets from the terminal's width less this.
LIST_FRAME_CELLS = 6

#: The columns the listing's spare width flows into when the
#: terminal runs wider than the fixed columns' span (#347): the
#: name and the image — the two columns whose content runs long
#: (workspace names, image references) — while the status,
#: egress, and created columns hold their vocabulary-sized shape
#: whatever the terminal's width.
GROWING_LABELS = frozenset({"NAME", "IMAGE"})


def fixed_span(columns) -> int:
    """The columns' fixed span: their widths and the gaps between
    them together — the budget an 80-column frame pays, and the
    line a wider terminal's spare cells are counted from."""
    return sum(width for _label, width in columns) + len(COLUMN_GAP) * (
        len(columns) - 1
    )


def grown_widths(spare: int, growing: list[str]) -> dict[str, int]:
    """The cells each growing column adds: the spare split evenly
    between the growing columns, an odd cell to the earlier
    ones."""
    share, extra = divmod(spare, len(growing))
    return {
        label: share + (1 if index < extra else 0)
        for index, label in enumerate(growing)
    }


def expanded_columns(
    available: int, base=LIST_COLUMNS, grow=GROWING_LABELS
) -> tuple:
    """The listing's columns widened to fill ``available`` display
    cells: the fixed widths are the minimums, and every cell
    beyond their span splits evenly between the growing columns,
    an odd cell to the earlier one. A budget at or under the
    fixed span returns the fixed columns — the frame clips the
    cells beyond, as it always did."""
    spare = available - fixed_span(base)
    if spare <= 0:
        return base
    growing = [label for label, _width in base if label in grow]
    added = grown_widths(spare, growing)
    return tuple((label, width + added.get(label, 0)) for label, width in base)


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


def list_header(columns=LIST_COLUMNS, sort=None) -> str:
    """The listing's header row (#347): the column labels, each
    left-justified to its column's width — the offsets the rows
    pad their fields to. A sort's active column carries its
    direction arrow beside the label (#854); the others carry
    none. The sort is opt-in: a caller that passes none (the
    secrets and proxies pages, #390) renders bare labels whatever
    its columns are named — an arrow there would misalign a
    header whose widths sit tight and promise a sort no click
    serves."""
    return COLUMN_GAP.join(
        cell_pad(sort_label(label, sort), width) for label, width in columns
    ).rstrip()


def sort_label(label: str, sort=None) -> str:
    """One header label with its sort arrow when the sort names
    it (#854): the active column reads ``LABEL ↓`` or
    ``LABEL ↑``, every other column its bare label, and no sort
    leaves every label bare."""
    if sort is None:
        return label
    column, descending = sort
    if label != column:
        return label
    arrow = SORT_ARROW_DESCENDING if descending else SORT_ARROW_ASCENDING
    return f"{label}{arrow}"


def padded_cells(cells: tuple[str, ...], columns=LIST_COLUMNS) -> str:
    """One listing line's cells joined: each left-justified to its
    column's display width, two spaces between columns."""
    return COLUMN_GAP.join(
        cell_pad(cell, width)
        for cell, (_label, width) in zip(cells, columns, strict=True)
    )


#: The running status's color (#348).
RUNNING_COLOR = "$success"

#: The statuses that sit at rest (#850): ``stopped`` — the
#: normal resting state — and ``created`` — never booted. The
#: word rides the muted text color (:func:`muted_style`), the
#: color the pre-#470 rows used, keeping the cue dot as the
#: state's cue — a fleet at rest reads calm, and the abnormal
#: row is the row the eye finds.
RESTING_STATUSES = frozenset({"stopped", "created"})

#: The statuses the daemon lost track of (#850): ``absent`` —
#: the artifacts are gone — and ``unknown`` — the daemon lost
#: the thread. Both want action, and the error color asks for
#: it.
LOST_STATUSES = frozenset({"absent", "unknown"})
LOST_STATUS_COLOR = "$error"

#: The color every other status renders in (#470 S1, #850): the
#: states that want the eye — ``starting`` and ``stopping``
#: while they pass, and ``paused``, a freeze no msks verb
#: produces and none resumes (the client's own advice is a
#: stop, then a start again) — and a state the map does not
#: know, so a vocabulary the daemon grows still stands out.
OTHER_STATUS_COLOR = "$warning"

#: The muted ratio a theme spells some other way than Textual's
#: own "auto 60%" falls back to.
DEFAULT_MUTED_RATIO = "60%"


def muted_style(theme_variables: dict) -> str:
    """The muted accent both header lines share (#366, #470 S1):
    the theme's text variable at the theme's own muted ratio —
    "$text-muted" itself is a widget-css color ("auto 60%"), and
    a content span parses its style as a rich style, where the
    auto half does not resolve; riding ``$text`` at the same
    ratio renders near the same muted text, within a few color
    values (the auto base composes slightly differently in a
    span than in widget css). The stopped status rode this color
    until #470's cue-dot pass; the meta line and its separators
    still do. A theme that spells its muted color without a
    ratio rides the 60% Textual's own themes use."""
    parts = theme_variables.get("text-muted", "").split()
    ratio = (
        parts[1]
        if len(parts) == 2 and parts[1].endswith("%")
        else DEFAULT_MUTED_RATIO
    )
    return f"$text {ratio}"


def status_color(status: str, theme_variables: dict | None = None) -> str:
    """The status cue's color (#348, #470 S1, #850), graded by
    state class: a theme variable the render resolves against
    the active theme — ``running`` in the success color; the
    resting statuses in the muted text color the pre-#470 rows
    used (the cue dot carries the state, so the word reads calm
    beside the muted metadata around it); the lost statuses in
    the error color; every other state — ``starting`` and
    ``stopping`` in passing, ``paused`` until an operator
    intervenes, and a status the map does not know — in the
    warning color."""
    if status == "running":
        return RUNNING_COLOR
    if status in RESTING_STATUSES:
        return muted_style(theme_variables or {})
    if status in LOST_STATUSES:
        return LOST_STATUS_COLOR
    return OTHER_STATUS_COLOR


#: The status cue's dot (#470 S1): a filled circle before the
#: status word — the colored cell the eye finds in a column of
#: one state, where the muted word read as plain text.
STATUS_DOT = "●"


def status_text(status: str) -> str:
    """The status word behind its cue dot (#470 S1) — the dot and
    the word ride the status color together."""
    return f"{STATUS_DOT} {status}"


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


def row_cells(row: dict, columns=LIST_COLUMNS) -> tuple[str, ...]:
    """The row's column cells (#347) at the given columns' widths,
    each clipped to its column's width whatever the daemon's
    vocabulary grows; the created cell reads as a relative label
    (#350)."""
    widths = [width for _label, width in columns]
    return (
        clip(workspace_label(row), widths[0]),
        clip(status_with_issues(row), widths[1]),
        clip(row.get("egress_mode") or "-", widths[2]),
        clip(row.get("image_ref") or row.get("image_hash") or "-", widths[3]),
        clip(created_label(row.get("created_at")), widths[4]),
    )


#: The mounts-issue marker's color (#850): the warning color —
#: the prominence the ``✗N`` count keeps beside a status word
#: whose own class may paint the word muted.
ISSUE_MARKER_COLOR = "$warning"


def issue_marker(row: dict) -> str | None:
    """The mounts-issue count's marker (#620, part #624, #850):
    ``✗N`` for a row whose mounts skip, else ``None`` — the
    piece the listing paints the warning color whatever the
    status word's own class paints the word behind it."""
    issues = row.get("mount_issues") or []
    return f"✗{len(issues)}" if issues else None


def status_with_issues(row: dict) -> str:
    """The status cell with the mounts' standing marker (#620,
    part #624): a row whose mounts skip leads with the count
    beside a plain status word — ahead, where a trailing one
    would middle-clip the word it marks — and the
    workspace's page names each issue. A healthy row prints the
    status dot and word alone."""
    marker = issue_marker(row)
    if marker is None:
        return status_text(row["status"])
    return f"{marker} {row['status']}"


def row_content(
    row: dict,
    theme_variables: dict | None = None,
    focused: bool = False,
    columns=LIST_COLUMNS,
) -> Content:
    """One listing row (#348, #470 S1/S3) at the given columns'
    widths (:func:`expanded_columns` widens a row to the
    terminal): the padded cells with
    the status cell alone carrying its state's color (dot and
    word), and the focused row's name bold — the focus cue that
    survives a theme whose highlight bar reads weakly. The span's
    style is a theme variable the render resolves against the
    active theme; the cells ride a Content's plain text — never
    parsed as markup — so a markup-carrying name cannot shift the
    columns (the name cell's own length fixes the span's offset:
    a wide-character name pads with fewer characters than its
    display width). A row whose mounts skip splits its status
    cell's span (#850): the ``✗N`` marker keeps the warning
    color while the status word behind it rides its own class's
    color."""
    cells = row_cells(row, columns)
    name, status, *_ = cells
    line = padded_cells(cells, columns).rstrip()
    offset = len(cell_pad(name, columns[0][1])) + len(COLUMN_GAP)
    color = status_color(row["status"], theme_variables)
    marker = issue_marker(row)
    if marker is None:
        spans = [Span(offset, offset + len(status), color)]
    else:
        marker_end = offset + min(len(marker), len(status))
        spans = [
            Span(offset, marker_end, ISSUE_MARKER_COLOR),
            Span(marker_end, offset + len(status), color),
        ]
    if focused:
        spans.append(Span(0, len(name), "$text bold"))
    return Content(line, spans)


def name_sort_key(row: dict) -> str:
    """The NAME column's value key (#854): the label case-folded."""
    return workspace_label(row).casefold()


def status_sort_key(row: dict) -> int:
    """The STATUS column's value key (#854): the lifecycle rank.
    A status outside the vocabulary ranks after every known one."""
    status = row.get("status") or ""
    return STATUS_SORT_RANK.get(status, len(STATUS_SORT_ORDER))


def image_sort_key(row: dict) -> str:
    """The IMAGE column's value key (#854): the ref the cell reads
    (the hash when no ref came)."""
    return row.get("image_ref") or row.get("image_hash") or ""


def stamp_sort_key(descending: bool):
    """The CREATED column's sort key (#854): the parsed creation
    timestamp, never the relative label (whose text order and
    date order disagree). A stamp that does not parse sinks to
    the end in either direction; the id breaks the ties."""

    def key(row: dict) -> tuple:
        stamp = parse_stamp(row.get("created_at") or "")
        moment = stamp.timestamp() if stamp is not None else 0.0
        return (stamp is None, -moment if descending else moment, row["id"])

    return key


def egress_sort_key(row: dict) -> str:
    """The EGRESS column's value key (#854): the posture's name."""
    return row.get("egress_mode") or ""


#: The sort key builder per column (#854): CREATED builds its own
#: per direction; every other column reads one of the keys above.
def sort_key(column: str, descending: bool):
    if column == "CREATED":
        return stamp_sort_key(descending)
    keys = {
        "NAME": name_sort_key,
        "STATUS": status_sort_key,
        "EGRESS": egress_sort_key,
        "IMAGE": image_sort_key,
    }
    return keys[column]


def id_sort_key(row: dict) -> str:
    """The tiebreak every column shares (#854): the id alone."""
    return row["id"]


def sort_rows(rows: list[dict], sort=DEFAULT_SORT) -> list[dict]:
    """The listing's display order under a column sort (#854):
    the rows ordered by the column's key, ties broken by the id
    ascending in either direction, so the order is deterministic
    whatever the rows carry and a tie renders one way whatever
    the column. The default is CREATED descending — #470 L1's
    newest-first. CREATED embeds its direction in its key (a
    broken stamp sinks in either one); every other column runs a
    stable value-sort, descending, over rows first sorted by id —
    stability under ``reverse`` keeps the id order intact."""
    column, descending = sort
    if column == "CREATED":
        return sorted(rows, key=sort_key(column, descending))
    value_key = sort_key(column, False)
    by_id = sorted(rows, key=id_sort_key)
    if not descending:
        return sorted(by_id, key=value_key)
    return sorted(by_id, key=value_key, reverse=True)


def column_at(columns, offset: int) -> str:
    """The column a header click's cell offset lands in (#854):
    each column owns its width plus the gap behind it, and an
    offset past the last span reads as the last column. The
    offsets are display cells, the same arithmetic the header and
    the rows render through."""
    start = 0
    for label, width in columns:
        start += width + len(COLUMN_GAP)
        if offset < start:
            return label
    return columns[-1][0]


def header_name(
    row: dict, pending: int = 0, theme_variables: dict | None = None
) -> Content:
    """The header's first line (#351, #470 S1): the workspace's
    name in the default foreground, its status behind the cue dot
    beside it in the status color (the list's status coloring),
    and — while holds wait on the page's queue — the pending-egress
    count (#354): the segment leaves with the last hold. The name
    rides a Content's plain text, so a markup-carrying name
    cannot shift the span."""
    name = workspace_label(row)
    status = status_text(row["status"])
    text = f" {name}  ·  {status}"
    if pending:
        text += f"  ·  egress to decide: {pending}"
    offset = 1 + len(name) + len("  ·  ")
    span = Span(
        offset,
        offset + len(status),
        status_color(row["status"], theme_variables),
    )
    return Content(text, [span])


#: Each meta field's cell budget (#470 S2, #523): the line clips
#: every field to its own width instead of running past the
#: terminal's edge — at an 80-column terminal the whole line (its
#: padding and separators included) fits with the created date
#: whole ("id" stays on the CLI; eight cells identify a row as
#: well as ten). The image's budget widened to the reference's
#: shape (#523): ``debian:13`` fits whole, longer references keep
#: their tail around the middle ellipsis (the version half, the
#: part a head-only clip eats) — the host narrowed to pay for it.
META_ID_W = 8
META_IMAGE_W = 14
META_HOST_W = 8

#: The meta line's fixed frame (#752): the cells an 80-column
#: terminal leaves the line after its own padding — at and under
#: this budget every field keeps its fixed minimum whatever the
#: row's own span (a row with slack at that width clips all the
#: same), so an 80-column terminal renders exactly the fixed
#: layout and only a wider one spends spare cells.
META_FRAME_W = 78


def image_cell(row: dict) -> str:
    """The meta line's image cell (#523): the catalog's
    ``name:version`` — the words the listing's IMAGE column reads
    — with the row's digest as the fallback for a row the catalog
    cannot resolve (a pruned image, a row older than the hash).
    The digest prefix names nothing an operator can act on."""
    return clip(meta_raw(row)[1], META_IMAGE_W)


def meta_raw(row: dict) -> tuple[str, str, str]:
    """The meta line's three clipped fields' raw values: the
    immutable id, the image (the catalog reference, the row's
    digest as its fallback), and the host — each with its honest
    fallback for a row that predates it."""
    return (
        row["id"],
        row.get("image_ref") or row.get("image_hash") or "-",
        row.get("host") or "-",
    )


def meta_text(cells: tuple[str, str, str, str]) -> str:
    """The meta line's text from its four cells."""
    wid, image, host, created = cells
    return f" id {wid}  ·  image {image}  ·  host {host}  ·  created {created}"


def meta_created(row: dict) -> str:
    """The meta line's created cell: the stamp's date, or the
    honest dash for a row that predates it."""
    return (row.get("created_at") or "")[:10] or "-"


def grow_budget(raw: str, width: int, spare: int) -> tuple[int, int]:
    """One meta field's widened budget (#752): the spare cells
    fill the field to its whole value, what remains of the spare
    handed back for the next field."""
    give = min(max(0, cell_len(raw) - width), max(0, spare))
    return width + give, spare - give


def widen_budgets(
    raw: tuple[str, str, str],
    minimums: tuple[int, int, int],
    spare: int,
) -> tuple[int, int, int]:
    """The minimums with the spare spent (#752): each clipped
    field fills to its whole value in order, what remains of the
    spare handed along to the next."""
    widths = list(minimums)
    for index in range(len(widths)):
        widths[index], spare = grow_budget(raw[index], widths[index], spare)
    return (widths[0], widths[1], widths[2])


def meta_budgets(row: dict, available: int | None) -> tuple[int, int, int]:
    """The meta line's clip budgets (#470 S2, #752): the fixed
    minimums at and under the 80-column frame (its span leaving
    slack still clips — the fixed layout is the whole frame's,
    not a row's), and on a wider terminal the spare cells fill
    each clipped field to its whole value, the id first, then
    the image, then the host, so a wide terminal reads every
    field whole."""
    minimums = (META_ID_W, META_IMAGE_W, META_HOST_W)
    if available is None or available <= META_FRAME_W:
        return minimums
    # Past the frame the whole budget is spare: the fixed span
    # rides the frame's own 78 cells, whatever the row's fields
    # clip to.
    return widen_budgets(meta_raw(row), minimums, available - META_FRAME_W)


def meta_fields(
    row: dict, available: int | None = None
) -> tuple[str, str, str, str]:
    """The meta line's fields, each clipped to its budget (#470
    S2, #752): the immutable id, the image, the host, and the
    created date. The image reads the catalog reference (#523):
    the ``name:version`` the listing's IMAGE column shows, the
    row's digest only when the catalog cannot resolve it (a
    pruned image) — the digest prefix names nothing an operator
    can act on."""
    widths = meta_budgets(row, available)
    raw = meta_raw(row)
    return (
        clip(raw[0], widths[0]),
        clip(raw[1], widths[1]),
        clip(raw[2], widths[2]),
        meta_created(row),
    )


def header_meta(
    row: dict,
    theme_variables: dict | None = None,
    available: int | None = None,
) -> Content:
    """The header's second line (#351, #470 S2, #523, #752): the
    immutable id, the image (the catalog reference, the digest as
    its fallback), the host, and the created date — every field
    clipped to a budget that holds the line inside an 80-column
    terminal with the date whole, and widens on a wider one
    (:func:`meta_budgets`) until every field reads whole. The
    page paints the line muted, and its ``·`` separators carry
    the same muted span style the name line gives the status
    (#366): a theme variable the render resolves, the one
    expression both header lines' muted accents share. The
    fields ride a Content's plain text (the name line's rule —
    no escaping)."""
    text = meta_text(meta_fields(row, available))
    style = muted_style(theme_variables or {})
    spans = [
        Span(mark.start(), mark.end(), style)
        for mark in re.finditer("·", text)
    ]
    return Content(text, spans)


#: The workspace page's status lines' label column (#752): the
#: consent and mounts lines share one grammar — the label muted
#: in a fixed column, the value beside it in the page's own
#: foreground, one ``·`` between the value's parts.
STATUS_LABEL_W = 8


def status_line(
    label: str, value: str, theme_variables: dict | None = None
) -> Content:
    """One status line's paint (#752): the label muted in the
    fixed column, the value beside it — the one grammar the
    workspace page's consent and mounts lines share. The value
    rides a Content's plain text, so free text in it (a daemon
    refusal, a mount path) cannot shift the span."""
    pad = " " * max(1, STATUS_LABEL_W - len(label))
    text = f" {label}{pad}{value}"
    style = muted_style(theme_variables or {})
    return Content(text, [Span(1, 1 + len(label) + len(pad), style)])


#: The daemon URL's cell budget in the status line (#349): the
#: URL is a hint, not data — the clip keeps the standing line on
#: one row at 80 columns whatever the operator's MSKSC_URL
#: carries, the middle ellipsis keeping both ends of a long URL
#: readable.
URL_W = 48

#: The URL's floor when the state summary eats the line (#853):
#: the summary wins cells before the URL, and the URL crops to
#: this many — a head and a tail around the ellipsis, still a
#: recognizable hint — before the line's own crop takes over.
URL_FLOOR_W = 8

#: The status line's whole cell budget (#853): the cells an
#: 80-column terminal shows the line after its one-cell padding
#: at each edge — the span the count, the state summary, and the
#: URL share. The meta line's META_FRAME_W is the same derivation
#: for its own widget; move a padding, move both.
STATUS_LINE_W = 78


def state_counts(rows: list[dict]) -> dict[str, int]:
    """The rows' counts by status (#853). A row without a status
    reads as the vocabulary's own ``unknown`` — a guard; the
    listing's own render indexes the field, so a row this maps
    never reaches the screen."""
    counts: dict[str, int] = {}
    for row in rows:
        status = row.get("status") or "unknown"
        counts[status] = counts.get(status, 0) + 1
    return counts


def state_summary(rows: list[dict]) -> str | None:
    """The fleet's standing at one glance (#853): ``all stopped``
    while one state stands, else each state's count in the
    lifecycle order the STATUS column sorts in (``1 running 3
    stopped``; a status the vocabulary does not know reads after
    the known ones). No rows read no summary — the count alone
    says the fleet is empty. The rows are the listing's own, so
    the summary costs no daemon roundtrip."""
    counts = state_counts(rows)
    if not counts:
        return None
    if len(counts) == 1:
        return f"all {next(iter(counts))}"
    after = len(STATUS_SORT_ORDER)
    order = sorted(
        counts,
        key=lambda status: (STATUS_SORT_RANK.get(status, after), status),
    )
    return " ".join(f"{counts[status]} {status}" for status in order)


#: The detail strip's cell budgets (#849): the id's ten cells
#: carry a whole daemon id, the image shares the meta line's
#: budget, and the egress budget covers the posture's vocabulary
#: — both lines hold inside an 80-column terminal's box with
#: every labeled fact whole.
DETAIL_ID_W = 10
DETAIL_EGRESS_W = 12

#: The detail strip's field separator (#849): one cell each side
#: of the dot, muted with the labels beside it.
DETAIL_JOIN = " · "

#: The strip's facts whose value rides the warning color (#849):
#: the mounts-issue marker, the same cue the status cell carries.
DETAIL_WARNED = frozenset({"mounts"})


def mem_value(mem_mib) -> str:
    """The memory fact in the strip's own unit: binary units
    (:func:`human_bytes`), the same renderer the storage figure
    uses, with the honest dash for a row that predates the
    field."""
    if mem_mib is None:
        return "-"
    return human_bytes(mem_mib * MIB)


def detail_pairs(
    row: dict, used_bytes: int | None
) -> tuple[list[tuple], list[tuple]]:
    """The strip's facts as (label, value) pairs, one list per
    line (#849): the identity — the id (bare: the row's own
    name for itself), the image, the egress posture — on the
    first, the topology and costs — cpu, memory, the
    mounts-issue count while any mount skips, the storage the
    capacity report charges — on the second. Every fact but
    storage reads the row the listing already holds."""
    marker = issue_marker(row)
    second = [
        ("cpu", "-" if row.get("cpus") is None else str(row["cpus"])),
        ("mem", mem_value(row.get("mem_mib"))),
    ]
    if marker is not None:
        second.append(("mounts", marker))
    second.append(
        ("storage", "-" if used_bytes is None else human_bytes(used_bytes))
    )
    return (
        [
            (None, clip(row["id"], DETAIL_ID_W)),
            ("image", clip(meta_raw(row)[1], META_IMAGE_W)),
            (
                "egress",
                clip(row.get("egress_mode") or "-", DETAIL_EGRESS_W),
            ),
        ],
        second,
    )


def pair_spans(
    label: str | None, value: str, end: int, offset: int, muted: str
) -> list[Span]:
    """One pair's spans (#849), placed by the pair's end in the
    line: the muted label ahead of its value (a pair without a
    label renders its value alone), and the warning color on a
    warned pair's value — the ``✗N`` marker, the same cue the
    status cell carries."""
    if label is None:
        return []
    spans = [
        Span(
            offset + end - len(value) - len(label) - 1,
            offset + end - len(value) - 1,
            muted,
        )
    ]
    if label in DETAIL_WARNED:
        spans.append(
            Span(offset + end - len(value), offset + end, ISSUE_MARKER_COLOR)
        )
    return spans


def detail_line(
    pairs: list[tuple], theme_variables: dict | None, offset: int = 0
) -> tuple[str, list[Span]]:
    """One strip line (#849): each fact's label muted in the
    muted accent, its value beside it in the line's own
    foreground, the dot joins muted with the labels — the
    strip's own paint, distinct from the rows it summarizes. A
    pair whose label is None (the id) renders its value alone.
    ``offset`` shifts the spans onto the line's place in the
    two-line whole."""
    muted = muted_style(theme_variables or {})
    spans: list[Span] = []
    text = " "
    for label, value in pairs:
        if len(text) > 1:
            text += DETAIL_JOIN
            spans.append(
                Span(
                    offset + len(text) - len(DETAIL_JOIN),
                    offset + len(text),
                    muted,
                )
            )
        text += value if label is None else f"{label} {value}"
        spans.extend(pair_spans(label, value, len(text), offset, muted))
    return text, spans


def detail_strip(
    row: dict, used_bytes: int | None, theme_variables: dict | None = None
) -> Content:
    """The listing's detail strip (#849): the focused row's quick
    facts on two lines at the listing's bottom — every fact but
    storage read off the row the listing holds, so a selection
    move pays no roundtrip. The id opens the first line bare;
    each fact behind it reads its muted label and its value in
    the strip's own foreground, and the ``✗N`` mounts marker
    rides the warning color."""
    first_pairs, second_pairs = detail_pairs(row, used_bytes)
    first, spans = detail_line(first_pairs, theme_variables)
    second, more = detail_line(
        second_pairs, theme_variables, offset=len(first) + 1
    )
    return Content(f"{first}\n{second}", [*spans, *more])


def status_content(
    count: int,
    url: str,
    summary: str | None = None,
    noun: str = "workspace",
) -> Content:
    """The status line's standing content (#349, #853): the row
    count — the fact that moves while the operator works — set in
    the bold default foreground, the fleet's state summary
    (:func:`state_summary`) after it, and the daemon's URL riding
    last in the line's own muted color (the span names ``$text``
    so a theme's foreground answers, not the line's muted base).
    The summary wins cells before the URL (#853): the URL's clip
    budget is what the line's span leaves after the count and the
    summary, so a long summary crops the URL — both ends kept —
    before the summary gives a cell up. The secrets page's
    placeholder count rides the same shape (#390)."""
    plural = "" if count == 1 else "s"
    head = f" {count} {noun}{plural}"
    parts = [head, summary] if summary else [head]
    spent = cell_len("  ·  ".join(parts)) + len("  ·  ")
    width = max(URL_FLOOR_W, min(URL_W, STATUS_LINE_W - spent))
    line = "  ·  ".join([*parts, clip(url, width)])
    return Content(line, [Span(0, len(head), "$text bold")])
