"""The workspace screen: one workspace's page.

The header's two lines, the consent status line, and the
actions — shell, consent page, edit, start, stop — the page
carries while it is the workspace's decider. The whole page
sits one block, capped at :data:`PAGE_BLOCK_W` cells and
centered on a wider terminal (#881).
"""

from __future__ import annotations

import json

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.content import Content
from textual.css.query import NoMatches
from textual.screen import Screen
from textual.widgets import Footer, ListItem, ListView, Static

from ...resize import resize_message
from ...sshsessions import count_sessions
from .. import follow
from ..consent_ui import (
    EMPTY_STATIC_QUESTION,
    ConfirmScreen,
    ConsentPage,
    FlashLine,
    ModeScreen,
    OneFlight,
    flash_safe,
    focus_attr,
    focused_attr,
    hold_flash,
    sighting_flash,
    switch_mode_path,
)
from ..detail import header_meta
from ..follow import FLOW_SHELL
from ..forms import EDIT_FREE_STATUSES, EditScreen, edit_stop_question
from ..help import HelpKeys
from ..link import CONNECTED, DeciderLink
from ..mounts import MountsScreen, mounts_line
from ..proxies import PROXY_REFRESH_S, ProxiesScreen, proxies_line
from ..rows import header_name, workspace_label
from .actions import (
    ACTION_CONSENT,
    ACTION_SHELL_WINDOW,
    PAGE_ACTIONS,
    CloneNameScreen,
    PageAction,
    action_content,
    action_label,
    action_note,
)
from .lines import consent_line

#: The page's block width (#881): the cells the page's whole
#: content — the header lines, the status lines, and the action
#: panel — holds as one block. A terminal at and under it is
#: filled edge to edge (the default 80-column window's layout);
#: a wider one centers the block, so the page reads on a single
#: axis at any width. The ``#page-body`` rule in
#: :mod:`msks.client.tui.main_app`'s CSS carries the same number,
#: and the wide-terminal tests pin the pair together. The meta
#: line's budget caps with the block, so past it the fixed frame
#: layout (``META_FRAME_W``, #752) is the page's whole budget —
#: the growth past the frame is the consent page's alone, a page
#: that fills its terminal.
PAGE_BLOCK_W = 80


