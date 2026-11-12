"""The TUI's image page (#565, #571): the catalog and the
published channel on one screen.

Two labeled groups — *In the catalog* (the daemon's rows, the
default marker, each import's date) and *Published* (the channel
manifest's rows with their descriptions, sizes, and build dates)
— with membership markers that survive rebuilds: a channel
import's catalog hash equals the manifest row's hash
(``[imported]``), while the same ref under a different hash
(the pre-channel plain tar, a locally built archive) reads
``[local]``. The published group carries the
rows the daemon's own machine can boot (#717): a row declaring
another architecture stays off the page — the daemon's #638
import refusal reads the same word in the archive's own
image.json, and the page does not offer what the guard must
refuse. Enter imports a published row
(the daemon downloads, verifies, and floor-checks; the modal
holds the terminal with an elapsed timer) and opens the detail
view for a catalog row; ``D`` deletes a catalog row through the
confirm screen — the same DELETE the CLI's ``image rm`` makes,
refusals surfaced on the failure panel.
"""

import asyncio
import time
from datetime import UTC, datetime

from textual.app import ComposeResult
from textual.binding import Binding
from textual.screen import ModalScreen, Screen
from textual.widgets import Footer, OptionList, Static

from ..config import DEFAULT_IMAGE_MANIFEST_URL
from ..images import fetch_manifest, imported_cell
from .consent_ui import ConfirmScreen, FailurePanel, panel_safe
from .help import HelpKeys

#: The published description's cap when no width is known (a
#: caller outside the screen); the screen computes its own cap
#: from the terminal's width — the description column absorbs
#: what the ref, size, and marker columns leave.
PICKER_CAP = 44

#: The column separator's width.
GAP = 2

#: The description column's floor (0: the column drops when
#: the terminal has no room for one).
DESC_FLOOR = 0

#: The cells the picker's own frame eats before a row's first
#: column: the OptionList's two-cell scrollbar and its two-cell
#: option padding — the screen itself carries no border. A row
#: laid out against the screen's width less this never wraps.
PICKER_FRAME_CELLS = 4


def size_label(size: int | None) -> str:
    """The download size in MiB, or a dash when the manifest row
    carried none (or carried nonsense a strict channel would not
    — a negative size is no size)."""
    if not size or size < 0:
        return "-"
    return f"{size / (1024 * 1024):.0f} MiB"


def capped_description(row: dict, cap: int = PICKER_CAP) -> str:
    """The row's baked identity, capped for the page's line — the
    full line is the detail view's and the manifest's own."""
    description = (row.get("description") or "").strip() or "-"
    if len(description) > cap:
        description = description[: cap - 1] + "…"
    return description


def membership(row: dict, catalog: list[dict]) -> str:
    """The published row's marker against the catalog: an exact
    hash match is ``[imported]``; the same ref under a different
    hash (the pre-channel plain tar, a locally built archive) is
    ``[local]`` — the build you hold lives in the local catalog,
    and the word names where, not how it got there. A rebuild
    must not make the marker lie. Anything else carries no
    marker: the page's default date slot names its build day."""
    hashes = {image["hash"] for image in catalog}
    if row.get("hash") in hashes:
        return "[imported]"
    refs = {f"{image['name']}:{image['version']}" for image in catalog}
    if row.get("ref") in refs:
        return "[local]"
    return ""


def built_label(row: dict) -> str:
    """The published row's build date (the manifest's ``built_at``
    date half) or a dash — v0.1's rows predate the field."""
    built = str(row.get("built_at") or "")[:10]
    return built or "-"


def daemon_machine(health: dict | None) -> str | None:
    """The daemon host's uname machine (#717) — the architecture
    its workspaces boot. None when the document carries no
    machine word at all (a daemon predating the field, a
    non-dict body, a mistyped word — a number where the name
    belongs): the filter steps aside, every row shows, and
    the #638 import refusal stays the boundary. A word that
    names a machine no manifest row declares leaves the
    declared-arch rows hidden — the page trusts the daemon's
    own answer, the same trust every other field of the
    document rides on."""
    machine = health.get("machine") if health else None
    return machine if isinstance(machine, str) and machine else None


