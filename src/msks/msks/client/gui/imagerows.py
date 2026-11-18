"""The images tab's presenter (#963, phase 4.6 of #924): the
catalog and published groups as plain row models — daemon rows
and manifest rows in, dataclasses out, nothing Qt-bound in
between. The catalog group carries the daemon's rows with the
default designation and each import's date (the shared cell
:func:`msks.client.images.imported_cell` renders, reused rather
than duplicated). The published group carries the channel
manifest's rows with their descriptions, sizes, and build
dates, beside the membership markers that survive rebuilds: a
channel import's catalog hash equals the manifest row's hash
(``[imported]``), while the same ref under a different hash
reads ``[local]``. The published group keeps only the rows the
daemon's own machine can boot (#717): a row declaring another
architecture stays off, by the #638 guard's own rule. The
helpers the TUI page owns (:mod:`msks.client.tui.importer`)
are mirrored here — the GUI imports nothing from the TUI, and
the suite pins the pair's parity — so the module holds no
PySide6 import and the coverage gate lands on it whole.
"""

from dataclasses import dataclass
from datetime import UTC, datetime

from ..images import imported_cell

#: The hash-prefix column's width — the same 12-character
#: prefix `msks image ls` prints, so the page, the CLI, and the
#: ``name@hash`` reference forms share one spelling of a build.
HASH_PREFIX = 12

#: The published row's marker when the exact build already sits
#: in the catalog (#565): the hash comparison owns the word, so
#: a rebuild cannot make the marker lie.
IMPORTED_MARKER = "[imported]"

#: The published row's marker when the same ref sits in the
#: catalog under a different hash (#565): the pre-channel plain
#: tar, a locally built archive — a build the operator holds,
#: just not this one.
LOCAL_MARKER = "[local]"

#: The catalog order's floor: a row without a usable import
#: stamp sorts below every dated row of its ref's block.
OLDEST_MOMENT = datetime.min.replace(tzinfo=UTC)


@dataclass(frozen=True)
class CatalogRow:
    """One daemon catalog row as the images tab renders it
    (#963): the ref, the default designation (the marker column
    the widget shows), the build's full hash — the key the
    remove and designate actions send — its 12-character prefix
    cell, and the import date cell. Plain data with nothing
    Qt-bound in it."""

    ref: str
    default: bool
    hash: str
    hash_prefix: str
    imported: str


@dataclass(frozen=True)
class PublishedRow:
    """One channel manifest row as the images tab renders it
    (#963): the ref, the baked description at full length (the
    cap is the widget's own layout call), the download size,
    the build's hash prefix and build date, the membership
    marker against the catalog, and the url the import action
    POSTs. Plain data with nothing Qt-bound in it."""

    ref: str
    description: str
    size: str
    hash_prefix: str
    built: str
    marker: str
    url: str


def hash_cell(value: object) -> str:
    """A build's hash-prefix cell: the first 12 characters, or a
    dash when the row carried no hash (a v0.1 channel row). The
    column exists on both groups, so a published row's prefix
    reads against the catalog's by eye."""
    text = str(value or "")
    return text[:HASH_PREFIX] if text else "-"


def size_label(size: int | None) -> str:
    """The download size in whole MiB, or a dash when the
    manifest row carried none — or nonsense a strict channel
    would not (a negative size is no size)."""
    if not size or size < 0:
        return "-"
    return f"{size / (1024 * 1024):.0f} MiB"


def built_label(row: dict) -> str:
    """The published row's build date — the manifest's
    ``built_at`` date half — or a dash when the row predates the
    field."""
    built = str(row.get("built_at") or "")[:10]
    return built or "-"


def membership(row: dict, catalog: list[dict]) -> str:
    """The published row's marker against the catalog (#565):
    an exact hash match is ``[imported]``; the same ref under a
    different hash is ``[local]`` — the build the operator
    holds lives in the local catalog, and the word says where,
    not how it got there. Anything else carries no marker."""
    hashes = {image["hash"] for image in catalog}
    if row.get("hash") in hashes:
        return IMPORTED_MARKER
    refs = {f"{image['name']}:{image['version']}" for image in catalog}
    if row.get("ref") in refs:
        return LOCAL_MARKER
    return ""


def daemon_machine(health: dict | None) -> str | None:
    """The daemon host's uname machine (#717) — the architecture
    its workspaces boot, read from the /health document. None
    when the document carries no machine word at all (a daemon
    predating the field, a mistyped word): the guard steps
    aside, every row shows, and the #638 import refusal stays
    the boundary."""
    machine = health.get("machine") if health else None
    return machine if isinstance(machine, str) and machine else None


def row_serves_arch(row: dict, machine: str | None) -> bool:
    """Whether one published row belongs on this daemon's page
    (#717), by the #638 guard's own rule: a row whose arch is
    absent, null, or empty declares nothing and shows
    everywhere, and a row with a declared arch shows on its
    match alone."""
    if machine is None:
        return True
    return not row.get("arch") or row["arch"] == machine


