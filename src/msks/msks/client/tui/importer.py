"""The TUI's published-image import (#565): the manifest picker
and the import-in-progress modal.

The client fetches the channel's ``images.json`` itself — a small
public document, no daemon involvement — and offers the rows with
their human identity (#564's baked descriptions) beside their
download size, so "which image is which" is answered in the
picker, before anyone commits to the bytes. The pick POSTs the
row's URL to the daemon's import endpoint: the daemon fetches,
hash-verifies, and floor-checks the archive; the modal holds the
terminal with an honest elapsed time while it does.
"""

import asyncio
import time

from textual.app import ComposeResult
from textual.binding import Binding
from textual.screen import ModalScreen
from textual.widgets import OptionList, Static

from ..config import DEFAULT_IMAGE_MANIFEST_URL
from ..images import fetch_manifest
from .consent_ui import FailurePanel, panel_safe

#: One picker row's label: the ref, the capped description, and
#: the download size — the fields the manifest carries (#564).
PICKER_CAP = 44


def size_label(size: int | None) -> str:
    """The download size in MiB, or a dash when the manifest row
    carried none (or carried nonsense a strict channel would not
    — a negative size is no size)."""
    if not size or size < 0:
        return "-"
    return f"{size / (1024 * 1024):.0f} MiB"


def capped_description(row: dict) -> str:
    """The row's baked identity, capped for the picker's line —
    the full line is the manifest's own."""
    description = (row.get("description") or "").strip() or "-"
    if len(description) > PICKER_CAP:
        description = description[: PICKER_CAP - 1] + "…"
    return description


def picker_label(row: dict, dedupe: bool = False) -> str:
    """One manifest row's picker label: ref, description (capped
    — the full line is the manifest's own), and size. *dedupe*
    appends the archive's short hash: two rows sharing a ref (a
    rebuilt archive) are otherwise the same label, and the second
    would be unselectable."""
    label = (
        f"{row['ref']}  —  {capped_description(row)}  "
        f"({size_label(row.get('size'))})"
    )
    if dedupe and row.get("hash"):
        label += f"  [{row['hash'][:12]}]"
    return label


def manifest_row(row: object) -> dict:
    """One manifest row, validated: ``ref`` and ``url`` are the
    str pair the pick needs; ``description`` (a str) and ``size``
    (a number) may sit absent — their dash renders — but a
    mistyped value is a broken channel, and it refuses by name
    instead of crashing the picker half-mounted (a hand-written
    manifest is exactly the fork/self-hosted case the setting
    exists for)."""
    check_present(row)
    check_str_fields(row, ("ref", "url"))
    check_optional(row, "description", check_str)
    check_optional(row, "size", check_size_field)
    return row


def check_str(row: dict, field: str) -> None:
    """One field is a string, or the row refuses by name."""
    check_str_fields(row, (field,))


def check_present(row: object) -> None:
    """The row is a mapping carrying the pair the pick needs, or
    it refuses."""
    if not isinstance(row, dict) or not row.get("ref") or not row.get("url"):
        raise ValueError(f"manifest row lacks ref/url: {row!r}")


def check_optional(row: dict, field: str, check) -> None:
    """An absent field renders as its dash; a present one takes
    its check."""
    if field in row:
        check(row, field)


def check_size_field(row: dict, field: str) -> None:
    """The size check in the optional-field shape."""
    check_size(row)


def check_str_fields(row: dict, fields: tuple[str, ...]) -> None:
    """Every named field is a string, or the row refuses by
    name."""
    for field in fields:
        if not isinstance(row[field], str):
            raise ValueError(
                f"manifest row {row['ref']!r}: {field} must be a string, "
                f"got {type(row[field]).__name__}"
            )


def check_size(row: dict) -> None:
    """The size field is a whole number (bools are ints in
    Python — a manifest written by hand with ``true`` is not a
    size), or the row refuses by name."""
    size = row["size"]
    if not isinstance(size, int) or isinstance(size, bool):
        raise ValueError(
            f"manifest row {row['ref']!r}: size must be a number, "
            f"got {type(size).__name__}"
        )


