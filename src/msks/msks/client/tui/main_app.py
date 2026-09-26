"""The workspace TUI (#309): the full-screen tree rooted at the
workspaces list, launched by ``msks tui``.

The tree's one leaf that needs the whole terminal — a console
shell as the new-terminal action's dead-launcher fallback — runs
as a chained app: the page action records itself on the
:class:`TuiFollow` queue and exits the tree, :func:`run_main_tui`
runs the flow, and the tree restarts where it left off (the
workspace page reopens). The consent decider lives inside the
tree instead (#358): the workspace page pushes a
:class:`ConsentOverlay` — a modal panel over the page holding the
held-request queue — by hand from its action list, and by itself
when a hold arrives. The screens talk to the daemon through
:class:`TuiData` — the same REST surface the ``msksc`` commands
use — and the page holds a :class:`DeciderLink` so pending holds
land on it.

The page's shell action (#341) is the one shell that does not
chain: it spawns the operator's terminal launcher with an
ssh invocation appended (:func:`spawn_window`) — ssh, not the
console, because a live window resizes and only ssh propagates the
resize to the guest — and the tree keeps running beside the
window.

Spatial navigation: every screen is a list the arrows walk, the
create form's arrows move between its fields, and Escape always
leaves the screen it is on — no screen traps focus.
"""

import asyncio
import json
import sys
import time
from datetime import UTC, datetime

from rich.cells import cell_len
from rich.markup import escape
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.content import Content, Span
from textual.css.query import NoMatches
from textual.screen import ModalScreen, Screen
from textual.widgets import (
    Button,
    Footer,
    Input,
    ListItem,
    ListView,
    Select,
    Static,
)

from ..config import DEFAULT_TERMINAL_CMD, ClientConfig
from ..console import run_workspace_shell
from ..create import invoking_user
from ..resize import resize_message
from ..rest import env_token, env_url
from .consent_ui import (
    DURATION_DEFAULT,
    EMPTY_STATIC_QUESTION,
    FLASH_TTL,
    ConfirmScreen,
    DurationScreen,
    EventsScreen,
    ModeScreen,
    OneFlight,
    RulesScreen,
    dest_line,
    duration_label,
    ensure_focus,
    flash_safe,
    focus_by_id,
    focused_request_id,
    mode_label,
    row_ids,
    row_map,
    shared_ssl,
    sighting_flash,
    switch_mode_path,
)
from .data import TuiData
from .link import CONNECTED, REJECTED, DeciderLink

#: The full-terminal flows a page can record (#309): the console
#: shell as the new-terminal action's dead-launcher fallback. The
#: consent decider stopped chaining when it moved into the page's
#: overlay (#358).
FLOW_SHELL = "shell"

#: The workspace page's shell action (#341) — a page action, not a
#: flow: the tree keeps running while the window owns its own
#: terminal.
ACTION_SHELL_WINDOW = "shell-window"

#: The workspace page's egress-mode action (#344): the shared mode
#: picker over the page — the posture switches without leaving the
#: workspace.
ACTION_EGRESS_MODE = "egress-mode"

#: The workspace page's consent action (#358): the consent overlay
#: — the held-request queue as a panel over the page, opened by
#: hand; the page also opens it by itself when a hold arrives.
ACTION_CONSENT = "egress-consent"

#: How many granted scopes the consent line spells out before the
#: "… (+N more)" cap.
GRANT_CAP = 3


def require_terminal() -> None:
    """The tree owns the terminal; a pipe on either side cannot host
    it (the console command's own guard, with this command's
    name)."""
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        raise SystemExit(
            "msks: the TUI needs an interactive tty on stdin and stdout"
        )


def workspace_label(row: dict) -> str:
    """The row's human-facing label (#246): its name, else its id."""
    return row.get("name") or row["id"]


#: The listing's columns (#347): the header's label and the
#: column's width, in row order — every row pads each field to its
#: column's width, so the columns line up down the list. The name
#: column pays for the frame's edges (#349): at 80 columns the
#: border and the scrollbar together leave 74 cells for a row,
#: and the widest label the created column reads (#350,
#: ``yesterday``) must fit whole beside the columns before it.
LIST_COLUMNS = (
    ("NAME", 22),
    ("STATUS", 10),
    ("EGRESS", 12),
    ("IMAGE", 12),
    ("CREATED", 10),
)

#: The space between two listing columns.
COLUMN_GAP = "  "

#: The listing's column widths, in row order.
NAME_W, STATUS_W, EGRESS_W, IMAGE_W, CREATED_W = (
    width for _label, width in LIST_COLUMNS
)


def cell_prefix(text: str, width: int) -> str:
    """The longest head of ``text`` that fits ``width`` display
    cells (a wide character that would cross the budget stays
    whole; the head may land a cell short)."""
    out: list[str] = []
    used = 0
    for char in text:
        wide = cell_len(char)
        if used + wide > width:
            break
        out.append(char)
        used += wide
    return "".join(out)


def cell_pad(text: str, width: int) -> str:
    """The text left-justified to ``width`` display cells — a
    wide-character name cannot shift the columns beside it."""
    short = width - cell_len(text)
    return text + " " * short if short > 0 else text


def list_header() -> str:
    """The listing's header row (#347): the column labels, each
    left-justified to its column's width — the offsets the rows
    pad their fields to."""
    return COLUMN_GAP.join(
        cell_pad(label, width) for label, width in LIST_COLUMNS
    ).rstrip()


def padded_cells(cells: tuple[str, ...]) -> str:
    """One listing line's cells joined: each left-justified to its
    column's display width, two spaces between columns."""
    return COLUMN_GAP.join(
        cell_pad(cell, width)
        for cell, (_label, width) in zip(cells, LIST_COLUMNS, strict=True)
    )


#: The running status's color (#348) and the color every
#: other unmapped state renders in — a state the map does not
#: know takes the warning color, so a vocabulary the daemon
#: grows still stands out.
RUNNING_COLOR = "$success"

#: The color every status outside the map's two entries renders
#: in.
OTHER_STATUS_COLOR = "$warning"

#: The muted ratio a theme spells some other way than Textual's
#: own "auto 60%" falls back to.
DEFAULT_MUTED_RATIO = "60%"


def muted_style(theme_variables: dict) -> str:
    """The stopped status's color (#348): the theme's text
    variable at the theme's own muted ratio — "$text-muted"
    itself is a widget-css color ("auto 60%"), and a content
    span parses its style as a rich style, where the auto half
    does not resolve; riding ``$text`` at the same ratio renders
    near the same muted text, within a few color values (the
    auto base composes slightly differently in a span than in
    widget css). A theme that spells its muted color without a
    ratio rides the 60% Textual's own themes use."""
    parts = theme_variables.get("text-muted", "").split()
    ratio = (
        parts[1]
        if len(parts) == 2 and parts[1].endswith("%")
        else DEFAULT_MUTED_RATIO
    )
    return f"$text {ratio}"


def status_color(status: str, theme_variables: dict | None = None) -> str:
    """The status column's color (#348): a theme variable the
    render resolves against the active theme — running in the
    success color, stopped in muted text at the theme's own
    ratio, any other state in the warning color."""
    if status == "running":
        return RUNNING_COLOR
    if status == "stopped":
        return muted_style(theme_variables or {})
    return OTHER_STATUS_COLOR


def status_class(status: str) -> str:
    """The class a row's status becomes (#348): the status itself
    when it reads as one ASCII CSS word (Textual's class names
    are ASCII — a wider word would raise), else ``other``."""
    return status if status.isidentifier() and status.isascii() else "other"


