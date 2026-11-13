"""The workspace tree's row rendering (#309, #347–#351): the
listing's columns, the status colors, the relative created labels,
the header/status lines' content, and the detail strip's lines
(#849) — pure text arithmetic over daemon rows: no Textual
widgets, no screens, and no daemon imports, so every screen
module renders through one vocabulary.
"""

from datetime import UTC, datetime

from rich.cells import cell_len
from textual.content import Content, Span


def workspace_label(row: dict) -> str:
    """The row's human-facing label (#246): its name, else its id."""
    return row.get("name") or row["id"]


#: The listing's columns (#347): the header's label and the
#: column's width, in row order — every row pads each field to its
#: column's width, so the columns line up down the list. The widths
#: are the fixed minimums the 80-column frame budgets: at that width
#: the border and the scrollbar together leave 74 cells for a row,
#: two past the fixed span (#849's ID column sits ten wide where
#: EGRESS sat twelve), and the widest label the created column
#: reads (#350, ``yesterday``) must fit whole beside the columns
#: before it. A wider terminal widens the growing columns past
#: these minimums
#: (:func:`expanded_columns`). The ID column (#849) carries a whole
#: daemon id at ten cells; the egress posture it replaced moved to
#: the detail strip.
LIST_COLUMNS = (
    ("NAME", 22),
    ("STATUS", 10),
    ("ID", 10),
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
#: padding, and the scrollbar's two — the workspaces' fixed span
#: (#849) sits two cells under the 74 an 80-column terminal leaves
#: a row, so the growing columns take those two at that width, and
#: both listings (the workspaces', the secrets') compute their
#: widened budgets from the terminal's width less this.
LIST_FRAME_CELLS = 6

#: The columns the listing's spare width flows into when the
#: terminal runs wider than the fixed columns' span (#347): the
#: name and the image — the two columns whose content runs long
#: (workspace names, image references) — while the status, id,
#: and created columns hold their fixed shape whatever the
#: terminal's width.
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
    (#350). The ID cell (#849) carries the immutable id — the
    egress posture the column carried moved to the detail
    strip."""
    widths = [width for _label, width in columns]
    return (
        clip(workspace_label(row), widths[0]),
        clip(status_with_issues(row), widths[1]),
        clip(row["id"], widths[2]),
        clip(row.get("image_ref") or row.get("image_hash") or "-", widths[3]),
        clip(created_label(row.get("created_at")), widths[4]),
    )


#: The mounts-issue marker's color (#850): the warning color —
#: the prominence the ``✗N`` count keeps beside a status word
#: whose own class may paint the word muted.
ISSUE_MARKER_COLOR = "$warning"

#: The open-shells marker's color (#858): the success color — a
#: live shell count is the row's healthy, live signal, distinct
#: from the warning the mounts-issue count carries and from
#: whatever color the status word's own class paints the word.
SHELL_MARKER_COLOR = "$success"


def issue_marker(row: dict) -> str | None:
    """The mounts-issue count's marker (#620, part #624, #850):
    ``✗N`` for a row whose mounts skip, else ``None`` — the
    piece the listing paints the warning color whatever the
    status word's own class paints the word behind it."""
    issues = row.get("mount_issues") or []
    return f"✗{len(issues)}" if issues else None


def shell_marker(row: dict) -> str | None:
    """The open-shells count's marker (#858): ``»N`` for a row
    with live shells on this host, else ``None``. The count is
    client-side state — the session records ``msks ssh`` writes,
    landed on the row by the listing — so a row the daemon serves
    alone carries nothing and prints no marker."""
    shells = row.get("shells") or 0
    return f"»{shells}" if shells else None


def standing_markers(row: dict) -> list[tuple[str, str]]:
    """The status cell's leading markers with their colors (#620,
    #858): the mounts-issue count first — the established
    marker, ahead of the word — then the open-shells count, each
    keeping its own color whatever the status word's own class
    paints the word behind them."""
    markers = []
    marker = issue_marker(row)
    if marker is not None:
        markers.append((marker, ISSUE_MARKER_COLOR))
    marker = shell_marker(row)
    if marker is not None:
        markers.append((marker, SHELL_MARKER_COLOR))
    return markers


def status_with_issues(row: dict) -> str:
    """The status cell with its standing markers (#620, part
    #624, #858): a row whose mounts skip leads with the count
    beside a plain status word — ahead, where a trailing one
    would middle-clip the word it marks — and a row with live
    shells carries the open-shells count beside it the same way
    (`✗1 »2 running`). The workspace's page names each mount
    issue. A healthy, shell-free row prints the status dot and
    word alone."""
    markers = [text for text, _color in standing_markers(row)]
    if not markers:
        return status_text(row["status"])
    return " ".join([*markers, row["status"]])


def status_spans(
    markers: list[tuple[str, str]], offset: int, status: str, color: str
) -> list[Span]:
    """The status cell's spans (#850, #858): each standing marker
    its own color over its slice of the cell's head, the status
    word behind them — the separator spaces included, the #850
    convention — its own class's color. A marker the cell's clip
    ate (a composed cell wider than the STATUS column) paints
    nothing: the word's span carries the clipped cell whole, so
    no marker's color rides characters the clip rearranged."""
    end = offset + len(status)
    spans: list[Span] = []
    word_start = offset
    pos = offset
    for text, mark_color in markers:
        if status[pos - offset : pos - offset + len(text)] == text:
            spans.append(Span(pos, pos + len(text), mark_color))
            word_start = pos + len(text)
        pos += len(text) + 1
    spans.append(Span(min(word_start, end), end, color))
    return spans


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
    display width). A row with standing markers splits its status
    cell's span (#850, #858): each marker — the ``✗N`` mounts
    count, the ``»N`` open-shells count — keeps its own color
    while the status word behind them rides its own class's
    color."""
    cells = row_cells(row, columns)
    name, status, *_ = cells
    line = padded_cells(cells, columns).rstrip()
    offset = len(cell_pad(name, columns[0][1])) + len(COLUMN_GAP)
    color = status_color(row["status"], theme_variables)
    spans = status_spans(standing_markers(row), offset, status, color)
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


#: The sort key builder per column (#854): CREATED builds its own
#: per direction; every other column reads one of the keys above.
def sort_key(column: str, descending: bool):
    if column == "CREATED":
        return stamp_sort_key(descending)
    keys = {
        "NAME": name_sort_key,
        "STATUS": status_sort_key,
        "ID": id_sort_key,
        "IMAGE": image_sort_key,
    }
    return keys[column]


def id_sort_key(row: dict) -> str:
    """The ID column's value key (#849) — and the tiebreak every
    other column shares (#854): the id alone."""
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
    count (#354): the segment leaves with the last hold. While
    shells are open on this host, their count rides the same line
    (#858): the page lands it on the row beside its per-second
    refresh, and the segment leaves with the last shell. The name
    rides a Content's plain text, so a markup-carrying name
    cannot shift the span."""
    name = workspace_label(row)
    status = status_text(row["status"])
    text = f" {name}  ·  {status}"
    if pending:
        text += f"  ·  egress to decide: {pending}"
    shells = row.get("shells") or 0
    if shells:
        noun = "shell" if shells == 1 else "shells"
        text += f"  ·  {shells} {noun} open"
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