def has_pair(row: object) -> bool:
    """Whether the row is a mapping carrying the ref/url pair
    the import action needs."""
    return (
        isinstance(row, dict) and bool(row.get("ref")) and bool(row.get("url"))
    )


def require_str_field(row: dict, field: str) -> None:
    """One field answers as a string, or the row refuses by
    name; an absent or null field passes (the required pair was
    checked before, the optionals render as their dash)."""
    if row.get(field) is None:
        return
    if not isinstance(row[field], str):
        raise ValueError(
            f"manifest row {row['ref']!r}: {field} must be a string, "
            f"got {type(row[field]).__name__}"
        )


def require_whole_size(row: dict) -> None:
    """The size answers as a whole number, or the row refuses by
    name — a bool is an int in Python, so a manifest written
    with ``true`` is not a size."""
    size = row.get("size")
    if size is None:
        return
    if not isinstance(size, int) or isinstance(size, bool):
        raise ValueError(
            f"manifest row {row['ref']!r}: size must be a number, "
            f"got {type(size).__name__}"
        )


def checked_row(row: object) -> dict:
    """One manifest row, validated (#565): ``ref`` and ``url``
    are the str pair the import action needs; ``description``,
    ``hash``, ``built_at``, and ``arch`` (strs) and ``size`` (a
    number) may sit absent or null — their dash renders — but a
    mistyped value is a broken channel, and it refuses by name
    instead of crashing the page half-built (a hand-written
    manifest is exactly the fork/self-hosted case the setting
    exists for)."""
    if not has_pair(row):
        raise ValueError(f"manifest row lacks ref/url: {row!r}")
    for field in ("ref", "url", "description", "hash", "built_at", "arch"):
        require_str_field(row, field)
    require_whole_size(row)
    return row


def served_rows(manifest: list[dict], machine: str | None) -> list[dict]:
    """The published group's source rows (#717): each manifest
    row validated, then narrowed to the machine the daemon's
    /health names — one helper so the tab's fetch stays a
    straight line, and a broken channel refuses unchanged."""
    return [
        row for row in manifest if row_serves_arch(checked_row(row), machine)
    ]


def imported_moment(row: dict) -> datetime:
    """The catalog row's import stamp as an aware moment: a
    missing or unparsable one reads as the oldest (the date
    cell renders its dash, the order keeps a place for it), and
    a naive stamp reads as UTC — the TUI page's own rule, so
    the two clients order a catalog alike."""
    try:
        moment = datetime.fromisoformat(str(row.get("imported") or ""))
    except ValueError:
        return OLDEST_MOMENT
    if moment.tzinfo is None:
        return moment.replace(tzinfo=UTC)
    return moment


def sorted_catalog(catalog: list[dict]) -> list[dict]:
    """The catalog group's rows in reading order: ref ascending
    (name, then version), and one ref's rows newest import
    first — the row an operator just imported or refreshed
    stands at the top of its block. Two stable sorts compose the
    order (newest-first, then the ref's ascending pass over
    it)."""
    newest_first = sorted(catalog, key=imported_moment, reverse=True)
    return sorted(
        newest_first,
        key=lambda row: (row["name"], str(row["version"])),
    )


def catalog_row(row: dict) -> CatalogRow:
    """One daemon row as its catalog-group model (#963): the
    ref, the default designation, the full hash the actions key
    on, and the prefix and import-date cells the row renders —
    the date off the shared leaf, the same cell the CLI table
    prints."""
    return CatalogRow(
        ref=f"{row['name']}:{row['version']}",
        default=bool(row.get("default")),
        hash=row["hash"],
        hash_prefix=hash_cell(row.get("hash")),
        imported=imported_cell(row),
    )


def catalog_rows(catalog: list[dict]) -> list[CatalogRow]:
    """The catalog group's row models (#963) in the page's
    reading order — every caller (mount, refresh after an
    import or a delete) takes the same order."""
    return [catalog_row(row) for row in sorted_catalog(catalog)]


def published_row(row: dict, catalog: list[dict]) -> PublishedRow:
    """One manifest row as its published-group model (#963):
    the cells the row renders, beside the membership marker
    read against the catalog and the url the import action
    POSTs. The description carries at full length; the widget
    caps it to its column."""
    return PublishedRow(
        ref=row["ref"],
        description=(row.get("description") or "").strip() or "-",
        size=size_label(row.get("size")),
        hash_prefix=hash_cell(row.get("hash")),
        built=built_label(row),
        marker=membership(row, catalog),
        url=row["url"],
    )


def published_rows(
    published: list[dict], catalog: list[dict]
) -> list[PublishedRow]:
    """The published group's row models (#963) in the
    manifest's own order — the channel's sequence is the order
    the operator reads."""
    return [published_row(row, catalog) for row in published]