def clock_now() -> datetime:
    """The wall clock the relative created labels read (#350) —
    the tests' seam for a pinned date."""
    return datetime.now().astimezone()


#: The pinned bucketing ladder (#350): a day count under the
#: bound reads as its days divided by the span, in the span's
#: unit — under 7 reads ``Nd ago``, under 30 ``Nw ago`` (13 days
#: reads ``1w ago``), under 365 ``Nmo ago``; past the ladder a
#: year count reads ``Ny ago``.
AGE_BUCKETS = (
    (7, 1, "d"),
    (30, 7, "w"),
    (365, 30, "mo"),
)


def age_label(days: int) -> str:
    """One bucket of the pinned rule (#350) over whole calendar
    days: ``today`` and ``yesterday`` cover the first two days
    (a clock that trails its stamp — skew — stays at ``today``),
    then the AGE_BUCKETS ladder, then ``Ny ago``."""
    if days <= 0:
        return "today"
    if days == 1:
        return "yesterday"
    for bound, span, unit in AGE_BUCKETS:
        if days < bound:
            return f"{days // span}{unit} ago"
    return f"{days // 365}y ago"


def parse_stamp(created_at: str) -> datetime:
    """The stamp as an aware datetime — the daemon sends naive
    UTC, so a bare stamp reads as UTC — or ``None`` when the text
    does not parse."""
    try:
        stamp = datetime.fromisoformat(created_at)
    except ValueError:
        return None
    return stamp if stamp.tzinfo else stamp.replace(tzinfo=UTC)


def created_label(created_at: str | None, now: datetime | None = None) -> str:
    """The CREATED column's relative label (#350), the pinned
    bucketing (:func:`age_label`) over whole calendar days
    between the creation and the clock. The daemon stamps in
    UTC; the label reads the creation in the operator's own
    calendar day. A stamp that is missing or unparseable reads
    ``-``."""
    stamp = parse_stamp(created_at) if created_at else None
    if stamp is None:
        return "-"
    when = clock_now() if now is None else now
    local = stamp.astimezone(when.tzinfo) if when.tzinfo else stamp
    return age_label((when.date() - local.date()).days)


def row_cells(row: dict) -> tuple[str, ...]:
    """The row's column cells (#347), each clipped to its
    column's width whatever the daemon's vocabulary grows; the
    created cell reads as a relative label (#350)."""
    return (
        clip(workspace_label(row), NAME_W),
        clip(row["status"], STATUS_W),
        clip(row.get("egress_mode") or "-", EGRESS_W),
        clip(row.get("image_hash") or "-", IMAGE_W),
        clip(created_label(row.get("created_at")), CREATED_W),
    )


def row_content(row: dict, theme_variables: dict | None = None) -> Content:
    """One listing row (#348): the padded cells with the status
    cell alone carrying its state's color. The span's style is a
    theme variable the render resolves against the active theme
    (the muted ratio reads the theme's own); the cells ride a
    Content's plain text — never parsed as markup — so a
    markup-carrying name cannot shift the columns (the name
    cell's own length fixes the span's offset: a wide-character
    name pads with fewer characters than its display width)."""
    cells = row_cells(row)
    name, status, *_ = cells
    line = padded_cells(cells).rstrip()
    offset = len(cell_pad(name, NAME_W)) + len(COLUMN_GAP)
    span = Span(
        offset,
        offset + len(status),
        status_color(row["status"], theme_variables),
    )
    return Content(line, [span])


def header_name(
    row: dict, pending: int = 0, theme_variables: dict | None = None
) -> Content:
    """The header's first line (#351): the workspace's name in
    the default foreground, its status beside it in the status
    color (the list's status coloring), and — while holds wait on
    the page's queue — the pending-egress count (#354): the
    segment leaves with the last hold. The name rides a Content's
    plain text, so a markup-carrying name cannot shift the span."""
    name = workspace_label(row)
    status = row["status"]
    text = f" {name}  ·  {status}"
    if pending:
        text += f"  ·  egress to decide: {pending}"
    offset = 1 + len(name) + len("  ·  ")
    span = Span(
        offset,
        offset + len(status),
        status_color(status, theme_variables),
    )
    return Content(text, [span])


def header_meta(row: dict) -> str:
    """The header's second line (#351): the immutable id, the
    image hash, the host, and the created date — the page paints
    the line muted, and it truncates at the terminal's edge
    (an ellipsis marks the cut) beside the name's own line."""
    image = (row.get("image_hash") or "-")[:12]
    host = row.get("host") or "-"
    created = (row.get("created_at") or "")[:10] or "-"
    return (
        f" id {escape(row['id'])}  ·  image {escape(image)}"
        f"  ·  host {escape(host)}  ·  created {escape(created)}"
    )


def grant_text(rule, remaining: float | None) -> str:
    """One granted scope with its expiry: host, port (or all
    ports), and the duration label."""
    port = " (all ports)" if rule.dest_port == 0 else f":{rule.dest_port}"
    label = duration_label(rule, remaining)
    return f"{escape(rule.dest_host)}{port} ({label})"


def granted_line(controller) -> str:
    """The granted scopes for the consent status line, capped; the
    honest absence when nothing is in effect."""
    rules = controller.rules
    if rules is None or not rules.allowed:
        return "no active consent"
    grants = [
        grant_text(rule, controller.rule_remaining(rule))
        for rule in rules.allowed[:GRANT_CAP]
    ]
    extra = len(rules.allowed) - GRANT_CAP
    suffix = f" (+{extra} more)" if extra > 0 else ""
    return ", ".join(grants) + suffix


def consent_line(link, row: dict) -> str:
    """The workspace page's consent status line (#309): the granted
    scope with its expiry, or the honest absence — the row's
    recorded mode until the first rules frame lands. The state is
    named whenever it is not connected: a drop never implies that
    silence is data (the controller keeps its last snapshot through
    the backoff ladder, so the line says so beside it)."""
    if link.state == REJECTED:
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


def focused_attr(rows: ListView | None, attr: str):
    """The focused row's ``attr`` value, or None when nothing is
    focused (a None rows is a rebuild's swap window)."""
    child = rows.highlighted_child if rows is not None else None
    return getattr(child, attr, None) if child is not None else None


def focus_attr(rows: ListView, attr: str, target) -> None:
    """Highlight the row carrying ``target`` (the top when it left
    or was never set) — positions come from a freshly-built list,
    so every child under the index is real."""
    for position, child in enumerate(rows.children):
        if target is not None and getattr(child, attr, None) == target:
            rows.index = position
            return
    ensure_focus(rows)


#: The daemon URL's cell budget in the status line (#349): the
#: URL is a hint, not data — the clip keeps the standing line on
#: one row at 80 columns whatever the operator's MSKSC_URL
#: carries, the middle ellipsis keeping both ends of a long URL
#: readable.
URL_W = 48


def status_content(count: int, url: str) -> Content:
    """The status line's standing content (#349): the workspace
    count — the fact that moves while the operator works — set in
    the bold default foreground, the daemon's URL riding after it
    in the line's own muted color (the span names ``$text`` so a
    theme's foreground answers, not the line's muted base)."""
    plural = "" if count == 1 else "s"
    head = f" {count} workspace{plural}"
    line = f"{head}  ·  {clip(url, URL_W)}"
    return Content(line, [Span(0, len(head), "$text bold")])


class FlashLine:
    """A message that owns the TUI's status lines until its TTL
    lapses — the consent app's flash, lifted one level so every
    screen's status line shows it."""

    def __init__(self) -> None:
        self.msg = ""
        self.until = 0.0

    def set(self, message: str) -> None:
        """Give the status lines to a message for FLASH_TTL
        seconds."""
        self.msg = message
        self.until = time.time() + FLASH_TTL

    def text(self, default: str | Content) -> str | Content:
        """The flash while it lives, else ``default``."""
        if self.until > time.time():
            return self.msg
        return default


