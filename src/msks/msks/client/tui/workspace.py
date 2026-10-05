"""The workspace page (#309) and the egress consent page it
opens (#454) — extracted from the app shell so
:mod:`msks.client.tui.main_app` composes them.

The page is the workspace's decider while it is open: holds land
on its link, the header counts them, and the consent line flashes
each first-seen hold's destination — the page pushes nothing on a
hold's arrival; the operator decides on the consent page, opened
with ``e`` or the action row, and the holds keep waiting beneath.
Spatial navigation: arrows walk the page's action rows, Enter runs
the focused one, ``q`` or Escape returns to the list, and the
consent page's verdict keys live on that page alone (Textual
routes keys to the active screen alone).
"""

import json
from typing import NamedTuple

from rich.markup import escape
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.content import Content, Span
from textual.css.query import NoMatches
from textual.screen import Screen
from textual.widgets import Footer, ListItem, ListView, Static

from ..resize import resize_message

# The ``follow`` module-object import below is load-bearing: the
# page calls ``follow.spawn_window``/``follow.ssh_child_argv``
# through it, so the tests' patches on the follow module reach
# the page's calls.
from . import follow
from .consent_ui import (
    EMPTY_STATIC_QUESTION,
    ConfirmScreen,
    ConsentPage,
    FlashLine,
    ModeScreen,
    OneFlight,
    duration_label,
    flash_safe,
    fmt_duration,
    focus_attr,
    focused_attr,
    hold_flash,
    sighting_flash,
    switch_mode_path,
)
from .follow import FLOW_SHELL
from .forms import EDIT_FREE_STATUSES, EditScreen, edit_stop_question
from .link import CONNECTED, REJECTED, UNUSABLE_TOKEN, DeciderLink
from .rows import header_meta, header_name, muted_style, workspace_label

#: The workspace page's shell action (#341) — a page action, not a
#: flow: the tree keeps running while the window owns its own
#: terminal.
ACTION_SHELL_WINDOW = "shell-window"

#: The workspace page's consent action (#358, #454): the egress
#: consent page — the holds queue and the in-effect verdicts on
#: one full-screen visit, opened by hand; holds surface passively
#: while it is closed (the header's count, the consent line's
#: flash). The consent page's ``m`` owns the egress-mode switch
#: (#344) — the picker's one path.
ACTION_CONSENT = "egress-consent"


def grant_text(rule, remaining: float | None) -> str:
    """One granted scope with its expiry: host, port (or all
    ports), and the duration label."""
    port = " (all ports)" if rule.dest_port == 0 else f":{rule.dest_port}"
    label = duration_label(rule, remaining)
    return f"{escape(rule.dest_host)}{port} ({label})"


def next_expiry(controller, rules) -> float | None:
    """The grant stack's nearest countdown, or None while no
    grant carries one (open-ended verdicts alone)."""
    timed = [
        remaining
        for remaining in (
            controller.rule_remaining(rule) for rule in rules.allowed
        )
        if remaining is not None
    ]
    return min(timed, default=None)


def granted_line(controller) -> str:
    """The granted scopes for the consent status line (#366): one
    grant names itself — host, port, expiry; two or more collapse
    to the count with the nearest expiry (``3 grants · next
    expires 4h``), so a workspace with several grants keeps the
    line readable at 80 columns instead of running it to the
    terminal's edge — every grant stays spelled out on the egress
    consent page (``e`` from the workspace page). A
    stack with no
    countdown among its grants (open-ended verdicts alone)
    carries the count alone. The honest absence when nothing is
    in effect."""
    rules = controller.rules
    if rules is None or not rules.allowed:
        return "no active consent"
    if len(rules.allowed) == 1:
        return grant_text(
            rules.allowed[0], controller.rule_remaining(rules.allowed[0])
        )
    summary = f"{len(rules.allowed)} grants"
    expiry = next_expiry(controller, rules)
    if expiry is not None:
        summary += f" · next expires {fmt_duration(expiry)}"
    return summary