def row_serves_arch(row: dict, machine: str | None) -> bool:
    """Whether one published row belongs on this daemon's page
    (#717), by the #638 guard's own rule: a row whose arch is
    falsy — absent, null, empty — declares nothing and shows
    everywhere, and a row with a truthy arch shows on its match
    alone. A mistyped arch never reaches this far:
    :func:`manifest_row` names it as a broken channel first, the
    same refusal every other mistyped field takes."""
    if machine is None:
        return True
    return not row.get("arch") or row["arch"] == machine


def columnar(cells: list[str], widths: list[int]) -> str:
    """Cells into one column-aligned row: every cell pads to its
    column's width, two spaces separate the columns, and the
    line's trailing padding strips. A zero-width column (one
    every row leaves empty) drops its cell entirely."""
    parts = [
        cell.ljust(width) for cell, width in zip(cells, widths) if width > 0
    ]
    return (" " * GAP).join(parts).rstrip()


def imported_moment(row: dict) -> datetime:
    """The row's import stamp as an aware moment; a missing or
    unparsable one sorts as the oldest (the stamp cell renders
    its dash, the order keeps a place for it)."""
    try:
        moment = datetime.fromisoformat(str(row.get("imported") or ""))
    except ValueError:
        return datetime.min.replace(tzinfo=UTC)
    if moment.tzinfo is None:
        return moment.replace(tzinfo=UTC)
    return moment


def sorted_catalog(catalog: list[dict]) -> list[dict]:
    """The catalog group's rows in reading order: ref ascending
    (name, then version), and one ref's rows newest import first
    — the row an operator just imported or refreshed stands at
    the top of its block. Two stable sorts compose the order
    (newest-first, then the ref's ascending pass over it)."""
    newest_first = sorted(catalog, key=imported_moment, reverse=True)
    return sorted(
        newest_first,
        key=lambda row: (row["name"], str(row["version"])),
    )


#: The hash-prefix column's width — the same 12-character
#: prefix `msks image ls` prints, so the page, the CLI, and the
#: ``name@hash`` commands share one spelling of a build.
HASH_PREFIX = 12


def hash_cell(value: object) -> str:
    """A build's hash-prefix cell: the first 12 characters, or a
    dash when the row carried no hash (a v0.1 channel row). The
    column exists on both groups so a published row's prefix is
    matched against the catalog's by eye."""
    text = str(value or "")
    return text[:HASH_PREFIX] if text else "-"


def longest_cells(rows: list[dict], cells) -> list[int]:
    """Each cell function's longest cell across the rows — a
    group's column widths from its cell functions."""
    return [max(map(len, cells_for(rows)), default=0) for cells_for in cells]


def catalog_widths(catalog: list[dict]) -> list[int]:
    """The catalog group's column widths: the ref column takes
    the group's longest ref, the default-marker column collapses
    when no row is the default, and the hash and date columns
    are their fixed cells (the import moment alone — the column's
    position, not a word, says what it is)."""

    def refs(rows):
        return [f"{row['name']}:{row['version']}" for row in rows]

    def tags(rows):
        return ["[default]" if row.get("default") else "" for row in rows]

    def hashes(rows):
        return [hash_cell(row.get("hash")) for row in rows]

    def dates(rows):
        return [imported_cell(row) for row in rows]

    return longest_cells(catalog, [refs, tags, hashes, dates])


def marker_cell(row: dict, catalog: list[dict]) -> str:
    """The published row's last column: the membership marker, or
    the build date when the catalog holds no relation."""
    marker = membership(row, catalog)
    return marker or f"built {built_label(row)}"


