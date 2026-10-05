"""The standalone consent-decider app (#467).

``msks-term-popup``'s launch starts this app in a hidden tmux
session on its own socket, where it stands for the window's whole
life: it speaks the events websocket as the workspace's decider
(the :class:`~msks.client.tui.link.DeciderLink` ladder — connect,
register, pump, reconnect), shows the live queue of held requests,
and posts verdicts through the same REST contract as the egress
consent page. A hold arriving with the popup closed raises a
``display-popup`` viewer over the shell that attaches to the
hidden session; the viewer hides without stopping the app — the
queue lives on, holds leave it as the daemon resolves them, and
the reopen key (``C-b p``, the launch's binding) or the next hold
brings the popup back.

The footer carries the bindings map at the popup's bottom
(#467): the verdict keys beside the hide/show entry, whose
``ctrl+b`` half closes the viewer from inside it — the popup
captures input while it stands, so the launch's ``C-b p`` cannot
fire there, and the prefix key alone answers instead. ``m``
switches the workspace's egress mode from the popup (#465): the
picker, the gate, and the empty-static confirmation are the
consent page's own shapes, the switch rides the same endpoint
``msks egress mode`` speaks, and the landed switch or the
daemon's refusal names itself on the status line.

Standalone — ``python -m msks.client.tui.decide_app -w WORKSPACE``
— the same app runs outside a shell window; it quits on ``q``
instead of hiding. The shared pieces are the page's own: the
controller, the duration picker, the row and status formatting,
and the connection ladder.
"""

import asyncio
import json
import subprocess
import sys
import time

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.message import Message
from textual.widgets import Footer, ListItem, ListView, Static

from .. import context, term_popup
from .consent import (
    ADDED,
    DURATION_DEFAULT,
    ConsentRequest,
)
from .consent_ui import (
    EMPTY_STATIC_QUESTION,
    FLASH_TTL,
    ConfirmScreen,
    DurationScreen,
    FlashLine,
    ModeScreen,
    dest_line,
    flash_safe,
    shared_ssl,
    switch_mode_path,
)
from .link import DeciderLink

#: The app's repaint beat: the countdowns move once a second.
TICK_S = 1.0

#: The show path's retry budget: a first attempt that targeted
#: nothing (a contended server, no client yet) retries while holds
#: remain, so a burst that arrived inside the dedupe window is
#: never left unpopup'd.
POPUP_SHOW_ATTEMPTS = 3
POPUP_SHOW_RETRY_DELAY = 0.5

#: The window-gone retirement's clock (#467 review): the launch's
#: client is the window; gone past the empty window with one seen
#: before, or never attached past the grace, the decider retires
#: (the hidden session ends with the app, the server with the
#: session) — a closed window leaves no decider holding the
#: workspace's egress to its timeouts.
LIVENESS_TICK_S = 5.0
LIVENESS_EMPTY_S = 10.0
LIVENESS_GRACE_S = 30.0


async def rest_decide(
    workspace_id: str, request_id: str, decision: str, duration: str
) -> dict:
    """POST one verdict through the shared REST contract — the
    egress consent page's decide, the same exchange ``msks egress
    decide`` makes; failures raise SystemExit with the readable
    line the status line shows."""
    return await context.call(
        "POST",
        f"/api/v1/workspaces/{workspace_id}/egress/requests/{request_id}",
        json_body={"decision": decision, "duration": duration},
        ssl_ctx=shared_ssl(),
    )


async def rest_mode(
    workspace_id: str, mode: str, *, confirm_empty: bool = False
) -> dict:
    """PUT one mode switch through the shared REST contract
    (#465) — the consent page's switch and ``msks egress mode``'s
    exchange; ``confirm_empty`` rides only when set, and the
    daemon's refusal names it. Returns the reply: the fresh rules
    frame with ``applied`` beside it."""
    body: dict = {"mode": mode}
    if confirm_empty:
        body["confirm_empty"] = True
    return await context.call(
        "PUT",
        f"/api/v1/workspaces/{workspace_id}/egress/policy",
        json_body=body,
        ssl_ctx=shared_ssl(),
    )


