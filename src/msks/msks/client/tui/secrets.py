"""The tree's secrets page (#390, #393): every placeholder row the
daemon holds with revoke and renew on the focused row, the
daemon-wide audit stream one key away, the mint form, and the
mint's one-time panel — extracted from the app shell so
:mod:`msks.client.tui.main_app` composes them.

Spatial navigation: arrows walk the rows, ``k``/``w`` pick the
audit view's filters, ``r`` or Escape returns to the page, the
mint form rides the shared form walk, and the picker's edges hand
the walk back to the form — no screen traps focus.
"""

import asyncio
import base64
import re
from datetime import datetime

from rich.markup import escape
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.css.query import NoMatches
from textual.screen import ModalScreen, Screen
from textual.widgets import (
    Button,
    Footer,
    Input,
    ListItem,
    ListView,
    Select,
    SelectionList,
    Static,
)
from textual.widgets.selection_list import Selection

from .consent_ui import (
    ConfirmScreen,
    DurationScreen,
    FlashLine,
    OneFlight,
    PickerScreen,
    event_item,
    events_note,
    flash_safe,
    fmt_duration,
    focus_attr,
    focus_event_by_id,
    focused_attr,
    focused_event_id,
    render_order,
)
from .forms import FormSelect, FormWalk
from .link import AuditLink
from .rows import clip as clip_cell
from .rows import (
    clock_now,
    created_label,
    list_header,
    padded_cells,
    parse_stamp,
    status_content,
    workspace_label,
)

#: The secrets page's columns (#390): the header's label and the
#: column's width, in row order — the listing's own rule (#347)
#: over the placeholder row: coverage (``*`` for the daemon-wide
#: row, the scoped id list), name, destination allowlist, the
#: remaining lifetime, the created date. At 80 columns the frame's
#: edges and the scrollbar leave 74 cells; the columns and their
#: gaps together are exactly that wide.
SECRET_COLUMNS = (
    ("COVERAGE", 14),
    ("NAME", 16),
    ("DESTS", 20),
    ("EXPIRES", 8),
    ("CREATED", 8),
)

#: The secrets page's column widths, in row order.
(
    SECRET_COVERAGE_W,
    SECRET_NAME_W,
    SECRET_DESTS_W,
    SECRET_EXPIRES_W,
    SECRET_CREATED_W,
) = (width for _label, width in SECRET_COLUMNS)

#: The renew picker's TTL choices (#390): the duration picker over
#: TTL-appropriate labels — the hours-to-a-month span a shared
#: credential lives, not the verdict picker's minutes-to-forever.
SECRET_TTLS = ("1h", "6h", "1d", "7d", "30d")

#: Each choice's seconds — the ``ttl_s`` the renew endpoint takes.
SECRET_TTL_SECONDS = {
    "1h": 3600,
    "6h": 21600,
    "1d": 86400,
    "7d": 604800,
    "30d": 2592000,
}


def coverage_text(row: dict) -> str:
    """The row's coverage label (#390): ``*`` for the daemon-wide
    row, the covered workspace ids joined for a scoped one — the
    client's own copy of the CLI's label (the client-isolation
    rule keeps the daemon's table out of this process)."""
    workspaces = row.get("workspaces") or []
    return ",".join(sorted(set(workspaces))) if workspaces else "*"


def ttl_text(row: dict, now: datetime | None = None) -> str:
    """The EXPIRES cell (#390): the remaining lifetime's compact
    label — a live countdown the tick repaints — ``never`` for an
    unbounded row, ``expired`` past the deadline, ``-`` for a
    stamp that does not parse."""
    expires = row.get("expires_at")
    if not expires:
        return "never"
    stamp = parse_stamp(expires)
    if stamp is None:
        return "-"
    when = clock_now() if now is None else now
    remaining = (stamp - when).total_seconds()
    return fmt_duration(remaining) if remaining > 0 else "expired"


