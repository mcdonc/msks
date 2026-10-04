"""The workspace TUI's app shell (#309): the tree app, its styles,
and the chained-flow runner — the full-screen tree rooted at the
workspaces list, launched by ``msks tui``. The screens live in
sibling modules (the listing here in
:mod:`msks.client.tui.main_screen`, the workspace page and its
consent overlay in :mod:`msks.client.tui.workspace`, the secrets
page in :mod:`msks.client.tui.secrets`, the forms in
:mod:`msks.client.tui.forms`) and this module composes them.

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

Spatial navigation: every screen is a list the arrows walk, the
create form's arrows move between its fields, and Escape always
leaves the screen it is on — no screen traps focus.
"""

import asyncio
import sys

from textual.app import App
from textual.screen import Screen

from ..config import DEFAULT_TERMINAL_CMD, ClientConfig
from ..env import env_token, env_url
from .consent_ui import FlashLine, flash_safe, shared_ssl
from .data import TuiData
from .follow import TuiFollow, run_follow_up
from .main_screen import MainScreen
from .workspace import WorkspaceScreen


def require_terminal() -> None:
    """The tree owns the terminal; a pipe on either side cannot host
    it (the console command's own guard, with this command's
    name)."""
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        raise SystemExit(
            "msks: the TUI needs an interactive tty on stdin and stdout"
        )


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
    #status { height: 1; padding: 0 1; background: $panel;
              color: $text-muted; text-wrap: nowrap;
              text-overflow: ellipsis; }
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
    #consent { height: 1; padding: 0 1; color: $text-muted;
               text-wrap: nowrap; text-overflow: ellipsis; }
    #page { height: 1fr; align: center middle; }
    #actions { width: 64; height: auto; max-width: 100%;
              max-height: 100%; }
    #actions ListItem { height: 1; padding: 0 1; }
    #actions ListItem.group-lead { margin-top: 1; }
    #actions ListItem Static { text-wrap: nowrap;
                              text-overflow: ellipsis; }
    WorkspaceForm { align: center middle; }
    #form { width: 64; height: auto; background: $panel;
            border: round $primary; padding: 1 2; }
    #form-note { color: $text-muted; margin-bottom: 1;
                 text-wrap: nowrap; text-overflow: ellipsis; }
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
    #secrets-listing { border: round $primary; background: $panel; }
    #secret-columns { padding: 0 1; color: $text-muted; }
    #secret-rows ListItem { height: 1; padding: 0 1; }
    #secret-rows ListItem Static { text-wrap: nowrap; }
    #secret-empty { padding: 1 2; color: $text-muted; }
    #audit-note { padding: 0 1; color: $text-muted; }
    #audit-status { height: 1; padding: 0 1; color: $text-muted;
                    text-wrap: nowrap; text-overflow: ellipsis; }
    #audit-empty { padding: 0 1; color: $text-muted; }
    #audit-rows ListItem { height: 1; }
    #audit-rows ListItem.sighting { color: $warning; text-style: bold; }
    MintScreen { align: center middle; }
    .form-row.tall { height: auto; }
    .form-row.tall SelectionList { width: 1fr; height: 4;
                                   border: none; padding: 0;
                                   background: $boost; }
    SentinelPanel { align: center middle; }
    #sentinel-panel { width: 64; height: auto; background: $panel;
                      border: round $primary; padding: 1 2; }
    #panel-note { color: $text-muted; margin-bottom: 1;
                  text-wrap: nowrap; text-overflow: ellipsis; }
    #panel-label { color: $text-muted; }
    #panel-sentinel { text-style: bold; text-wrap: nowrap;
                      text-overflow: ellipsis; margin-bottom: 1; }
    #panel-reach { margin-bottom: 1; }
    #panel-rule { color: $text-muted; margin-bottom: 1; }
    #panel-buttons { height: auto; align-horizontal: center;
                     margin-top: 1; }
    #panel-buttons Button { margin: 0 2; }
    FailurePanel { align: center middle; }
    #failure-panel { width: 64; height: auto; max-height: 100%;
                     background: $panel; border: round $primary;
                     padding: 1 2; }
    #failure-title { color: $warning; margin-bottom: 1;
                     text-wrap: wrap; }
    #failure-detail { margin-bottom: 1; text-wrap: wrap; }
    #failure-buttons { height: auto; align-horizontal: center;
                       margin-top: 1; }
    #failure-buttons Button { margin: 0 2; }
    """

    # (#442) The exit keys are Textual's own, carried with no
    # override here: ctrl+q quits from every screen (a priority
    # binding on the App), and a bare ctrl+c names the quit key
    # in a notification — on a form field the Input keeps ctrl+c
    # as the field's own copy shortcut. The tree's q and Escape
    # exits stand as they were. (#437) Ctrl+Shift+C stays the
    # terminal's own gesture: the kitty keyboard protocol is off
    # (the package's __init__), and a terminal that passes the
    # key through sends Ctrl+C's byte — the notification answers
    # it, so no path of the gesture exits the client.

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
        # The configured daemon's URL, read once at construction:
        # the bootstrap materialized every winner into the
        # environment before the tree started, and the screens'
        # status lines read it off the app instead of each holding
        # its own env edge.
        self.url = env_url()
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
            # tree's restart would otherwise eat), escaped for the
            # status line's markup parsing.
            self.flash(flash_safe(self.follow.seed))
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