class ImageImportScreen(ModalScreen[dict | None]):
    """The manifest picker: rows from the channel's manifest,
    Enter choosing one (its row dict rides the callback), ``q``/
    Escape cancelling. A manifest that cannot be fetched or read
    names its cause in the failure panel — the default channel
    needs no configuration, so its failure is the first an
    operator meets."""

    BINDINGS = [
        Binding("q", "cancel", "Cancel", show=False),
        Binding("escape", "cancel", "Cancel", show=False),
    ]

    def __init__(self, picked) -> None:
        super().__init__()
        self.picked = picked
        self.rows: list[dict] = []
        self._left = False
        self._fetch_task = None

    def compose(self) -> ComposeResult:
        yield Static("", id="import-picker-status")

    def on_mount(self) -> None:
        self.status("fetching the published image manifest…")
        self._fetch_task = asyncio.create_task(self.fetch_rows())

    def on_unmount(self) -> None:
        """A cancelled picker leaves nothing behind: the in-flight
        fetch dies here (its later failure must not pop whatever
        screen stands on top — Screen.dismiss pops blindly, and
        the operator's next screen is not this one's to take)."""
        self._left = True
        if self._fetch_task is not None and not self._fetch_task.done():
            self._fetch_task.cancel()

    def status(self, text: str) -> None:
        self.query_one("#import-picker-status", Static).update(text)

    async def fetch_rows(self) -> None:
        url = (
            getattr(self.app.conf, "image_manifest_url", None)
            or DEFAULT_IMAGE_MANIFEST_URL
        )
        try:
            rows = await fetch_manifest(url)
            self.read_rows(rows)
        except asyncio.CancelledError:
            raise
        except (Exception, SystemExit) as exc:
            self.land(exc)

    def read_rows(self, rows: list) -> None:
        """Validate the fetched rows into the picker's list and
        mount them; an empty manifest reads as its own refusal."""
        self.rows = [manifest_row(row) for row in rows]
        if not self.rows:
            raise SystemExit("the manifest carries no images")
        if not self._left:
            self.mount_rows()

    def land(self, exc: BaseException) -> None:
        """A fetch or read that failed while this screen still
        stands lands on the failure panel; a screen already left
        takes nothing (its dismissal cancelled the fetch — the
        failure belongs to no one)."""
        if self._left:
            return
        self.dismiss()
        self.app.push_screen(FailurePanel("manifest fetch", None, str(exc)))

    def mount_rows(self) -> None:
        """Offer the fetched rows: the picker's list, the first
        row highlighted (a bare Enter must act — the mode
        picker's #465-review rule), and the one-line hint. A ref
        the manifest carries twice (a rebuilt archive) takes the
        hash tail, so every row stays its own selectable label."""
        seen: set[str] = set()
        labels: list[str] = []
        for row in self.rows:
            duplicate = row["ref"] in seen
            seen.add(row["ref"])
            labels.append(picker_label(row, dedupe=duplicate))
        options = OptionList(*labels)
        options.add_class("import-picker")
        self.mount(options)
        options.highlighted = 0
        options.focus()
        self.status(
            "Enter imports the highlighted image; q cancels — the "
            "daemon downloads and verifies the archive itself"
        )

    def on_option_list_option_selected(
        self, event: OptionList.OptionSelected
    ) -> None:
        # The prompt names its row (an index read back from the
        # widget races a rebuild); the pick rides the label.
        label = str(event.option.prompt)
        for row in self.rows:
            if (
                picker_label(row) == label
                or picker_label(row, dedupe=True) == label
            ):
                self.dismiss_with(row)
                return

    def dismiss_with(self, row: dict | None) -> None:
        if self._left:
            return
        self._left = True
        self.dismiss()
        self._pick_task = asyncio.create_task(self.picked(row))

    def action_cancel(self) -> None:
        self.dismiss_with(None)


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
        # The done-callback is a plain refresh (sync): the list's
        # rows re-query on the next tick either way.
        self.done()