def secret_row_cells(
    row: dict, now: datetime | None = None
) -> tuple[str, ...]:
    """The placeholder row's column cells (#390), each clipped to
    its column's width; the created cell reads the listing's own
    relative label (#350)."""
    return (
        clip_cell(coverage_text(row), SECRET_COVERAGE_W),
        clip_cell(row.get("name") or "-", SECRET_NAME_W),
        clip_cell(", ".join(row.get("dests") or []) or "-", SECRET_DESTS_W),
        clip_cell(ttl_text(row, now), SECRET_EXPIRES_W),
        clip_cell(created_label(row.get("created_at")), SECRET_CREATED_W),
    )


def secret_row_text(row: dict, now: datetime | None = None) -> str:
    """One placeholder listing row's text (#390): the padded cells
    — plain text, never markup, so a markup-carrying name cannot
    shift the columns."""
    return padded_cells(secret_row_cells(row, now), SECRET_COLUMNS).rstrip()


def ttl_default(row: dict) -> str:
    """The renew picker's highlighted choice (#390): the choice
    nearest the row's remaining lifetime — a bounded row renews
    onto something like what it had — the longest choice for an
    unbounded one (the endpoint takes a ttl; the picker offers no
    unbounded renew)."""
    expires = row.get("expires_at")
    stamp = parse_stamp(expires) if expires else None
    if stamp is None:
        return SECRET_TTLS[-1]
    remaining = (stamp - clock_now()).total_seconds()
    return min(
        SECRET_TTLS,
        key=lambda label: abs(SECRET_TTL_SECONDS[label] - remaining),
    )


def revoke_note(row: dict, reply: dict) -> str:
    """The revoke's outcome line (#390): the retired row's label,
    plus the store's leftover note when the value stayed behind
    (the CLI's own wording — ``msks secret check`` reports it)."""
    note = f"revoked {escape(coverage_text(row))}/{escape(row['name'])}"
    if not reply.get("store_cleaned", True):
        note += " (store value left behind; msks secret check reports it)"
    return note


def renew_note(row: dict, reply: dict) -> str:
    """The renew's outcome line (#390): the extended row's label
    with its new remaining lifetime — the page speaks the
    countdown's vocabulary, not the CLI's absolute stamp."""
    label = f"{escape(coverage_text(row))}/{escape(row['name'])}"
    return f"renewed {label} · expires {ttl_text(reply)}"


#: The mint form's daemon-wide choice (#393): one row every
#: accepting workspace honors — the CLI's mint without a
#: ``--workspace`` target, the daemon's own default.
COVERAGE_WIDE = "daemon-wide"

#: The mint form's scoped choice (#393): one row whose coverage
#: set is the workspaces the form's picker holds.
COVERAGE_SCOPED = "scoped"

#: The coverage select's choices (#393): the label names what each
#: pick mints.
COVERAGE_CHOICES = (
    ("daemon-wide — every accepting workspace", COVERAGE_WIDE),
    ("scoped — the picked workspaces", COVERAGE_SCOPED),
)

#: The mint form's TTL choices (#393): the unbounded row first —
#: the daemon's default when a mint carries no ttl — then the
#: renew picker's own span (#390).
MINT_TTLS = ("unbounded", *SECRET_TTLS)

#: The TTL select's choices (#393): the label is the choice.
MINT_TTL_CHOICES = tuple((label, label) for label in MINT_TTLS)

#: The scoped sentinel's prefix (#393): the client's own copy of
#: the daemon's spelling (the client-isolation rule keeps the
#: daemon's secretstore out of this process) — the panel's reach
#: line decodes it.
SCOPED_SENTINEL = "mskssec1_"

#: The daemon-wide sentinel's prefix (#393): every accepting
#: workspace's tap swaps it.
WIDE_SENTINEL = "mskssec2_"

#: The mint name's pattern (#393): the client's own copy of
#: the store's identifier rule — letters, numbers, and underscores,
#: no leading digit.
MINT_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

#: One destination entry's pattern (#393): an exact hostname or a
#: label-anchored suffix — the client's own copy of the daemon's
#: rule, so a junk entry is refused on the form where the
#: operator can still edit it.
DEST_ENTRY = re.compile(
    r"^\.?[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)*$"
)