class TuiFollow:
    """What happens after the TUI exits (#309): one full-terminal
    flow — a console shell, the dead-launcher fallback — or nothing
    (the operator quit; the consent decider stopped chaining when it
    moved into the page's overlay, #358). Also carries the workspace
    page the tree reopens when a flow hands the terminal back."""

    def __init__(self) -> None:
        self.action: tuple[str, str] | None = None
        self.reopen: str | None = None
        self.seed: str | None = None

    def request(self, kind: str, workspace_id: str) -> None:
        """Record one flow to run after the TUI exits."""
        self.action = (kind, workspace_id)

    def take(self) -> tuple[str, str] | None:
        """The recorded flow, cleared as it is taken."""
        action, self.action = self.action, None
        return action


def run_shell_flow(workspace_id: str) -> None:
    """A console shell in the workspace — the dead-launcher
    fallback's flow (the console boots a stopped workspace first)."""
    run_workspace_shell(workspace_id)


def ssh_child_argv(workspace_id: str) -> list[str]:
    """The ssh invocation the new-terminal action appends to the
    launcher (#341): this client's own interpreter and module (an
    editable checkout spawns itself; an installed client its own
    environment), then the ssh command and the workspace — ssh
    over the console because a fresh window gets resized, and the
    console session sizes its guest pty once, at connect, while
    ssh carries every resize to the guest. The child needs no
    connection flags: the tree's bootstrap already materialized
    every winner — the file's and the ``--daemon`` flag's alike —
    into the environment the child inherits, so it reaches the
    same daemon by inheritance.
    """
    return [sys.executable, "-m", "msks.client.cli", "ssh", workspace_id]


async def spawn_window(argv: list[str]):
    """Run the launcher detached (#341): its own session, its
    stdio on devnull — the window borrows no terminal the tree
    holds, and the tree's later exit never takes it down. The
    asyncio child watcher reaps the launcher when it closes, so
    the tree holds no waitable handle and leaves no zombie.
    """
    return await asyncio.create_subprocess_exec(
        *argv,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
        start_new_session=True,
    )


#: The full-terminal flows, keyed by the kind a page records.
FLOWS = {
    FLOW_SHELL: run_shell_flow,
}


def run_follow_up(action: tuple[str, str]) -> None:
    """Run one recorded flow; the kinds a page can record are
    exactly the FLOWS keys."""
    kind, workspace_id = action
    FLOWS[kind](workspace_id)


def run_main_tui(
    open_ref: str | None = None, data=None, conf: ClientConfig | None = None
) -> int:
    """``msks tui`` (#309): the tree, chaining the full-terminal
    flows.

    ``open_ref`` names a workspace (by name or id) whose page the
    tree opens directly — ``msks tui my-workspace`` — instead of
    the list. The env and TLS context are read before the first
    screen draws (a missing token exits with the one readable line
    every sibling command prints, and the TOFU warning lands once,
    before a screen owns the terminal). Each recorded flow runs
    between two runs of the tree; the tree reopens the workspace
    page it was on, and an exit with no recorded flow is the
    operator's quit.

    ``conf`` is the invocation's resolved client config (#341):
    the page's new-terminal shell action reads the terminal
    launcher from it.
    """
    if data is None:
        require_terminal()
        env_url()
        env_token()
        shared_ssl()
    follow = TuiFollow()
    follow.reopen = open_ref
    while True:
        MsksTuiApp(follow, data=data, conf=conf).run()
        action = follow.take()
        if action is None:
            return 0
        try:
            run_follow_up(action)
        except SystemExit as exc:
            # The flow refused: its one-line reason rides the
            # follow queue as the restarted tree's first flash (a
            # SystemExit prints nowhere until the interpreter's
            # top level, which the restart would eat). Real
            # exceptions (bugs) still surface.
            follow.seed = str(exc)


class MsksTuiApp(App):
    """The tree app: the workspaces list at the root, one page per
    workspace above it, the create form above that."""

    CSS = """
    Screen { layout: vertical; }
    #status { padding: 0 1; background: $panel; color: $text-muted; }
    #listing { border: round $primary; background: $panel; }
    #columns { padding: 0 1; color: $text-muted; }
    #rows ListItem { height: 1; padding: 0 1; }
    #rows ListItem Static { text-wrap: nowrap; }
    #empty { padding: 1 2; color: $text-muted; }
    #header { height: 1; padding: 0 1; background: $panel;
              text-wrap: nowrap; text-overflow: ellipsis; }
    #header-meta { height: 1; padding: 0 1; background: $panel;
                   color: $text-muted; text-wrap: nowrap;
                   text-overflow: ellipsis; }
    #consent { padding: 0 1; color: $text-muted; }
    #actions ListItem { height: 1; }
    WorkspaceForm { align: center middle; }
    #form { width: 64; height: auto; background: $panel;
            border: round $primary; padding: 1 2; }
    #form-note { color: $text-muted; margin-bottom: 1; }
    .form-row { height: 1; margin-bottom: 1; }
    .form-label { width: 12; color: $text-muted; }
    .form-row Input, .form-row Select { width: 1fr; }
    #form Input:focus { background-tint: $foreground 15%; }
    #form-buttons { height: auto; align-horizontal: center;
                    margin-top: 1; }
    #form-buttons Button { margin: 0 2; }
    ConfirmScreen { align: center middle; }
    #question { padding: 1 2; background: $panel;
                border: round $primary; }
    ConsentOverlay { align: center middle; }
    #consent-panel { width: 64; height: auto; background: $panel;
                     border: round $primary; padding: 1 2; }
    #consent-status { height: 1; padding: 0 1; margin-bottom: 1;
                      color: $text-muted; text-wrap: nowrap;
                      text-overflow: ellipsis; }
    #consent-rows { height: auto; max-height: 12; }
    #consent-rows ListItem { height: 1; }
    #consent-empty { height: 1; padding: 0 1; color: $text-muted; }
    #events-note { padding: 0 1; color: $text-muted; }
    #events-empty { padding: 0 1; color: $text-muted; }
    #event-rows ListItem { height: 1; }
    #event-rows ListItem.sighting { color: $warning; text-style: bold; }
    """

    def get_default_screen(self) -> Screen:
        """The tree's root: the workspaces list."""
        return MainScreen()

    def __init__(
        self, follow: TuiFollow | None = None, data=None, conf=None
    ) -> None:
        super().__init__()
        self.follow = follow or TuiFollow()
        self.data = data or TuiData()
        self.flash_line = FlashLine()
        # The operator's terminal launcher (#341), read by the
        # page's new-terminal shell action. A tree without a
        # resolution (a test's injected data seam) launches with
        # the built-in.
        self.terminal_cmd = (
            list(conf.terminal_open_cmd)
            if conf is not None
            else list(DEFAULT_TERMINAL_CMD)
        )
        # The launchers the page has spawned (#341): held until
        # they exit, so a running window's Process never collects
        # with an unawaited exit (the reaper drops each at its
        # close; the asyncio watcher does the reaping itself).
        self.reapers: set = set()

    def hold_child(self, proc) -> None:
        """Hold one spawned launcher until it exits (#341): the
        referenced task survives collection, and its done-callback
        drops it from the set once the window has closed."""
        task = asyncio.create_task(proc.wait())
        self.reapers.add(task)
        task.add_done_callback(self.reapers.discard)

    def flash(self, message: str) -> None:
        """Give the status lines to a message for FLASH_TTL
        seconds."""
        self.flash_line.set(message)

    def quit_after(self, kind: str, workspace_id: str) -> None:
        """Record one full-terminal flow and exit the tree; the
        runner executes the flow and restarts the tree on the page
        it left."""
        self.follow.request(kind, workspace_id)
        self.exit()

    def on_mount(self) -> None:
        self.title = "msks"
        if self.follow.seed is not None:
            # A refused flow's one-line refusal, carried from the
            # plain terminal the flow owned (a SystemExit prints
            # nowhere until the interpreter's top level — which the
            # tree's restart would otherwise eat).
            self.flash(self.follow.seed)
            self.follow.seed = None
        if self.follow.reopen is not None:
            self.run_worker(self.push_remembered, exclusive=True)

    async def push_remembered(self) -> None:
        """Open the remembered workspace's page against a fresh
        listing; a workspace that left stays on the list (its own
        refresh names the failure)."""
        ref = self.follow.reopen
        try:
            rows = await self.data.workspaces()
        except Exception, SystemExit:
            return
        for row in rows:
            if ref in (row["id"], row.get("name")):
                # The operator may have opened a page by hand while
                # this worker fetched — never stack a second one.
                if isinstance(self.screen, MainScreen):
                    self.push_screen(WorkspaceScreen(row))
                return


