"""The secret audit screen: the sighting history.

The event rows a placeholder's audit trail draws, one line per
sighting with its destination and time.
"""

from __future__ import annotations

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.css.query import NoMatches
from textual.screen import Screen
from textual.widgets import Footer, ListView, Static

from ..help import HelpKeys
from ..link import AuditLink
from .ui import (
    OneFlight,
    PickerScreen,
    event_item,
    events_note,
    flash_safe,
    focus_event_by_id,
    focused_event_id,
    render_order,
)

#: The audit filter's no-filter sentinel (#510).
FILTER_ALL = "all"


#: The audit view's kind filter choices (#390): every kind a row
#: can carry, in the issue's own order.
AUDIT_KINDS = ("swap", "sighting", "mint", "revoke", "expiry", "unlock")


class SecretAuditScreen(HelpKeys, Screen):
    """The daemon-wide audit view (#390): every workspace's
    placeholder lifecycle and wire events, newest first — the
    newest hundred recorded rows replayed from the audit listing
    at open (the endpoint's bound), the live kinds streaming in
    beside them over the events socket (a plain subscriber: no
    decider registration, so no hold ever waits on this view).
    One fact lands once however it arrives — the audit identity
    dedups the live frame against the replayed row (#305). A
    sighting row carries the highlight beside its ``!`` marker —
    the exfil signal. ``k`` and ``w`` pick the kind and workspace
    filters: a daemon-wide row's events cover every workspace, a
    scoped row's its members, a per-flow swap or sighting the tap
    that saw it (#305's replay rule carried to the filter).
    Arrows move the list; ``r`` or Escape returns to the secrets
    page — no focus trap.
    """

    #: The ``?`` overlay's title (#851): the docs' name for the
    #: view.
    help_title = "secrets audit"

    BINDINGS = [
        Binding("k", "kind", "Kind"),
        Binding("w", "workspace", "Workspace"),
        Binding("r", "back", "Back"),
        Binding("escape", "back", "Back", show=False),
        Binding("q", "back", "Back", show=False),
        # `e` from here returns instead of stacking another audit
        # view (the page's key would push otherwise) — the rules
        # screen's `r` follows the same shape.
        Binding("e", "back", "Back", show=False),
        Binding("question_mark", "help", "Help", show=False),
    ]

    def __init__(self, data, *, link_factory=None) -> None:
        super().__init__()
        self.data = data
        self.link_factory = link_factory or AuditLink
        self.link: AuditLink | None = None
        self.kind: str | None = None
        self.workspace: str | None = None
        # The replay failure's one-line reason (#390): empty when
        # the listing served its rows.
        self.seed_failed = ""
        self.rebuilds = OneFlight(
            lambda: self.rebuild_rows(),
            lambda: self.app.is_running,
            "secret-audit",
        )
        self._built: tuple | None = None

    def compose(self) -> ComposeResult:
        with Vertical(id="audit-body"):
            yield Static(events_note(), id="audit-note")
            yield Static(id="audit-status")
            yield Static(id="audit-empty")
            yield ListView(id="audit-rows")
        yield Footer()

    def link_or_stub(self) -> AuditLink:
        """The link, made on first use (compose runs before mount,
        and the link starts with the screen)."""
        if self.link is None:
            self.link = self.link_factory()
        return self.link

    def on_mount(self) -> None:
        self.link_or_stub().start()
        self.set_interval(1.0, self.tick)
        # call_after_refresh: the rows list and the seed worker both
        # wait for the compose stream to settle — a worker racing it
        # queries widgets that are not mounted yet (the overlay's
        # own rule).
        self.call_after_refresh(self.started)

    def started(self) -> None:
        """The compose has settled: focus the rows, pull the
        replay."""
        self.query_one("#audit-rows", ListView).focus()
        self.run_worker(self.load_audit, exclusive=True)

    def on_unmount(self) -> None:
        """The view is gone: the subscription closes with the
        link."""
        if self.link is not None:
            self.link.stop()

    async def load_audit(self) -> None:
        """Seed the replay from the audit listing (#390): the
        recorded lifecycle rows land oldest first behind any live
        tail, deduped on their audit identity. A listing the
        daemon cannot serve names itself on the status line — the
        live stream alone still stands."""
        try:
            rows = await self.data.secret_audit()
        except (Exception, SystemExit) as exc:
            self.seed_failed = flash_safe(str(exc))
            self.sync_status()
            return
        self.link_or_stub().controller.seed_audit(rows)
        self.rebuilds.request()

    def tick(self) -> None:
        """The per-second repaint: a moved log rebuilds the rows
        (single flight), the status line repaints beside it — its
        own guard swallows a teardown race."""
        if self.log_changed():
            self.rebuilds.request()
        self.sync_status()

    # -- the rows ----------------------------------------------------------

    def log_fingerprint(self) -> tuple:
        """The view's identity for repaint gating: the log's rows
        identity (length and newest seq) and the filters — a
        filter change repaints the rows even when the log stands
        still."""
        events = self.link_or_stub().controller.events
        rows_id = (len(events), events[-1].seq if events else 0)
        return (rows_id, self.kind, self.workspace)

    def log_changed(self) -> bool:
        """Whether the view moved since it last painted (never
        painted counts as moved)."""
        return self.log_fingerprint() != self._built

    def filtered(self) -> list:
        """The log's rows in render order under the standing
        filters, oldest first (the newest-first flip is the
        render's)."""
        rows = render_order(self.link_or_stub().controller.events)
        return [event for event in rows if self.shows(event)]

    def shows(self, event) -> bool:
        """Whether one row stands under the standing filters: the
        kind filter matches the row's kind, the workspace filter
        its coverage."""
        if self.kind is not None and event.kind != self.kind:
            return False
        return self.workspace is None or event.covers(self.workspace)

    async def rebuild_rows(self) -> None:
        """Repaint the view: the status line always, the rows list
        only when it moved — a list swap under a reading operator
        only when the rows changed, and never while the standing
        list is the truth (a missing list always rebuilds: the
        swap is also the heal)."""
        self.sync_status()
        rows_id = self.log_fingerprint()
        if self._built == rows_id and self.rows_widget() is not None:
            return
        await self.swap_rows(self.filtered())
        self._built = rows_id

    def rows_widget(self) -> ListView | None:
        """The rows list, or None during a rebuild's swap window."""
        try:
            return self.query_one("#audit-rows", ListView)
        except NoMatches:
            return None

    async def swap_rows(self, rows: list) -> None:
        """Swap in a freshly-built list (its mount awaited), newest
        first, preserving the focused row by seq (the top when it
        left): a mutating ListView carries asynchronously-pruned
        stale children that shift indexes, so positions come from
        children that are all real."""
        body = self.query_one("#audit-body", Vertical)
        old = None
        try:
            old = self.query_one("#audit-rows", ListView)
        except NoMatches:
            pass  # a died-mid-swap rebuild: mount the fresh list anew
        focused = focused_event_id(old)
        items = [event_item(event) for event in reversed(rows)]
        empty = self.query_one("#audit-empty", Static)
        empty.display = not items
        if not items:
            empty.update(self.empty_line())
        fresh = ListView(*items, id="audit-rows")
        if old is not None:
            await old.remove()  # frees the id before the fresh list mounts
        await body.mount(fresh)
        fresh.focus()
        focus_event_by_id(fresh, focused)  # after mount: index sticks

    def empty_line(self) -> str:
        """The empty state: honest about whether rows stand behind
        the filters or the log itself holds none."""
        if self.link_or_stub().controller.events:
            return "No events match the filters."
        return (
            "No placeholder events yet — mints, revokes, "
            "expiries, and unlocks appear here."
        )

    def sync_status(self) -> None:
        """The status line: the standing filters and the
        connection's state, with the seed failure named when the
        replay never landed. A teardown race leaves the query
        empty — noise, not a crash."""
        try:
            link = self.link_or_stub()
            line = (
                f" kind {self.kind or FILTER_ALL}"
                f"  ·  workspace {self.workspace or FILTER_ALL}"
                f"  ·  {link.state}"
            )
            if self.seed_failed:
                line += f"  ·  replay failed: {self.seed_failed}"
            self.query_one("#audit-status", Static).update(line)
        except NoMatches:
            pass

    # -- the filters -------------------------------------------------------

    def action_kind(self) -> None:
        """Pick the kind filter: every kind, or one."""
        current = self.kind or FILTER_ALL
        self.app.push_screen(
            PickerScreen((FILTER_ALL, *AUDIT_KINDS), current, self.kind_picked)
        )

    async def kind_picked(self, pick: str | None) -> None:
        """The kind picker's callback: a pick swaps the filter, a
        cancel keeps it."""
        if pick is None:
            return
        self.kind = None if pick == FILTER_ALL else pick
        self.rebuilds.request()

    def action_workspace(self) -> None:
        """Pick the workspace filter: every workspace, or one the
        log names — coverage decides what shows (#305's rule)."""
        current = self.workspace or FILTER_ALL
        self.app.push_screen(
            PickerScreen(
                self.workspace_options(), current, self.workspace_picked
            )
        )

    async def workspace_picked(self, pick: str | None) -> None:
        """The workspace picker's callback: a pick swaps the
        filter, a cancel keeps it."""
        if pick is None:
            return
        self.workspace = None if pick == FILTER_ALL else pick
        self.rebuilds.request()

    def workspace_options(self) -> tuple[str, ...]:
        """The workspace filter's choices: every workspace the
        log's rows name — their coverage lists and the taps that
        saw per-flow events — beside the all-rows choice. The
        names are workspace ids (the daemon mints them as hex), so
        the all-rows label can never collide with a choice."""
        events = self.link_or_stub().controller.events
        names = {
            event.workspace_id for event in events if event.workspace_id != "*"
        }
        names.update(
            workspace for event in events for workspace in event.workspaces
        )
        return (FILTER_ALL, *sorted(names))

    def action_back(self) -> None:
        """Return to the secrets page."""
        self.app.pop_screen()