def desc_column(published: list[dict], budget: int) -> int:
    """The description column's width against the remaining
    *budget*: the group's longest description caps it, and a
    budget without room drops the column (the identity columns
    never truncate)."""
    longest = max(
        (len((row.get("description") or "").strip()) for row in published),
        default=1,
    )
    return min(max(budget, DESC_FLOOR), longest)


def published_widths(
    published: list[dict], catalog: list[dict], width: int
) -> list[int]:
    """The published group's column widths against *width*: the
    identity columns (ref, size, hash, marker) take their longest
    cells, and the description column absorbs what remains — a
    narrower terminal truncates descriptions, a wider one lets
    them run to their full length."""
    refs = [row["ref"] for row in published]
    sizes = [f"({size_label(row.get('size'))})" for row in published]
    hashes = [hash_cell(row.get("hash")) for row in published]
    markers = [marker_cell(row, catalog) for row in published]
    ref_w = max(map(len, refs), default=0)
    size_w = max(map(len, sizes), default=0)
    hash_w = max(map(len, hashes), default=0)
    marker_w = max(map(len, markers), default=0)
    budget = (
        width
        - ref_w
        - size_w
        - hash_w
        - marker_w
        - GAP * 4
        - PICKER_FRAME_CELLS
    )
    return [ref_w, desc_column(published, budget), size_w, hash_w, marker_w]


def published_label(row: dict, catalog: list[dict], widths: list[int]) -> str:
    """One published row's label as columns: ref, description,
    size, the build's hash prefix, then the membership marker or
    the build date."""
    return columnar(
        [
            row["ref"],
            capped_description(row, widths[1]),
            f"({size_label(row.get('size'))})",
            hash_cell(row.get("hash")),
            marker_cell(row, catalog),
        ],
        widths,
    )


def catalog_label(row: dict, widths: list[int]) -> str:
    """One catalog row's label as columns: ref, the default
    marker, the build's hash prefix, and the import date (#571)
    — the hash cell `msks image ls` renders."""
    return columnar(
        [
            f"{row['name']}:{row['version']}",
            "[default]" if row.get("default") else "",
            hash_cell(row.get("hash")),
            imported_cell(row),
        ],
        widths,
    )


def check_present(row: object) -> None:
    """The row is a mapping carrying the pair the pick needs, or
    it refuses."""
    if not isinstance(row, dict) or not row.get("ref") or not row.get("url"):
        raise ValueError(f"manifest row lacks ref/url: {row!r}")


def check_str_fields(row: dict, fields: tuple[str, ...]) -> None:
    """Every named field is a string, or the row refuses by
    name."""
    for field in fields:
        if not isinstance(row[field], str):
            raise ValueError(
                f"manifest row {row['ref']!r}: {field} must be a string, "
                f"got {type(row[field]).__name__}"
            )


def check_number(row: dict, field: str) -> None:
    """The named field is a whole number (bools are ints in
    Python — a manifest written by hand with ``true`` is not a
    size), or the row refuses by name."""
    size = row[field]
    if not isinstance(size, int) or isinstance(size, bool):
        raise ValueError(
            f"manifest row {row['ref']!r}: {field} must be a number, "
            f"got {type(size).__name__}"
        )


def manifest_row(row: object) -> dict:
    """One manifest row, validated: ``ref`` and ``url`` are the
    str pair the pick needs; ``description``, ``hash``,
    ``built_at``, and ``arch`` (strs) and ``size`` (a number) may
    sit absent — their dash renders — but a mistyped value is a
    broken channel, and it refuses by name instead of crashing
    the page half-mounted (a hand-written manifest is exactly
    the fork/self-hosted case the setting exists for)."""
    check_present(row)
    check_str_fields(row, ("ref", "url"))
    for field in ("description", "hash", "built_at", "arch"):
        check_optional(row, field, check_str)
    check_optional(row, "size", check_number)
    return row