def split_entries(value: str) -> list[str]:
    """The repeatable entries one comma-separated field carries
    (#393): ``a, b , , c`` becomes ``[a, b, c]`` — the CLI's own
    repeatable, comma-splitting flag shape, held in one input."""
    return [entry.strip() for entry in value.split(",") if entry.strip()]


def refused_dest(dests: list[str]) -> str | None:
    """The first destination entry the daemon's pattern refuses
    (#393), or None — the check lands here, on the form, so a
    junk entry is named where the operator can still edit it."""
    for entry in dests:
        if not DEST_ENTRY.match(entry.lower().rstrip(".")):
            return entry
    return None


def sentinel_reach(row: dict) -> str:
    """The sentinel's reach line (#393), decoded from its prefix:
    ``mskssec2_`` swaps on every accepting workspace's tap,
    ``mskssec1_`` on the row's own coverage set — the prefix names
    the reach from the string alone (the daemon's own rule,
    duplicated here per the client-isolation rule)."""
    sentinel = row.get("sentinel") or ""
    if sentinel.startswith(SCOPED_SENTINEL):
        return f"the chosen workspaces: {escape(coverage_text(row))}"
    return "every accepting workspace"


def osc52_sequence(text: str) -> str:
    """The OSC 52 clipboard-copy sequence (#393): the payload
    base64-encoded inside the ``52;c;`` selection — a terminal
    that honors OSC 52 answers it by filling its clipboard, over
    ssh included."""
    payload = base64.b64encode(text.encode("utf-8")).decode("ascii")
    return f"\x1b]52;c;{payload}\x07"


def osc52_copy(app, text: str) -> bool:
    """Write the copy sequence through the app's driver (#393):
    the sequence rides beside the frame the driver flushes — a
    terminal that ignores OSC 52 shows nothing and copies
    nothing, and the sentinel stays on the panel. Returns whether
    the sequence left through a driver (a teardown race holds no
    device to write through)."""
    driver = getattr(app, "_driver", None)
    if driver is None:  # a teardown race — nothing to write through
        return False
    driver.write(osc52_sequence(text))
    driver.flush()
    return True


def minted_note(row: dict) -> str:
    """The mint's outcome line (#393, #423): the row's label in
    the page's own vocabulary — the value and the sentinel stay
    on the one-time panel above the page."""
    return f"minted {escape(coverage_text(row))}/{escape(row['name'])}"


class SecretsScreen(Screen):
    """The secrets page (#390): every placeholder row the daemon
    holds — coverage, name, destinations, a live TTL countdown,
    the created date — with revoke and renew on the focused row,
    the audit stream one key away, and the mint form on `c` (#393
    — its one-time panel, the value beside the sentinel, replaces
    the form). The coverage flip stays on the workspace page (a
    picker beside the egress-mode row); Enter on a row owns
    nothing yet — the placeholder-to-workspace links land with the
    cross-references (#394).
    """

    BINDINGS = [
        Binding("x", "revoke", "Revoke"),
        Binding("r", "renew", "Renew"),
        Binding("e", "audit", "Audit"),
        Binding("c", "create", "New"),
        Binding("q", "back", "Back"),
        Binding("escape", "back", "Back", show=False),
    ]

    def __init__(self) -> None:
        super().__init__()
        self.rows: list[dict] = []
        self.flash_line = FlashLine()
        self.shown_once = False

    def compose(self) -> ComposeResult:
        yield Static(id="status")
        with Vertical(id="secrets-listing"):
            yield Static(list_header(SECRET_COLUMNS), id="secret-columns")
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
        """One placeholder row, tagged with the row's id."""
        item = ListItem(Static(secret_row_text(row)))
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
                child.query_one(Static).update(secret_row_text(row, now))
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
            default = status_content(count, self.app.url, "placeholder")
            self.query_one("#status", Static).update(
                self.flash_line.text(default)
            )
            columns = self.query_one("#secret-columns", Static)
            columns.display = bool(self.rows)
            empty = self.query_one("#secret-empty", Static)
            empty.display = not self.rows
            empty.update(
                "No placeholders — c mints one; the sentinel shows once."
            )
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
        """Enter on a row decides nothing yet: the
        placeholder-to-workspace navigation lands with the
        cross-references (#394)."""

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
        """The mint form's callback (#393, #423): a mint that lands
        dismisses with its reply — the page refreshes so the row
        stands on the list, and the one-time panel (the value
        beside the sentinel) replaces the form; a cancel decides
        nothing."""
        if row is None:
            return
        self.refresh_rows()
        self.flash(minted_note(row))
        self.app.push_screen(SentinelPanel(row))


