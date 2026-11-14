"""The tree's proxies surfaces (#809, part #807, #890): the
workspace's proxies page, its add and edit form, and the
workspace page's compact proxies line.

The page rides the same REST surface the ``msks proxy`` commands
make, scoped to one workspace — its rows are that workspace's
alone, and it opens from the workspace page (the action row, or
``p``). Every proxy transition the daemon publishes
(``proxy.created``, ``proxy.listening``, ``proxy.failed``,
``proxy.closed``, ``proxy.removed``, ``proxy.updated``)
pokes a refresh through a plain-subscriber connection beside the
standing interval, so a bind that fails while the page stands
paints itself without a keypress.

Spatial navigation: the arrows walk the rows, ``a`` opens the
add form, ``e`` or Enter opens the edit form on the focused row,
``D`` removes the focused row behind a confirmation, ``q`` or
Escape returns to the workspace page — no screen traps focus.
"""

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.content import Content, Span
from textual.css.query import NoMatches
from textual.events import Resize
from textual.screen import Screen
from textual.widgets import (
    Button,
    Footer,
    Input,
    ListItem,
    ListView,
    Select,
    Static,
)

from ..proxies import parse_local_spec, parse_remote_spec
from .consent import decode_frame
from .consent_ui import (
    ConfirmScreen,
    FlashLine,
    flash_safe,
    focus_attr,
    focused_attr,
)
from .detail import (
    STATUS_LABEL_W,
)
from .forms import FormSelect, SeededForm
from .help import HelpKeys
from .link import AuditLink, proxy_frame
from .rows import (
    COLUMN_GAP,
    LIST_FRAME_CELLS,
    cell_pad,
    clip,
    expanded_columns,
    list_header,
    muted_style,
    workspace_label,
)

#: The listen address a forward row names when it names none,
#: and the target address a remote row names when it names none
#: — the daemon's own defaults, mirrored for display (the client
#: imports no daemon module).
DEFAULT_LISTEN_HOST = "127.0.0.1"
DEFAULT_TARGET_HOST = "127.0.0.1"

#: The status a listening row paints in — the compact line's green
#: and the page's status cell share it (#807's display spec).
LISTENING_COLOR = "$success"

#: The status a row whose bind refused paints in — the compact
#: line's red per unbindable port.
FAILED_COLOR = "$error"


#: The form's standing note (#809, #890): the two specs in the
#: one grammar the CLI reads — ssh's own — the verb naming the
#: path that opened the form.
def form_note(verb: str) -> str:
    return (
        f"{verb} a proxy — the same specs `msks proxy add` reads\n"
        "-L [BIND:]PORT:HOST:PORT · HOST localhost names the workspace\n"
        "-R RPORT:HOST:TPORT · the gateway reaches a daemon-side service"
    )


#: The direction select's two choices (#809): ssh's own letters.
FORWARD_CHOICE = "-L"
REMOTE_CHOICE = "-R"

#: The page's standing refresh (#809): the interval the frames'
#: poke rides without a live connection — the workspaces list's
#: own rule, carried to this page (a link that cannot connect
#: still serves rows every interval).
PROXY_REFRESH_S = 5.0


def spec_text(row: dict) -> str:
    """One row's spec in the CLI's own grammar (#809): the exact
    words ``msks proxy add`` takes — the listing, the compact
    line's tooltip-free cousin, and the add form's seeded input
    share one vocabulary. A forward's bind appears only when the
    row names one (loopback is the default, unnamed); its host
    field is the workspace's two spellings."""
    if row["direction"] == "remote":
        target = row.get("target_host") or DEFAULT_TARGET_HOST
        return f"-R {row['listen_port']}:{target}:{row['target_port']}"
    listen = row.get("listen_host") or DEFAULT_LISTEN_HOST
    bind = "" if listen == DEFAULT_LISTEN_HOST else f"{listen}:"
    return f"-L {bind}{row['listen_port']}:localhost:{row['target_port']}"


def pair_text(row: dict) -> str:
    """One row's port pair (#807's display spec): the forward
    direction's ``listen -> target``, the remote direction's
    ``listen <- target`` — the arrows point the way the traffic
    bridges, read left to right."""
    arrow = "->" if row["direction"] == "forward" else "<-"
    return f"{row['listen_port']} {arrow} {row['target_port']}"