def consent_line(link, row: dict) -> str:
    """The workspace page's consent status line (#309): the granted
    scopes — one named, several counted (#366) — or the honest
    absence, prefixed by the mode; the row's recorded mode stands
    in until the first rules frame lands. The state is named
    whenever it is not connected: a drop never implies that
    silence is data (the controller keeps its last snapshot through
    the backoff ladder, so the line says so beside it)."""
    if link.state in (REJECTED, UNUSABLE_TOKEN):
        return f"egress consent: {escape(link.reject_reason)}"
    rules = link.controller.rules
    if rules is None:
        mode = row.get("egress_mode") or "-"
        return f"egress consent: mode {mode} · {link.state}"
    line = (
        f"egress consent: mode {rules.mode} · {granted_line(link.controller)}"
    )
    if link.state != CONNECTED:
        line += f" · {link.state}"
    return line


#: The power verbs' dimming rule (#367): the status that makes
#: each verb pointless — the row dims with its reason while the
#: workspace sits in it, and Enter names the reason instead of
#: calling the daemon. Every other status leaves both rows live:
#: the daemon owns the vocabulary (a workspace may read paused,
#: starting, created, or a watcher word), and a verb it would
#: still take stays offered — a refusal names itself on the
#: page's consent line.
DIMMED_WHEN = {"start": "running", "stop": "stopped"}


class PageAction(NamedTuple):
    """One fixed action row of the workspace page (#309, #367):
    its kind, the name it paints, the muted description behind
    the em dash (empty when the name stands alone), and whether
    the row leads its group — the lead row carries the top margin
    that separates the groups."""

    kind: str
    name: str
    desc: str
    lead: bool


def action_note(kind: str, status: str) -> str | None:
    """The reason a power row stands dimmed (#367), or None
    while its verb runs: the status that makes it pointless."""
    if DIMMED_WHEN.get(kind) == status:
        return f"workspace is {status}"
    return None


def dimmed_action_content(
    name: str, note: str, marker: str, muted: str
) -> Content:
    """A dimmed power row's paint (#367): the whole row muted,
    its reason riding in the description's place."""
    text = f"{marker} {name} — {note}"
    offset = 2 + len(name)
    return Content(
        text,
        [Span(2, offset, muted), Span(offset + 3, len(text), muted)],
    )


def live_action_content(
    kind: str, name: str, desc: str, marker: str, muted: str
) -> Content:
    """A live action row's paint (#367): the name in the default
    foreground — bold on the shell row, the page's most-used
    action — the description muted behind an em dash."""
    text = f"{marker} {name}"
    spans = []
    offset = 2 + len(name)
    if kind == ACTION_SHELL_WINDOW:
        spans.append(Span(2, offset, "$text bold"))
    if desc:
        text += f" — {desc}"
        spans.append(Span(offset + 3, len(text), muted))
    return Content(text, spans)


def action_content(
    spec: PageAction, status: str, theme_variables: dict | None, focused: bool
) -> Content:
    """One action row's paint (#367): a marker cell on the row
    Enter acts on — the highlighted row (the highlight bar stays
    the list's own cue; the marker keeps the row legible where a
    theme's bar reads weakly) — beside the row's tone-painted
    content. A power row the status dims (#:data:`DIMMED_WHEN`)
    mutes the whole row behind its reason; every other row rides
    :func:`live_action_content`. The cells ride a Content's plain
    text, so a markup-carrying name cannot shift the spans."""
    marker = "▸" if focused else " "
    muted = muted_style(theme_variables or {})
    note = action_note(spec.kind, status)
    if note is not None:
        return dimmed_action_content(spec.name, note, marker, muted)
    return live_action_content(spec.kind, spec.name, spec.desc, marker, muted)


#: The workspace page's fixed actions (#309), top to bottom in
#: three groups — use, configure, power — each group's lead row
#: (``lead`` True) carrying the top margin that separates the
#: groups (#367); the use group's lead carries it too, the
#: breathing room between the status lines and the actions. The
#: LLM token's remint stays on the CLI (`msks llm-token
#: --remint` prints the fresh token, the part the page cannot
#: usefully show) — #343 took the action off the page: its flash
#: painted the list's status line, which the pushed page hides.
PAGE_ACTIONS = (
    PageAction(ACTION_SHELL_WINDOW, "Open a shell", "in a new terminal", True),
    PageAction(
        ACTION_CONSENT,
        "Egress consent",
        "decide holds and review rules",
        True,
    ),
    PageAction("edit", "Edit settings", "sizes and topology", False),
    PageAction("start", "Start", "", True),
    PageAction("stop", "Stop", "", False),
)