class MainScreen(Screen):
    """The tree's root (#309): every workspace one row; create,
    start, stop, and remove happen here; Enter opens the
    workspace's page."""

    BINDINGS = [
        Binding("enter", "open", "Open", show=False),
        Binding("c", "create", "New"),
        Binding("s", "start", "Start"),
        Binding("x", "stop", "Stop"),
        Binding("D", "remove", "Remove"),
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
            self.app.flash(f"listing failed: {escape(str(exc))}")
            return
        await self.rebuild_rows(rows)

    async def rebuild_rows(self, rows: list[dict]) -> None:
        """Swap in a freshly-built list (its mount awaited),
        preserving the focused workspace by id (the top when it
        left) — the consent queue's rebuild rule, carried to the
        listing."""
        self.rows = rows
        listing = self.query_one("#listing", Vertical)
        old = self.rows_widget()
        focused = focused_attr(old, "workspace_id")
        items = [self.row_item(row) for row in rows]
        fresh = ListView(*items, id="rows")
        if old is not None:
            await old.remove()  # frees the id before the fresh list mounts
        await listing.mount(fresh)
        fresh.focus()
        focus_attr(fresh, "workspace_id", focused)
        self.sync_status()

    def row_item(self, row: dict) -> ListItem:
        """One listing row, tagged with the workspace's id and a
        class from its status (#348)."""
        item = ListItem(Static(row_content(row, self.app.theme_variables)))
        item.workspace_id = row["id"]
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
        listing's header row shows while rows stand; the empty
        state takes its place when they do not. A screen going
        away under the timer or a worker leaves the query empty —
        teardown noise, not a crash."""
        try:
            count = len(self.rows)
            default = status_content(count, env_url())
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

    def action_start(self) -> None:
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
        (None) decides nothing."""
        if body is None:
            return
        result = await guarded_flash(
            self.app, "create", self.app.data.create(body)
        )
        if result is None:
            return
        row, path = result
        self.app.flash(created_note(row, path))
        self.refresh_rows()

    def action_quit(self) -> None:
        self.app.exit()


#: The workspace page's fixed actions (#309), top to bottom. The
#: LLM token's remint stays on the CLI (`msks llm-token
#: --remint` prints the fresh token, the part the page cannot
#: usefully show) — #343 took the action off the page: its flash
#: painted the list's status line, which the pushed page hides.
PAGE_ACTIONS = (
    (ACTION_SHELL_WINDOW, "Open a shell (new terminal)"),
    (ACTION_CONSENT, "Egress consent — decide holds, review rules and events"),
    (ACTION_EGRESS_MODE, "Switch the egress mode"),
    ("edit", "Edit settings — sizes and topology"),
    ("start", "Start"),
    ("stop", "Stop"),
)


class WorkspaceScreen(Screen):
    """One workspace's page (#309): the header's two lines (#351
    — the name with its status on the first, the id, image, host,
    and created date muted on the second, the pending-egress
    count beside the status while holds wait, #354), the consent
    status line, and the page's actions — a shell in a new window
    (#341), the consent overlay (#358), the egress-mode switch
    (#344), start, and stop. The page is the workspace's decider
    while it is open: holds land on its link, the header counts
    them, and the first hold of a burst opens the consent overlay
    by itself — the overlay's own docstring owns the panel's
    lifecycle."""

    BINDINGS = [
        Binding("enter", "run", "Go", show=False),
        Binding("q", "back", "Back"),
        Binding("escape", "back", "Back", show=False),
    ]

    def __init__(self, row: dict, *, link_factory=None) -> None:
        super().__init__()
        self.row = row
        self.link_factory = link_factory or self.make_link
        self.link: DeciderLink | None = None
        # The consent overlay over this page (#358): None while the
        # page owns the terminal alone. The page pushes it by hand
        # (the action row) and by itself (the first hold of a
        # burst); the overlay pops itself closed.
        self.overlay: ConsentOverlay | None = None
        # The parked burst for the overlay's auto-open (#358): the
        # ids of the holds the operator closed the overlay on (the
        # burst keeps its rows without re-popping). It ends when
        # none of its ids stand in a settled queue — the replay
        # re-landing them keeps it, a live queue resolving them ends
        # it, and the registration's reset window (the queue's truth
        # in flight) touches it not at all.
        self.parked_ids: frozenset[str] | None = None
        # The page's own flash (#344): a message that owns the
        # consent line for FLASH_TTL seconds. The app-level flash
        # paints the list's status line, which the pushed page
        # hides (#343) — a page-raised failure names itself here,
        # where the operator reads it.
        self.flash_line = FlashLine()
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
        yield Static(header_meta(self.row), id="header-meta")
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
        the header's pending-egress count, the sighting drain, and
        the overlay's auto-open watch. A screen going away under
        the timer leaves the queries empty — teardown noise, not a
        crash."""
        try:
            self.watch_holds()
            self.paint_consent()
            self.paint_header()
            self.sync_actions()
        except NoMatches:
            pass

    # -- the consent overlay (#358) ----------------------------------

    def watch_holds(self) -> None:
        """The per-tick watch over the link: drain the sighting
        buffer (#201 over #358), then the burst bookkeeping that
        owns the overlay's auto-open."""
        if self.link is None:
            return
        self.drain_sightings()
        self.settle_park()
        if self.link.replay_pending:
            return  # the replay is in flight: the queue's truth waits
        if self.pending_count() and self.burst_surfaces():
            self.push_overlay(auto=True)

    def drain_sightings(self) -> None:
        """Flash the off-allowlist sightings the frames landed since
        the last drain, each on the surface that owns the
        terminal."""
        for event in self.link.take_sightings():
            self.flash_sighting(sighting_flash(event))

    def settle_park(self) -> None:
        """End the parked burst when none of its holds stand in a
        settled queue. The registration's reset window (between the
        controller's reset and the replay's first frame) never ends
        one: the holds the operator parked are in flight, and the
        replay re-lands the survivors of them."""
        if self.parked_ids is None or self.link.replay_pending:
            return
        standing = set(self.link.controller.pending)
        if self.parked_ids & standing:
            return
        self.parked_ids = None

    def burst_surfaces(self) -> bool:
        """Whether the burst may surface an overlay now: the overlay
        already stands, the burst is parked, or a modal owns the
        terminal's top (the mode picker, a confirmation) — each
        says the burst needs no new panel this tick."""
        if self.overlay is not None or self.parked_ids is not None:
            return False
        return self.app.screen is self

    def flash_sighting(self, line: str) -> None:
        """One drained sighting on the surface that owns the
        terminal: the overlay's status line while it is up, this
        page's consent line when it is not (#358 carries #201's
        rule — the exfil signal interrupts wherever the operator
        is)."""
        if self.overlay is not None:
            self.overlay.flash(line)
        else:
            self.flash(line)

    def push_overlay(self, *, auto: bool) -> None:
        """Push the consent overlay over this page — by hand from
        the action row (``auto`` False: it stays until the operator
        closes it) or by the hold watch (``auto`` True: it closes
        itself when the queue empties). One panel stands at a time:
        a delayed Enter worker landing after the tick auto-opened
        one no-ops here (the panel the operator asked for is up)."""
        if self.overlay is not None:
            return
        self.overlay = ConsentOverlay(self, auto=auto)
        self.app.push_screen(self.overlay)

    def overlay_parked(self) -> None:
        """The overlay closed on the operator's key: the burst stays
        surfaced by the header's count alone. The park records the
        queue's ids — the truth about what waits — because the count
        folds the connection state (0 on a dropped link whose
        snapshot still carries holds) and a park recorded there
        would be forgotten; the drop's reconnection replays the same
        holds, and the panel the operator closed stays closed."""
        self.overlay = None
        if self.link is not None and self.link.controller.ordered():
            self.parked_ids = frozenset(
                request.id for request in self.link.controller.ordered()
            )

    def overlay_auto_closed(self) -> None:
        """The overlay's queue emptied and it closed itself — no
        park to remember (nothing waits)."""
        self.overlay = None

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
                header_meta(self.row)
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
        status); one flight at a time."""
        self.run_worker(self.load_row, exclusive=True)

    async def load_row(self) -> None:
        try:
            rows = await self.app.data.workspaces()
        except Exception, SystemExit:
            return  # the page keeps its row; the header stays as it was
        fresh = next(
            (row for row in rows if row["id"] == self.row["id"]), None
        )
        if fresh is not None:
            self.row = fresh
            self.paint_header()

    # -- the action rows -----------------------------------------------------

    def actions_widget(self) -> ListView | None:
        """The action list, or None during a rebuild's swap
        window."""
        try:
            return self.query_one("#actions", ListView)
        except NoMatches:
            return None

    def fixed_items(self) -> list[ListItem]:
        """The page's actions, each tagged with its kind."""
        items = []
        for kind, text in PAGE_ACTIONS:
            item = ListItem(Static(text))
            item.page_action = kind
            item.page_key = ("action", kind)
            items.append(item)
        return items

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

    def sync_actions(self) -> None:
        """The list carries only the fixed actions — a tick has no
        countdowns to repaint and no membership to watch; a list
        gone missing (a swap window, a teardown race) rebuilds
        itself."""
        if self.actions_widget() is None:
            self.rebuilds.request()

    # -- the actions ---------------------------------------------------

    async def action_run(self) -> None:
        """Enter: the focused row's page action runs."""
        kind = self.focused_action()
        if kind is None:
            return
        if kind == ACTION_CONSENT:
            self.push_overlay(auto=False)
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
            ACTION_EGRESS_MODE: self.pick_egress_mode,
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
        child = ssh_child_argv(self.row["id"])
        try:
            proc = await spawn_window([*self.app.terminal_cmd, *child])
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
        nothing."""
        if body is None:
            return
        reply = await self.guarded_page_flash(
            "edit", self.app.data.resize(self.row["id"], body)
        )
        if reply is None:
            return
        for field in ("root_mib", "home_mib", "cpus", "mem_mib"):
            if field in reply:
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
        """Push the mode picker (#344, #358) over whatever surface
        the page hosts — the page's own action row, the consent
        overlay's ``m``, and the rules screen's ``m`` all take this
        one path: the current mode starts highlighted (the
        snapshot's mode; the row's until the first rules frame
        lands), and the pick goes to the switch path, which owns the
        empty-static confirmation."""
        rules = self.page_rules()
        current = (
            rules.mode
            if rules is not None
            else (self.row.get("egress_mode") or "")
        )
        self.app.push_screen(ModeScreen(current, self.switch_mode))

    async def pick_egress_mode(self) -> None:
        """Open the mode picker over the page (#344)."""
        self.open_mode_picker()

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
        names itself on the page's consent line: the app-level
        flash paints the list's status line, which the pushed page
        hides (#343)."""
        try:
            reply = await self.app.data.set_egress_mode(
                self.row["id"], mode, confirm_empty=confirm_empty
            )
        except (Exception, SystemExit) as exc:
            self.flash(f"mode switch failed: {flash_safe(str(exc))}")
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
        self.overlay = None
        self.app.pop_screen()


class ConsentOverlay(ModalScreen):
    """The consent panel over the workspace page (#358): the
    held-request queue with the verdict keys, one centered panel in
    the create form's shape — the surface the standalone decider
    app (#195) was, folded into the tree.

    Lifecycle: the page pushes it by hand (the action row — it
    stays open until the operator closes it, an empty queue
    included: reviewing rules, revoking, and switching the mode all
    start here) or by itself (the first hold of a burst). ``q`` or
    Escape parks it — holds keep waiting, the header's count keeps
    naming them — and an overlay the page opened closes itself
    when its queue empties, whichever way the last hold resolved;
    the close waits while the rules or events screen sits stacked
    above, so back returns here first.

    Keys: ``a``/``d`` decide the focused hold for the default
    duration, ``A``/``D`` pick a duration first, ``m`` opens the
    page's mode picker, ``r``/``e`` push the rules and events
    screens, ``q``/Escape close. Enter carries no verdict — the
    queue is a ListView, and a stray Enter aimed at the page when
    the hold arrived must not decide anything; only an explicit
    letter decides. The bindings live on this screen, so the page's
    keymap and the overlay's cannot collide (Textual routes keys to
    the active screen alone), and pickers pushed above work without
    shadow bindings.
    """

    BINDINGS = [
        Binding("a", "allow", "Allow"),
        Binding("A", "allow_duration", "Allow…"),
        Binding("d", "deny", "Deny"),
        Binding("D", "deny_duration", "Deny…"),
        Binding("m", "mode", "Mode"),
        Binding("r", "rules", "Rules"),
        Binding("e", "events", "Events"),
        Binding("q", "park", "Close"),
        Binding("escape", "park", "Close", show=False),
    ]

    def __init__(self, host: WorkspaceScreen, *, auto: bool) -> None:
        super().__init__()
        self.host = host
        self.auto = auto
        self.workspace_id = host.row["id"]
        self.link = host.link_or_stub()
        self.flash_line = FlashLine()
        self.rebuilds = OneFlight(
            lambda: self.rebuild_queue(self.controller.ordered()),
            lambda: self.app.is_running,
            "consent-overlay",
        )

    @property
    def controller(self):
        """The page link's controller — the queue's state, shared
        with the page's own lines."""
        return self.link.controller

    def compose(self) -> ComposeResult:
        with Vertical(id="consent-panel"):
            yield Static(id="consent-status")
            yield ListView(id="consent-rows")
            yield Static(id="consent-empty")
        yield Footer()

    def on_mount(self) -> None:
        self.set_interval(1.0, self.tick)
        # call_after_refresh: the first rebuild waits for the compose
        # stream to settle — a timer or worker racing it queries
        # widgets that are not mounted yet (the page's own rule).
        self.call_after_refresh(self.started)

    def started(self) -> None:
        """The compose has settled: focus the rows, build them, and
        paint the status line."""
        self.query_one("#consent-rows", ListView).focus()
        self.rebuilds.request()
        self.update_status()

    # -- the per-second repaint ------------------------------------------

    def tick(self) -> None:
        """The per-second repaint: the countdowns, the status line,
        the rules/events screens while one is on top (their rows
        tick and a fresh frame shows up — the decider app's repaint
        rule, one owner), and the self-close check. A teardown race
        leaves the queries empty — noise, not a crash."""
        try:
            self.sync_rows()
            self.update_status()
            self.refresh_screens()
            self.maybe_autoclose()
        except NoMatches:
            pass

    def refresh_screens(self) -> None:
        """Repaint the rules or events screen while it is on top of
        this overlay (the rebuilds await widget mounts, so they run
        as tasks, one flight at a time; the events screen skips an
        unchanged log — its rows are static, nothing ticks)."""
        screen = self.app.screen
        if isinstance(screen, RulesScreen):
            screen.schedule_refresh()
        elif isinstance(screen, EventsScreen) and screen.log_changed():
            screen.schedule_refresh()

    def maybe_autoclose(self) -> None:
        """The self-close: an auto-opened overlay whose queue
        emptied pops itself — its purpose is gone, and nothing is
        left to yank out from under the operator. A screen stacked
        above (rules, events, a picker) holds the close: back
        returns here first, and the next tick after it settles takes
        the panel down. An overlay the operator opened by hand stays
        until the operator closes it."""
        if not self.auto or self.controller.ordered():
            return
        if self.app.screen is not self:
            return
        self.host.overlay_auto_closed()
        self.app.pop_screen()

    # -- the queue rows ---------------------------------------------------

    def queue_rows(self) -> ListView | None:
        """The queue list, or None during a rebuild's swap window."""
        try:
            return self.query_one("#consent-rows", ListView)
        except NoMatches:
            return None

    def sync_rows(self) -> None:
        """Sync the queue to state. A membership change (a hold
        resolved, a new one arrived) rebuilds the list fresh —
        Textual prunes removed children asynchronously, so mutating
        a live ListView leaves stale copies that shift every index
        under the highlight; a fresh list keeps the destructive
        keys' target derivable from children that are all real.
        Same-set ticks repaint survivors' countdowns in place. A
        missing list (a rebuild died mid-swap) schedules a rebuild —
        the queue self-heals instead of wedging blank."""
        rows = self.queue_rows()
        if rows is None:
            self.rebuilds.request()
            return
        ordered = self.controller.ordered()
        if row_ids(rows) != {request.id for request in ordered}:
            self.rebuilds.request()
            return
        self.repaint_countdowns(rows, ordered)
        self.sync_empty(ordered)

    def sync_empty(self, ordered: list) -> None:
        """The empty state rides beside the list, honest about the
        connection state."""
        empty = self.query_one("#consent-empty", Static)
        empty.display = not ordered
        empty.update(self.empty_line())

    def empty_line(self) -> str:
        """The empty-queue line, honest about the link's state."""
        if self.link.state == CONNECTED:
            return "No held requests — connected, waiting."
        return f"No held requests — {self.link.state}."

    def repaint_countdowns(self, rows: ListView, ordered: list) -> None:
        """Repaint each survivor's countdown in place."""
        existing = row_map(rows)
        for request in ordered:
            item = existing.get(request.id)
            if item is not None:
                item.query_one(Static).update(
                    dest_line(request, self.controller.remaining(request))
                )

    async def rebuild_queue(self, ordered: list) -> None:
        """Swap in a freshly-built queue list (its mount awaited),
        restoring focus by id (the top when the focused hold left)
        so ``a``/``d`` never retarget through a shifted index. Focus
        is read from the live list here, at rebuild time — never
        captured at arm time. A missing old list (a rebuild died
        mid-swap) is fine: the fresh list mounts anew."""
        body = self.query_one("#consent-panel", Vertical)
        old = self.queue_rows()
        focused = focused_request_id(old)
        items = [self.render_item(request) for request in ordered]
        fresh = ListView(*items, id="consent-rows")
        if old is not None:
            await old.remove()  # frees the id before the fresh list mounts
        await body.mount(fresh)
        fresh.focus()
        focus_by_id(fresh, focused)  # after mount: index sticks
        self.sync_empty(ordered)

    def render_item(self, request) -> ListItem:
        """One queue row."""
        item = ListItem(
            Static(dest_line(request, self.controller.remaining(request)))
        )
        item.request_id = request.id
        return item

    # -- the status line ---------------------------------------------------

    def flash(self, message: str) -> None:
        """Give the status line to a message for FLASH_TTL seconds —
        a verdict's failure, a sighting's alarm (#201): the surfaces
        this overlay owns."""
        self.flash_line.set(message)
        self.update_status()

    def update_status(self) -> None:
        """The status line: workspace, current mode, the link's
        state, held count; a flash owns it until its TTL lapses. A
        rejected registration names its reason — the daemon refused
        this page as the decider, and the line says why."""
        if self.link.state == REJECTED:
            state = escape(self.link.reject_reason or "rejected")
        else:
            state = self.link.state
        held = len(self.controller.pending)
        default = (
            f" {escape(self.workspace_id)}  ·  mode "
            f"{mode_label(self.controller.rules)}"
            f"  ·  {state}  ·  {held} held"
        )
        self.query_one("#consent-status", Static).update(
            self.flash_line.text(default)
        )

    # -- verdicts ------------------------------------------------------------

    async def action_allow(self) -> None:
        await self.decide_focused("allow", DURATION_DEFAULT)

    async def action_deny(self) -> None:
        await self.decide_focused("deny", DURATION_DEFAULT)

    async def action_allow_duration(self) -> None:
        await self.pick_duration("allow")

    async def action_deny_duration(self) -> None:
        await self.pick_duration("deny")

    async def decide_focused(self, decision: str, duration: str) -> None:
        """Send the verdict for the focused hold through the data
        seam; a failure flashes, never crashes the tree (SystemExit
        included — the REST seam's error surface). A key pressed
        inside a rebuild's swap window reads as nothing focused."""
        request_id = focused_request_id(self.queue_rows())
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
        request_id = focused_request_id(self.queue_rows())
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

    async def revoke_rule(self, request_id: str) -> None:
        """One revoke through the data seam — the same exchange
        ``msks egress revoke`` makes; the row leaves on the
        refreshed ``egress.rules`` frame, never optimistically."""
        try:
            await self.host.app.data.revoke(self.workspace_id, request_id)
        except (Exception, SystemExit) as exc:
            self.flash(f"revoke failed: {flash_safe(str(exc))}")

    def on_list_view_selected(self, event: ListView.Selected) -> None:
        """Enter on a row decides nothing: the queue is a ListView,
        and Enter fires its selection — a stray Enter (the operator
        aimed at the page's action list when the hold arrived and
        the overlay opened under the keypress) must never become a
        verdict. Only an explicit letter decides."""

    # -- the pushed screens ---------------------------------------------

    def action_rules(self) -> None:
        """Push the rules screen over the overlay — the deliberation
        flow keeps the in-effect verdicts one key away; back returns
        to the held hold still focused."""
        self.app.push_screen(
            RulesScreen(
                self.controller, self.revoke_rule, self.host.open_mode_picker
            )
        )

    def action_events(self) -> None:
        """Push the events screen over the overlay."""
        self.app.push_screen(EventsScreen(self.controller))

    def action_mode(self) -> None:
        """Open the page's mode picker — the same path the page's
        action row takes (#344)."""
        self.host.open_mode_picker()

    def action_park(self) -> None:
        """``q``/Escape: close without deciding — holds keep
        waiting, the header's count names them, and the action row
        reopens this panel."""
        self.host.overlay_parked()
        self.app.pop_screen()


#: The create form's fields (#309): the create body's optional
#: inputs in walk order — a short label at the left of each
#: row, the hint riding the input's placeholder (the image
#: select's hint is its blank prompt; the root/home and user
#: placeholders name their defaults at mount).
FORM_FIELDS = (
    ("name", "name", "workspace name"),
    ("image", "image ref", "the default image"),
    ("cpus", "vcpus", "default 2"),
    ("mem_mib", "memory", "MiB — 8192"),
    ("root_mib", "root", "MiB"),
    ("home_mib", "home", "MiB"),
    ("user", "user", "the account it seeds"),
)

#: The fields whose values must be whole numbers.
INT_FIELDS = frozenset({"cpus", "mem_mib", "root_mib", "home_mib"})

#: The fields an edit cannot move (#331): name, image, and user
#: are create-time — the daemon answers a mutation with its named
#: 405, and the seed planted them at first boot — so the edit
#: dialog shows them read-only and a value that moved stays home
#: with the note naming it, never silently dropped. The sizes are
#: the editable half: they move through the resize route, which
#: owns the stopped-workspace rule.
CREATE_TIME_FIELDS = frozenset({"name", "image", "user"})


#: The row keys the edit dialog's fields seed from (#331), in
#: form-field order — the sizes and topology read the row's own
#: facts, the image reads its hash, the user reads the login user
#: (blank on a row created before per-workspace users).
EDIT_ROW_KEYS = {
    "name": "name",
    "image": "image_hash",
    "cpus": "cpus",
    "mem_mib": "mem_mib",
    "root_mib": "root_mib",
    "home_mib": "home_mib",
    "user": "login_user",
}


def edit_seeds(row: dict) -> dict[str, str]:
    """The edit dialog's seeded values (#331): every form field's
    current value read off the workspace's row, as the field's
    own text — the sizes and topology as their numbers, a row
    that predates a field seeding it blank."""
    seeds: dict[str, str] = {}
    for field, key in EDIT_ROW_KEYS.items():
        value = row.get(key)
        seeds[field] = "" if value is None else str(value)
    return seeds


def edit_note(row: dict) -> str:
    """The edit dialog's note (#331): the title, then the rule the
    issue pins — which fields land live (home bytes move at
    once), which wait for a stop/start cycle (the resize needs
    the workspace stopped; root growth and the new topology
    apply at the next boot), and which cannot change (the
    create-time fields, marked * on their labels). The explicit
    line breaks keep the form fitting 80x24 terminals whatever
    the workspace's name carries."""
    label = clip(workspace_label(row), 40)
    return (
        f"edit {label}\n"
        "stop the workspace to resize · home bytes move at once ·\n"
        "root growth and topology at next boot · * = create-time"
    )


def whole_number(value: str) -> bool:
    """Whether a form value is a whole number (the sizes and counts
    ride the wire as ints; ASCII digits only — int() refuses some
    unicode digits isdigit() accepts, and a paste can carry them)."""
    return value.isascii() and value.isdigit()


def clip(text: str, width: int = 12) -> str:
    """One clipped column: the text, or its head and tail kept
    around a middle ellipsis when it runs wider than ``width``
    display cells (the tail carries the version half of a
    reference, the part a head-only clip eats; a wide character
    that would cross a budget stays whole, so a clipped cell may
    land a cell short and the padding fills it)."""
    if cell_len(text) <= width:
        return text
    head = (width - 1) // 2
    return (
        f"{cell_prefix(text, head)}…"
        f"{cell_prefix(text[::-1], width - 1 - head)[::-1]}"
    )


def image_options(rows: list[dict]) -> list[tuple[str, str]]:
    """The image select's options from the catalog listing: the
    reference and the hash as two 12-character columns, the
    designated default marked after them (the hash column keeps
    references that clip to the same 12 characters apart). A
    reference two entries share rides the hash in the option's
    value (name@hash resolves to exactly that entry — name:version
    would pick the oldest of the two)."""
    options: list[tuple[str, str]] = []
    seen: set[str] = set()
    for row in rows:
        ref = f"{row['name']}:{row['version']}"
        value = f"{row['name']}@{row['hash']}" if ref in seen else ref
        seen.add(ref)
        label = f"{clip(ref)} {clip(row['hash'])}"
        if row.get("default"):
            label += " — default"
        options.append((label, value))
    return options


class ImageSelect(Select):
    """The create form's image select: stock Select with the
    walk's arrows kept — a closed select answers up/down by
    walking the form's fields (Enter or space opens the list;
    an open list keeps the stock arrows for its own rows)."""

    BINDINGS = [
        Binding("up", "walk_previous", show=False),
        Binding("down", "walk_next", show=False),
    ]

    def action_walk_next(self) -> None:
        self.app.action_focus_next()

    def action_walk_previous(self) -> None:
        self.app.action_focus_previous()


class WorkspaceForm(ModalScreen[dict | None]):
    """The workspace form (#309 create, #331 edit): one
    label-plus-input row per field, in walk order, Enter and the
    arrows moving between them; the buttons submit and cancel.
    The form stands 80x24 terminals tall — every control, the
    buttons included, must stay on screen. The submitted body goes
    to the callback given at construction (the pushing screen owns
    the exchange and its flashes); local checks refuse here so the
    daemon only sees whole bodies.

    One implementation serves both surfaces: the create subclass
    (:class:`CreateScreen`) leaves every field open and submits
    the filled body; the edit subclass (:class:`EditScreen`)
    seeds every field from the workspace's row, marks the
    create-time fields read-only, and submits the changed sizes as
    a resize body (#331).
    """

    BINDINGS = [
        Binding("up", "walk_previous", show=False),
        Binding("down", "walk_next", show=False),
        Binding("escape", "cancel", "Cancel", show=False),
        Binding("q", "cancel", show=False),
    ]

    def action_walk_next(self) -> None:
        """Down walks the form — the same walk Enter makes (the
        arrows own the walk from a plain input; the image select
        carries its own down that opens its list)."""
        self.app.action_focus_next()

    def action_walk_previous(self) -> None:
        """Up walks the form back."""
        self.app.action_focus_previous()

    def __init__(self, submitted) -> None:
        super().__init__()
        self.submitted = submitted

    # -- the mode's hooks ------------------------------------------

    def form_note(self) -> str:
        """The note line's standing text: the form's title, or the
        rule the mode carries."""
        raise NotImplementedError  # pragma: no cover — abstract hook

    def field_label(self, field: str, label: str) -> str:
        """One field's label; an edit marks its create-time fields
        with the note's * legend."""
        return label

    def image_control(self, hint: str):
        """The image field's control."""
        return ImageSelect([], prompt=hint, id="field-image", compact=True)

    def editable(self, field: str) -> bool:
        """Whether the operator can change the field here (an edit
        locks its create-time fields read-only)."""
        return True

    def image_value(self) -> str:
        """The image field's value — blank is the daemon's default
        image."""
        select = self.query_one("#field-image", Select)
        return "" if select.is_blank() else str(select.value)

    def first_field(self) -> str:
        """The field the walk starts from."""
        return "name"

    def form_mounted(self) -> None:
        """The mode's mount work, after the shared focus."""

    def submit_id(self) -> str:
        """The submit button's id."""
        return "do-create"

    def submit_label(self) -> str:
        """The submit button's label."""
        return "Create"

    def body(self) -> dict | None:
        """The submitted body; None (with the note naming the
        refusal) keeps the form standing."""
        raise NotImplementedError  # pragma: no cover — abstract hook

    # -- the shared form ---------------------------------------------

    def compose(self) -> ComposeResult:
        with Vertical(id="form"):
            yield Static(
                self.form_note(),
                id="form-note",
            )
            for field, label, hint in FORM_FIELDS:
                with Horizontal(classes="form-row"):
                    yield Static(
                        self.field_label(field, label), classes="form-label"
                    )
                    if field == "image":
                        yield self.image_control(hint)
                    else:
                        control = Input(
                            placeholder=hint,
                            id=f"field-{field}",
                            compact=True,
                        )
                        control.disabled = not self.editable(field)
                        yield control
            with Horizontal(id="form-buttons"):
                yield Button(
                    self.submit_label(),
                    id=self.submit_id(),
                    variant="primary",
                    compact=True,
                )
                yield Button("Cancel", id="do-cancel", compact=True)
        yield Footer()

    def on_mount(self) -> None:
        self.query_one(f"#field-{self.first_field()}", Input).focus()
        self.form_mounted()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        """Enter in a field moves the walk to the next control (the
        buttons included — the arrows make the same walk; the
        action is the app's, the screen hosts the binding)."""
        self.app.action_focus_next()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == self.submit_id():
            self.submit()
        else:
            self.dismiss_with(None)

    def action_cancel(self) -> None:
        self.dismiss_with(None)

    def note(self, text: str) -> None:
        """The form's note line: its title, or the local
        refusal that keeps a half-filled body home."""
        self.query_one("#form-note", Static).update(text)

    def field_value(self, field: str) -> str:
        """One field's value, stripped — the image select's blank
        (no pick) stays the daemon's default image."""
        if field == "image":
            return self.image_value()
        return self.query_one(f"#field-{field}", Input).value.strip()

    def land_value(self, field: str, value: str, body: dict) -> bool:
        """One filled value into the body; False (with the note
        naming it) on a local refusal. Blank stays unset."""
        if not value:
            return True
        if field in INT_FIELDS and not whole_number(value):
            self.note(f"{field}: a whole number, or leave it blank")
            return False
        body[field] = int(value) if field in INT_FIELDS else value
        return True

    def submit(self) -> None:
        """Hand the body to the callback, or keep the form on a
        local refusal."""
        body = self.body()
        if body is not None:
            self.dismiss_with(body)

    def dismiss_with(self, body: dict | None) -> None:
        """Dismiss and hand the body to the callback (async — the
        exchange runs as a task, so the modal closes without
        waiting on it)."""
        self.dismiss()
        # Referenced: an unreferenced task can be collected mid-await.
        self._task = asyncio.create_task(self.submitted(body))


class CreateScreen(WorkspaceForm):
    """The create form (#309): every field open, blank fields left
    to the daemon's defaults, the image picked from the catalog."""

    def form_note(self) -> str:
        return "create a workspace"

    def body(self) -> dict | None:
        """The create body: only the fields the operator filled
        (blank stays the daemon's default), whole numbers checked
        locally — the daemon's validation stays the authority."""
        body: dict = {}
        for field, _label, _hint in FORM_FIELDS:
            if not self.land_value(field, self.field_value(field), body):
                return None
        if "name" not in body:
            self.note("a workspace name is required")
            return None
        return body

    def form_mounted(self) -> None:
        self.query_one("#field-user", Input).placeholder = invoking_user()
        self.run_worker(self.load_hints, exclusive=True)

    async def load_hints(self) -> None:
        """The form's daemon hints: the image select's catalog and
        the size placeholders' defaults. A refusal keeps the form
        standing — blank fields still create against the daemon's
        defaults."""
        try:
            rows = await self.app.data.images()
        except (Exception, SystemExit) as exc:
            self.note(f"image list failed: {escape(str(exc))}")
        else:
            self.query_one("#field-image", Select).set_options(
                image_options(rows)
            )
        try:
            defaults = await self.app.data.create_defaults()
        except (Exception, SystemExit) as exc:
            self.note(f"defaults failed: {escape(str(exc))}")
        else:
            self.size_placeholders(defaults)

    def size_placeholders(self, defaults: dict) -> None:
        """The root/home placeholders: the MiB unit beside the
        default a blank field lands on."""
        for field in ("root_mib", "home_mib"):
            self.query_one(
                f"#field-{field}", Input
            ).placeholder = f"MiB — {defaults[field]}"


class EditScreen(WorkspaceForm):
    """The edit dialog (#331): the create form's layout seeded from
    the workspace's row. The sizes are editable — they submit as a
    resize body through the route that owns the stopped-workspace
    rule — and the create-time fields (name, image, user) are
    marked * and read-only: a value that moved is refused with the
    note naming it, never silently dropped. The walk skips the
    read-only rows (a disabled control cannot hold focus), so the
    arrows move between the sizes and the buttons alone."""

    def __init__(self, row: dict, submitted) -> None:
        self.row = row
        self.seeded = edit_seeds(row)
        super().__init__(submitted)

    def form_note(self) -> str:
        return edit_note(self.row)

    def field_label(self, field: str, label: str) -> str:
        if field in CREATE_TIME_FIELDS:
            return f"{label} *"
        return label

    def image_control(self, hint: str):
        """The image field as a read-only line: the row's image
        hash, not a catalog pick — the create-time image cannot
        move, so the select that would offer a new one stays out."""
        control = Input(placeholder=hint, id="field-image", compact=True)
        control.value = self.seeded["image"]
        control.disabled = True
        return control

    def editable(self, field: str) -> bool:
        return field not in CREATE_TIME_FIELDS

    def image_value(self) -> str:
        return self.query_one("#field-image", Input).value.strip()

    def first_field(self) -> str:
        return "cpus"

    def submit_id(self) -> str:
        return "do-apply"

    def submit_label(self) -> str:
        return "Apply"

    def form_mounted(self) -> None:
        """Seed every plain field with the workspace's current
        values — the prefill the issue pins (the image seeds in
        its own read-only control)."""
        for field, _label, _hint in FORM_FIELDS:
            if field != "image":
                self.query_one(f"#field-{field}", Input).value = self.seeded[
                    field
                ]

    def body(self) -> dict | None:
        """The resize body: the changed sizes alone. A create-time
        field that moved (a programmatic set — the dialog's own
        fields are read-only) stays home with the note naming it,
        and a body with nothing changed stays home too: the
        daemon's nothing-to-resize refusal, said locally where the
        operator can still edit."""
        moved = self.refused_create_time()
        if moved is not None:
            self.note(
                f"{moved} is create-time — delete and recreate the "
                "workspace to change it"
            )
            return None
        body: dict = {}
        if not self.changed_sizes(body):
            return None
        if not body:
            self.note("nothing to resize — change a size, or cancel")
            return None
        return body

    def refused_create_time(self) -> str | None:
        """The first create-time field whose value moved, else None
        — the dialog's own fields are read-only, so a move is a
        programmatic set, and the submit refuses it by name."""
        for field in sorted(CREATE_TIME_FIELDS):
            if self.field_value(field) != self.seeded[field]:
                return field
        return None

    def changed_sizes(self, body: dict) -> bool:
        """Land the changed sizes into ``body``; False (with the
        note naming the refusal) on a junk value. A field left at
        its seeded value rides nothing — the resize moves only
        what the operator changed."""
        for field, _label, _hint in FORM_FIELDS:
            if field in CREATE_TIME_FIELDS:
                continue
            value = self.field_value(field)
            if value != self.seeded[field]:
                if not self.land_value(field, value, body):
                    return False
        return True