class FrameLanded(Message):
    """One events frame's arrival, from the link's worker to the
    UI's queue: ``holds_added`` says whether it added a hold — the
    popup shows the moment one lands, not on the next tick."""

    def __init__(self, holds_added: bool) -> None:
        super().__init__()
        self.holds_added = holds_added


class ConsentDeciderApp(App[None]):
    """The decider's screen: the held-request queue, the verdict
    keys, the mode switch, and the popup viewer's controls — a
    thin view over the shared controller, the same shapes the
    consent page renders."""

    #: The command palette's footer entry is noise in a popup.
    ENABLE_COMMAND_PALETTE = False

    CSS = """
    Screen { layout: vertical; }
    #status { padding: 0 1; background: $panel; color: $text-muted; }
    #queue { height: 1fr; }
    #requests { height: 1fr; }
    #empty { padding: 1 2; color: $text-muted; }
    ConfirmScreen { align: center middle; }
    #question { padding: 1 2; background: $panel;
                border: round $primary; }
    """

    BINDINGS = [
        Binding("a", "allow", "Allow"),
        Binding("A", "allow_duration", "Allow…"),
        Binding("d", "deny", "Deny"),
        Binding("D", "deny_duration", "Deny…"),
        Binding("m", "mode", "Mode"),
    ]

    def __init__(
        self,
        workspace_id: str,
        *,
        ws_factory=None,
        decide=rest_decide,
        mode=rest_mode,
        clock=time.time,
        popup_socket: str | None = None,
        popup_session: str | None = None,
    ) -> None:
        super().__init__()
        self.workspace_id = workspace_id
        self.link = DeciderLink(workspace_id, ws_factory=ws_factory)
        self.link.on_frame = self.frame_arrived
        self.decide_call = decide
        self.mode_call = mode
        self.clock = clock
        self.popup_socket = popup_socket
        self.popup_session = popup_session
        self.flash_line = FlashLine()
        #: Whether the queue has held a hold since the viewer last
        #: stood — the empty transition hides a popup the show path
        #: raised, and leaves one the operator opened alone.
        self.holds_known = False
        self.show_task: asyncio.Task | None = None
        self.hide_task: asyncio.Task | None = None
        self.verdicts: set[asyncio.Task] = set()
        self.started = clock()
        self.client_seen = False
        self.empty_since: float | None = None
        self.bind_leave_keys()

    @property
    def persistent(self) -> bool:
        """Whether the app runs inside the hidden popup session."""
        return self.popup_session is not None

    def compose(self) -> ComposeResult:
        yield Static(id="status")
        with Vertical(id="queue"):
            yield ListView(id="requests")
            yield Static("No held requests — connected, waiting.", id="empty")
        yield Footer()

    def bind_leave_keys(self) -> None:
        """Install the mode's leave keys beside the verdict keys:
        the popup hides on ``q``/``Q``/``Esc``/``C-b`` (the decider
        is persistent — a key never quits it) and the footer names
        the reopen binding's spelling beside them; standalone
        quits as ever. Textual builds its binding map at
        construction, so the mode's keys land through ``bind``,
        not a class-list swap."""
        if not self.persistent:
            self.bind("q", "quit", description="Quit")
            self.bind("Q", "quit", description="Quit")
            self.bind("escape", "quit", description="Quit", show=False)
            return
        self.bind(
            "ctrl+b", "hide", description="Hide/Show", key_display="C-b p"
        )
        for key in ("q", "Q", "escape"):
            self.bind(key, "hide", description="Hide", show=False)

    def on_mount(self) -> None:
        self.title = f"consent — {self.workspace_id}"
        self.run_worker(
            self.link_worker, exclusive=True, group="ws", exit_on_error=False
        )
        self.set_interval(TICK_S, self.repaint)
        if self.persistent:
            self.set_interval(LIVENESS_TICK_S, self.retire_when_gone)
        self.query_one("#requests", ListView).focus()

    # --- the connection -----------------------------------------------------

    async def link_worker(self) -> None:
        """Run the link's ladder for the app's life; a rejected
        registration (a workspace id that names nothing, a token
        the daemon refuses to re-take) ends it — the reason holds
        the status line for a read, then the app steps aside."""
        await self.link.run()
        self.repaint()
        await asyncio.sleep(FLASH_TTL)
        self.exit()

    def frame_arrived(self, outcome: str, payload: object) -> None:
        """The link's per-frame hook, on the worker's task: hand
        the arrival to the UI's queue."""
        self.post_message(FrameLanded(outcome == ADDED))

    def on_frame_landed(self, event: FrameLanded) -> None:
        """One frame's arrival: repaint, and show the popup the
        moment a hold lands while it stands closed."""
        self.repaint()
        if event.holds_added and self.persistent:
            self.schedule_show()

    # --- the screen ---------------------------------------------------------

    def repaint(self) -> None:
        """The per-second pass: the queue's rows (a membership
        change swaps the list, a same-set tick repaints the
        countdowns in place), the status line, and the popup's
        empty-queue retirement. A repaint during teardown finds
        the screen half-dismantled and stands down instead."""
        if not self.is_running:
            return
        self.repaint_queue(self.link.controller.ordered())

    def repaint_queue(self, rows: list) -> None:
        """The queue's pass: the rows, the status line, and the
        popup's empty-queue retirement."""
        queue = self.query_one("#requests", ListView)
        if self.queue_stale(queue, rows):
            held = self.selected_id(queue)
            queue.clear()
            for row in rows:
                item = ListItem(Static(self.row_text(row)))
                item.hold_id = row.id  # type: ignore[attr-defined]
                queue.append(item)
            if rows:
                # The selection rides its hold across the swap (a
                # resolved hold falls to the top) — a membership
                # change must never move the target of a
                # destructive key (#467 review).
                queue.index = self.row_position(held, rows)
        else:
            for item, row in zip(queue.children, rows):
                item.query_one(Static).update(self.row_text(row))
        self.query_one("#empty", Static).display = not rows
        self.query_one("#status", Static).update(self.status_text())
        self.retire_popup(rows)

    def selected_id(self, queue: ListView) -> str | None:
        """The highlighted row's hold, read from the list as it
        stands — the visual rows, not the controller's fresh
        order: a row above the selection may already have
        resolved, and the fresh order would hand back the hold one
        row down."""
        index = queue.index
        if index is None or not 0 <= index < len(queue.children):
            return None
        return getattr(queue.children[index], "hold_id", None)

    def row_position(self, held: str | None, rows: list) -> int:
        """Where the selection lands after a swap: on its hold
        when it survived, else the top."""
        for position, row in enumerate(rows):
            if row.id == held:
                return position
        return 0

    def queue_stale(self, queue: ListView, rows: list) -> bool:
        """Whether the queue's list needs a rebuild: a membership
        change, or a row whose mount is still pending — its text
        widget only joins the tree when the append's mount lands,
        and the in-place countdown path would miss it."""
        if [getattr(row, "hold_id", None) for row in queue.children] != [
            row.id for row in rows
        ]:
            return True
        return any(not item.children for item in queue.children)

    def row_text(self, request: ConsentRequest) -> str:
        """One hold's row: the page's own destination line."""
        return dest_line(request, self.remaining(request))

    def remaining(self, request: ConsentRequest) -> float:
        """The hold's seconds left, from the frame's honest
        deadline; a frame without one counts down from zero (an
        older daemon, and the row still renders)."""
        if request.expires_at is None:
            return 0.0
        return max(0.0, request.expires_at - self.clock())

    def status_text(self) -> str:
        """The status line: the flash while it lives, else the
        link's state beside the mode (once a rules frame named
        it) and the queue's count."""
        state = self.link.reject_reason or self.link.state
        rules = self.link.controller.rules
        if rules is not None:
            state = f"{state} · mode {rules.mode}"
        counted = f"{len(self.link.controller.pending)} held"
        # The daemon's refusal text is its own string; the status
        # line parses markup, so it arrives escaped (#318's rule).
        return self.flash_line.text(flash_safe(f"{state} · {counted}"))

    def retire_popup(self, rows: list[ConsentRequest]) -> None:
        """The queue emptying hides a popup the show path raised:
        the last hold's resolution closes the viewer while the
        decider keeps standing. A queue that never held one leaves
        a viewer the operator opened where it stands."""
        if rows:
            self.holds_known = True
        elif self.holds_known:
            self.holds_known = False
            self.schedule_hide()

    # --- the verdicts -------------------------------------------------------

    def focused_hold(self) -> ConsentRequest | None:
        """The hold the verdict keys act on: the highlighted
        row's hold, read from the list as it stands — a frame may
        already have reshaped the controller's order while the
        list still shows the rows the operator sees."""
        held = self.selected_id(self.query_one("#requests", ListView))
        for row in self.link.controller.ordered():
            if row.id == held:
                return row
        return None

    def action_allow(self) -> None:
        """``a``: allow the focused hold until restart — the
        common case stays one keypress."""
        self.decide("allow", DURATION_DEFAULT)

    def action_deny(self) -> None:
        """``d``: deny the focused hold now."""
        self.decide("deny", "once")

    def action_allow_duration(self) -> None:
        """``A``: pick a duration first, then allow."""
        self.pick_duration("allow")

    def action_deny_duration(self) -> None:
        """``D``: pick a duration first, then deny."""
        self.pick_duration("deny")

    def pick_duration(self, decision: str) -> None:
        """Open the shared duration picker for the focused hold.
        The request id is captured here, not re-read at pick
        time: the focused row can change — or resolve — while the
        picker stands, and the verdict must land on the hold the
        operator keyed it for (the page's own rule; a resolved
        hold answers at the post with the daemon's reason)."""
        hold = self.focused_hold()
        if hold is not None:
            self.push_screen(
                DurationScreen(
                    picked=lambda pick: self.picked(hold.id, decision, pick)
                )
            )

    async def picked(
        self, request_id: str, decision: str, pick: str | None
    ) -> None:
        """The picker's answer: a duration decides the hold the
        picker was keyed on, a cancel changes nothing."""
        if pick is not None:
            self.decide_id(request_id, decision, pick)

    def decide(self, decision: str, duration: str) -> None:
        """Post one verdict for the focused hold, off the key
        path: the row leaves when its resolution frame lands, not
        when the post returns."""
        hold = self.focused_hold()
        if hold is not None:
            self.decide_id(hold.id, decision, duration)

    def decide_id(self, request_id: str, decision: str, duration: str) -> None:
        """Post one verdict for a named hold, off the key path."""
        task = asyncio.create_task(
            self.post_verdict(request_id, decision, duration)
        )
        self.verdicts.add(task)
        task.add_done_callback(self.verdicts.discard)

    async def post_verdict(
        self, request_id: str, decision: str, duration: str
    ) -> None:
        """One verdict through the REST contract: the outcome (or
        its one-line reason) owns the status line for a read."""
        try:
            await self.decide_call(
                self.workspace_id, request_id, decision, duration
            )
        except SystemExit as exc:
            self.flash_line.set(flash_safe(str(exc)))
        except Exception as exc:  # noqa: BLE001 - the status line
            self.flash_line.set(flash_safe(f"verdict post failed: {exc}"))
        else:
            self.flash_line.set(f"{decision} ({duration})")
        if self.is_running:
            self.repaint()

    # --- the mode switch (#465) ---------------------------------------------

    def action_mode(self) -> None:
        """``m``: the mode picker (#465) — the consent page's own
        screen, with the current mode highlighted once a rules
        frame has landed. Before one lands nothing is highlighted
        and a bare Enter decides nothing — the picker never
        pre-selects a posture the app has not seen."""
        rules = self.link.controller.rules
        current = rules.mode if rules is not None else ""
        self.push_screen(ModeScreen(current, self.switch_mode))

    async def switch_mode(self, mode: str | None) -> None:
        """One picked mode (#465): the pick goes to the shared
        switch path — the same gate and confirmation the consent
        page's ``m`` takes."""
        await switch_mode_path(
            mode,
            self.link.controller.rules,
            self.ask_empty_static,
            self.send_mode,
        )

    def ask_empty_static(self, answered) -> None:
        """Push the empty-static confirmation over the queue —
        this app's screen stack is the host the shared path
        needs."""
        self.push_screen(ConfirmScreen(EMPTY_STATIC_QUESTION, answered))

    async def send_mode(
        self, mode: str, *, confirm_empty: bool = False
    ) -> None:
        """One mode switch through the REST contract (#465): a
        switch the daemon refuses names its reason on the status
        line; a landed one takes its success beat through
        :meth:`mode_landed`. Either way the repaint follows only
        while the app runs — a reply that outlives the exit still
        records."""
        try:
            reply = await self.mode_call(
                self.workspace_id, mode, confirm_empty=confirm_empty
            )
        except (Exception, SystemExit) as exc:
            self.flash_line.set(flash_safe(f"mode switch failed: {exc}"))
        else:
            self.mode_landed(reply, mode)
        if self.is_running:
            self.repaint()

    def mode_landed(self, reply: dict, mode: str) -> None:
        """The switch's success beat (#465): the reply's fresh
        rules frame lands through the controller — the same path
        the events socket's frames take, the page's own rule, so
        the mode's name lands without waiting for the pushed frame
        (the daemon pushes the same frame on the events socket,
        and it re-lands the same data idempotently) — and the
        flash confirms the switch with its effect."""
        self.link.controller.apply_frame(
            json.dumps({"event": "egress.rules", "data": reply})
        )
        effect = (
            "in effect now"
            if reply.get("applied")
            else "takes effect at next start"
        )
        self.flash_line.set(
            flash_safe(f"mode {reply.get('mode') or mode} ({effect})")
        )

    # --- the window's life ---------------------------------------------------

    async def retire_when_gone(self) -> None:
        """The liveness tick: a window gone past its grace retires
        the decider — the hidden session ends with the app, and
        the launch's server with the session. The probe runs off
        the loop (it is the same blocking tmux work the show and
        hide paths thread); the clock is this app's own tick, so
        a wedged loop leaves the session standing until the
        operator's next launch — every probe it makes is bounded
        at three seconds."""
        live = await asyncio.to_thread(self.clients_present)
        if self.window_gone(live):
            self.exit()

    def clients_present(self) -> bool:
        """The probe, thread-side: whether the launch's window
        still carries its client."""
        socket = self.popup_socket or ""
        return bool(term_popup.shell_clients(socket, self.popup_session or ""))

    def window_gone(self, live: bool) -> bool:
        """The clock, loop-side: a client attached once and none
        stands past the empty window, or no client ever attached
        past the launch grace."""
        if live:
            self.client_seen = True
            self.empty_since = None
            return False
        if self.empty_since is None:
            self.empty_since = self.clock()
        if not self.client_seen:
            return self.clock() - self.started >= LIVENESS_GRACE_S
        return self.clock() - self.empty_since >= LIVENESS_EMPTY_S

    # --- the viewer ---------------------------------------------------------

    def action_quit(self) -> None:
        """``q``/``Q``/``Esc``: quit standalone; hide the viewer
        in the popup, where the decider outlives its window on
        the shell."""
        if self.persistent:
            self.schedule_hide()
        else:
            self.exit()

    def action_hide(self) -> None:
        """``C-b`` inside the popup: the close half of the toggle
        the footer names — the launch's ``C-b p`` cannot reach in
        here, so the prefix key alone answers."""
        self.schedule_hide()

    def schedule_show(self) -> None:
        """Show the popup, once at a time: the show is blocking
        tmux work, so it runs off the event loop, and a burst of
        holds reuses the show already opening — the popup carries
        the whole queue, not one request."""
        if not self.persistent:
            return
        if self.show_task is not None and not self.show_task.done():
            return
        self.show_task = asyncio.create_task(self.show_worker())

    async def show_worker(self) -> None:
        """Run the show off the loop, retrying an attempt that
        targeted nothing while holds remain — a hold that arrived
        inside the attempt's window must not sit unpopup'd."""
        try:
            for _ in range(POPUP_SHOW_ATTEMPTS):
                shown = await asyncio.to_thread(self.show_popup)
                if shown or not self.link.controller.pending:
                    return
                await asyncio.sleep(POPUP_SHOW_RETRY_DELAY)
        except Exception:  # noqa: BLE001 - fire-and-forget
            return

    def show_popup(self) -> bool:
        """Show the viewer on every shell client, skipped when one
        already stands. Returns whether any client could be
        targeted."""
        socket, session = self.popup_socket, self.popup_session
        if not socket or not session:
            return False
        if term_popup.hidden_has_viewer(socket, session):
            return True
        return self.popup_on_clients(
            term_popup.shell_clients(socket, session), socket, session
        )

    def popup_on_clients(
        self, clients: list[str], socket: str, session: str
    ) -> bool:
        """The show over the targeted clients: each one's
        ``display-popup`` runs to its end — the blocking call
        outliving its timeout means the popup opened and stayed —
        and a failed one ends the pass."""
        for client in clients:
            if not self.popup_on(socket, client, session):
                return False
        return bool(clients)

    def popup_on(self, socket: str, client: str, session: str) -> bool:
        """One client's display-popup, run to its end: True when it
        ran — the blocking call outliving its timeout means the
        popup opened and stayed."""
        try:
            proc = subprocess.run(
                term_popup.show_popup_argv(socket, client, session),
                capture_output=True,
                timeout=term_popup.TMUX_TIMEOUT_S,
            )
        except subprocess.SubprocessError:
            return False
        return proc.returncode == 0

    def schedule_hide(self) -> None:
        """Hide the viewer, once at a time — the detach is the
        same blocking tmux work the show is."""
        if not self.persistent:
            return
        if self.hide_task is not None and not self.hide_task.done():
            return
        self.hide_task = asyncio.create_task(
            asyncio.to_thread(self.hide_viewer)
        )

    def hide_viewer(self) -> None:
        """Detach the hidden session's viewer: the popup hides
        while the app inside keeps running. A failed or stale
        detach changes nothing — the decider stands either way."""
        socket, session = self.popup_socket, self.popup_session
        if not socket or not session:
            return
        try:
            subprocess.run(
                term_popup.detach_argv(socket, session),
                capture_output=True,
                timeout=term_popup.TMUX_TIMEOUT_S,
            )
        except subprocess.SubprocessError:
            return