def pair_color(status: str) -> str | None:
    """One pair's color (#807): green while it listens, red while
    its bind refused; every other state (stopped, a stopped
    workspace's rows) paints the line's own foreground — the color
    names the port's reachability, not its bookkeeping."""
    if status == "listening":
        return LISTENING_COLOR
    if status == "failed":
        return FAILED_COLOR
    return None


def status_text(row: dict) -> str:
    """One row's status cell: the word, its reason riding along
    when one stands — a listening row the guest cannot reach says
    so."""
    status = row.get("status") or "stopped"
    if row.get("reason"):
        return f"{status} ({row['reason']})"
    return status


def display_order(rows: list[dict]) -> list[dict]:
    """The rows in the compact line's display order (#807): the
    forwards first, then the remotes — whatever order the daemon
    served them in."""
    return [row for row in rows if row["direction"] == "forward"] + [
        row for row in rows if row["direction"] == "remote"
    ]


def pair_separator(before: dict | None, after: dict) -> str:
    """The separator two printed pairs join with: nothing before
    the first, a comma inside a half, the bar across the divide."""
    if before is None:
        return ""
    if before["direction"] != after["direction"]:
        return " | "
    return ", "


def value_and_spans(rows: list[dict], base: int) -> tuple[str, list[Span]]:
    """The compact line's value and its colored spans, one pass in
    display order: each pair's span starts where the separators
    actually emitted put it — a comma inside a half, the bar
    across the divide."""
    value = ""
    spans: list[Span] = []
    before: dict | None = None
    for row in display_order(rows):
        sep = pair_separator(before, row)
        start = base + len(value) + len(sep)
        pair = pair_text(row)
        color = pair_color(row.get("status") or "")
        if color is not None:
            spans.append(Span(start, start + len(pair), color))
        value += sep + pair
        before = row
    return value, spans


def proxies_line(rows: list[dict], theme_variables=None) -> Content:
    """The workspace page's compact proxies line (#807, #809): one
    port pair per row — the forwards before the bar, the remotes
    after it, ``8888 -> 8889, 9111 -> 9115 | 8881 <- 8882`` — each
    pair colored by its reachability, in the status lines' own
    grammar (the ``proxies`` label muted in the label column).
    Empty when the workspace holds no rows.

    The text and the spans build in one pass over the display
    order, each separator counted as it is emitted — the pairs sit
    wherever the daemon's serve order and the halves' join put
    them, and a span computed any other way would color the
    neighbor's cells."""
    if not rows:
        return Content("")
    pad = " " * max(1, STATUS_LABEL_W - len("proxies"))
    head = f" proxies{pad}"
    value, spans = value_and_spans(rows, len(head))
    style = muted_style(theme_variables or {})
    return Content(
        head + value,
        [Span(1, 1 + len("proxies") + len(pad), style), *spans],
    )


#: The proxies page's columns (#809): the header's label and the
#: column's width, in row order — the workspaces listing's own
#: shape. The workspace's own page names the workspace in its
#: status line, so the rows spend no column on it. The fixed
#: widths hold an 80-column terminal: the widest spec
#: (``-L 0.0.0.0:2201:localhost:22``) clips its middle at the
#: SPEC minimum and reads whole on a wider one.
PROXY_COLUMNS = (
    ("SPEC", 28),
    ("LABEL", 10),
    ("STATUS", 18),
)

#: The columns the page's spare width flows into when the
#: terminal runs wider than the fixed span: the specs — the one
#: column whose content runs long.
PROXY_GROWING_LABELS = frozenset({"SPEC"})


def proxy_cells(row: dict, columns=PROXY_COLUMNS) -> tuple[str, ...]:
    """One page row's cells at the columns' widths, each clipped
    to its column whatever the daemon's vocabulary grows."""
    widths = [width for _label, width in columns]
    return (
        clip(spec_text(row), widths[0]),
        clip(row.get("label") or "", widths[1]),
        clip(status_text(row), widths[2]),
    )