def check_optional(row: dict, field: str, check) -> None:
    """An absent field renders as its dash — an explicit null too
    (a generator's idiom for absent, common in hand-written
    channels) — and a present one takes its check."""
    if row.get(field) is not None:
        check(row, field)


def check_str(row: dict, field: str) -> None:
    """One field is a string, or the row refuses by name."""
    check_str_fields(row, (field,))


def detail_lines(row: dict) -> list[str]:
    """A catalog row's full record as the page shows it: the
    boot facts, the baked identity, the import date, and the
    default designation."""
    kernel = kernel_cell(row)
    return [
        f"ref          {row['name']}:{row['version']}",
        f"hash         {row['hash']}",
        f"kernel       {kernel}",
        f"description  {description_cell(row)}",
        f"imported     {imported_cell(row)}",
        f"default      {'yes' if row.get('default') else 'no'}",
    ]


def kernel_cell(row: dict) -> str:
    """The kernel facts, both halves dashing when absent."""
    return (
        f"{row.get('kernel_version') or '-'} "
        f"({row.get('kernel_format') or '-'})"
    )


def description_cell(row: dict) -> str:
    """The baked identity at full length; a manifest without one
    says so."""
    return (row.get("description") or "").strip() or "- (none baked)"


async def gathered_rows(data, url: str) -> tuple[list[dict], list[dict]]:
    """The two groups' source rows (#565, #717): the daemon's
    catalog and the channel's manifest, the published rows
    already narrowed to the machine the daemon's /health names —
    one helper so the screen's fetch stays a straight line
    (a refusal from any leg lands on the failure panel
    unchanged)."""
    catalog = await data.images()
    machine = daemon_machine(await data.health())
    published = [
        row
        for row in await fetch_manifest(url)
        if row_serves_arch(manifest_row(row), machine)
    ]
    return catalog, published


class ImageDetailScreen(ModalScreen):
    """One catalog row's full record (#571): Enter on a catalog
    row opens it; Enter, q, or Escape dismisses."""

    BINDINGS = [
        Binding("enter", "close", "Close", show=False),
        Binding("q", "close", "Close"),
        Binding("escape", "close", "Close", show=False),
    ]

    def __init__(self, row: dict) -> None:
        super().__init__()
        self.row = row

    def compose(self) -> ComposeResult:
        yield Static(
            "\n".join(detail_lines(self.row)),
            id="image-detail",
            markup=False,
        )

    def action_close(self) -> None:
        self.dismiss()