class WorkspaceScreen(HelpKeys, Screen):
    """One workspace's page (#309): the header's two lines (#351
    — the name with its status on the first, the id, image, host,
    and created date muted on the second, the pending-egress
    count beside the status while holds wait, #354, and the
    host's open-shell count the same way, #858), the consent
    status line, and the page's actions — a shell in a new window
    (#341), the egress consent page (#454), the edit dialog, and
    start and stop; the egress-mode switch (#344) lives on the
    consent page, its own ``m`` — and the page binds one quick
    key per action row (#880): ``e`` the consent page, ``h`` a
    shell, ``m`` the mounts editor, ``p`` the proxies page,
    ``t`` the edit dialog, ``c`` the clone prompt. The footer's
    descriptions abbreviate so every key fits one 80-column
    line, the #581 style (``Go``, ``Egr``, ``Mnts``, ``Prx``);
    Enter keeps its footer hint (#470 S4). The page is
    the workspace's
    decider while it is open: holds land on its link, the header
    counts them, and the consent line flashes each first-seen
    hold's destination — the page pushes nothing on a hold's
    arrival; the operator opens the consent page with ``e`` or
    the action row and decides there (the consent page's own
    docstring owns that page's shape). The page's whole content
    — the header lines, the status lines, and the action panel —
    sits one block capped at :data:`PAGE_BLOCK_W` cells,
    centered on a wider terminal (#881): the page reads on a
    single axis at any width, and a terminal at and under the
    cap keeps the full-width layout."""

    #: The ``?`` overlay's title (#851): the docs' name for the
    #: page.
    help_title = "workspace page"

    BINDINGS = [
        Binding("enter", "run", "Go", priority=True),
        Binding("e", "consent", "Egr"),
        Binding("h", "shell_page", "Shell"),
        Binding("m", "mounts_page", "Mnts"),
        Binding("p", "proxies_page", "Prx"),
        Binding("t", "settings_page", "Edit"),
        Binding("c", "clone_page", "Clone"),
        Binding("s", "quick_start", "Start"),
        Binding("S", "quick_stop", "Stop"),
        Binding("q", "back", "Back"),
        Binding("escape", "back", "Back", show=False),
        Binding("question_mark", "help", "Help", show=False),
    ]

    def __init__(self, row: dict, *, link_factory=None) -> None:
        super().__init__()
        self.row = row
        self.link_factory = link_factory or self.make_link
        self.link: DeciderLink | None = None
        # The holds the consent line has named (#454): ids only
        # join, never leave — a reconnect's replay re-lands the
        # same holds without re-flashing them, and the holds that
        # arrived while the link was down flash once, on the tick
        # after the replay lands them.
        self.seen_hold_ids: set[str] = set()
        # The page's own flash (#344): a message that owns the
        # consent line for FLASH_TTL seconds. The app-level flash
        # paints the list's status line, which the pushed page
        # hides (#343) — a page-raised failure names itself here,
        # where the operator reads it.
        self.flash_line = FlashLine()
        # The workspace's proxy rows (#809): the compact line's
        # data, fetched at page start, on each proxy transition
        # the link lands, and on a slow standing backstop — not on
        # the tick (the events own the immediacy; the backstop
        # covers a link that cannot connect).
        self.proxy_rows: list[dict] | None = None
        # The edit waiting on the stop-and-resize answer (#380):
        # the body a running workspace's Apply parked while the
        # confirmation asks. None when no question stands.
        self.pending_edit: dict | None = None
        self.rebuilds = OneFlight(
            lambda: self.rebuild_actions(),
            lambda: self.app.is_running,
            "workspace-page",
        )

    def make_link(self) -> DeciderLink:
        """The page's decider connection (#309): a link the tests
        replace with a scripted one."""
        return DeciderLink(self.row["id"])

    def compose(self) -> ComposeResult:
        link = self.link_or_stub()
        # The page's one block (#881): the header lines, the status
        # lines, and the action panel share it — capped at
        # PAGE_BLOCK_W cells, centered on a wider terminal, and
        # filling one at and under it (the screen's own alignment
        # does the centering; the ``#page-body`` rule carries the
        # cap).
        with Vertical(id="page-body"):
            yield Static(
                header_name(
                    self.row, theme_variables=self.app.theme_variables
                ),
                id="header",
            )
            yield Static(
                header_meta(
                    self.row, self.app.theme_variables, self.meta_width()
                ),
                id="header-meta",
            )
            yield Static(
                consent_line(link, self.row, self.app.theme_variables),
                id="consent",
            )
            # The mounts line (#620, part #624): the row's mounts with
            # their derived state, repainted with the header each tick.
            yield Static(
                mounts_line(self.row, self.app.theme_variables),
                id="mounts",
            )
            # The compact proxies line (#809): the workspace's port
            # pairs with their reachability, repainted with the mounts.
            yield Static(
                proxies_line(self.proxy_rows or [], self.app.theme_variables),
                id="proxy-line",
            )
            # The action list mounts on the first rebuild (compose
            # yields the container alone).
            yield Vertical(id="page")
        yield Footer()

    def link_or_stub(self) -> DeciderLink:
        """The link, made on first use (compose runs before mount,
        and the link starts with the screen)."""
        if self.link is None:
            self.link = self.link_factory()
        return self.link

    def on_mount(self) -> None:
        self.app.follow.reopen = self.row["id"]
        self.link_or_stub().start()
        self.set_interval(1.0, self.tick)
        # The compact proxies line's backstop (#809): the events
        # own the live path, and this fetch covers a link that
        # cannot connect (the proxies page's own rule, at its own
        # cadence).
        self.set_interval(PROXY_REFRESH_S, self.refresh_proxies)
        # The UI rebuilds wait for the compose stream to settle (a
        # worker racing it queries widgets not mounted yet); the
        # link starts now — frames land in the controller either
        # way, and the first repaint reads them.
        self.call_after_refresh(self.page_started)

    def page_started(self) -> None:
        """The compose has settled: build the action rows and pull a
        fresh row for the header."""
        self.rebuilds.request()
        self.refresh_row()
        self.refresh_proxies()

    def refresh_proxies(self) -> None:
        """Reload the workspace's proxy rows for the compact line
        (#809); one flight at a time, exclusive in its own worker
        group — a stale fetch cannot paint over a fresh one, and
        the page's row refresh runs beside it untouched."""
        self.run_worker(
            self.load_proxies, group="page-proxies", exclusive=True
        )

    async def load_proxies(self) -> None:
        """Fetch the rows; a refusal keeps the line as it was — the
        daemon's named refusal lands on the editor's own surface
        when one is open, and the page's line stays honest."""
        try:
            self.proxy_rows = await self.app.data.workspace_proxies(
                self.row["id"]
            )
        except Exception, SystemExit:
            return
        self.paint_proxies()

    def on_resize(self) -> None:
        """The terminal's width moved (#752): the meta line's
        budgets follow it, so the id, image, and host read as
        whole as the new width allows — at once, not on the next
        tick."""
        self.paint_header()

    def meta_width(self) -> int | None:
        """The meta line's available cells: the page's block width
        (#881) less the line's own padding. The block caps at
        :data:`PAGE_BLOCK_W` on a wider terminal, so the line's
        budgets cap with it — past the cap the fixed frame layout
        is the page's whole budget. A width the layout has not
        settled yet (compose, before the first layout) paints the
        fixed budgets."""
        width = min(self.size.width, PAGE_BLOCK_W) - 2
        return width if width > 0 else None

    def on_unmount(self) -> None:
        """The page is gone: stop deciding for the workspace (the
        socket closes with the link)."""
        if self.link is not None:
            self.link.stop()

    def tick(self) -> None:
        """The per-second repaint: the consent line's countdowns,
        the header's pending-egress count, the sighting drain, the
        hold flash, and a fresh row (the page's dimming and header
        follow a workspace another surface — the CLI, another
        operator — may have moved while the page stood). A screen
        going away under the timer leaves the queries empty —
        teardown noise, not a crash."""
        try:
            self.watch_holds()
            self.watch_proxies()
            if self.is_mounted:
                self.refresh_row()
            self.paint_consent()
            self.paint_mounts()
            self.paint_proxies()
            self.paint_header()
            self.sync_actions()
        except NoMatches:
            pass

    # -- the holds watch (#454) ----------------------------------------

    def watch_holds(self) -> None:
        """The per-tick watch over the link: drain the sighting
        buffer (#201 over #358), then name each first-seen hold on
        the consent line (#454) — the passive attention a hold
        gets, the header's count beside it."""
        if self.link is None:
            return
        self.drain_sightings()
        self.flash_new_holds()

    def watch_proxies(self) -> None:
        """The per-tick watch over the link's proxy transitions
        (#809): each one refetches the compact line's rows — the
        same immediacy the events socket gives the proxies page,
        on the connection the page already holds."""
        if self.link is None:
            return
        if self.link.take_proxy_events():
            self.refresh_proxies()

    def flash_new_holds(self) -> None:
        """Flash each hold the queue has not named yet (#454): the
        page pushes nothing on a hold's arrival, so the consent
        line names the destination and the key in. The flash waits
        while another screen owns the terminal (the consent page,
        the edit form, a picker): the ids stay unseen and the
        first tick after the stack unwinds names what arrived —
        the overlay-era burst guard's rule, carried to the flash
        (a flash painted on a hidden line is consumed unseen).
        A burst takes one flash naming its count (a FlashLine
        owns the line for its TTL — one destination a tick would
        overwrite the rest)."""
        if self.app.screen is not self:
            return
        fresh = self.take_unseen_holds()
        if len(fresh) == 1:
            self.flash(hold_flash(fresh[0]))
        elif fresh:
            self.flash(f"{len(fresh)} egress holds to decide — press e")

    def take_unseen_holds(self) -> list:
        """The queue's holds the consent line has not named yet,
        taken: their ids join the seen set here and never leave it
        — a reconnect's replay re-lands the same holds without
        re-flashing them, and the holds that arrived while the
        link was down name themselves once, on the tick after the
        replay lands them."""
        pending = self.link.controller.ordered()
        fresh = [
            request
            for request in pending
            if request.id not in self.seen_hold_ids
        ]
        self.seen_hold_ids.update(request.id for request in pending)
        return fresh

    def drain_sightings(self) -> None:
        """Flash the off-allowlist sightings the frames landed since
        the last drain, each on the surface that owns the terminal
        (#358 carries #201's rule — the exfil signal interrupts
        wherever the operator is)."""
        for event in self.link.take_sightings():
            self.flash_visible(sighting_flash(event))

    def flash_visible(self, message: str) -> None:
        """Give one message to the surface that owns the terminal: the
        consent page's status line while it is up, this page's
        consent line when it is not."""
        page = self.consent_page()
        if page is not None:
            page.flash(message)
        else:
            self.flash(message)

    def consent_page(self) -> ConsentPage | None:
        """The open consent page wherever it sits in the stack (a
        picker may stand above it)."""
        for screen in self.app.screen_stack:
            if isinstance(screen, ConsentPage):
                return screen
        return None

    def open_consent_page(self) -> None:
        """Push the egress consent page over this page (#454): the
        holds queue and the in-effect verdicts on one screen, the
        tree's only deciding surface. This page keeps the link
        running beneath it — the consent page reads the same
        controller, and holds keep landing while it stands."""
        self.app.push_screen(ConsentPage(self))

    def action_consent(self) -> None:
        """``e``: the consent page one key away (#454) — a hold's
        flash names the key, and the hold's clock is running."""
        self.open_consent_page()

    # -- the header and the consent line -------------------------------------

    def pending_count(self) -> int:
        """The holds waiting on the page's queue — the header's
        indicator count. A page without a live link counts none:
        a bare page has no queue yet, and a dead socket's snapshot
        may hold holds the server already resolved."""
        if self.link is None or self.link.state != CONNECTED:
            return 0
        return len(self.link.controller.ordered())

    def paint_header(self) -> None:
        try:
            self.query_one("#header", Static).update(
                header_name(
                    self.row,
                    self.pending_count(),
                    self.app.theme_variables,
                )
            )
            self.query_one("#header-meta", Static).update(
                header_meta(
                    self.row, self.app.theme_variables, self.meta_width()
                )
            )
        except NoMatches:
            pass  # teardown unmounted a header line under the worker

    def paint_mounts(self) -> None:
        """Repaint the mounts line (#620, part #624): the row's
        mounts with the derived state — the per-second row refresh
        keeps it current, and the editor's reply lands it
        immediately."""
        try:
            self.query_one("#mounts", Static).update(
                mounts_line(self.row, self.app.theme_variables)
            )
        except NoMatches:
            pass  # teardown unmounted the line under the timer

    def paint_proxies(self) -> None:
        """Repaint the compact proxies line (#809): the port pairs
        with their reachability — page start, the link's
        transitions, and the slow backstop land it, and the tick's
        repaint keeps it beside the mounts line."""
        try:
            self.query_one("#proxy-line", Static).update(
                proxies_line(self.proxy_rows or [], self.app.theme_variables)
            )
        except NoMatches:
            pass  # teardown unmounted the line under the timer

    def paint_consent(self) -> None:
        if self.link is not None:
            try:
                self.query_one("#consent", Static).update(
                    self.flash_line.text(
                        consent_line(
                            self.link, self.row, self.app.theme_variables
                        )
                    )
                )
            except NoMatches:
                pass  # teardown unmounted the line under the timer

    def flash(self, message: str) -> None:
        """Give the page's consent line to a message for FLASH_TTL
        seconds — the page's own surface: the app-level flash
        paints the list's status line, which the pushed page hides
        (#343), so a failure this screen raises names itself
        here. While the flash lives it stands in for the line's
        state naming (a drop's label included) — a bounded window,
        failure and outcome messages only."""
        self.flash_line.set(message)
        self.paint_consent()

    async def guarded_page_flash(self, label: str, work):
        """Await one page action, flashing the failure on the
        page's consent line — the app-level guard paints the
        list's status line, which the pushed page hides (#343)."""
        try:
            return await work
        except (Exception, SystemExit) as exc:
            self.flash(f"{label} failed: {flash_safe(str(exc))}")
            return None

    def refresh_row(self) -> None:
        """Reload this workspace's row (a page's actions change its
        status, and so does work done away from the page); one
        flight at a time, in the page's own worker group — the
        default group belongs to the action runs, and a refresh
        landing mid-start must not cancel the call it describes."""
        self.run_worker(self.load_row, group="page-row", exclusive=True)

    async def load_row(self) -> None:
        try:
            rows = await self.app.data.workspaces()
        except Exception, SystemExit:
            return  # the page keeps its row; the header stays as it was
        fresh = next(
            (row for row in rows if row["id"] == self.row["id"]), None
        )
        if fresh is None:
            self.close_removed()
            return
        # The host's open-shell count lands on the row the way the
        # listing lands it (#858): client-side, per-host state the
        # header's shells segment reads — the per-second refresh
        # keeps it within a heartbeat of each open and close.
        fresh["shells"] = count_sessions().get(self.row["id"], 0)
        self.row = fresh
        self.paint_header()
        self.paint_actions()

    def close_removed(self) -> None:
        """A listing that cannot see the workspace names its removal
        — another surface deleted it — and the page closes behind
        a notice on the list's status line: a page for a workspace
        that no longer exists offers only refusals. A screen above
        the page (the consent page, the edit form) holds the
        close until it goes; the per-second read tries again."""
        if self.app.screen is not self:
            return
        self.app.follow.reopen = None
        self.app.flash(
            flash_safe(
                f"{workspace_label(self.row)} removed — closing its page"
            )
        )
        self.app.pop_screen()

    # -- the action rows -----------------------------------------------------

    def actions_widget(self) -> ListView | None:
        """The action list, or None during a rebuild's swap
        window."""
        try:
            return self.query_one("#actions", ListView)
        except NoMatches:
            return None

    def action_rows(self, rows: ListView) -> list:
        """The list's action rows — the group caption rows (#752)
        excluded, so a walk addresses the actions alone."""
        return [
            child for child in rows.children if "caption" not in child.classes
        ]

    def fixed_items(self) -> list[ListItem]:
        """The page's rows (#367, #752): each group's muted caption
        above its action rows, every row tagged with its kind. A
        caption rides a disabled ListItem, so the list's cursor
        walks straight through it — the group's bounds read on
        screen without ever taking the highlight."""
        items: list[ListItem] = []
        group = None
        for spec in PAGE_ACTIONS:
            if spec.group != group:
                group = spec.group
                caption = ListItem(Static(f" {group}"), disabled=True)
                caption.page_caption = group
                caption.add_class("caption")
                if items:
                    caption.add_class("spaced")
                items.append(caption)
            item = ListItem(Static(self.row_paint(spec, focused=False)))
            item.page_action = spec.kind
            item.page_key = ("action", spec.kind)
            items.append(item)
        return items

    def row_paint(self, spec: PageAction, focused: bool) -> Content:
        """One spec's content for the row's current state — the
        row's own status and the page's theme at paint time (a
        bare, never-mounted page paints against the default
        theme)."""
        theme = self.app.theme_variables if self.is_mounted else {}
        return action_content(
            spec,
            self.row.get("status") or "",
            theme,
            focused,
        )

    def paint_actions(self) -> None:
        """Repaint the action rows in place (#367): the marker
        follows the row Enter acts on and the power pair's dimming
        follows the row's status — without rebuilding the list,
        so focus and identity stay put. The caption rows stand
        outside the walk: their text never moves."""
        rows = self.actions_widget()
        if rows is None:
            return
        highlighted = rows.highlighted_child
        actions = self.action_rows(rows)
        # Teardown shrinks the list under the paint: the walk
        # paints the rows that stand, never the specs beyond, and
        # a row that lost its Static skips alone.
        for child, spec in zip(actions, PAGE_ACTIONS):
            try:
                child.query_one(Static).update(
                    self.row_paint(spec, focused=child is highlighted)
                )
            except NoMatches:
                pass  # teardown unmounted this row's Static

    async def rebuild_actions(self) -> None:
        """Swap in a freshly-built action list (its mount awaited),
        preserving the focused row by key (the top when it left) —
        the consent queue's rebuild rule, carried to the page."""
        old = self.actions_widget()
        focused = focused_attr(old, "page_key")
        fresh = ListView(*self.fixed_items(), id="actions")
        if old is not None:
            await old.remove()  # frees the id before the fresh list mounts
        await self.query_one("#page", Vertical).mount(fresh)
        fresh.focus()
        focus_attr(fresh, "page_key", focused)
        self.paint_actions()  # the marker lands on the row focus kept

    def sync_actions(self) -> None:
        """The list carries only the fixed actions — a tick has no
        countdowns to repaint and no membership to watch; a list
        gone missing (a swap window, a teardown race) rebuilds
        itself."""
        if self.actions_widget() is None:
            self.rebuilds.request()

    def on_list_view_highlighted(self, event: ListView.Highlighted) -> None:
        """The row Enter acts on moved: repaint the rows so the
        marker follows it (#367) — the paint is in place, so
        identity and focus stay put."""
        self.paint_actions()

    # -- the actions ---------------------------------------------------

    async def action_run(self) -> None:
        """Enter: the focused row's page action runs — a dimmed
        power row (#367) names its reason on the consent line
        instead of calling the daemon."""
        kind = self.focused_action()
        if kind is None:
            return
        note = action_note(kind, self.row.get("status") or "")
        if note is not None:
            self.flash(f"{action_label(kind)} skipped: {note}")
            return
        if kind == ACTION_CONSENT:
            self.open_consent_page()
            return
        await self.run_page_action(kind)

    def on_list_view_selected(self, event: ListView.Selected) -> None:
        """Enter on a row runs it (the list owns the key — the
        binding is the same action for a focused footer walk)."""
        self.run_worker(self.action_run, exclusive=True)

    def focused_action(self) -> str | None:
        """The focused row's kind; an untagged row (or a swap
        window) is None."""
        rows = self.actions_widget()
        child = rows.highlighted_child if rows is not None else None
        if child is None:
            return None
        return getattr(child, "page_action", None)

    async def run_page_action(self, kind: str) -> None:
        handler = {
            ACTION_SHELL_WINDOW: self.open_shell_window,
            "edit": self.edit_workspace,
            "mounts": self.open_mounts,
            "proxies": self.open_proxies,
            "clone": self.clone_workspace,
            "start": self.start_workspace,
            "stop": self.stop_workspace,
        }[kind]
        await handler()

    async def open_shell_window(self) -> None:
        """Open a workspace shell (#341, #562). The default runs
        ``msks ssh`` in the TUI's own terminal: the tree exits,
        the ssh session runs where it stood — ssh, not the
        console, because the console sizes its guest pty once at
        connect while ssh carries the terminal's resizes — and the
        tree restarts when the session hands the terminal back; a
        forward the flow's probe names unreachable — or an ssh
        that fails to connect — opens nothing in the session's
        place (#748): the restart's flash leads with the
        ``msks console`` suggestion and follows with the refusal's
        first line, and the full refusal is logged. A configured terminal
        launcher (``terminal_open_cmd``) opens windows beside the
        running tree instead: the launcher runs the ssh invocation
        as its child, which inherits the tree's materialized
        connection, so it reaches the same daemon. A launcher that
        cannot start — a missing binary, one without the execute
        bit, a word the exec itself refuses — seeds the restart's
        flash with its reason and takes the same-terminal shell
        flow instead (the setting's documented fallback). The
        action runs on a stopped workspace too (#479): the ssh
        session — window or same-terminal — boots the workspace
        itself."""
        if self.app.terminal_cmd is None:
            self.app.quit_after(follow.FLOW_SSH, self.row["id"])
            return
        child = follow.ssh_child_argv(self.row["id"])
        try:
            proc = await follow.spawn_window([*self.app.terminal_cmd, *child])
        except (OSError, ValueError) as exc:
            # The reason rides the follow queue as the restarted
            # tree's first flash: this tree exits on the spot, and
            # its own status line dies with it.
            self.app.follow.seed = (
                f"shell window failed: {flash_safe(str(exc))}"
                " — opening in this terminal"
            )
            self.app.quit_after(FLOW_SHELL, self.row["id"])
            return
        self.app.hold_child(proc)
        self.flash(
            flash_safe(
                f"opened a shell window for {workspace_label(self.row)}"
            )
        )

    async def clone_workspace(self) -> None:
        """Push the clone's name prompt (#773) over the page — the
        daemon owns the stopped-source rule; a refusal names
        itself on the page's consent line."""
        self.app.push_screen(
            CloneNameScreen(workspace_label(self.row), self.cloned)
        )

    async def cloned(self, name: str | None) -> None:
        """The name prompt's callback: a name clones this
        workspace through the daemon — both disks and the
        create-time config; the new row's label flashes here, and
        the list names it on its next refresh. A cancel decides
        nothing."""
        if name is None:
            return
        row = await self.guarded_page_flash(
            "clone", self.app.data.clone(self.row["id"], name)
        )
        if row is not None:
            self.flash(
                flash_safe(
                    f"cloned into {workspace_label(row)} (id {row['id']})"
                )
            )

    async def start_workspace(self) -> None:
        await self.power_workspace("start")

    async def stop_workspace(self) -> None:
        await self.power_workspace("stop")

    async def action_quick_start(self) -> None:
        """``s``: the list's start letter, carried onto the page
        (#470 W5) — the same muscle memory both screens answer."""
        await self.power_key("start")

    async def action_quick_stop(self) -> None:
        """``S``: the list's stop letter, carried onto the page
        (#470 W5)."""
        await self.power_key("stop")

    async def power_key(self, verb: str) -> None:
        """One quick power key (#470 W5): the row's status
        pre-flights the verb — the dimmed row's note names the
        skip — and a live verb runs the page's own power
        exchange."""
        note = action_note(verb, self.row.get("status") or "")
        if note is not None:
            self.flash(f"{action_label(verb)} skipped: {note}")
            return
        await self.power_workspace(verb)

    async def power_workspace(self, verb: str) -> None:
        """Boot or power off this workspace; the header and the
        page's consent line name the outcome."""
        call = self.app.data.start if verb == "start" else self.app.data.stop
        reply = await self.guarded_page_flash(verb, call(self.row["id"]))
        if reply is not None:
            self.row["status"] = reply["status"]
            self.paint_header()
            self.paint_actions()
            self.flash(
                flash_safe(f"{workspace_label(self.row)} {reply['status']}")
            )

    async def open_mounts(self) -> None:
        """Push the mounts list editor (#620, part #624) over the
        page — the editor owns its rows and posts the whole list;
        the reply's row lands here, so the page's mounts line and
        the header follow the save without waiting for the tick."""
        self.app.push_screen(
            MountsScreen(self.row, self.app.data), self.mounts_edited
        )

    def mounts_edited(self, row: dict | None) -> None:
        """The editor's close: the row it returns (every save
        refreshes it) repaints the page's mounts line."""
        if row is None:
            return
        self.row = row
        self.paint_mounts()

    async def open_proxies(self) -> None:
        """Push the workspace's proxies page (#809) over this one
        — the same rows the compact line paints, with the add form
        and the remove behind it."""
        self.app.push_screen(ProxiesScreen(self.row))

    async def action_proxies_page(self) -> None:
        """``p``: the proxies page one key away — the action row's
        own destination."""
        await self.open_proxies()

    async def action_mounts_page(self) -> None:
        """``m`` (#880): the mounts editor one key away — the
        action row's own destination."""
        await self.open_mounts()

    async def action_shell_page(self) -> None:
        """``h`` (#880): the shell action one key away — the use
        group's own destination."""
        await self.open_shell_window()

    async def action_settings_page(self) -> None:
        """``t`` (#880): the edit dialog one key away — the
        Edit row's own destination."""
        await self.edit_workspace()

    async def action_clone_page(self) -> None:
        """``c`` (#880): the clone prompt one key away — the Clone
        row's own destination."""
        await self.clone_workspace()

    # -- the edit dialog (#331) ---------------------------------------

    async def edit_workspace(self) -> None:
        """Push the edit dialog over the page (#331): the create
        form's own implementation, seeded from this workspace's row
        — the sizes editable, the create-time fields read-only."""
        self.app.push_screen(EditScreen(self.row, self.edited))

    async def edited(self, body: dict | None) -> None:
        """The edit dialog's callback: a body resizes this workspace
        (the daemon owns the stopped-workspace rule — a refusal
        names itself on the page's consent line), a cancel decides
        nothing. A running workspace asks first (#380): the sizes
        move only while it is stopped, so Apply offers the stop
        instead of losing the edit to a refusal the operator never
        reads."""
        if body is None:
            return
        if self.row["status"] not in EDIT_FREE_STATUSES:
            self.pending_edit = body
            self.app.push_screen(
                ConfirmScreen(
                    edit_stop_question(self.row), self.edit_stop_answered
                )
            )
            return
        await self.apply_edit(body)

    async def edit_stop_answered(self, yes: bool) -> None:
        """The stop-and-resize answer (#380): a yes stops the
        workspace through the page's own stop exchange and applies
        the edit that waited for it; a no keeps the workspace as
        it is, sizes untouched."""
        body = self.pending_edit
        self.pending_edit = None
        if not yes:
            self.flash(
                flash_safe(
                    f"edit skipped — {workspace_label(self.row)} "
                    "keeps its sizes"
                )
            )
            return
        # power_workspace's exchange minus its flash: the resize's
        # outcome line owns the consent line after the stop.
        stop = await self.guarded_page_flash(
            "stop", self.app.data.stop(self.row["id"])
        )
        if stop is None:
            return
        self.row["status"] = stop["status"]
        self.paint_header()
        self.paint_actions()
        await self.apply_edit(body)

    async def apply_edit(self, body: dict) -> None:
        """The resize exchange (#331): the reply's sizes land in the
        page's row and the outcome line names what moved."""
        reply = await self.guarded_page_flash(
            "edit", self.app.data.resize(self.row["id"], body)
        )
        if reply is None:
            return
        for field in ("root_mib", "home_mib", "cpus", "mem_mib"):
            # A reply from a daemon older than a field omits it; the
            # row's own fact fills the gap — the page's row keeps a
            # value and the outcome line prints whole instead of
            # dying on the missing key.
            if field not in reply:
                reply[field] = self.row[field]
            self.row[field] = reply[field]
        self.paint_header()
        self.flash(flash_safe(resize_message(reply, body)))

    # -- the egress-mode switch (#344) ---------------------------------

    def page_rules(self):
        """The link controller's rules snapshot, or None before the
        page mounted its link (compose makes it)."""
        if self.link is None:
            return None
        return self.link.controller.rules

    def open_mode_picker(self) -> None:
        """Push the mode picker (#344) over the consent page — its
        ``m`` is the picker's one path (#454, #460): the current
        mode starts highlighted (the snapshot's mode; the row's
        until the first rules frame lands), a current the picker
        does not know highlights nothing — a bare Enter decides
        nothing (#465 review) — and the pick goes to the switch
        path, which owns the empty-static confirmation."""
        rules = self.page_rules()
        current = (
            rules.mode
            if rules is not None
            else (self.row.get("egress_mode") or "")
        )
        self.app.push_screen(ModeScreen(current, self.switch_mode))

    async def switch_mode(self, mode: str | None) -> None:
        """One picked mode (#344): the pick goes to the shared
        switch path — the same gate and confirmation the decider
        app's picker takes."""
        await switch_mode_path(
            mode, self.page_rules(), self.ask_empty_static, self.send_mode
        )

    def ask_empty_static(self, answered) -> None:
        """Push the empty-static confirmation with the given
        callback (the page's host is the app's screen stack)."""
        self.app.push_screen(ConfirmScreen(EMPTY_STATIC_QUESTION, answered))

    async def send_mode(
        self, mode: str, *, confirm_empty: bool = False
    ) -> None:
        """One mode switch through the data seam (#344). The reply
        carries the fresh rules frame — fed through the
        controller's frame applier, the consent line names the new
        mode the moment the switch lands, a dropped link included
        (the daemon pushes the same frame on the events socket,
        and it re-lands the same data idempotently). A refusal
        names itself on the surface that owns the terminal — the
        consent page's status line while it stands (the switch's
        one path, its ``m``), the page's consent line otherwise:
        the app-level flash paints the list's status line, which
        a pushed page hides (#343)."""
        try:
            reply = await self.app.data.set_egress_mode(
                self.row["id"], mode, confirm_empty=confirm_empty
            )
        except (Exception, SystemExit) as exc:
            self.flash_visible(f"mode switch failed: {flash_safe(str(exc))}")
            return
        self.row["egress_mode"] = reply.get("mode") or mode
        self.land_rules_reply(reply)
        self.paint_consent()

    def land_rules_reply(self, reply: dict) -> None:
        """Feed the policy reply's fresh rules frame through the
        controller's frame applier — the same path the events
        socket's frames take — so the consent line names the new
        mode without waiting for the pushed frame, a dropped link
        included. A page without its link (a reply landing at
        teardown) keeps the row's update alone."""
        if self.link is None:
            return
        self.link.controller.apply_frame(
            json.dumps({"event": "egress.rules", "data": reply})
        )

    def action_back(self) -> None:
        """Return to the workspaces list; the page stops deciding
        for the workspace as it goes (unmount closes the link)."""
        self.app.follow.reopen = None
        self.app.pop_screen()