def proxy_row_content(
    row: dict, columns=PROXY_COLUMNS, theme_variables: dict | None = None
) -> Content:
    """One page row's paint: the padded cells with the status
    cell alone carrying its state's color (the compact line's
    rule carried to the column). The cells ride a Content's plain
    text, so a markup-carrying label cannot shift the columns."""
    cells = proxy_cells(row, columns)
    spec, _label, status = cells
    line = COLUMN_GAP.join(
        cell_pad(cell, width)
        for cell, (_label, width) in zip(cells, columns, strict=True)
    ).rstrip()
    offset = len(cell_pad(spec, columns[0][1])) + len(COLUMN_GAP) * 2
    offset += len(cell_pad(cells[1], columns[1][1]))
    color = pair_color(row.get("status") or "")
    if color is None:
        return Content(line)
    return Content(line, [Span(offset, offset + len(status), color)])


def parsed_body(direction: str, raw: str) -> dict:
    """One spec as the create body — the CLI's own parser for the
    picked direction, its refusal raised for the note line."""
    if direction == FORWARD_CHOICE:
        return parse_local_spec(raw)
    return parse_remote_spec(raw)


def seed_spec(row: dict) -> str:
    """The row's spec as the form's spec field reads it (#890):
    the listing's own text with the direction's letter aside —
    the select carries the letter, the field carries the rest."""
    return spec_text(row).split(" ", 1)[1]


def seeded_direction(seed: dict) -> str:
    """The direction select's value for one seed row (#890): the
    letter ssh gives the row's direction; the add path's own
    forward default when no seed stands."""
    return (
        REMOTE_CHOICE if seed.get("direction") == "remote" else FORWARD_CHOICE
    )


def seeded_fields(seed: dict | None) -> tuple[str, str, str]:
    """The form's seeded values for one row (#890): the note's
    verb, the spec field's text, and the label field's text —
    the add path's blank triple when no seed stands."""
    if seed is None:
        return "add", "", ""
    return "edit", seed_spec(seed), str(seed.get("label") or "")


class ProxyLink(AuditLink):
    """The proxies page's plain-subscriber connection (#809): the
    events socket without registration, the audit view's own
    lifecycle — the page holds no decider authority, and the
    socket's relay carries every published frame to every
    subscriber. Each proxy transition pokes the page's refresh;
    nothing else lands anywhere (the page's rows come from the
    REST listing, refetched whole)."""

    def __init__(self, on_event, *, ws_factory=None) -> None:
        super().__init__(ws_factory=ws_factory)
        self.on_event = on_event

    def land_frame(self, raw: str) -> bool:
        """One frame's landing: a proxy transition pokes the
        refresh; the rest is not this page's state, and no
        registration was sent, so no rejection can arrive."""
        msg = decode_frame(raw)
        if msg is not None and proxy_frame(msg):
            self.on_event()
        return False


class ProxyFormScreen(SeededForm):
    """The add and edit form (#809, #890): the create form's
    pattern over the proxy's own fields — the workspace fixed at
    the page that opened it, the direction (ssh's two letters),
    the spec in the CLI's own grammar, and a label. The spec
    parses with the CLI's own parser, so a refusal here names
    itself on the note in the same words the command prints and
    the daemon never sees a half-parsed body. An edit arrives
    seeded with the focused row — its direction, its spec, its
    label — the add form's own shape (the mounts editor's shared
    form, one vocabulary across the pages; the walk's keys and
    the seed live on the :class:`SeededForm` base). A body that
    lands dismisses with it; the pushing page owns the exchange.
    """

    def compose(self) -> ComposeResult:
        seeded = self.seed or {}
        verb, spec, label = seeded_fields(self.seed)
        with Vertical(id="form"):
            yield Static(
                form_note(verb),
                id="form-note",
            )
            with Horizontal(classes="form-row"):
                yield Static("workspace", classes="form-label")
                # The workspace is the page's own, shown as the
                # name it reads — no control, so the walk starts at
                # the direction and never visits a fixed field.
                yield Static(workspace_label(self.scope), id="field-workspace")
            with Horizontal(classes="form-row"):
                yield Static("direction", classes="form-label")
                yield FormSelect(
                    [
                        ("forward (-L)", FORWARD_CHOICE),
                        ("reverse (-R)", REMOTE_CHOICE),
                    ],
                    value=seeded_direction(seeded),
                    allow_blank=False,
                    id="field-direction",
                    compact=True,
                )
            with Horizontal(classes="form-row"):
                yield Static("spec", classes="form-label")
                yield Input(
                    value=spec,
                    placeholder="2201:localhost:22",
                    id="field-spec",
                    compact=True,
                )
            with Horizontal(classes="form-row"):
                yield Static("label", classes="form-label")
                yield Input(
                    value=label,
                    placeholder="a note the listings show",
                    id="field-proxy-label",
                    compact=True,
                )
            with Horizontal(id="form-buttons"):
                yield Button(
                    "Save" if verb == "edit" else "Add",
                    id="do-add",
                    variant="primary",
                    compact=True,
                )
                yield Button("Cancel", id="do-cancel", compact=True)
        yield Footer()

    def on_mount(self) -> None:
        self.query_one("#field-spec", Input).focus()

    def note(self, text: str) -> None:
        """The form's note line: its standing rule, or the local
        refusal that keeps a half-parsed body home."""
        self.query_one("#form-note", Static).update(text)

    def submit(self) -> None:
        """Hand the parsed body to the callback, or keep the form
        on a local refusal — the CLI's own parser and its words
        (the ``msks: `` prefix aside, the note owns the line)."""
        raw = self.query_one("#field-spec", Input).value.strip()
        if not raw:
            self.note("the spec is the form's one required field")
            return
        direction = str(self.query_one("#field-direction", Select).value)
        try:
            body = parsed_body(direction, raw)
        except SystemExit as exc:
            # The parser's message embeds the raw spec; a markup
            # tag in it must not crash the note's Static.
            self.note(flash_safe(str(exc).replace("msks: ", "refused: ")))
            return
        label = self.query_one("#field-proxy-label", Input).value.strip()
        if label:
            body["label"] = label
        self.dismiss_with(body)