class ImagesScreen(HelpKeys, Screen):
    """The images page (#565, #571): the catalog and the published
    channel in two labeled groups, a full page with its bindings
    on the footer row like every other page. Enter imports a
    published row (the importing modal returns here, both groups
    refreshing) or opens the detail view for a catalog row;
    ``D`` deletes a catalog row through the confirm screen, and
    ``d`` designates the default; ``q``/Escape closes the page.
    A manifest that cannot be fetched or read names its cause on
    the failure panel — the default channel needs no
    configuration, so its failure is the first an operator
    meets."""

    #: The ``?`` overlay's title (#851): the docs' name for the
    #: page.
    help_title = "images page"

    BINDINGS = [
        Binding("d", "default", "Def"),
        Binding("D", "delete", "Del"),
        Binding("q", "back", "Back"),
        Binding("escape", "back", "Back", show=False),
        Binding("question_mark", "help", "Help", show=False),
    ]

    def __init__(self) -> None:
        super().__init__()
        self.catalog: list[dict] = []
        self.published: list[dict] = []
        #: The rendered rows: (kind, row) parallel to the options.
        self.entries: list[tuple[str, dict | None]] = []
        self._left = False
        self._fetch_task = None
        self._refresh_task = None

    def compose(self) -> ComposeResult:
        yield Static("", id="import-picker-status")
        yield Footer()

    def on_mount(self) -> None:
        self.status("fetching the catalog and the published manifest…")
        self._fetch_task = asyncio.create_task(self.fetch_rows())

    def on_unmount(self) -> None:
        """A closed page leaves nothing behind: the in-flight
        fetch dies here (its later failure must not pop whatever
        screen stands on top — Screen.dismiss pops blindly, and
        the operator's next screen is not this one's to take)."""
        self._left = True
        for task in (self._fetch_task, self._refresh_task):
            if task is not None and not task.done():
                task.cancel()

    def status(self, text: str) -> None:
        self.query_one("#import-picker-status", Static).update(text)

    async def fetch_rows(self) -> None:
        url = (
            getattr(self.app.conf, "image_manifest_url", None)
            or DEFAULT_IMAGE_MANIFEST_URL
        )
        try:
            catalog, published = await gathered_rows(self.app.data, url)
        except asyncio.CancelledError:
            raise
        except (Exception, SystemExit) as exc:
            self.land(exc)
            return
        self.catalog = catalog
        self.published = published
        self.mount_rows()

    def render_rows(self) -> list[str]:
        """Rebuild :attr:`entries` and answer the labels from one
        walk — every caller (mount, refresh) takes both from
        here, never one without the other (a refresh that moved
        the rows moves both together)."""
        built = self.build_rows()
        self.entries = [(kind, row) for kind, row, _label in built]
        return [label for _kind, _row, label in built]

    def build_rows(self) -> list[tuple[str, dict | None, str]]:
        """The page's rows in order: the catalog heading, the
        catalog rows (default marker, import date), a spacer, the
        published heading, the published rows (membership marker
        or build date). One source for the entries and their
        labels — the selection indexes into both. The columns
        derive from the screen's current width, so the groups
        align and the description column contracts and expands
        with the terminal."""
        cat_w = catalog_widths(self.catalog)
        pub_w = published_widths(self.published, self.catalog, self.size.width)
        rows: list[tuple[str, dict | None, str]] = [
            ("heading", None, "In the catalog")
        ]
        rows += [
            ("catalog", entry, catalog_label(entry, cat_w))
            for entry in sorted_catalog(self.catalog)
        ]
        rows.append(("heading", None, ""))
        rows.append(("heading", None, "Published"))
        rows += [
            (
                "published",
                entry,
                published_label(entry, self.catalog, pub_w),
            )
            for entry in self.published
        ]
        return rows

    def mount_rows(self) -> None:
        """Offer the two groups: the catalog first (the default
        marker, the import dates), the published rows under it
        (their membership markers or build dates). The list takes
        its labels as data — markup parsing would eat the
        brackets the markers live in (the row-content rule the
        workspace list follows)."""
        options = OptionList(*self.render_rows(), markup=False)
        options.add_class("import-picker")
        # Before the footer: the page's compose already yielded it,
        # and a bare mount would land the list below the bindings
        # row it belongs above.
        self.mount(options, before=self.query_one(Footer))
        options.highlighted = self.first_actionable()
        options.focus()
        if not self.catalog and not self.published:
            self.status(
                "the catalog and the channel both carry nothing — "
                "import from a channel URL once one is published"
            )
            return
        self.status(
            f"{len(self.catalog)} in the catalog · "
            f"{len(self.published)} published"
        )

    def first_actionable(self) -> int:
        """The first non-heading row's index (a bare Enter must
        act — the mode picker's #465-review rule; an empty catalog
        falls to the first published row, and nothing actionable
        keeps the heading)."""
        return next(
            (
                index
                for index, (kind, _row) in enumerate(self.entries)
                if kind != "heading"
            ),
            0,
        )

    def on_resize(self) -> None:
        """A resized terminal re-lays the columns: the widths
        derive from the width the screen now has, so the groups
        re-align and the description column takes what the new
        width leaves. The operator's place moves with the rows."""
        options_list = self.query(OptionList)
        if not options_list:
            return  # rows not up: the mount lays them out itself
        options = options_list[0]
        self.rerender(options.highlighted or 0)

    def refresh_rows(self) -> None:
        """Re-read the catalog after an import or a delete: the
        published rows' markers move with it. The manifest needs
        no refetch — the channel's rows did not change."""
        self._refresh_task = asyncio.create_task(self.refetch_catalog())

    async def refetch_catalog(self) -> None:
        if self._left:
            return  # a closed page fetches nothing, rebuilds nothing
        try:
            catalog = await self.app.data.images()
        except (Exception, SystemExit) as exc:
            self.land(exc, verb="catalog read")
            return
        if self._left:
            return  # closed while the read was in flight
        self.catalog = catalog
        options = self.query_one(OptionList)
        self.rerender(options.highlighted or 0)

    def rerender(self, prior: int) -> None:
        """Rebuild the list from one walk, restoring the
        operator's place clamped into the new rows (an
        imported-then-highlighted row moved; a deleted one clamps
        back) — and never onto a heading, where Enter would go
        dead."""
        options = self.query_one(OptionList)
        options.clear_options()
        options.add_options(self.render_rows())
        clamped = min(prior, len(self.entries) - 1)
        if self.entries[clamped][0] == "heading":
            clamped = self.first_actionable()
        options.highlighted = clamped

    def land(self, exc: BaseException, verb: str = "manifest fetch") -> None:
        """A fetch or read that failed while this screen still
        stands lands on the failure panel; a screen already left
        takes nothing (its dismissal cancelled the fetch — the
        failure belongs to no one)."""
        if self._left:
            return
        # The flag first: a q queued as the failure lands must
        # find the screen already left, or its pop eats the panel
        # this just pushed.
        self._left = True
        self.dismiss()
        self.app.push_screen(FailurePanel(verb, None, str(exc)))

    def selected_entry(self) -> tuple[str, dict] | None:
        """The highlighted row's entry, or None when the list is
        not up (or a heading holds the highlight)."""
        options = self.query_one(OptionList)
        index = options.highlighted
        if index is None or index >= len(self.entries):
            return None
        kind, row = self.entries[index]
        return (kind, row) if kind != "heading" and row is not None else None

    def on_option_list_option_selected(
        self, event: OptionList.OptionSelected
    ) -> None:
        entry = self.selected_entry()
        if entry is None:
            return
        kind, row = entry
        if kind == "catalog":
            # A plain push (not push_screen_wait, which demands a
            # worker): the page stands under the detail view, and
            # its close returns here with nothing to resume.
            self.app.push_screen(ImageDetailScreen(row))
        elif membership(row, self.catalog) == "[imported]":
            # The exact build is already in the catalog: no import
            # runs (the daemon would download the archive again to
            # verify bytes it already holds), and the flash names
            # the ref so the skip reads as intentional.
            self.status(f"{row['ref']} is already in the catalog")
        else:
            self.start_import(row)

    def start_import(self, row: dict) -> None:
        """Import a published row through the modal; the page
        stays mounted and refreshes when the import lands."""
        self.app.push_screen(ImportingScreen(row, self.refresh_rows))

    def action_delete(self) -> None:
        """``D``: a catalog row deletes through the confirm
        screen — the same DELETE the CLI's image rm makes. The row
        is captured here: the highlight can move while the
        confirm stands."""
        entry = self.selected_entry()
        if entry is None or entry[0] != "catalog":
            self.status("D deletes a catalog row — highlight one")
            return
        row = entry[1]
        ref = f"{row['name']}:{row['version']}"

        async def answered(answer: bool) -> None:
            await self.delete_answered(answer, row)

        self.app.push_screen(ConfirmScreen(f"delete image {ref}?", answered))

    async def action_default(self) -> None:
        """``d``: designate the highlighted catalog row the image
        a bare create boots — the same POST the CLI's ``image
        default`` makes, keyed by the row's full hash. The row
        that already holds the designation flashes so; a refusal
        lands on the failure panel. The refresh moves the
        ``[default]`` tag with the answer."""
        entry = self.selected_entry()
        if entry is None or entry[0] != "catalog":
            self.status("d sets the default — highlight a catalog row")
            return
        row = entry[1]
        ref = f"{row['name']}:{row['version']}"
        if row.get("default"):
            self.status(f"{ref} is already the default")
            return
        try:
            await self.app.data.set_default_image(row["hash"])
        except (Exception, SystemExit) as exc:
            self.app.push_screen(
                FailurePanel("default", row.get("hash"), str(exc))
            )
            return
        self.app.flash(f"designated {ref} the default image")
        self.refresh_rows()

    async def delete_answered(self, answer: bool, row: dict) -> None:
        """The confirm's callback: True deletes by hash and
        refreshes; a refusal lands on the failure panel."""
        if not answer:
            return
        try:
            await self.app.data.remove_image(row["hash"])
        except (Exception, SystemExit) as exc:
            self.app.push_screen(
                FailurePanel("delete", row.get("hash"), str(exc))
            )
            return
        self.app.flash(f"deleted {row['name']}:{row['version']}")
        self.refresh_rows()

    def action_back(self) -> None:
        """``q``/Escape close the page — an import happens only
        through its own modal, so a close carries no callback."""
        if not self._left:
            self._left = True
            self.dismiss()