#: The audit view's kind filter choices (#390): every kind a row
#: can carry, in the issue's own order.
AUDIT_KINDS = ("swap", "sighting", "mint", "revoke", "expiry")

#: The pickers' no-filter label.
FILTER_ALL = "all"


class SecretAuditScreen(Screen):
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

    BINDINGS = [
        Binding("k", "kind", "Kind"),
        Binding("w", "workspace", "Workspace"),
        Binding("r", "back", "Back"),
        Binding("escape", "back", "Back", show=False),
        Binding("q", "back", "Back", show=False),
        # `e` from here returns instead of stacking another audit
        # view (the page's key would push otherwise) — the rules
        # screen's `r` follows the same shape.
        Binding("e", "back", show=False),
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
            "No placeholder events yet — mints, revokes, and "
            "expiries appear here."
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


class WorkspacePicker(SelectionList):
    """The mint form's workspace multi-select (#393): space
    toggles the highlighted option, and the arrows keep the
    stock walk through the options — leaving the list at its
    edges: up from the first option returns to the field above,
    down from the last moves to the field below (the
    spatial-navigation rule: the arrows alone reach every field
    in reading order)."""

    BINDINGS = [
        Binding("up", "edge_previous", show=False),
        Binding("down", "edge_next", show=False),
    ]

    def on_focus(self) -> None:
        """A freshly-seeded picker holds no highlight (its options
        land after construction), so the focus puts one on the
        first option — space toggles from the first press."""
        if self.highlighted is None and self.option_count:
            self.highlighted = 0

    def at_top(self) -> bool:
        """Whether the walk leaves upward from here: no options,
        nothing highlighted, or the first option highlighted."""
        return self.option_count == 0 or self.highlighted in (None, 0)

    def at_bottom(self) -> bool:
        """Whether the walk leaves downward from here: no options,
        nothing highlighted, or the last option highlighted."""
        return self.option_count == 0 or self.highlighted == (
            self.option_count - 1
        )

    def action_edge_previous(self) -> None:
        """Up: the interior walks the options, the top edge
        returns the walk to the form."""
        if self.at_top():
            self.app.action_focus_previous()
        else:
            self.action_cursor_up()

    def action_edge_next(self) -> None:
        """Down: the interior walks the options, the bottom edge
        hands the walk back to the form."""
        if self.at_bottom():
            self.app.action_focus_next()
        else:
            self.action_cursor_down()


class MintScreen(FormWalk, ModalScreen[dict | None]):
    """The mint form (#393, #423): the create form's pattern over
    the mint's own fields — name, repeatable destinations, coverage
    (the daemon-wide row, or the workspaces a multi-select picks
    from the tree's own list), and the TTL (unbounded, the
    daemon's default, or the renew picker's span). There is no
    value field: the daemon mints the value itself. Submit checks
    the store first (``msks secret check``'s endpoint), then
    mints; a refusal anywhere names itself on the note and the
    fields stay for a retry. A mint that lands dismisses with its
    reply — the row carrying the value and the sentinel exactly
    once — and the page replaces the form with the one-time
    panel."""

    # The walk's keys: named here, not on FormWalk — Textual
    # merges BINDINGS from DOMNode bases alone (the mixin's
    # note records the rule), and the actions live there.
    BINDINGS = [
        Binding("up", "walk_previous", show=False),
        Binding("down", "walk_next", show=False),
        Binding("escape", "cancel", "Cancel", show=False),
        Binding("q", "cancel", show=False),
    ]

    #: Whether the mint's exchange is in the air (#393): the
    #: sentinel rides exactly one reply, so the flight owns the
    #: form until it lands — a second submit or a cancel in the
    #: air would drop the reply on a mint the daemon already took
    #: (and the retry would find the name taken).
    flighting = False

    def compose(self) -> ComposeResult:
        with Vertical(id="form"):
            yield Static(
                "mint a placeholder — the value and sentinel show once",
                id="form-note",
            )
            with Horizontal(classes="form-row"):
                yield Static("name", classes="form-label")
                yield Input(
                    placeholder="letters, numbers, underscores",
                    id="field-name",
                    compact=True,
                )
            with Horizontal(classes="form-row"):
                yield Static("dests", classes="form-label")
                yield Input(
                    placeholder="host or .suffix, comma-separated",
                    id="field-dests",
                    compact=True,
                )
            with Horizontal(classes="form-row"):
                yield Static("coverage", classes="form-label")
                yield FormSelect(
                    COVERAGE_CHOICES,
                    value=COVERAGE_WIDE,
                    allow_blank=False,
                    id="field-coverage",
                    compact=True,
                )
            with Horizontal(classes="form-row tall", id="workspaces-row"):
                yield Static("workspaces", classes="form-label")
                yield WorkspacePicker(id="field-workspaces")
            with Horizontal(classes="form-row"):
                yield Static("ttl", classes="form-label")
                yield FormSelect(
                    MINT_TTL_CHOICES,
                    value=MINT_TTLS[0],
                    allow_blank=False,
                    id="field-ttl",
                    compact=True,
                )
            with Horizontal(id="form-buttons"):
                yield Button(
                    "Mint", id="do-mint", variant="primary", compact=True
                )
                yield Button("Cancel", id="do-cancel", compact=True)
        yield Footer()

    def on_mount(self) -> None:
        self.query_one("#field-name", Input).focus()
        # The daemon-wide default hides the picker row — a hidden
        # control holds no focus, so the walk skips it.
        self.sync_coverage()
        self.run_worker(self.load_workspaces, exclusive=True)

    async def load_workspaces(self) -> None:
        """Seed the picker from the tree's own workspace list
        (#393); a refusal names itself on the note — the form
        stands, and the daemon-wide mint needs no list."""
        try:
            rows = await self.app.data.workspaces()
        except (Exception, SystemExit) as exc:
            self.note(f"workspace list failed: {flash_safe(str(exc))}")
            return
        self.query_one("#field-workspaces", WorkspacePicker).add_options(
            Selection(workspace_label(row), row["id"]) for row in rows
        )

    # -- the fields ------------------------------------------------------

    def field_value(self, field: str) -> str:
        """One plain field's value, stripped."""
        return self.query_one(f"#field-{field}", Input).value.strip()

    def coverage_value(self) -> str:
        """The coverage select's choice."""
        return str(self.query_one("#field-coverage", Select).value)

    def ttl_value(self) -> str | None:
        """The TTL select's choice — the unbounded pick rides no
        ``ttl_s``, the daemon's own default."""
        value = str(self.query_one("#field-ttl", Select).value)
        return None if value == MINT_TTLS[0] else value

    def picked_workspaces(self) -> list[str]:
        """The picker's selected workspace ids, sorted — the
        coverage set the mint scopes to."""
        picker = self.query_one("#field-workspaces", WorkspacePicker)
        return sorted(picker.selected)

    # -- the coverage toggle -----------------------------------------------

    def on_select_changed(self, event: Select.Changed) -> None:
        """The coverage select's pick drives the picker row (#393):
        the daemon-wide row needs no workspaces, so the row hides;
        a scoped pick shows it."""
        if event.select.id == "field-coverage":
            self.sync_coverage()

    def sync_coverage(self) -> None:
        """Show the picker row only for a scoped pick."""
        self.query_one("#workspaces-row", Horizontal).display = (
            self.coverage_value() == COVERAGE_SCOPED
        )

    # -- the submit --------------------------------------------------------

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "do-mint":
            self.submit()
        else:
            self.action_cancel()

    def action_cancel(self) -> None:
        """Cancel — never mid-flight (#393): the reply the daemon
        already owes this form would land on a screen nobody is
        reading, and the retry would find the name taken. The
        flight lands first (it names its own refusal, or the
        panel replaces the form)."""
        if self.flighting:
            return
        self.dismiss_with(None)

    def submit(self) -> None:
        """The mint: the local checks first (a refusal keeps the
        fields), then the store check and the mint through the
        data seam — one worker, one flight, and a submit while a
        flight is in the air is a no-op (its reply is the one the
        daemon already owes this form)."""
        if self.flighting:
            return
        self.run_worker(self.do_mint, exclusive=True)

    def note(self, text: str) -> None:
        """The form's note line: its title, or the refusal that
        keeps a half-filled body home."""
        self.query_one("#form-note", Static).update(text)

    def body(self) -> dict | None:
        """The mint body (#393, #423): the daemon only sees whole
        bodies — the name's shape, at least one destination, and
        the scoped coverage's non-empty pick are checked
        piecewise, each refusal naming itself on the note. The
        value is none of the form's business: the daemon mints
        it."""
        identity = self.identity()
        if identity is None:
            return None
        name, dests = identity
        body: dict = {"name": name, "dests": dests}
        if not self.coverage_body(body):
            return None
        ttl = self.ttl_value()
        if ttl is not None:
            body["ttl_s"] = SECRET_TTL_SECONDS[ttl]
        return body

    def identity(self) -> tuple[str, list[str]] | None:
        """The name and the destinations, checked locally (#393);
        None (with the note naming the refusal) when either fails
        its check."""
        name = self.field_value("name")
        if not name:
            self.note("a name is required")
            return None
        if not MINT_NAME.match(name):
            self.note(
                "name: letters, numbers, or underscores, no leading digit"
            )
            return None
        dests = split_entries(self.field_value("dests"))
        if not dests:
            self.note("at least one destination is required")
            return None
        bad = refused_dest(dests)
        if bad is not None:
            self.note(
                f"dest {flash_safe(bad)}: an exact hostname or a "
                "label-anchored suffix like .example.com"
            )
            return None
        return name, dests

    def coverage_body(self, body: dict) -> bool:
        """The scoped coverage's pick into ``body``; False (with
        the note naming the refusal) when a scoped pick holds
        nothing."""
        if self.coverage_value() != COVERAGE_SCOPED:
            return True
        workspaces = self.picked_workspaces()
        if not workspaces:
            self.note(
                "scoped coverage picks at least one workspace — space toggles"
            )
            return False
        body["workspaces"] = workspaces
        return True

    async def do_mint(self) -> None:
        """The mint's flight: the local checks first (a refusal
        keeps the fields and never takes off), then the exchange
        with the flight owning the form until it lands."""
        body = self.body()
        if body is None:
            return
        self.flighting = True
        try:
            await self.mint_flight(body)
        finally:
            self.flighting = False

    async def mint_flight(self, body: dict) -> None:
        """The exchange proper: the store check names a broken
        store before a doomed mint runs; a refused mint names
        itself on the note with the fields kept for a retry; a
        mint that lands dismisses with its reply (#393 — the
        value and the sentinel ride the reply exactly once)."""
        self.note("checking the secret store…")
        try:
            await self.app.data.secret_check()
        except (Exception, SystemExit) as exc:
            self.note(f"secret store check failed: {flash_safe(str(exc))}")
            return
        self.note("minting…")
        try:
            row = await self.app.data.mint_secret(body)
        except (Exception, SystemExit) as exc:
            self.note(f"mint failed: {flash_safe(str(exc))}")
            return
        self.dismiss_with(row)

    def dismiss_with(self, row: dict | None) -> None:
        """Dismiss and hand the reply to the callback (async — the
        exchange runs as a task, so the modal closes without
        waiting on it)."""
        self.dismiss()
        # Referenced: an unreferenced task can be collected mid-await.
        self._task = asyncio.create_task(self.submitted(row))


class SentinelPanel(ModalScreen):
    """The mint's one-time panel (#393, #423): the reply replaces
    the form with the value — the secret the operator pastes into
    the external service — and the sentinel, each with its reach
    line, and the closing rule: the display ends with the panel,
    and a lost value or sentinel is re-minted, never recalled.
    `c` copies the value and `s` the sentinel over OSC 52 — the
    copy path a terminal that honors the sequence answers, over
    ssh included; a terminal that does not honors nothing and the
    strings stay on the panel until it closes. Closing clears the
    panel's text: neither leaves a trace in the widget tree
    behind it.
    """

    BINDINGS = [
        Binding("c", "copy_value", "Copy value"),
        Binding("s", "copy_sentinel", "Copy sentinel"),
        Binding("q", "close", "Close"),
        Binding("escape", "close", "Close", show=False),
    ]

    def __init__(self, row: dict) -> None:
        super().__init__()
        self.row = row

    def compose(self) -> ComposeResult:
        with Vertical(id="sentinel-panel"):
            yield Static(
                f"minted {escape(coverage_text(self.row))}/"
                f"{escape(self.row['name'])}",
                id="panel-note",
            )
            yield Static("value (shown once):", id="panel-value-label")
            yield Static(self.row.get("value") or "", id="panel-value")
            yield Static("sentinel (shown once):", id="panel-label")
            yield Static(self.row.get("sentinel") or "", id="panel-sentinel")
            yield Static(
                f"reach: {sentinel_reach(self.row)}", id="panel-reach"
            )
            yield Static(
                "a lost value or sentinel is re-minted, never recalled — "
                "this display ends with the panel",
                id="panel-rule",
            )
            with Horizontal(id="panel-buttons"):
                yield Button(
                    "Copy value",
                    id="do-copy-value",
                    variant="primary",
                    compact=True,
                )
                yield Button(
                    "Copy sentinel", id="do-copy-sentinel", compact=True
                )
                yield Button("Close", id="do-close", compact=True)
        yield Footer()

    def on_mount(self) -> None:
        self.query_one("#do-copy-value", Button).focus()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "do-copy-value":
            self.action_copy_value()
        elif event.button.id == "do-copy-sentinel":
            self.action_copy_sentinel()
        else:
            self.action_close()

    def copied_note(self, what: str) -> str:
        """The copy's outcome line: what copied, and where the
        terminal honors the sequence."""
        return (
            f"copied the {what} to the clipboard — where the terminal "
            "honors OSC 52"
        )

    def action_copy_value(self) -> None:
        """The value's OSC 52 copy (#423): the sequence rides the
        driver beside the frame; the note names what happened."""
        copied = osc52_copy(self.app, self.row.get("value") or "")
        self.query_one("#panel-note", Static).update(
            self.copied_note("value")
            if copied
            else "the copy did not land — no terminal to write through"
        )

    def action_copy_sentinel(self) -> None:
        """The sentinel's OSC 52 copy (#393), beside the value's."""
        copied = osc52_copy(self.app, self.row.get("sentinel") or "")
        self.query_one("#panel-note", Static).update(
            self.copied_note("sentinel")
            if copied
            else "the copy did not land — no terminal to write through"
        )

    def action_close(self) -> None:
        """Close: the panel's text clears first (#393, #423) — the
        value and the sentinel leave the widget tree with the
        display."""
        for widget_id in (
            "panel-note",
            "panel-value-label",
            "panel-value",
            "panel-label",
            "panel-sentinel",
            "panel-reach",
            "panel-rule",
        ):
            try:
                self.query_one(f"#{widget_id}", Static).update("")
            except NoMatches:
                pass  # teardown unmounted this line first
        self.row = {}
        self.dismiss()
