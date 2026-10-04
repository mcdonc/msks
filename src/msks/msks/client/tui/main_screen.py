"""The tree's root screen (#309): the workspaces listing —
extracted from the app shell so :mod:`msks.client.tui.main_app`
composes it. Create, start, stop, and remove happen here; Enter
opens the focused workspace's page, and ``e`` opens the secrets
page (#431).

Spatial navigation: arrows walk the rows in reading order, the
page keys act on the focused row, and ``q`` or Escape quits the
tree — no trap.
"""

from rich.markup import escape
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.css.query import NoMatches
from textual.screen import Screen
from textual.widgets import Footer, ListItem, ListView, Static

from .consent_ui import (
    ConfirmScreen,
    FailurePanel,
    flash_safe,
    focus_attr,
    focused_attr,
)
from .forms import CreateScreen
from .rows import (
    list_header,
    row_content,
    status_class,
    status_content,
    workspace_label,
)
from .secrets import SecretsScreen
from .workspace import WorkspaceScreen


def created_note(row: dict, path) -> str:
    """The create's flash: the created line, plus the private
    half's path when one was written."""
    note = f"created {escape(workspace_label(row))} (id {row['id']})"
    if path is not None:
        note += f" · identity {path}"
    return note


async def guarded_flash(app, label: str, work):
    """Await one screen action, flashing the failure instead of
    tearing the TUI down (SystemExit included — the REST seam's
    error surface, the daemon's named refusal among them). The
    refusal text is escaped for a status-line flash (``flash_safe``
    — the daemon echoes operator-typed references, a workspace
    name among them, back, and one carrying rich markup — a
    truncated closing tag included — would otherwise crash the
    screen)."""
    try:
        return await work
    except (Exception, SystemExit) as exc:
        app.flash(f"{label} failed: {flash_safe(str(exc))}")
        return None


