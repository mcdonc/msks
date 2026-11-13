"""The detail side of the workspace rows: the meta line, the
detail strip, and the status line.

Everything below the list row proper — the grown meta line
under the name, the key/value detail strip, and the status
bar's state summary with the version-skew marker.
"""

from __future__ import annotations

import re

from rich.cells import cell_len
from textual.content import Content, Span

from ...spec.version import __version__
from ..volumes import MIB, human_bytes
from .rows import (
    ISSUE_MARKER_COLOR,
    META_FRAME_W,
    META_HOST_W,
    META_ID_W,
    META_IMAGE_W,
    STATUS_SORT_ORDER,
    STATUS_SORT_RANK,
    clip,
    issue_marker,
    muted_style,
    shell_marker,
    workspace_label,
)


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


#: The version-skew marker's color (#852): the warning color —
#: the standing hint keeps the prominence the mounts-issue count
#: keeps on a row, beside a URL the line paints muted.
SKEW_MARKER_COLOR = "$warning"

#: Each version word's cell ceiling in the skew marker (#852): a
#: release word fits whole; a longer one (a proxy's junk string)
#: clips both ends rather than push the marker past the line's
#: span.
SKEW_WORD_W = 16


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


#: The detail strip's cell budgets (#849): the name leads with
#: the NAME column's fixed minimum, the id's ten cells carry a whole
#: daemon id, and the egress budget covers the posture's
#: vocabulary — both lines hold inside an 80-column terminal's
#: box with every fact whole.
DETAIL_NAME_W = 22
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


def marker_pairs(row: dict) -> list[tuple]:
    """The strip's marker pairs (#849, #858): the mounts count
    while any mount skips, then the shells count while any shell
    is live — the same ✗N/»N cues the status cell carries, read
    beside the costs."""
    pairs: list[tuple] = []
    marker = issue_marker(row)
    if marker is not None:
        pairs.append(("mounts", marker))
    shells = shell_marker(row)
    if shells is not None:
        pairs.append(("shells", shells))
    return pairs


def detail_pairs(
    row: dict, used_bytes: int | None
) -> tuple[list[tuple], list[tuple]]:
    """The strip's facts as (label, value) pairs, one list per
    line (#849): the identity — the name (bare: the strip's own
    answer to which row it describes), the id, the image — on
    the first, the posture and costs — cpu, memory, the egress
    mode, the markers (:func:`marker_pairs`), the storage the
    capacity report charges — on the second. Every fact but
    storage reads the row the listing already holds."""
    second = [
        ("cpu", "-" if row.get("cpus") is None else str(row["cpus"])),
        ("mem", mem_value(row.get("mem_mib"))),
        (
            "egress",
            clip(row.get("egress_mode") or "-", DETAIL_EGRESS_W),
        ),
    ]
    second += marker_pairs(row)
    second.append(
        ("storage", "-" if used_bytes is None else human_bytes(used_bytes))
    )
    return (
        [
            (None, clip(workspace_label(row), DETAIL_NAME_W)),
            ("id", clip(row["id"], DETAIL_ID_W)),
            ("image", clip(meta_raw(row)[1], META_IMAGE_W)),
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


def printable_word(word: str) -> str:
    """The word's printable characters only (#852): control
    codes drop — a junk word's BEL would desync the warning
    span's offset, and a newline would split the line's one row.
    """
    return "".join(ch for ch in word if ch.isprintable())


def daemon_version_word(health: dict | None) -> str | None:
    """The daemon's version word as the marker prints it (#852):
    the ``/health`` document's version with its control codes
    dropped and its edges trimmed (:func:`printable_word`), or
    None for every silent side — no document, no word, a
    non-string word, a word only control codes or spaces, and a
    word that matches this client's once printed."""
    theirs = health.get("version") if health else None
    if not isinstance(theirs, str):
        return None
    word = printable_word(theirs).strip()
    if not word or word == __version__:
        return None
    return word


def skew_marker(health: dict | None) -> str | None:
    """The version-skew marker (#852): ``msksd 1.2.3 ≠ client
    1.2.4`` — both words in short form — when the daemon's
    ``/health`` version and this client's differ, else ``None``.
    The CLI prints the full notice with its remedy once per
    invocation (#706, #715); the marker is the TUI's standing
    form, the one an operator leaves on screen. An unknown side
    stays silent — :func:`daemon_version_word` owns every silent
    shape."""
    word = daemon_version_word(health)
    if word is None:
        return None
    return (
        f"msksd {clip(word, SKEW_WORD_W)}"
        f" ≠ client {clip(__version__, SKEW_WORD_W)}"
    )


def summary_fit(
    head: str, summary: str | None, url_text: str, tail: list[str]
) -> str | None:
    """The summary at the cells the line's span leaves it (#852):
    the summary whole while it fits, clipped by what the count,
    the URL, and the marker spend together, and gone when the
    overrun eats it past recognition — the marker never gives a
    cell up, because every row already shows the fleet's shape
    and nothing else on the line shows the skew."""
    if summary is None:
        return None
    whole = "  ·  ".join([head, summary, url_text, *tail])
    over = cell_len(whole) - STATUS_LINE_W
    if over <= 0:
        return summary
    room = cell_len(summary) - over
    return None if room < 4 else clip(summary, room)


def fitted_url(
    url: str, head: str, summary: str | None, tail: list[str], floor: int
) -> str:
    """The URL at its clip budget (#853, #852): the line's
    remainder after the count, the summary, and the marker. The
    first pass crops at ``URL_FLOOR_W`` — the summary clips
    before the URL goes past its floor — and the re-fit pass,
    run after the summary gave what it could, takes the floor
    down to one cell while a marker stands: the URL yields its
    last cells before the marker loses its last one."""
    before = [head, summary] if summary else [head]
    spent = cell_len("  ·  ".join([*before, *tail])) + len("  ·  ")
    return clip(url, max(floor, min(URL_W, STATUS_LINE_W - spent)))


def status_line_spans(head: str, skew: str | None, line: str) -> list[Span]:
    """The standing line's spans: the count in the bold default
    foreground, the version-skew marker in the warning color
    while one stands (#852)."""
    spans = [Span(0, len(head), "$text bold")]
    if skew:
        spans.append(Span(len(line) - len(skew), len(line), SKEW_MARKER_COLOR))
    return spans


def status_content(
    count: int,
    url: str,
    summary: str | None = None,
    noun: str = "workspace",
    skew: str | None = None,
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
    before the summary gives a cell up. The version-skew marker
    (#852) rides last in the warning color and outranks both: the
    URL crops to its floor first, then the summary clips — the
    marker never gives a cell, because every row already shows
    the fleet's shape and nothing else on the line shows the
    skew. The secrets page's placeholder count rides the same
    shape (#390)."""
    plural = "" if count == 1 else "s"
    head = f" {count} {noun}{plural}"
    tail = [skew] if skew else []
    url_text = fitted_url(url, head, summary, tail, URL_FLOOR_W)
    summary_text = summary_fit(head, summary, url_text, tail)
    # The re-fit pass: a summary the fit clipped or dropped frees
    # cells the first pass spent against the whole summary, and a
    # marker that still overruns the line takes the URL's floor
    # — the URL re-fits at the width that actually stands, down
    # to one cell before the marker loses its last one.
    url_text = fitted_url(
        url, head, summary_text, tail, 1 if tail else URL_FLOOR_W
    )
    parts = (
        [head, summary_text, url_text, *tail]
        if summary_text is not None
        else [head, url_text, *tail]
    )
    line = "  ·  ".join(parts)
    return Content(line, status_line_spans(head, skew, line))
