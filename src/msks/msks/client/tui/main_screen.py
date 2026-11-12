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
from textual.events import Click, Resize
from textual.screen import ModalScreen, Screen
from textual.widgets import Footer, Input, ListItem, ListView, Static

from .consent_ui import (
    ConfirmScreen,
    FailurePanel,
    flash_safe,
    focus_attr,
    focused_attr,
)
from .forms import CreateScreen
from .help import HelpKeys
from .importer import ImagesScreen
from .rows import (
    DEFAULT_SORT,
    LIST_COLUMNS,
    LIST_FRAME_CELLS,
    NATURAL_DESCENDING,
    column_at,
    expanded_columns,
    list_header,
    row_content,
    sort_rows,
    status_class,
    status_content,
    workspace_label,
)
from .secrets import SecretsScreen
from .workspace import WorkspaceScreen, action_label, action_note


def created_note(row: dict) -> str:
    """The create's flash: the created line."""
    return f"created {escape(workspace_label(row))} (id {row['id']})"


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


#: The standing listing refresh (#470 L2): the list reloads on
#: this interval while it owns the screen, so a workspace another
#: surface moved — the CLI, another operator — appears without a
#: keypress. An unchanged listing repaints nothing (the rebuild
#: skips the swap).
LIST_REFRESH_S = 5.0


class SearchScreen(ModalScreen):
    """The listing's typeahead (#470 L4): ``/`` opens a one-line
    input over the list; each keystroke hands the term to the
    host's jump, Enter closes the modal with the match in place,
    Escape closes it with nothing decided. The input owns the
    keys while it stands, so a term sharing a letter with the
    footer's bindings still types whole."""

    BINDINGS = [
        Binding("escape", "close", "Close", show=False),
        Binding("enter", "close", show=False),
    ]

    def __init__(self, jump) -> None:
        super().__init__()
        self.jump = jump

    def compose(self) -> ComposeResult:
        yield Input(placeholder="jump to a workspace", id="search-input")

    def on_mount(self) -> None:
        self.query_one("#search-input", Input).focus()

    def on_input_changed(self, event: Input.Changed) -> None:
        """Each keystroke jumps: the list beneath keeps its rows
        and takes the focus move."""
        self.jump(event.value)

    def on_input_submitted(self, event: Input.Submitted) -> None:
        """Enter closes the modal with the match standing — the
        input owns the key while it is focused."""
        self.dismiss()

    def action_close(self) -> None:
        self.dismiss()


class ColumnsHeader(Static):
    """The listing's header row, clickable per column (#854): a
    click reports its cell offset to the screen, which maps it to
    the column under it through the live column widths and
    re-sorts. The widget stays display-only — it takes no focus,
    adds no binding, and the spatial navigation is untouched: the
    sort is a click, not a key."""

    def __init__(self, renderable, pick) -> None:
        super().__init__(renderable, id="columns")
        self.pick = pick

    def on_click(self, event: Click) -> None:
        """The click's offset reaches the screen in content cells:
        the widget's own padding (#columns pads a cell left, the
        frame's gutter) sits before the first column's first
        character, and the mapping reads cell offsets from there
        (#854 review — a raw widget offset would skew every
        column's hit region one cell left of its drawn one)."""
        gutter = self.content_region.x - self.region.x
        self.pick(event.x - gutter)


