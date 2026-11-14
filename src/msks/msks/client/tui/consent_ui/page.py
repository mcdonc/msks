"""The consent page: the TUI's main screen.

The websocket-driven screen that lists live destinations and the
rules that answer them, holding focus across updates.
"""

from __future__ import annotations

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.css.query import NoMatches
from textual.screen import Screen
from textual.widgets import Footer, ListItem, ListView, Static

from ..consent import (
    DURATION_DEFAULT,
    ConsentController,
)
from ..detail import header_meta
from ..help import HelpKeys
from ..rows import header_name
from .flash import FlashLine, flash_safe
from .focus import (
    ensure_focus,
    focus_by_id,
    focus_rule_by_id,
    focused_request_id,
    focused_rule_id,
    focused_rule_or_none,
    row_ids,
    row_map,
    row_rule_ids,
)
from .lines import allowlist_text, dest_line, mode_label, rule_line, rule_rows
from .screens import HOLDS_HINT, DurationScreen, EdgeListView
from .wire import CONNECTED, REJECTED, UNUSABLE_TOKEN, OneFlight


class ConsentPage(HelpKeys, Screen):
    """The egress consent page (#454): the held-request queue and
    the in-effect verdicts on one full-screen page — the consent
    overlay (#358) and its rules screen merged, the tree's only
    deciding surface. The workspace page beneath keeps the link
    running; this page reads its controller.

    Two zones in reading order: the held requests (rows with
    countdowns, the queue the overlay owned) above the in-effect
    verdicts (the rules rows), under the workspace page's own
    two-line header (#460 — the name with its status and the
    pending-egress count, the id, image, host, and created date
    muted beneath, so the identity stays visible while verdicts
    are made), the mode line and static allowlist, and a status
    line naming the mode, the link's state, and the held count.
    The arrows walk a zone's rows and cross between the zones at
    their edges (an :class:`EdgeListView` a side) — no focus trap
    anywhere, and a list swap leaves the zones' focus where it
    stood (the empty queue's first hold excepted: that arrival
    takes the focus once, the moment the page's purpose
    materializes).

    The verdict keys act on the holds zone alone: ``a``/``d``
    decide the focused hold for the default duration, ``A``/``D``
    pick a duration first, and on a verdict row they decide
    nothing — a mis-zoned press names itself on the status line
    (#470 C2). ``x`` revokes the focused verdict and acts on
    nothing in the holds zone; ``m`` opens the host's mode
    picker; ``q`` or Escape returns to the workspace page — holds
    keep waiting, the header's count keeps naming them. Enter
    opens the duration picker on a focused hold (#470 C4) — the
    key answers, the pick decides; only an explicit letter or a
    picked duration ever decides.

    The page owns its per-second repaint: an unchanged row set
    repaints its countdowns in place; only a membership change
    swaps a list (the swap was the once-a-second flash #301
    reports)."""

    #: The ``?`` overlay's title (#851): the docs' name for the
    #: page.
    help_title = "egress consent page"

    #: The page sets its own initial focus (``started``: the holds
    #: zone when a hold waits, the verdicts otherwise) once the
    #: compose has settled — Textual's auto-focus would grab the
    #: first focusable (always the holds list) before that decision
    #: runs, and the holds zone's first swap would then faithfully
    #: restore the wrong zone. The empty string (not None — None
    #: inherits the app's setting) matches no widget (#454).
    AUTO_FOCUS = ""

    BINDINGS = [
        Binding("enter", "enter_pick", "Allow for…", priority=True),
        Binding("a", "allow", "Allow"),
        Binding("A", "allow_duration", "Allow…"),
        Binding("d", "deny", "Deny"),
        Binding("D", "deny_duration", "Deny…"),
        Binding("x", "revoke", "Revoke"),
        Binding("m", "mode", "Mode"),
        Binding("q", "back", "Back"),
        Binding("escape", "back", "Back", show=False),
        Binding("question_mark", "help", "Help", show=False),
    ]

    def check_action(self, action: str, parameters) -> bool | None:
        """The verdict keys ride the queue's life (#470 C3): while
        no hold waits, ``a``/``d``/``A``/``D`` leave the footer and
        go inert — a dead control hides rather than flash — and
        they return the moment a hold lands."""
        if action in {
            "allow",
            "deny",
            "allow_duration",
            "deny_duration",
        }:
            return bool(self.controller.ordered())
        return True

    def __init__(self, host) -> None:
        super().__init__()
        self.host = host
        self.workspace_id = host.row["id"]
        self.link = host.link_or_stub()
        self.flash_line = FlashLine()
        #: Whether the queue has ever held a hold while this page
        #: stood (#454): ``started`` seeds it from the frames that
        #: landed before the page opened, and the first later
        #: arrival flips it — the one focus edge the page owns.
        self.holds_known = False
        self.hold_rebuilds = OneFlight(
            lambda: self.rebuild_holds(self.controller.ordered()),
            lambda: self.app.is_running,
            "consent-holds",
        )
        self.rule_rebuilds = OneFlight(
            lambda: self.rebuild_rules(),
            lambda: self.app.is_running,
            "consent-rules",
        )

    @property
    def controller(self) -> ConsentController:
        """The host link's controller — the queue's state, shared
        with the workspace page's own lines."""
        return self.link.controller

    def compose(self) -> ComposeResult:
        yield Static(
            header_name(
                self.host.row, theme_variables=self.app.theme_variables
            ),
            id="header",
        )
        yield Static(
            header_meta(
                self.host.row,
                self.app.theme_variables,
                self.meta_width(),
            ),
            id="header-meta",
        )
        with Vertical(id="consent-page"):
            yield Static(id="allowlist")
            yield Static(id="consent-status")
            with Vertical(id="holds-zone"):
                yield Static("held requests", id="holds-label")
                yield Static(HOLDS_HINT, id="holds-hint")
                # The empty line sits above the list so the list can
                # mount at its zone's end — an anchored mount would
                # wedge the zone the moment its anchor left.
                yield Static(id="holds-empty")
                yield EdgeListView(leave_down=self.enter_rules, id="hold-rows")
            with Vertical(id="rules-zone"):
                yield Static("in effect", id="rules-label")
                yield EdgeListView(leave_up=self.enter_holds, id="rule-rows")
        yield Footer()

    def on_mount(self) -> None:
        self.set_interval(1.0, self.tick)
        # call_after_refresh: the first rebuild waits for the compose
        # stream to settle — a timer or worker racing it queries
        # widgets that are not mounted yet (the page's own rule).
        self.call_after_refresh(self.started)

    def started(self) -> None:
        """The compose has settled: build both zones, paint the
        header and the status line, and focus the holds when a hold
        waits (the urgent zone), the verdicts otherwise — the
        verdicts too when the holds list sits in a rebuild's swap
        window (a stalled loop can run the tick's first rebuild
        before this lands; a page with nowhere focused would take
        keys as inert until Tab)."""
        self.hold_rebuilds.request()
        self.rule_rebuilds.request()
        self.paint_header()
        self.update_status()
        self.holds_known = bool(self.controller.ordered())
        if self.holds_known and self.hold_rows() is not None:
            self.focus_holds()
        else:
            self.focus_rules()

    def on_show(self) -> None:
        self.hold_rebuilds.request()
        self.rule_rebuilds.request()

    # -- the per-second repaint ------------------------------------------

    def tick(self) -> None:
        """The per-second repaint: the header, both zones'
        countdowns, and the status line. A teardown race leaves the
        queries empty — noise, not a crash."""
        try:
            self.sync_holds()
            self.update_status()
            self.sync_rules()
            self.paint_header()
        except NoMatches:
            pass

    # -- the zones' focus handoffs ---------------------------------------

    def focus_holds(self) -> None:
        """Focus the holds zone from above (the page's own start):
        a highlighted row when the list carries one. The focus move
        is the screen's synchronous ``set_focus`` — ``focus()``
        lands through ``call_later``, and a list swap between the
        call and the landing leaves the screen focused on a removed
        list (keys then die on a dead widget, #322's class)."""
        rows = self.hold_rows()
        if rows is not None:
            self.set_focus(rows)
            ensure_focus(rows)

    def focus_rules(self) -> None:
        """Focus the verdicts zone from above: the first row when
        the list carries one."""
        rows = self.rule_rows()
        if rows is not None:
            self.set_focus(rows)
            ensure_focus(rows)

    def enter_rules(self) -> None:
        """The holds list's bottom edge hands the walk down: focus
        the verdicts at their first row."""
        self.focus_rules()

    def enter_holds(self) -> None:
        """The verdicts' top edge hands the walk up: focus the holds
        at their last row — the row nearest the boundary crossed."""
        rows = self.hold_rows()
        if rows is not None:
            self.set_focus(rows)
            if rows.children:
                rows.index = len(rows.children) - 1
            else:
                ensure_focus(rows)

    # -- the holds zone ---------------------------------------------------

    def hold_rows(self) -> ListView | None:
        """The holds list, or None during a rebuild's swap window."""
        try:
            return self.query_one("#hold-rows", ListView)
        except NoMatches:
            return None

    def sync_holds(self) -> None:
        """Sync the holds zone to state. A membership change (a hold
        resolved, a new one arrived) rebuilds the list fresh —
        Textual prunes removed children asynchronously, so mutating
        a live ListView leaves stale copies that shift every index
        under the highlight; a fresh list keeps the destructive
        keys' target derivable from children that are all real.
        Same-set ticks repaint survivors' countdowns in place. A
        missing list (a rebuild died mid-swap) schedules a rebuild —
        the zone self-heals instead of wedging blank."""
        rows = self.hold_rows()
        if rows is None:
            self.hold_rebuilds.request()
            return
        ordered = self.controller.ordered()
        if row_ids(rows) != {request.id for request in ordered}:
            self.hold_rebuilds.request()
            return
        self.repaint_countdowns(rows, ordered)
        self.sync_empty(ordered)

    def sync_empty(self, ordered: list) -> None:
        """The empty state rides beside the holds list, honest
        about the connection state."""
        empty = self.query_one("#holds-empty", Static)
        empty.display = not ordered
        empty.update(self.empty_line())

    def empty_line(self) -> str:
        """The empty-holds line, honest about the link's state."""
        if self.link.state == CONNECTED:
            return "No held requests — connected, waiting."
        return f"No held requests — {self.link.state}."

    def repaint_countdowns(self, rows: ListView, ordered: list) -> None:
        """Repaint each survivor's countdown in place (#470 S3):
        the bold cue follows the focused row."""
        existing = row_map(rows)
        for request in ordered:
            item = existing.get(request.id)
            if item is not None:
                item.query_one(Static).update(
                    dest_line(
                        request,
                        self.controller.remaining(request),
                        focused=item is rows.highlighted_child,
                    )
                )

    async def rebuild_holds(self, ordered: list) -> None:
        """Swap in a freshly-built holds list (its mount awaited),
        restoring focus by id (the top when the focused hold left)
        so ``a``/``d`` never retarget through a shifted index.
        Focus is read from the live list here, at rebuild time —
        never captured at arm time. A missing old list (a rebuild
        died mid-swap) is fine: the fresh list mounts anew."""
        body = self.query_one("#holds-zone", Vertical)
        old = self.hold_rows()
        focused = focused_request_id(old)
        # Read before the remove, from the screen's synchronous
        # focused widget — the reactive ``has_focus`` lags a focus
        # move by a message round-trip, and a swap reading the lag
        # would skip the restore while the move's callback still
        # targets this list. The focus is also CLEARED before the
        # remove: a focused widget's removal triggers Textual's
        # internal focus repair, which grabs the next focusable (the
        # verdicts zone) behind the swap's back.
        held_focus = old is not None and self.focused is old
        fresh = self.fresh_hold_list(ordered)
        if old is not None:
            if held_focus:
                self.set_focus(None)
            await old.remove()  # frees the id before the fresh list mounts
        await body.mount(fresh)
        self.land_hold_focus(fresh, focused, held_focus, ordered)
        self.reclaim_hold_focus(fresh, old, focused)
        self.sync_empty(ordered)
        self.repaint_countdowns(fresh, ordered)  # the bold cue lands
        # The footer follows the queue's membership (#470 C3, the
        # review round): the verdict keys live in
        # ``check_action`` — driven by the queue — and the footer
        # only re-renders its bindings on focus moves, so the
        # rebuild (a hold arriving or resolving) hands it the
        # gate's fresh answer itself.
        self.refresh_bindings()

    def fresh_hold_list(self, ordered: list) -> EdgeListView:
        """The holds zone's next list: the rows for ``ordered``, the
        down-edge handoff wired."""
        items = [self.render_hold(request) for request in ordered]
        return EdgeListView(
            *items, leave_down=self.enter_rules, id="hold-rows"
        )

    def land_hold_focus(
        self, fresh: ListView, focused, held_focus: bool, ordered: list
    ) -> None:
        """Give the fresh holds list the focus the swap owes it:
        the zone held it, or the queue's first hold landed on a page
        that opened empty — the moment the page's purpose
        materializes (#454; the overlay's unconditional
        fresh-focus, narrowed to the arrival; ``holds_known`` is
        ``started``'s record of the holds that already waited at
        open). Every other swap keeps the zones' focus where it
        stood."""
        if held_focus or (not self.holds_known and ordered):
            self.set_focus(fresh)
            focus_by_id(fresh, focused)  # after mount: index sticks
        if ordered:
            self.holds_known = True

    def reclaim_hold_focus(
        self, fresh: ListView, old: ListView | None, focused
    ) -> None:
        """The verdicts zone's reclaim, carried to the holds: a
        focus grab landing inside the remove/mount window
        (``started`` processing mid-swap on a starved loop) must not
        outlive the list it grabbed — the letters would die on the
        removed widget's closed pump ('a' never took effect,
        #468). A swap never leaves focus on the list it removed."""
        if old is not None and self.focused is old:
            self.set_focus(fresh)
            focus_by_id(fresh, focused)

    def render_hold(self, request) -> ListItem:
        """One holds-zone row."""
        item = ListItem(
            Static(dest_line(request, self.controller.remaining(request)))
        )
        item.request_id = request.id
        return item

    # -- the verdicts zone --------------------------------------------------

    def rule_rows(self) -> ListView | None:
        """The verdicts list, or None during a rebuild's swap
        window."""
        try:
            return self.query_one("#rule-rows", ListView)
        except NoMatches:
            return None

    def sync_rules(self) -> None:
        """Arm one verdicts rebuild each tick: the flight itself
        takes the cheap in-place countdown path when the row set
        is unchanged (the old rules screen's per-tick refresh, the
        decider app's repaint rule — one owner), and a membership
        change builds the fresh list."""
        self.rule_rebuilds.request()

    async def rebuild_rules(self) -> None:
        """Repaint the verdicts zone from the controller's rules
        snapshot. An unchanged row set (same ids, same order)
        repaints the survivors' countdowns in place — the per-tick
        refresh must not swap the whole list, the once-a-second
        flash #301 reports. A membership change builds a fresh
        list, preserving the focused rule by id (the top when it
        left): a mutating ListView carries asynchronously-pruned
        stale children that shift indexes, so positions must come
        from children that are all real — `x` must never retarget
        through a shifted index. A swap never moves focus between
        zones: the fresh list keeps the verdicts zone only when
        the old one held it."""
        rules = self.controller.rules
        ordered = rule_rows(rules)
        self.query_one("#allowlist", Static).update(allowlist_text(rules))
        body = self.query_one("#rules-zone", Vertical)
        old = self.rule_rows()
        if old is not None and row_rule_ids(old) == [
            rule.id for rule in ordered
        ]:
            self.repaint_rule_rows(old, ordered)
            return
        await self.swap_rule_rows(body, old, ordered)

    async def swap_rule_rows(
        self, body: Vertical, old: ListView | None, ordered: list
    ) -> None:
        """Swap in a freshly-built verdicts list (its mount
        awaited), preserving the focused rule by id (the top when
        it left) — and the holds zone's focus when it held it."""
        focused = focused_rule_id(old)
        held_focus = old is not None and self.focused is old
        fresh = EdgeListView(
            *self.render_rule_items(ordered),
            leave_up=self.enter_holds,
            id="rule-rows",
        )
        if old is not None:
            # Cleared first for the same reason the holds swap clears:
            # the removal's internal focus repair would grab the
            # holds zone without a set_focus the fresh list can win.
            if held_focus:
                self.set_focus(None)
            await old.remove()  # frees the id before the fresh list mounts
        await body.mount(fresh)
        self.land_rule_focus(fresh, old, focused, held_focus)
        self.repaint_rule_rows(fresh, ordered)  # the bold cue lands

    def land_rule_focus(
        self, fresh: ListView, old: ListView | None, focused, held_focus: bool
    ) -> None:
        """Give the fresh verdicts list the focus the swap owes it:
        the zone held it — or a focus grab landed on the dying list
        inside the remove/mount window, which the swap never leaves
        behind. The page's own ``started`` runs as a pump message,
        and on a starved loop it can process while the flight parks
        in ``old.remove()``, where the zone query still finds the
        dying list and focuses it; the list then unmounts with the
        focus on it and no repair ever comes — keys die on the
        removed widget's closed pump for good ('m' never took
        effect, #468's class, #476)."""
        if held_focus or (old is not None and self.focused is old):
            self.set_focus(fresh)
            focus_rule_by_id(fresh, focused)  # after mount: index sticks

    def render_rule_items(self, ordered: list) -> list:
        """The verdicts zone's next rows, each tagged with its rule
        id — ``x``'s target through the swap-safe read."""
        items = []
        for rule in ordered:
            item = ListItem(
                Static(rule_line(rule, self.controller.rule_remaining(rule)))
            )
            item.rule_id = rule.id
            items.append(item)
        return items

    def repaint_rule_rows(self, rows: ListView, ordered: list) -> None:
        """Repaint each surviving rule row's text in place (#470
        S3): the caller's order-equal membership match proves the
        children and ``ordered`` line up positionally, so the pass
        walks the two side by side (never id-keyed — a malformed
        frame with duplicate ids would repaint one row twice and
        leave its twin stale), moves no index, and takes no
        focus — the bold cue follows the focused row."""
        for child, rule in zip(rows.children, ordered):
            child.query_one(Static).update(
                rule_line(
                    rule,
                    self.controller.rule_remaining(rule),
                    focused=child is rows.highlighted_child,
                )
            )

    # -- the status line ---------------------------------------------------

    def meta_width(self) -> int | None:
        """The meta line's available cells: this page fills the
        terminal, so the budget follows the terminal's width
        alone (#752) — the workspace page beneath caps its own
        copy inside its centered block (#881). A width the layout
        has not settled yet (compose, before the first layout)
        paints the fixed budgets; ``started`` repaints with the
        settled width."""
        width = self.size.width - 2
        return width if width > 0 else None

    def paint_header(self) -> None:
        """The page's header — the workspace page's own two lines
        (#460): the name with its status and the pending-egress
        count, the id, image, host, and created date muted beneath.
        The row is the host's (its per-second read keeps both
        pages' headers following a workspace another surface may
        have moved), and the count rides the host's link — the
        same queue the workspace page beneath counts."""
        try:
            self.query_one("#header", Static).update(
                header_name(
                    self.host.row,
                    self.host.pending_count(),
                    self.app.theme_variables,
                )
            )
            self.query_one("#header-meta", Static).update(
                header_meta(
                    self.host.row,
                    self.app.theme_variables,
                    self.meta_width(),
                )
            )
        except NoMatches:
            pass  # teardown unmounted a header line under the worker

    def flash(self, message: str) -> None:
        """Give the status line to a message for FLASH_TTL seconds —
        a verdict's failure, a sighting's alarm (#201): the
        surfaces this page owns."""
        self.flash_line.set(message)
        self.update_status()

    def update_status(self) -> None:
        """The status line: the current mode, the link's state, the
        held count (the header above carries the workspace's
        identity); a flash owns it until its TTL lapses. A rejected
        registration names its reason — the daemon refused this
        page as the decider, and the line says why (escaped the way
        the flashes are: a truncated closing tag in the reason
        would raise in the parse, #318's rule). The held count
        drops to zero off a live link for the header's own reason:
        a dead socket's snapshot may carry holds the server
        already resolved."""
        if self.link.state in (REJECTED, UNUSABLE_TOKEN):
            state = flash_safe(self.link.reject_reason or "rejected")
        else:
            state = self.link.state
        held = (
            len(self.controller.pending) if self.link.state == CONNECTED else 0
        )
        default = (
            f"mode {mode_label(self.controller.rules)}"
            f"  ·  {state}  ·  {held} held"
        )
        # The zones' labels carry their counts (#470 C6): the
        # reader knows a section is complete without hunting a
        # scrollbar. A flash routed here while the compose still
        # settles (a sighting drained from the workspace page
        # beneath) finds the lines absent — the tick's own guard,
        # carried in, noise not a crash.
        try:
            self.query_one("#holds-label", Static).update(
                f"held requests ({held})"
            )
            self.query_one("#rules-label", Static).update(
                f"in effect ({len(rule_rows(self.controller.rules))})"
            )
            self.query_one("#consent-status", Static).update(
                self.flash_line.text(default)
            )
        except NoMatches:
            pass

    # -- verdicts ------------------------------------------------------------

    def holds_zone_focused(self) -> bool:
        """Whether the holds list owns the focus — the verdict keys
        act on it alone (#454): a hold is a question, a verdict row
        is state, and the letters never act on state."""
        rows = self.hold_rows()
        return rows is not None and rows.has_focus

    def rules_zone_focused(self) -> bool:
        """Whether the verdicts list owns the focus — ``x`` acts on
        it alone: a hold carries nothing to revoke."""
        rows = self.rule_rows()
        return rows is not None and rows.has_focus

    async def action_allow(self) -> None:
        if self.holds_zone_focused():
            await self.decide_focused("allow", DURATION_DEFAULT)
        else:
            self.flash("a decides a held request")

    async def action_deny(self) -> None:
        if self.holds_zone_focused():
            await self.decide_focused("deny", DURATION_DEFAULT)
        else:
            self.flash("d decides a held request")

    async def action_allow_duration(self) -> None:
        if self.holds_zone_focused():
            await self.pick_duration("allow")
        else:
            self.flash("A picks a duration on a held request")

    async def action_deny_duration(self) -> None:
        if self.holds_zone_focused():
            await self.pick_duration("deny")
        else:
            self.flash("D picks a duration on a held request")

    async def action_enter_pick(self) -> None:
        """Enter on a focused hold opens the duration picker (#470
        C4) — the key answers, the pick decides; Enter alone never
        decides. A focus on the verdicts answers nothing: a
        verdict row carries nothing to pick."""
        if self.holds_zone_focused():
            await self.pick_duration("allow")

    async def decide_focused(self, decision: str, duration: str) -> None:
        """Send the verdict for the focused hold through the data
        seam; a failure flashes, never crashes the tree (SystemExit
        included — the REST seam's error surface). A key pressed
        inside a rebuild's swap window reads as nothing focused."""
        request_id = focused_request_id(self.hold_rows())
        if request_id is None:
            self.flash("no hold focused")
            return
        await self.send_verdict(request_id, decision, duration)

    async def pick_duration(self, decision: str) -> None:
        """Open the duration picker for the FOCUSED hold; a picked
        duration decides that hold, a cancel decides nothing. The
        request id is captured here: the focused row can change (or
        resolve) while the picker is open, and Enter must not land
        the verdict on whatever holds focus when the pick arrives."""
        request_id = focused_request_id(self.hold_rows())
        if request_id is None:
            self.flash("no hold focused")
            return
        await self.app.push_screen(
            DurationScreen(self.finish_pick(decision, request_id))
        )

    def finish_pick(self, decision: str, request_id: str | None):
        """The callback the picker calls with the picked duration
        (or None on cancel)."""

        async def picked(duration: str | None) -> None:
            if duration is None:
                return
            # Sent unconditionally: after a reconnect the local
            # pending set is fresh (empty) while the hold may still
            # be live server-side — the server is the source of
            # truth and 404s ids that truly resolved.
            await self.send_verdict(request_id, decision, duration)

        return picked

    async def send_verdict(
        self, request_id: str, decision: str, duration: str
    ) -> None:
        """One decide through the data seam — the same exchange
        ``msks egress decide`` makes."""
        try:
            await self.host.app.data.decide(
                self.workspace_id, request_id, decision, duration
            )
        except (Exception, SystemExit) as exc:
            self.flash(f"decide failed: {flash_safe(str(exc))}")

    async def action_revoke(self) -> None:
        """Revoke the focused verdict through the data seam; the row
        leaves on the refreshed ``egress.rules`` frame, never
        optimistically (a still-enforced rule must not hide). A key
        pressed inside a rebuild's swap window reads as nothing
        focused, and a press in the holds zone decides nothing —
        a hold carries nothing to revoke."""
        if not self.rules_zone_focused():
            self.flash("x revokes a verdict row")
            return
        rule_id = focused_rule_or_none(self)
        if rule_id is not None:
            await self.revoke_rule(rule_id)

    async def revoke_rule(self, request_id: str) -> None:
        """One revoke through the data seam — the same exchange
        ``msks egress revoke`` makes; the row leaves on the
        refreshed ``egress.rules`` frame, never optimistically."""
        try:
            await self.host.app.data.revoke(self.workspace_id, request_id)
        except (Exception, SystemExit) as exc:
            self.flash(f"revoke failed: {flash_safe(str(exc))}")

    def on_list_view_selected(self, event: ListView.Selected) -> None:
        """Enter on a row decides nothing by selection: both zones
        are ListViews and the priority binding owns the key
        (#470 C4) — this handler stands for a future widget that
        selects without the binding, and only an explicit letter
        or a picked duration decides."""

    def on_list_view_highlighted(self, event: ListView.Highlighted) -> None:
        """The focused row moved: repaint the standing rows so the
        bold cue follows it (#470 S3) — in place, identity and
        focus stay put. Teardown prunes a zone's Static under the
        repaint — the walk stops, noise, not a crash."""
        try:
            rows = self.hold_rows()
            if rows is not None:
                self.repaint_countdowns(rows, self.controller.ordered())
            self.paint_rules_in_place()
        except NoMatches:
            pass

    def paint_rules_in_place(self) -> None:
        """Repaint the verdict rows when their membership stands
        (#470 S3): a swap window takes the tick's rebuild instead
        — this pass never builds."""
        rows = self.rule_rows()
        ordered = rule_rows(self.controller.rules)
        if rows is None or row_rule_ids(rows) != [rule.id for rule in ordered]:
            return
        self.repaint_rule_rows(rows, ordered)

    # -- the mode picker and back -------------------------------------------

    def action_mode(self) -> None:
        """Open the host's mode picker — the switch's one path
        (#344, #460)."""
        self.host.open_mode_picker()

    def action_back(self) -> None:
        """``q``/Escape: return to the workspace page — holds keep
        waiting, the header's count keeps naming them, and ``e``
        reopens this page."""
        self.app.pop_screen()
