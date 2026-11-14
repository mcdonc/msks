"""The secrets screen: the placeholder list and its actions.

The workspace's minted placeholders with coverage, ttl, and the
renew/revoke/new actions, beside the sentinel panel it pushes.
"""

from __future__ import annotations

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.css.query import NoMatches
from textual.events import Resize
from textual.screen import Screen
from textual.widgets import Footer, ListItem, ListView, Static

from ..detail import status_content
from ..help import HelpKeys
from ..link import AuditLink
from ..unlock import PassphraseScreen, unlock_flash, unlock_flow
from .audit import SecretAuditScreen
from .cells import (
    SECRET_COLUMNS,
    SECRET_GROWING_LABELS,
    SECRET_TTL_SECONDS,
    SECRET_TTLS,
    coverage_text,
    minted_note,
    renew_note,
    revoke_note,
    secret_row_text,
    ttl_default,
)
from .mint import MintScreen
from .sentinel import SentinelPanel
from .ui import (
    LIST_FRAME_CELLS,
    ConfirmScreen,
    DurationScreen,
    FlashLine,
    clock_now,
    expanded_columns,
    flash_safe,
    focus_attr,
    focused_attr,
    list_header,
)


class SecretsScreen(HelpKeys, Screen):
    """The secrets page (#390): every placeholder row the daemon
    holds — coverage, name, destinations, a live TTL countdown,
    the created date — with revoke and renew on the focused row,
    the audit stream one key away, and the mint form on `c` (#393
    — its sentinel panel replaces
    the form). The coverage flip stays on the workspace page (a
    picker beside the egress-mode row); Enter or a click on a row
    shows its sentinel on the panel (#440) — the placeholder-to-
    workspace links land with the cross-references (#394).
    """

    #: The ``?`` overlay's title (#851): the docs' name for the
    #: page.
    help_title = "secrets page"

    BINDINGS = [
        Binding("x", "revoke", "Revoke"),
        Binding("r", "renew", "Renew"),
        Binding("e", "audit", "Audit"),
        Binding("c", "create", "New"),
        Binding("u", "unlock", "Unlock"),
        Binding("q", "back", "Back"),
        Binding("escape", "back", "Back", show=False),
        Binding("question_mark", "help", "Help", show=False),
    ]

    def __init__(self) -> None:
        super().__init__()
        self.rows: list[dict] = []
        # The page's columns at the terminal's width: the fixed
        # minimums until the first resize event lands, then the
        # widened shape (#347's rule) — the header and every row
        # render through this one tuple, so their offsets stay
        # shared.
        self.columns = SECRET_COLUMNS
        self.flash_line = FlashLine()
        self.shown_once = False

    def compose(self) -> ComposeResult:
        yield Static(id="status")
        with Vertical(id="secrets-listing"):
            yield Static(list_header(self.columns), id="secret-columns")
            yield Static(id="secret-empty")
        yield Footer()

    def on_mount(self) -> None:
        self.set_interval(1.0, self.tick)
        # call_after_refresh: the first rebuild waits for the
        # screen's compose stream to settle — the main screen's
        # own rule.
        self.call_after_refresh(self.refresh_rows)

    def on_screen_resume(self) -> None:
        """Refresh on every return to the page (rows may have moved
        while the audit view stood above, or from another surface);
        the first resume rides on_mount's own refresh."""
        if self.shown_once:
            self.refresh_rows()
        self.shown_once = True

    def on_resize(self, event: Resize) -> None:
        """The terminal's width moved: the page's columns re-width
        to the frame (#347's rule over #390's columns) — the
        coverage, name, and destinations columns share the cells a
        wider terminal brings, and the header and rows repaint
        together so their offsets stay shared. A resize that
        leaves the widths where they were (a height-only move, a
        terminal under the fixed span) repaints nothing."""
        columns = expanded_columns(
            event.size.width - LIST_FRAME_CELLS,
            base=SECRET_COLUMNS,
            grow=SECRET_GROWING_LABELS,
        )
        if columns == self.columns:
            return
        self.columns = columns
        try:
            self.query_one("#secret-columns", Static).update(
                list_header(columns)
            )
        except NoMatches:
            # Compose has not built the page yet: the first rows
            # build at these widths when they land.
            return
        self.sync_rows()

    def tick(self) -> None:
        """The per-second repaint: the TTL cells' live countdown and
        the status line — the rows themselves refresh on mount, on
        resume, and after an action (the listing's own rule, not
        the workspace page's per-second re-read: the countdown is
        paint-time math over the cached rows)."""
        self.sync_rows()
        self.sync_status()

    # -- the listing -------------------------------------------------------

    def refresh_rows(self) -> None:
        """Reload the listing; one flight at a time."""
        self.run_worker(self.load_rows, exclusive=True)

    async def load_rows(self) -> None:
        try:
            rows = await self.app.data.secrets()
        except (Exception, SystemExit) as exc:
            self.flash(f"listing failed: {flash_safe(str(exc))}")
            return
        await self.rebuild_rows(rows)

    async def rebuild_rows(self, rows: list[dict]) -> None:
        """Swap in a freshly-built list (its mount awaited),
        preserving the focused placeholder by id (the top when it
        left) — the consent queue's rebuild rule, carried to the
        listing."""
        self.rows = rows
        listing = self.query_one("#secrets-listing", Vertical)
        old = self.rows_widget()
        focused = focused_attr(old, "secret_id")
        items = [self.row_item(row) for row in rows]
        fresh = ListView(*items, id="secret-rows")
        if old is not None:
            await old.remove()  # frees the id before the fresh list mounts
        await listing.mount(fresh)
        fresh.focus()
        focus_attr(fresh, "secret_id", focused)
        self.sync_status()

    def row_item(self, row: dict) -> ListItem:
        """One placeholder row at the terminal's column widths,
        tagged with the row's id."""
        item = ListItem(Static(secret_row_text(row, columns=self.columns)))
        item.secret_id = row["id"]
        return item

    def rows_widget(self) -> ListView | None:
        """The rows list, or None during a rebuild's swap window."""
        try:
            return self.query_one("#secret-rows", ListView)
        except NoMatches:
            return None

    def sync_rows(self) -> None:
        """Repaint the standing rows' TTL cells in place — the
        countdown moves, the membership does not (a refresh swaps
        the whole list when the daemon's rows moved)."""
        rows = self.rows_widget()
        if rows is None:
            return
        now = clock_now()
        for child, row in zip(rows.children, self.rows):
            try:
                child.query_one(Static).update(
                    secret_row_text(row, now, columns=self.columns)
                )
            except NoMatches:
                pass  # teardown unmounted this row's Static

    def sync_status(self) -> None:
        """The status line: the placeholder count and the daemon's
        URL (the listing's own shape); a flash owns the line until
        its TTL lapses; the empty state names the page's own
        absence — nothing minted. A teardown race leaves the
        queries empty — noise, not a crash."""
        try:
            count = len(self.rows)
            default = status_content(count, self.app.url, noun="placeholder")
            self.query_one("#status", Static).update(
                self.flash_line.text(default)
            )
            columns = self.query_one("#secret-columns", Static)
            columns.display = bool(self.rows)
            empty = self.query_one("#secret-empty", Static)
            empty.display = not self.rows
            empty.update("No placeholders — c mints one.")
        except NoMatches:
            pass

    def flash(self, message: str) -> None:
        """Give the page's status line to a message for FLASH_TTL
        seconds — the page's own surface: the app-level flash
        paints the list's status line, which the pushed page hides
        (#343)."""
        self.flash_line.set(message)
        self.sync_status()

    async def guarded(self, label: str, work):
        """Await one page action, flashing the failure on this
        page's status line — the app-level guard paints the list's
        status line, which the pushed page hides (#343)."""
        try:
            return await work
        except (Exception, SystemExit) as exc:
            self.flash(f"{label} failed: {flash_safe(str(exc))}")
            return None

    # -- the focused row into actions ---------------------------------------

    def focused_row(self) -> dict | None:
        """The focused row's dict, or None when nothing is focused
        (an empty listing, or a rebuild's swap window)."""
        secret_id = focused_attr(self.rows_widget(), "secret_id")
        if secret_id is None:
            return None
        return next((row for row in self.rows if row["id"] == secret_id), None)

    def action_unlock(self) -> None:
        """Run the session unlock now (#515): the configured
        identity file's private half against the daemon's locked
        rows — the same flow the tree ran at start, offered here
        so a cold row heals the moment it is seen (an unlock
        window can lapse mid-session). The run is a worker in
        its own group — Textual's wait_for_dismiss (the
        passphrase modal's answer) requires a worker, and the
        page's own exclusive refreshes (the modal's pop resumes
        the screen and fires one) must not cancel the upload
        mid-POST."""
        self.run_worker(self.run_unlock(), group="unlock", exclusive=True)

    async def run_unlock(self) -> None:
        """The action's awaited half: the outcome flashes on this
        page's status line and the rows repaint their state."""
        message = await self.guarded(
            "unlock",
            unlock_flow(self.app.data, self.ask_passphrase, explicit=True),
        )
        if message is not None:
            self.flash(unlock_flash(message))
            self.refresh_rows()

    async def ask_passphrase(self, path) -> str | None:
        """The one prompt per session (#515): the masked modal
        over the page, answered by Enter with the typed
        passphrase or cancelled with Escape."""
        answer = await self.app.push_screen(
            PassphraseScreen(path), wait_for_dismiss=True
        )
        return answer or None

    def action_revoke(self) -> None:
        """Ask first (#390: a revoke retires the row everywhere at
        once), then delete through the data seam."""
        row = self.focused_row()
        if row is None:
            self.flash("no placeholder focused")
            return
        question = (
            f"revoke {coverage_text(row)}/{row['name']}? the row "
            "retires everywhere at once"
        )
        self.app.push_screen(
            ConfirmScreen(question, self.revoke_answered(row))
        )

    def revoke_answered(self, row: dict):
        """The confirmation's callback: a yes deletes, a no decides
        nothing."""

        async def answered(yes: bool) -> None:
            if not yes:
                return
            reply = await self.guarded(
                "revoke", self.app.data.revoke_secret(row["id"])
            )
            if reply is not None:
                self.flash(revoke_note(row, reply))
                self.refresh_rows()

        return answered

    def action_renew(self) -> None:
        """Open the duration picker over TTL-appropriate choices
        (#390); the picked duration extends the row's lifetime in
        place — the sentinel and the row's identity stay as they
        are."""
        row = self.focused_row()
        if row is None:
            self.flash("no placeholder focused")
            return
        self.app.push_screen(
            DurationScreen(
                self.renew_picked(row),
                choices=SECRET_TTLS,
                default=ttl_default(row),
            )
        )

    def renew_picked(self, row: dict):
        """The picker's callback: a pick renews through the data
        seam, a cancel decides nothing."""

        async def picked(duration: str | None) -> None:
            if duration is None:
                return
            reply = await self.guarded(
                "renew",
                self.app.data.renew_secret(
                    row["id"], SECRET_TTL_SECONDS[duration]
                ),
            )
            if reply is not None:
                self.flash(renew_note(row, reply))
                self.refresh_rows()

        return picked

    def on_list_view_selected(self, event: ListView.Selected) -> None:
        """Enter (or a click) on a row (#440): fetch the row's
        sentinel and show it on the panel — the sentinel reads
        back on demand, not only at mint. The placeholder-to-
        workspace navigation lands with the cross-references
        (#394)."""
        secret_id = getattr(event.item, "secret_id", None)
        row = next((r for r in self.rows if r["id"] == secret_id), None)
        if row is None:
            return  # a rebuild's swap window: the row is already gone
        self.run_worker(self.show_sentinel(row), exclusive=True)

    async def show_sentinel(self, row: dict) -> None:
        """Fetch the row with its sentinel (#440) and open the
        panel over it; a refusal names itself on the page's
        status line."""
        fresh = await self.guarded("sentinel", self.app.data.secret(row["id"]))
        if fresh is not None:
            self.app.push_screen(SentinelPanel(fresh))

    def action_audit(self) -> None:
        """Push the daemon-wide audit view (#390)."""
        self.app.push_screen(
            SecretAuditScreen(self.app.data, link_factory=self.audit_link)
        )

    def audit_link(self) -> AuditLink:
        """The audit view's connection — the tests' seam for a
        scripted one."""
        return AuditLink()

    def action_back(self) -> None:
        """Return to the workspaces list."""
        self.app.pop_screen()

    def action_create(self) -> None:
        """`c`: the mint form (#393) — the create form's pattern
        over the mint's own fields."""
        self.app.push_screen(MintScreen(self.minted))

    async def minted(self, row: dict | None) -> None:
        """The mint form's callback (#393): a mint that lands
        dismisses with its reply — the page refreshes so the row
        stands on the list, and the sentinel panel
        replaces the form; a cancel decides nothing."""
        if row is None:
            return
        self.refresh_rows()
        self.flash(minted_note(row))
        self.app.push_screen(SentinelPanel(row))


#: The audit view's kind filter choices (#390): every kind a row