class MainScreen(HelpKeys, Screen):
    """The tree's root (#309): every workspace one row; create,
    start, stop, and remove happen here; Enter opens the
    workspace's page, ``e`` the secrets page (#431), ``/`` the
    typeahead jump (#470 L4), ``?`` the key overlay (#851). The
    footer's descriptions stay short (#524): every binding must
    fit the 80-column footer a 24-row terminal shows — the longer
    words (``Jmp`` for the typeahead, ``Del`` for remove,
    ``Rload`` for the standing refresh) abbreviate their actions
    without hiding a binding; ``?`` stays off the footer for the
    same budget and lists itself in the overlay it opens."""

    #: The ``?`` overlay's title (#851): the docs' name for the
    #: list.
    help_title = "workspaces list"

    BINDINGS = [
        Binding("enter", "open", "Open", priority=True),
        Binding("/", "search", "Jmp"),
        Binding("c", "create", "New"),
        Binding("i", "image", "Img"),
        Binding("s", "start", "Strt"),
        Binding("S", "stop", "Stp"),
        Binding("D", "remove", "Del"),
        Binding("e", "secrets", "Scrts"),
        Binding("r", "refresh", "Rload"),
        Binding("q", "quit", "Quit"),
        Binding("escape", "quit", "Quit", show=False),
        Binding("question_mark", "help", "Help", show=False),
    ]

    def __init__(self) -> None:
        super().__init__()
        self.rows: list[dict] = []
        # The standing column sort (#854): CREATED newest-first is
        # the order the tree opens with; a header click changes
        # it, and every later refresh re-sorts through it.
        self.sort = DEFAULT_SORT
        # The listing's columns at the terminal's width: the fixed
        # minimums until the first resize event lands, then the
        # widened shape (#347) — the header and every row render
        # through this one tuple, so their offsets stay shared.
        self.columns = LIST_COLUMNS
        self.shown_once = False
        # The listing refresh's in-flight mark (#776): a skip, not
        # a cancel — see refresh_rows.
        self.rows_loading = False

    def compose(self) -> ComposeResult:
        yield Static(id="status")
        with Vertical(id="listing"):
            yield ColumnsHeader(self.header_content(), self.pick_column)
            yield Static(id="empty")
        yield Footer()

    def header_content(self) -> str:
        """The header line at the live widths, the active sort's
        arrow on its column (#854)."""
        return list_header(self.columns, self.sort)

    def pick_column(self, offset: int) -> None:
        """A header click picked the column under ``offset`` (#854):
        the active column flips its direction, a new column starts
        at its natural one (CREATED descends, the others ascend),
        and the standing rows re-sort through the same swap the
        refresh uses — focus kept by row, the arrow repainted."""
        column = column_at(self.columns, offset)
        active, descending = self.sort
        if column == active:
            self.sort = (column, not descending)
        else:
            self.sort = (column, column in NATURAL_DESCENDING)
        try:
            self.query_one("#columns", Static).update(self.header_content())
        except NoMatches:
            pass
        self.resort_rows()

    def resort_rows(self) -> None:
        """Re-sort the standing rows through the listing's one
        rebuild flight (#854): a click landing while a refresh is
        in flight is dropped here — its sort state stands, the
        header carries it, and the in-flight refresh (or the
        standing interval's next tick) serves the order."""
        if self.rows_loading:
            return
        self.rows_loading = True
        self.run_worker(self.resort_standing_rows())

    async def resort_standing_rows(self) -> None:
        try:
            await self.rebuild_rows(self.rows)
        finally:
            self.rows_loading = False

    def on_mount(self) -> None:
        self.set_interval(0.5, self.sync_status)
        # The standing reload (#470 L2): a listing that sits on the
        # screen keeps following the daemon — rows another surface
        # moved appear without a keypress, and an unchanged listing
        # repaints nothing (the rebuild's skip).
        self.set_interval(LIST_REFRESH_S, self.refresh_rows)
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

    def on_resize(self, event: Resize) -> None:
        """The terminal's width moved (#347): the listing's columns
        re-width to the frame — the name and image columns share the
        cells a wider terminal brings, and the header and rows
        repaint together so their offsets stay shared. A resize
        that leaves the widths where they were (a height-only move,
        a terminal under the fixed span) repaints nothing."""
        columns = expanded_columns(event.size.width - LIST_FRAME_CELLS)
        if columns == self.columns:
            return
        self.columns = columns
        try:
            self.query_one("#columns", Static).update(self.header_content())
        except NoMatches:
            # Compose has not built the listing yet: the first rows
            # build at these widths when they land.
            return
        self.paint_rows()

    # -- the listing -------------------------------------------------------

    def refresh_rows(self) -> None:
        """Reload the listing; one flight at a time, a skip and not
        a cancel. An exclusive worker cancels the running refresh,
        and a cancellation landing inside the swap (#776's mac-lane
        data: an interval tick firing while the first build still
        awaits on a starved runner) strands the tree — the old list
        removed, the fresh one never mounted — while the rows
        recorded say the swap finished, so every later refresh
        skips against a listing that never painted. The in-flight
        mark skips instead; the standing interval's next tick (five
        seconds) serves the rows, and an ``r`` that lands mid-flight
        rides the same next tick.
        """
        if self.rows_loading:
            return
        self.rows_loading = True
        self.run_worker(self.load_rows)

    async def load_rows(self) -> None:
        try:
            rows = await self.app.data.workspaces()
            await self.rebuild_rows(rows)
        except (Exception, SystemExit) as exc:
            self.app.flash(f"listing failed: {flash_safe(str(exc))}")
        finally:
            self.rows_loading = False

    async def rebuild_rows(self, rows: list[dict]) -> None:
        """Swap in a freshly-built list (its mount awaited), the
        rows in the standing sort (#470 L1, #854), preserving the
        focused row by key (the top when it left) — the consent
        queue's rebuild rule, carried to the listing. An unchanged
        listing keeps its list (#470 L2): the comparison reads in
        the display order (the daemon serves oldest-first — plain
        equality with the daemon's list would differ by order
        alone and never skip), so the standing refresh repaints
        nothing while the daemon serves the same rows, and focus
        and scroll never blip on the interval."""
        ordered = sort_rows(rows, self.sort)
        if ordered == self.rows and self.rows_widget() is not None:
            self.sync_status()
            return
        self.rows = ordered
        listing = self.query_one("#listing", Vertical)
        old = self.rows_widget()
        focused = focused_attr(old, "row_key")
        items = [self.row_item(row) for row in self.rows]
        fresh = ListView(*items, id="rows")
        if old is not None:
            await old.remove()  # frees the id before the fresh list mounts
        await listing.mount(fresh)
        fresh.focus()
        focus_attr(fresh, "row_key", focused)
        self.paint_rows()  # the bold cue lands on the row focus kept
        self.sync_status()

    def row_item(self, row: dict) -> ListItem:
        """One listing row at the terminal's column widths, tagged
        with the workspace's id and a class from its status
        (#348)."""
        item = ListItem(
            Static(
                row_content(
                    row, self.app.theme_variables, columns=self.columns
                )
            )
        )
        item.workspace_id = row["id"]
        item.row_key = ("workspace", row["id"])
        item.add_class(status_class(row["status"]))
        return item

    def paint_rows(self) -> None:
        """Repaint the standing rows in place (#470 S3): the
        focused row's name takes the bold cue a theme's weak
        highlight bar cannot carry — identity and focus stay put,
        the swap never runs."""
        rows = self.rows_widget()
        if rows is None:
            return
        highlighted = rows.highlighted_child
        by_id = {row["id"]: row for row in self.rows}
        for child in rows.children:
            row = by_id.get(getattr(child, "workspace_id", None))
            if row is not None:
                self.paint_row(child, row, focused=child is highlighted)

    def paint_row(self, child, row: dict, focused: bool) -> None:
        """Repaint one standing row; teardown may have unmounted
        its Static under the walk — noise, not a crash."""
        try:
            child.query_one(Static).update(
                row_content(
                    row,
                    self.app.theme_variables,
                    focused=focused,
                    columns=self.columns,
                )
            )
        except NoMatches:
            pass

    def on_list_view_highlighted(self, event: ListView.Highlighted) -> None:
        """The focused row moved: repaint so the bold cue follows
        it (#470 S3)."""
        self.paint_rows()

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

    # -- the typeahead jump (#470 L4) ------------------------------------

    def action_search(self) -> None:
        """``/``: the typeahead over the list — typing a name moves
        the list's focus to the first row that starts with it."""
        self.app.push_screen(SearchScreen(self.jump_to))

    def jump_to(self, term: str) -> None:
        """Move the list's focus to the first row whose label
        starts with ``term`` (case-folded — the daemon's ids count
        beside their names); a term nothing matches names itself
        on the status line and the list keeps its place."""
        rows = self.rows_widget()
        if rows is None or not term:
            return
        wanted = term.casefold()
        for position, row in enumerate(self.rows):
            if workspace_label(row).casefold().startswith(wanted):
                rows.index = position
                return
        self.app.flash(flash_safe(f"no workspace starts with {term!r}"))

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
        the outcome, the listing refreshes. A verb the row's own
        status makes pointless names the same client-side note the
        workspace page's dimmed row carries (#470 L5) — the
        refusal costs no round-trip and reads identically on both
        screens."""
        row = self.focused_row()
        if row is None:
            self.app.flash("no workspace focused")
            return
        note = action_note(verb, row["status"])
        if note is not None:
            self.app.flash(f"{action_label(verb)} skipped: {note}")
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

    def action_image(self) -> None:
        """``i``: the image page (#565, #571) — the catalog and
        the published channel, the pick POSTed to the daemon's
        import endpoint, the modal holding the terminal through
        the download."""
        self.app.push_screen(ImagesScreen())

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
        self.app.flash(created_note(result))
        self.refresh_rows()

    def action_quit(self) -> None:
        self.app.exit()