class WorkspaceScreen(Screen):
    """One workspace's page (#309): the header's two lines (#351
    — the name with its status on the first, the id, image, host,
    and created date muted on the second, the pending-egress
    count beside the status while holds wait, #354), the consent
    status line, and the page's actions — a shell in a new window
    (#341), the egress consent page (#454), the edit dialog, and
    start and stop; the egress-mode switch (#344) lives on the
    consent page, its ``m``. The page is the workspace's
    decider while it is open: holds land on its link, the header
    counts them, and the consent line flashes each first-seen
    hold's destination — the page pushes nothing on a hold's
    arrival; the operator opens the consent page with ``e`` or
    the action row and decides there (the consent page's own
    docstring owns that page's shape)."""

    BINDINGS = [
        Binding("enter", "run", "Run"),
        Binding("e", "consent", "Egress consent"),
        Binding("q", "back", "Back"),
        Binding("escape", "back", "Back", show=False),
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
        yield Static(
            header_name(self.row, theme_variables=self.app.theme_variables),
            id="header",
        )
        yield Static(
            header_meta(self.row, self.app.theme_variables),
            id="header-meta",
        )
        yield Static(consent_line(link, self.row), id="consent")
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
            if self.is_mounted:
                self.refresh_row()
            self.paint_consent()
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
                header_meta(self.row, self.app.theme_variables)
            )
        except NoMatches:
            pass  # teardown unmounted a header line under the worker

    def paint_consent(self) -> None:
        if self.link is not None:
            try:
                self.query_one("#consent", Static).update(
                    self.flash_line.text(consent_line(self.link, self.row))
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

    def fixed_items(self) -> list[ListItem]:
        """The page's actions (#367), each tagged with its kind —
        a group's lead row carrying the class that paints its
        separating margin."""
        items = []
        for spec in PAGE_ACTIONS:
            item = ListItem(Static(self.row_paint(spec, focused=False)))
            item.page_action = spec.kind
            item.page_key = ("action", spec.kind)
            if spec.lead:
                item.add_class("group-lead")
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
        so focus and identity stay put."""
        rows = self.actions_widget()
        if rows is None:
            return
        highlighted = rows.highlighted_child
        # Teardown shrinks the list under the paint: the walk
        # paints the rows that stand, never the specs beyond, and
        # a row that lost its Static skips alone.
        for child, spec in zip(rows.children, PAGE_ACTIONS):
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
            self.flash(f"{kind} skipped: {note}")
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
            "start": self.start_workspace,
            "stop": self.stop_workspace,
        }[kind]
        await handler()

    async def open_shell_window(self) -> None:
        """Open a workspace shell in a new terminal window (#341):
        the launcher runs an ssh invocation as its child — ssh, not
        the console, because the console sizes its guest pty once
        at connect while ssh carries a live window's resizes — the
        child inherits the tree's materialized connection, so it
        reaches the same daemon — and the tree keeps running. A
        launcher that cannot start — a missing binary, one without
        the execute bit, a word the exec itself refuses — seeds the
        restart's flash with its reason and takes the
        same-terminal shell flow instead (the setting's documented
        fallback)."""
        child = follow.ssh_child_argv(self.row["id"])
        try:
            proc = await follow.spawn_window([*self.app.terminal_cmd, *child])
        except (OSError, ValueError) as exc:
            # The reason rides the follow queue as the restarted
            # tree's first flash: this tree exits on the spot, and
            # its own status line dies with it.
            self.app.follow.seed = (
                f"shell window failed: {escape(str(exc))}"
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

    async def start_workspace(self) -> None:
        await self.power_workspace("start")

    async def stop_workspace(self) -> None:
        await self.power_workspace("stop")

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