class MainScreen(Screen):
    """The tree's root (#309): every workspace one row; create,
    start, stop, and remove happen here; Enter opens the
    workspace's page, ``e`` the secrets page (#431)."""

    BINDINGS = [
        Binding("enter", "open", "Open", show=False),
        Binding("c", "create", "New"),
        Binding("s", "start", "Start"),
        Binding("x", "stop", "Stop"),
        Binding("D", "remove", "Remove"),
        Binding("e", "secrets", "Secrets"),
        Binding("r", "refresh", "Refresh"),
        Binding("q", "quit", "Quit"),
        Binding("escape", "quit", show=False),
    ]

    def __init__(self) -> None:
        super().__init__()
        self.rows: list[dict] = []
        self.shown_once = False

    def compose(self) -> ComposeResult:
        yield Static(id="status")
        with Vertical(id="listing"):
            yield Static(list_header(), id="columns")
            yield Static(id="empty")
        yield Footer()

    def on_mount(self) -> None:
        self.set_interval(0.5, self.sync_status)
        # call_after_refresh: the first rebuild waits for the
        # screen's compose stream to settle — a worker racing it
        # queries widgets that are not mounted yet.
        self.call_after_refresh(self.refresh_rows)

    def on_screen_resume(self) -> None:
        """Refresh on every return to the list (a page's actions
        changed statuses); the first resume rides on_mount's own
        refresh (on_show fires only when a screen is first pushed —
        the resume event is what a pop back down delivers)."""
        if self.shown_once:
            self.refresh_rows()
        self.shown_once = True

    # -- the listing -------------------------------------------------------

    def refresh_rows(self) -> None:
        """Reload the listing; one flight at a time."""
        self.run_worker(self.load_rows, exclusive=True)

    async def load_rows(self) -> None:
        try:
            rows = await self.app.data.workspaces()
        except (Exception, SystemExit) as exc:
            self.app.flash(f"listing failed: {flash_safe(str(exc))}")
            return
        await self.rebuild_rows(rows)

    async def rebuild_rows(self, rows: list[dict]) -> None:
        """Swap in a freshly-built list (its mount awaited),
        preserving the focused row by key (the top when it left) —
        the consent queue's rebuild rule, carried to the listing."""
        self.rows = rows
        listing = self.query_one("#listing", Vertical)
        old = self.rows_widget()
        focused = focused_attr(old, "row_key")
        items = [self.row_item(row) for row in rows]
        fresh = ListView(*items, id="rows")
        if old is not None:
            await old.remove()  # frees the id before the fresh list mounts
        await listing.mount(fresh)
        fresh.focus()
        focus_attr(fresh, "row_key", focused)
        self.sync_status()

    def row_item(self, row: dict) -> ListItem:
        """One listing row, tagged with the workspace's id and a
        class from its status (#348)."""
        item = ListItem(Static(row_content(row, self.app.theme_variables)))
        item.workspace_id = row["id"]
        item.row_key = ("workspace", row["id"])
        item.add_class(status_class(row["status"]))
        return item

    def rows_widget(self) -> ListView | None:
        """The listing list, or None during a rebuild's swap
        window."""
        try:
            return self.query_one("#rows", ListView)
        except NoMatches:
            return None

    def sync_status(self) -> None:
        """The status line (#349): the workspace count in the bold
        default foreground with the daemon's URL in muted text
        after it; a flash owns the line until its TTL lapses. The
        bar stands one row whatever the flash carries (#359): the
        #status CSS crops a long message at the terminal's edge,
        an ellipsis marking the cut, so the listing below holds
        its place. The
        listing's header row shows while rows stand; the empty
        state takes its place when they do not. A screen going
        away under the timer or a worker leaves the query empty —
        teardown noise, not a crash."""
        try:
            count = len(self.rows)
            default = status_content(count, self.app.url)
            text = self.app.flash_line.text(default)
            self.query_one("#status", Static).update(text)
            # The header row owns the listing's top; the empty
            # state takes its place when the last row leaves.
            columns = self.query_one("#columns", Static)
            columns.display = bool(self.rows)
            empty = self.query_one("#empty", Static)
            empty.display = not self.rows
            empty.update("No workspaces — c creates one.")
        except NoMatches:
            pass

    # -- the focused row into actions ---------------------------------------

    def focused_row(self) -> dict | None:
        """The focused listing row's dict, or None when nothing is
        focused (an empty listing, or a rebuild's swap window)."""
        ws_id = focused_attr(self.rows_widget(), "workspace_id")
        if ws_id is None:
            return None
        return next((row for row in self.rows if row["id"] == ws_id), None)

    def action_open(self) -> None:
        """Enter: the focused workspace's page."""
        row = self.focused_row()
        if row is None:
            self.app.flash("no workspace focused")
            return
        self.app.push_screen(WorkspaceScreen(row))

    def on_list_view_selected(self, event: ListView.Selected) -> None:
        """Enter on a row opens its workspace's page (the list owns
        the key — the binding is the same action for a focused
        footer walk)."""
        self.action_open()

    def action_refresh(self) -> None:
        self.refresh_rows()

    def action_secrets(self) -> None:
        """`e`: the secrets page (#431)."""
        self.app.push_screen(SecretsScreen())

    def action_start(self) -> None:
        """`s`: start the focused workspace."""
        self.run_worker(self.start_focused, exclusive=True)

    def action_stop(self) -> None:
        self.run_worker(self.stop_focused, exclusive=True)

    async def start_focused(self) -> None:
        """Boot the focused workspace."""
        await self.power_focused("start")

    async def stop_focused(self) -> None:
        """Power off the focused workspace."""
        await self.power_focused("stop")

    async def power_focused(self, verb: str) -> None:
        """Boot or power off the focused workspace; the flash names
        the outcome, the listing refreshes."""
        row = self.focused_row()
        if row is None:
            self.app.flash("no workspace focused")
            return
        call = self.app.data.start if verb == "start" else self.app.data.stop
        reply = await guarded_flash(self.app, verb, call(row["id"]))
        if reply is not None:
            self.app.flash(f"{escape(workspace_label(row))} {reply['status']}")
            self.refresh_rows()

    def action_remove(self) -> None:
        """Ask, then delete the focused workspace and its data."""
        row = self.focused_row()
        if row is None:
            self.app.flash("no workspace focused")
            return
        question = (
            f"remove {workspace_label(row)} ({row['id']}) and all "
            "its persistent data?"
        )
        self.app.push_screen(
            ConfirmScreen(question, self.remove_answered(row))
        )

    def remove_answered(self, row: dict):
        """The confirmation's callback: a yes deletes, a no decides
        nothing."""

        async def answered(yes: bool) -> None:
            if not yes:
                return
            reply = await guarded_flash(
                self.app, "remove", self.app.data.remove(row["id"])
            )
            if reply is not None:
                self.app.flash(f"{escape(workspace_label(row))} deleted")
                self.refresh_rows()

        return answered

    def action_create(self) -> None:
        self.app.push_screen(CreateScreen(self.created))

    async def created(self, body: dict | None) -> None:
        """The create form's callback: a body creates, a cancel
        (None) decides nothing. A refused create lands on the
        failure panel (#426) — a detail worth acting on outlives
        a five-second flash — and dismissing it returns here, to
        the list."""
        if body is None:
            return
        try:
            result = await self.app.data.create(body)
        except (Exception, SystemExit) as exc:
            self.app.push_screen(
                FailurePanel("create", body.get("name"), str(exc))
            )
            return
        row, path = result
        self.app.flash(created_note(row, path))
        self.refresh_rows()

    def action_quit(self) -> None:
        self.app.exit()