class ImportingScreen(ModalScreen):
    """The import in progress: the chosen ref, an elapsed timer,
    and the wait for the daemon's answer — the archive is
    hundreds of MiB and the operator deserves to see it moving.
    A refusal lands on the failure panel with the daemon's
    reason; a success flashes and the catalog (and the create
    form's image select) carries the image from there."""

    BINDINGS = [
        Binding("q", "cancel", "Cancel", show=False),
        Binding("escape", "cancel", "Cancel", show=False),
    ]

    def __init__(self, row: dict, done) -> None:
        super().__init__()
        self.row = row
        self.done = done
        self.started = time.monotonic()
        self._task = None
        self._left = False

    def compose(self) -> ComposeResult:
        yield Static(self.label(), id="importing-status")

    def label(self) -> str:
        elapsed = int(time.monotonic() - self.started)
        return (
            f"importing {panel_safe(self.row['ref'])} — the daemon "
            f"downloads and verifies the archive… {elapsed}s"
        )

    def on_mount(self) -> None:
        # The timer is stopped at every exit (a repeating interval
        # holds the message pump busy, and a second action on this
        # modal would wait on it).
        self._timer = self.set_interval(1.0, self.tick)
        self._task = asyncio.create_task(self.run_import())

    def stop_timer(self) -> None:
        """Stop the elapsed timer — every path out of the modal
        takes it, so the screen leaves nothing running."""
        self._timer.stop()

    def tick(self) -> None:
        self.query_one("#importing-status", Static).update(self.label())

    def action_cancel(self) -> None:
        """Close the modal without stopping the import: the POST
        the daemon runs cannot be cancelled, and the catalog (and
        the create form) carries the image when it lands."""
        self.stop_timer()
        self.leave()

    def leave(self) -> None:
        """Dismiss once: a cancelled modal is already leaving (its
        removal is queued — is_mounted still reads true), and the
        import's own finish must not pop a second time."""
        if not self._left:
            self._left = True
            self.dismiss()

    async def run_import(self) -> None:
        try:
            record = await self.app.data.import_image(self.row["url"])
        except (Exception, SystemExit) as exc:
            self.stop_timer()
            self.leave()
            self.app.push_screen(
                FailurePanel("import", self.row["ref"], str(exc))
            )
            return
        self.stop_timer()
        self.leave()
        self.app.flash(
            f"imported {record['ref']} ({record['hash'][:12]}) — "
            "the create form's image list carries it"
        )
        # The done-callback is a plain refresh (sync): the page's
        # rows re-query on the next tick either way.
        self.done()