class ProxiesScreen(HelpKeys, Screen):
    """One workspace's proxies page (#809, part #807, #890): the
    workspace's proxy rows with their live status, opened from
    its page (the action row, or ``p``). ``a`` opens the add
    form, ``e`` or Enter opens the edit form on the focused row
    (the mounts editor's own letter, one vocabulary across the
    pages), and ``D`` removes the focused row behind a
    confirmation (``D``, the workspaces list's own remove
    letter); every proxy transition the daemon publishes pokes a
    refresh beside the standing interval, so a bind that fails
    while the page stands paints itself without a keypress.
    Arrows walk the rows; ``q`` or Escape returns to the
    workspace page — no trap.

    Enter reaches the edit through the list's own Selected (a
    click on a row arrives the same way) and through the
    screen's binding when the walk sits elsewhere — the mounts
    editor's own pair, carried to this page.
    """

    #: The ``?`` overlay's title (#851): the docs' name for the
    #: page.
    help_title = "proxies page"

    BINDINGS = [
        Binding("a", "add", "Add"),
        Binding("e", "edit", "Edit"),
        Binding("D", "remove", "Del"),
        Binding("enter", "edit", "Edit", show=False),
        Binding("q", "back", "Back"),
        Binding("escape", "back", "Back", show=False),
        Binding("question_mark", "help", "Help", show=False),
    ]

    def __init__(self, workspace: dict) -> None:
        super().__init__()
        self.workspace = workspace
        self.rows: list[dict] = []
        self.columns = PROXY_COLUMNS
        self.flash_line = FlashLine()
        self.shown_once = False
        self.link: ProxyLink | None = None
        # The refresh's in-flight mark (#809): a skip, not a
        # cancel — and never the widget's own ``loading`` reactive,
        # which would replace the page with Textual's loading
        # indicator on every cycle (the workspaces list's own
        # name, carried here).
        self.rows_loading = False

    # -- the page's frame ---------------------------------------------

    def compose(self) -> ComposeResult:
        yield Static(id="proxy-status")
        with Vertical(id="proxies-listing"):
            yield Static(list_header(self.columns), id="proxy-columns")
            yield Static(id="proxy-empty")
        yield Footer()

    def on_mount(self) -> None:
        self.link = self.make_link()
        self.link.start()
        self.set_interval(PROXY_REFRESH_S, self.refresh_rows)
        self.call_after_refresh(self.refresh_rows)

    def on_unmount(self) -> None:
        """The page is gone: the subscriber closes with it."""
        if self.link is not None:
            self.link.stop()

    def make_link(self) -> ProxyLink:
        """The page's subscriber connection — the tests' seam for
        a scripted one."""
        return ProxyLink(self.refresh_rows)

    def on_screen_resume(self) -> None:
        """Refresh on every return to the page (rows may have
        moved while a form or confirmation stood above); the
        first resume rides on_mount's own refresh."""
        if self.shown_once:
            self.refresh_rows()
        self.shown_once = True

    def on_resize(self, event: Resize) -> None:
        """The terminal's width moved: the columns re-width to the
        frame — the workspaces listing's own rule, carried to this
        page's columns."""
        columns = expanded_columns(
            event.size.width - LIST_FRAME_CELLS,
            base=PROXY_COLUMNS,
            grow=PROXY_GROWING_LABELS,
        )
        if columns == self.columns:
            return
        self.columns = columns
        try:
            self.query_one("#proxy-columns", Static).update(
                list_header(columns)
            )
        except NoMatches:
            return  # compose has not built the page yet
        self.paint_rows()

    def paint_rows(self) -> None:
        """Repaint the standing rows in place at the current
        columns — identity and focus stay put, the swap never
        runs; teardown may have unmounted a row's Static under
        the walk."""
        rows = self.rows_widget()
        if rows is None:
            return
        for child, row in zip(rows.children, self.rows):
            try:
                child.query_one(Static).update(
                    proxy_row_content(
                        row,
                        columns=self.columns,
                        theme_variables=(
                            self.app.theme_variables
                            if self.is_mounted
                            else None
                        ),
                    )
                )
            except NoMatches:
                pass  # teardown unmounted this row's Static

    # -- the listing ---------------------------------------------------

    def refresh_rows(self) -> None:
        """Reload the listing; one flight at a time, a skip and
        not a cancel — the workspaces list's own rule (a cancel
        landing inside the swap strands the page)."""
        if self.rows_loading:
            return
        self.rows_loading = True
        self.run_worker(self.load_rows)

    async def load_rows(self) -> None:
        try:
            rows = await self.app.data.workspace_proxies(self.workspace["id"])
        except (Exception, SystemExit) as exc:
            self.flash(f"listing failed: {flash_safe(str(exc))}")
        else:
            await self.rebuild_rows(rows)
        finally:
            self.rows_loading = False

    async def rebuild_rows(self, rows: list[dict]) -> None:
        """Swap in a freshly-built list (its mount awaited),
        preserving the focused row by key (the top when it left)
        — the consent queue's rebuild rule, carried to the page.
        An unchanged listing repaints nothing (the workspaces
        list's own rule): the standing interval's read of the same
        rows keeps its list, so focus and scroll never blip and
        the page never flickers on the clock."""
        if rows == self.rows and self.rows_widget() is not None:
            self.sync_status()
            return
        self.rows = rows
        listing = self.query_one("#proxies-listing", Vertical)
        old = self.rows_widget()
        focused = focused_attr(old, "proxy_key")
        fresh = ListView(
            *[self.row_item(row) for row in rows], id="proxy-rows"
        )
        if old is not None:
            await old.remove()  # frees the id before the fresh list mounts
        await listing.mount(fresh)
        fresh.focus()
        focus_attr(fresh, "proxy_key", focused)
        self.sync_status()

    def row_item(self, row: dict) -> ListItem:
        """One proxy row at the terminal's column widths, tagged
        with its (workspace, id) key."""
        item = ListItem(
            Static(
                proxy_row_content(
                    row,
                    columns=self.columns,
                    theme_variables=(
                        self.app.theme_variables if self.is_mounted else None
                    ),
                )
            )
        )
        item.proxy_key = (row["workspace_id"], row["id"])
        return item

    def rows_widget(self) -> ListView | None:
        """The rows list, or None during a rebuild's swap
        window."""
        try:
            return self.query_one("#proxy-rows", ListView)
        except NoMatches:
            return None

    def sync_status(self) -> None:
        """The status line: the row count beside the workspace's
        name; a flash owns the line until its TTL lapses. The
        empty state names the add key. A teardown race leaves the
        queries empty — noise, not a crash."""
        try:
            count = len(self.rows)
            scope = workspace_label(self.workspace)
            head = f" {count} prox{'y' if count == 1 else 'ies'} · {scope}"
            self.query_one("#proxy-status", Static).update(
                self.flash_line.text(Content(head))
            )
            columns = self.query_one("#proxy-columns", Static)
            columns.display = bool(self.rows)
            empty = self.query_one("#proxy-empty", Static)
            empty.display = not self.rows
            empty.update("No proxies — a adds one.")
        except NoMatches:
            pass

    def flash(self, message: str) -> None:
        """Give the page's status line to a message for FLASH_TTL
        seconds — the page's own surface (the app-level flash
        paints the list's status line, which a pushed page
        hides)."""
        self.flash_line.set(message)
        self.sync_status()

    async def guarded(self, label: str, work):
        """Await one page action, flashing the failure on this
        page's status line."""
        try:
            return await work
        except (Exception, SystemExit) as exc:
            self.flash(f"{label} failed: {flash_safe(str(exc))}")
            return None

    # -- the focused row into actions ---------------------------------

    def focused_row(self) -> dict | None:
        """The focused row's dict, or None when nothing is focused
        (an empty listing, or a rebuild's swap window)."""
        key = focused_attr(self.rows_widget(), "proxy_key")
        if key is None:
            return None
        return next(
            (
                row
                for row in self.rows
                if (row["workspace_id"], row["id"]) == key
            ),
            None,
        )

    def on_list_view_selected(self, event: ListView.Selected) -> None:
        """Enter or a click on a row edits it — the list owns the
        key (the binding is the same action for a focus walk that
        lands elsewhere), the mounts editor's own rule."""
        self.action_edit()

    def action_add(self) -> None:
        """``a``: the add form over the page, the workspace fixed
        to the page's own — the form's callback runs the exchange
        (async, the mint form's rule)."""
        self.app.push_screen(ProxyFormScreen(self.workspace, self.added))

    async def added(self, body: dict | None) -> None:
        """The add form's callback: a body lands through the data
        seam — the daemon owns every gate (a squatted bind names
        its holder) — and the outcome names the row the way the
        CLI's own output does; a cancel decides nothing."""
        if body is None:
            return
        row = await self.guarded(
            "add",
            self.app.data.create_proxy(self.workspace["id"], body),
        )
        if row is not None:
            self.flash(f"added {flash_safe(spec_text(row))}")
            self.refresh_rows()

    def action_edit(self) -> None:
        """``e`` or Enter: the form over the page seeded with the
        focused row — its direction, its spec, its label — the
        form's callback runs the exchange."""
        row = self.focused_row()
        if row is None:
            self.flash("no proxy focused")
            return
        self.app.push_screen(
            ProxyFormScreen(self.workspace, self.edited(row), seed=row)
        )

    def edited(self, row: dict):
        """The edit form's callback: the row the form rewrites,
        the body it dismissed with."""

        async def answered(body: dict | None) -> None:
            if body is None:
                return
            fresh = await self.guarded(
                "edit",
                self.app.data.update_proxy(
                    row["workspace_id"], row["id"], body
                ),
            )
            if fresh is not None:
                self.flash(f"saved {flash_safe(spec_text(fresh))}")
                self.refresh_rows()

        return answered

    def action_remove(self) -> None:
        """``D``: ask first (a remove closes the listener
        at once), then delete through the data seam."""
        row = self.focused_row()
        if row is None:
            self.flash("no proxy focused")
            return
        label = (
            f" [{flash_safe(str(row['label']))}]" if row.get("label") else ""
        )
        # The spec embeds the stored bind address, whose charset
        # the parser leaves open — the same escape every flash
        # path here applies.
        question = (
            f"remove {flash_safe(spec_text(row))}{label}"
            f" (workspace {flash_safe(str(workspace_label(row)))})?"
        )
        self.app.push_screen(
            ConfirmScreen(question, self.remove_answered(row))
        )

    def remove_answered(self, row: dict):
        """The confirmation's callback: a yes deletes, a no
        decides nothing."""

        async def answered(yes: bool) -> None:
            if not yes:
                return
            reply = await self.guarded(
                "remove",
                self.app.data.remove_proxy(row["workspace_id"], row["id"]),
            )
            if reply is not None:
                self.flash(f"removed proxy {row['id']}")
                self.refresh_rows()

        return answered

    def action_back(self) -> None:
        """Return to the surface that pushed the page."""
        self.app.pop_screen()
