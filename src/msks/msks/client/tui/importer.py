"""The TUI's image page (#565, #571): the catalog and the
published channel on one screen.

Two labeled groups — *In the catalog* (the daemon's rows, the
default marker, each import's date) and *Published* (the channel
manifest's rows with their descriptions, sizes, and build dates)
— with membership markers that survive rebuilds: a channel
import's catalog hash equals the manifest row's hash
(``[imported]``), while the same ref under a different hash
(the pre-channel plain tar, a locally built archive) reads
``[in catalog, other build]``. Enter imports a published row
(the daemon downloads, verifies, and floor-checks; the modal
holds the terminal with an elapsed timer) and opens the detail
view for a catalog row; ``D`` deletes a catalog row through the
confirm screen — the same DELETE the CLI's ``image rm`` makes,
refusals surfaced on the failure panel.
"""

import asyncio
import time

from textual.app import ComposeResult
from textual.binding import Binding
from textual.screen import ModalScreen
from textual.widgets import OptionList, Static

from ..config import DEFAULT_IMAGE_MANIFEST_URL
from ..images import fetch_manifest, imported_cell
from .consent_ui import ConfirmScreen, FailurePanel, panel_safe

#: The published description's cap for the row (the full line is
#: the detail view's and the manifest's own).
PICKER_CAP = 44


def size_label(size: int | None) -> str:
    """The download size in MiB, or a dash when the manifest row
    carried none (or carried nonsense a strict channel would not
    — a negative size is no size)."""
    if not size or size < 0:
        return "-"
    return f"{size / (1024 * 1024):.0f} MiB"


def capped_description(row: dict) -> str:
    """The row's baked identity, capped for the page's line — the
    full line is the detail view's and the manifest's own."""
    description = (row.get("description") or "").strip() or "-"
    if len(description) > PICKER_CAP:
        description = description[: PICKER_CAP - 1] + "…"
    return description


def membership(row: dict, catalog: list[dict]) -> str:
    """The published row's marker against the catalog: an exact
    hash match is ``[imported]``; the same ref under a different
    hash (the pre-channel plain tar, a locally built archive) is
    ``[in catalog, other build]`` — a rebuild must not make the
    marker lie. Anything else carries no marker: the page's
    default date slot names its build day."""
    hashes = {image["hash"] for image in catalog}
    if row.get("hash") in hashes:
        return "[imported]"
    refs = {f"{image['name']}:{image['version']}" for image in catalog}
    if row.get("ref") in refs:
        return "[in catalog, other build]"
    return ""


def built_label(row: dict) -> str:
    """The published row's build date (the manifest's ``built_at``
    date half) or a dash — v0.1's rows predate the field."""
    built = str(row.get("built_at") or "")[:10]
    return built or "-"


def published_label(row: dict, catalog: list[dict]) -> str:
    """One published row's label: ref, capped description, size,
    then the membership marker or the build date."""
    label = (
        f"{row['ref']}  —  {capped_description(row)}  "
        f"({size_label(row.get('size'))})"
    )
    marker = membership(row, catalog)
    if marker:
        return f"{label}  {marker}"
    return f"{label}  built {built_label(row)}"


def catalog_label(row: dict) -> str:
    """One catalog row's label: ref, the default marker, and the
    import date (#571) — the cell `msks image ls` renders."""
    default = "[default]" if row.get("default") else ""
    return f"{row['name']}:{row['version']}  {default}".rstrip() + (
        f"  imported {imported_cell(row)}"
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
    str pair the pick needs; ``description``, ``hash``, and
    ``built_at`` (strs) and ``size`` (a number) may sit absent —
    their dash renders — but a mistyped value is a broken
    channel, and it refuses by name instead of crashing the page
    half-mounted (a hand-written manifest is exactly the
    fork/self-hosted case the setting exists for)."""
    check_present(row)
    check_str_fields(row, ("ref", "url"))
    for field in ("description", "hash", "built_at"):
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


class ImageImportScreen(ModalScreen):
    """The image page (#565, #571): the catalog and the published
    channel in two labeled groups. Enter imports a published row
    (the importing modal returns here, both groups refreshing) or
    opens the detail view for a catalog row; ``D`` deletes a
    catalog row through the confirm screen; ``q``/Escape closes
    the page. A manifest that cannot be fetched or read names its
    cause on the failure panel — the default channel needs no
    configuration, so its failure is the first an operator
    meets."""

    BINDINGS = [
        Binding("q", "cancel", "Close", show=False),
        Binding("escape", "cancel", "Close", show=False),
        Binding("D", "delete", "Del", show=False),
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
            catalog = await self.app.data.images()
            published = [
                manifest_row(row) for row in await fetch_manifest(url)
            ]
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
        labels — the selection indexes into both."""
        rows: list[tuple[str, dict | None, str]] = [
            ("heading", None, "In the catalog")
        ]
        rows += [
            ("catalog", entry, catalog_label(entry)) for entry in self.catalog
        ]
        rows.append(("heading", None, ""))
        rows.append(("heading", None, "Published"))
        rows += [
            ("published", entry, published_label(entry, self.catalog))
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
        self.mount(options)
        options.highlighted = self.first_actionable()
        options.focus()
        if not self.catalog and not self.published:
            self.status(
                "the catalog and the channel both carry nothing — "
                "import from a channel URL once one is published"
            )
            return
        self.status(
            "Enter imports a published image or opens a catalog "
            "row's detail; D deletes a catalog row; q closes"
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

    def action_cancel(self) -> None:
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