def parse_args(argv: list[str]) -> tuple[str, str | None, str | None]:
    """The app's argv: the workspace (a bare ``-w``), and the
    popup wiring (``--socket``/``--session``) the launch passes
    when it runs the app in its hidden session."""
    flags: dict[str, str] = {}
    rest = list(argv)
    while rest:
        flag = rest.pop(0)
        if not rest:
            raise SystemExit(f"msks: {flag} needs a value")
        take_flag(flags, flag, rest.pop(0))
    if "workspace" not in flags:
        raise SystemExit(
            "usage: python -m msks.client.tui.decide_app"
            " -w WORKSPACE [--socket S --session N]"
        )
    return (
        flags["workspace"],
        flags.get("socket"),
        flags.get("session"),
    )


def take_flag(flags: dict[str, str], flag: str, value: str) -> None:
    """One flag-value pair onto the map; a flag the app does not
    take refuses with its name."""
    known = {
        "-w": "workspace",
        "--socket": "socket",
        "--session": "session",
    }
    if flag not in known:
        raise SystemExit(f"msks: unknown argument {flag}")
    flags[known[flag]] = value


def main(argv: list[str] | None = None) -> int:
    """The ``-m`` entry the hidden session runs; the popup wiring
    is absent when the app runs standalone."""
    workspace, socket, session = parse_args(
        list(sys.argv[1:] if argv is None else argv)
    )
    ConsentDeciderApp(
        workspace, popup_socket=socket, popup_session=session
    ).run()
    return 0


if __name__ == "__main__":  # pragma: no cover — the -m entry
    raise SystemExit(main())
