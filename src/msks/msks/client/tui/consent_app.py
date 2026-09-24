"""The consent decider TUI (#195): a Textual app over the frames.

``msks egress tui <workspace>`` registers this client as the
workspace's decider on the events websocket, shows the pending-hold
snapshot and every live hold with its countdown, and sends verdicts
through the REST decide/revoke endpoints (msks's decider channel is a
read-only stream — verdicts carry the API's validation).

Every side effect has an injectable seam so the tests drive the app
without a socket or a daemon: ``ws_factory`` yields the connection
(default: websockets), ``decide``/``revoke`` send the verdicts
(default: REST via :mod:`msks.client.rest`). The protocol logic lives
in :mod:`msks.client.tui.consent`; this module is the view.

Fail-closed while disconnected: the daemon registers a decider only
while the socket lives, so a dropped connection means new off-list
connects fail fast and in-flight holds run their timeout — the
reconnect loop re-registers and re-sends the snapshot, and the status
line names the state rather than implying silence.
"""

import asyncio
import json
import logging
import time

import websockets
from rich.markup import escape
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.css.query import NoMatches
from textual.screen import ModalScreen, Screen
from textual.widgets import Footer, ListItem, ListView, OptionList, Static

from ..egress import events_url
from ..rest import api_call, env_token, env_url, ssl_context
from .consent import (
    DURATION_DEFAULT,
    DURATIONS,
    EGRESS_MODES,
    REJECTED,
    ConsentController,
    ConsentRequest,
    EgressRules,
    fmt_duration,
)

#: Reconnect backoff (seconds), capped; a repeatedly-dropping daemon
#: must not spin the client.
RECONNECT_DELAYS = (1.0, 2.0, 5.0)

#: After an auth refusal (the daemon's 4401 close for a bad token)
#: the retry slows to this fixed interval: bounded noise, and the
#: client still heals if a corrected token lands mid-session.
REFUSED_RETRY_INTERVAL = 60.0

#: How long a flashed message owns the status line.
FLASH_TTL = 5.0

#: The close code the events websocket uses for a refused token.
AUTH_CLOSE_CODE = 4401

CONNECTED = "connected"
RECONNECTING = "reconnecting"
REFUSED = "refused — bad token?"

logger = logging.getLogger(__name__)


def registration_frame(workspace_id: str) -> str:
    """The decider-registration frame the handshake sends."""
    return json.dumps({"type": "egress.decider", "workspace": workspace_id})


def dest_line(request: ConsentRequest, remaining: float) -> str:
    """One held request's row text: destination, port (or all
    ports), and the hold's countdown."""
    host = escape(request.dest_host)
    port = (
        " (all ports)" if request.dest_port == 0 else (f":{request.dest_port}")
    )
    return f"{host}{port}  ({int(remaining)}s)"


def duration_label(rule, remaining: float | None) -> str:
    """The verdict's remaining-effect label (no countdown for the
    open-ended durations)."""
    if rule.duration == "forever":
        return "forever"
    if rule.duration == "tilrestart":
        return "until restart"
    if remaining is None:
        return rule.duration or "—"
    return f"{fmt_duration(remaining)} left"


def rule_line(rule, remaining: float | None) -> str:
    """One in-effect verdict's row text with its duration label."""
    host = escape(rule.dest_host)
    port = " (all ports)" if rule.dest_port == 0 else f":{rule.dest_port}"
    return (
        f"{rule.decision:7s} {host}{port}  {duration_label(rule, remaining)}"
    )


def allowlist_text(rules: EgressRules | None) -> str:
    """The rules screen's header: mode + static allowlist (entries
    escaped — operator-supplied strings render literally)."""
    if rules is None:
        return "no rules snapshot yet"
    entries = (
        ", ".join(escape(entry) for entry in rules.allow_list)
        if rules.allow_list
        else "(empty)"
    )
    return f"mode {rules.mode}   allowlist: {entries}"


def rule_rows(rules: EgressRules | None) -> list:
    """The snapshot's verdict rows, allowed then denied."""
    if rules is None:
        return []
    return [*rules.allowed, *rules.denied]


def focused_rule_or_none(screen) -> str | None:
    """The rules list's focused rule id, or None while absent (a
    rebuild's swap window) or nothing focused."""
    try:
        return focused_rule_id(screen.query_one("#rule-rows", ListView))
    except Exception:
        return None


def row_map(rows: ListView) -> dict:
    """The list's rows keyed by their request id."""
    return {
        getattr(child, "request_id", None): child for child in rows.children
    }


def row_ids(rows: ListView) -> set:
    """The ids of the rows currently in the list."""
    return {
        getattr(child, "request_id", None)
        for child in rows.children
        if getattr(child, "request_id", None) is not None
    }


def focused_request_id(rows: ListView | None) -> str | None:
    """The focused queue row's id, or None when nothing is focused
    (a None rows is a rebuild's swap window)."""
    child = rows.highlighted_child if rows is not None else None
    return getattr(child, "request_id", None)


def focus_by_id(rows: ListView, target: str | None) -> None:
    """Highlight the row carrying *target* (the top when it is
    absent or None) — positions come from a freshly-built list, so
    every child under the index is real."""
    for position, child in enumerate(rows.children):
        if target is not None and getattr(child, "request_id", None) == target:
            rows.index = position
            return
    ensure_focus(rows)


def focused_rule_id(rows: ListView | None) -> str | None:
    """The focused rule row's id, or None when nothing is focused
    (a None rows is a rebuild's swap window)."""
    child = rows.highlighted_child if rows is not None else None
    return getattr(child, "rule_id", None)


def ensure_focus(rows: ListView) -> None:
    """Give the list a highlight when it has rows but none focused —
    a hold is always decidable from the keyboard."""
    if rows.index is None and rows.children:
        rows.index = 0


def focus_rule_by_id(rows: ListView, rule_id: str | None) -> None:
    """Highlight the rule row carrying *rule_id* (the top when it
    left or was never set) — a repaint must never move the target of
    a destructive key."""
    for position, child in enumerate(rows.children):
        if rule_id is not None and getattr(child, "rule_id", None) == rule_id:
            rows.index = position
            return
    ensure_focus(rows)


def refused_close(exc: websockets.ConnectionClosed) -> bool:
    """Whether a close was the daemon's token refusal (4401)."""
    return exc.rcvd is not None and exc.rcvd.code == AUTH_CLOSE_CODE


#: The keys every modal above the queue shadows (#280 review, the
#: RulesScreen precedent from #195): the app-level verdict, rules,
#: and quit bindings must not act on the hidden queue below —
#: `a`/`d` deciding a hold nobody can see is the bug class, `q`
#: closing the modal instead of the app is the same rule one level
#: up (each modal binds `q` itself, to its own cancel).
SHADOW_BINDINGS = (
    Binding("a", "noop", show=False),
    Binding("A", "noop", show=False),
    Binding("d", "noop", show=False),
    Binding("D", "noop", show=False),
    Binding("r", "noop", show=False),
)


class ModalShadow:
    """The action half of SHADOW_BINDINGS: swallowing a queue
    key pressed under a modal. A mixin, because Textual resolves
    an action as a method on the focused screen."""

    def action_noop(self) -> None:
        """Swallow a queue-action key pressed under this modal."""


def backoff(delays: tuple[float, ...], attempt: int) -> float:
    """The reconnect delay for an attempt (capped at the last)."""
    if not delays:
        return 0.0
    return delays[min(attempt - 1, len(delays) - 1)]


def effective_allows(rules: EgressRules | None) -> bool:
    """Whether anything effectively allows egress under the
    snapshot (#280): a non-empty allowlist or an in-effect allowed
    verdict — the condition the static switch's confirmation
    gates on."""
    if rules is None:
        return False
    return bool(rules.allow_list) or bool(rules.allowed)


class ModeScreen(ModalShadow, ModalScreen[str | None]):
    """The mode picker (#280): Enter picks, Escape or q cancels.
    The chosen mode (or None) goes to the callback given at
    construction."""

    BINDINGS = [
        *SHADOW_BINDINGS,
        Binding("q", "cancel", show=False),
        Binding("escape", "cancel", "Cancel", show=False),
    ]

    def __init__(self, current: str, picked) -> None:
        super().__init__()
        self.current = current
        self.picked = picked

    def compose(self) -> ComposeResult:
        yield OptionList(*EGRESS_MODES, id="modes")

    def on_mount(self) -> None:
        options = self.query_one("#modes", OptionList)
        options.focus()
        if self.current in EGRESS_MODES:
            options.highlighted = list(EGRESS_MODES).index(self.current)

    def on_option_list_option_selected(
        self, event: OptionList.OptionSelected
    ) -> None:
        self.dismiss_with(str(event.option.prompt))

    def action_cancel(self) -> None:
        self.dismiss_with(None)

    def dismiss_with(self, mode: str | None) -> None:
        """Dismiss and hand the pick to the callback (async — the
        switch runs as a task, so the modal closes without waiting
        on it)."""
        self.dismiss()
        # Referenced: an unreferenced task can be collected mid-await.
        self._pick_task = asyncio.create_task(self.picked(mode))


class ConfirmScreen(ModalShadow, ModalScreen[bool]):
    """A yes/no question (#280): ``y``/Enter answers True, ``n``/
    ``q``/Escape answers False. The callback given at construction
    runs as a task with the answer."""

    BINDINGS = [
        *SHADOW_BINDINGS,
        Binding("y", "yes", "Confirm"),
        Binding("enter", "yes", "Confirm", show=False),
        Binding("n", "no", "Cancel"),
        Binding("q", "no", "Cancel", show=False),
        Binding("escape", "no", "Cancel", show=False),
    ]

    def __init__(self, question: str, answered) -> None:
        super().__init__()
        self.question = question
        self.answered = answered

    def compose(self) -> ComposeResult:
        yield Static(self.question, id="question")

    def action_yes(self) -> None:
        self.dismiss_with(True)

    def action_no(self) -> None:
        self.dismiss_with(False)

    def dismiss_with(self, answer: bool) -> None:
        """Dismiss and hand the answer to the callback."""
        self.dismiss()
        self._pick_task = asyncio.create_task(self.answered(answer))


class DurationScreen(ModalShadow, ModalScreen[str | None]):
    """The duration picker: Enter picks, Escape or q cancels. The
    chosen duration (or None) goes to the callback given at
    construction."""

    BINDINGS = [
        *SHADOW_BINDINGS,
        Binding("q", "cancel", show=False),
        Binding("escape", "cancel", "Cancel", show=False),
    ]

    def __init__(self, picked) -> None:
        super().__init__()
        self.picked = picked

    def compose(self) -> ComposeResult:
        yield OptionList(*DURATIONS, id="durations")

    def on_mount(self) -> None:
        options = self.query_one("#durations", OptionList)
        options.focus()
        options.highlighted = list(DURATIONS).index(DURATION_DEFAULT)

    def on_option_list_option_selected(
        self, event: OptionList.OptionSelected
    ) -> None:
        self.dismiss_with(str(event.option.prompt))

    def action_cancel(self) -> None:
        self.dismiss_with(None)

    def dismiss_with(self, duration: str | None) -> None:
        """Dismiss and hand the pick to the callback (the callback is
        async — the deciding runs as a task, so the modal closes
        without waiting on it)."""
        self.dismiss()
        # Referenced: an unreferenced task can be collected mid-await.
        self._pick_task = asyncio.create_task(self.picked(duration))


class RulesScreen(Screen):
    """The in-effect verdicts: allow/deny rows with durations and
    countdowns, the static allowlist, and revoke on the focused row.

    Arrows move the rule list; ``x`` revokes the focused rule, ``r``
    or Escape returns to the queue — no focus trap anywhere."""

    BINDINGS = [
        Binding("x", "revoke", "Revoke"),
        Binding("m", "mode", "Mode"),
        Binding("r", "back", "Back"),
        Binding("escape", "back", "Back", show=False),
        Binding("q", "back", "Back", show=False),
        # Shadows: the app-level verdict keys must not decide the
        # hidden queue's focused hold from here — a/d/A/D are queue
        # actions, and bubbling them would silently allow or deny a
        # live connection behind this screen.
        Binding("a", "noop", show=False),
        Binding("A", "noop", show=False),
        Binding("d", "noop", show=False),
        Binding("D", "noop", show=False),
    ]

    def action_noop(self) -> None:
        """Swallow a queue-action key pressed on the rules screen."""

    def __init__(
        self, controller: ConsentController, revoke, set_mode
    ) -> None:
        super().__init__()
        self.controller = controller
        self.revoke = revoke
        self.set_mode = set_mode
        self._refresh_scheduled = False
        self._refresh_pending = False

    def compose(self) -> ComposeResult:
        with Vertical(id="rules-body"):
            yield Static(id="allowlist")
            yield ListView(id="rule-rows")
        yield Footer()

    def on_mount(self) -> None:
        self.query_one("#rule-rows", ListView).focus()

    def on_show(self) -> None:
        self.schedule_refresh()

    def schedule_refresh(self) -> None:
        """Arm one rows rebuild; single flight, with a re-arm when a
        frame lands mid-flight (the in-progress rebuild already
        captured the old snapshot — the re-arm applies the new one
        the moment it lands)."""
        if self._refresh_scheduled:
            self._refresh_pending = True
            return
        self._refresh_scheduled = True

        async def flight() -> None:
            try:
                while self.app.is_running:
                    self._refresh_pending = False
                    await self.rebuild_rows()
                    if not self._refresh_pending:
                        return
            except Exception:
                # Teardown unmounts the tree under a mid-swap flight;
                # that is not a bug worth a traceback after exit.
                if self.app.is_running:
                    logger.exception("rules rebuild failed")
            finally:
                self._refresh_scheduled = False

        # Referenced: an unreferenced task can be collected mid-await.
        self._refresh_task = asyncio.create_task(flight())

    async def rebuild_rows(self) -> None:
        """Repaint from the controller's rules snapshot with a
        freshly-built list, preserving the focused rule by id (the
        top when it left): a mutating ListView carries
        asynchronously-pruned stale children that shift indexes, so
        positions must come from children that are all real —
        `x` must never retarget through a shifted index."""
        rules = self.controller.rules
        self.query_one("#allowlist", Static).update(allowlist_text(rules))
        body = self.query_one("#rules-body", Vertical)
        old = None
        try:
            old = self.query_one("#rule-rows", ListView)
        except NoMatches:
            pass  # a died-mid-swap rebuild: mount the fresh list anew
        focused = focused_rule_id(old)
        items = []
        for rule in rule_rows(rules):
            item = ListItem(
                Static(rule_line(rule, self.controller.rule_remaining(rule)))
            )
            item.rule_id = rule.id
            items.append(item)
        fresh = ListView(*items, id="rule-rows")
        if old is not None:
            await old.remove()  # frees the id before the fresh list mounts
        await body.mount(fresh)
        fresh.focus()
        focus_rule_by_id(fresh, focused)  # after mount: index sticks

    async def action_revoke(self) -> None:
        """Revoke the focused rule through the injected seam; the row
        leaves on the refreshed ``egress.rules`` frame, never
        optimistically (a still-enforced rule must not hide). A key
        pressed inside a rebuild's swap window reads as nothing
        focused."""
        rule_id = focused_rule_or_none(self)
        if rule_id is not None:
            await self.revoke(rule_id)

    def action_mode(self) -> None:
        """Open the mode picker (#280); the current mode starts
        highlighted. The picked mode goes to the app's switch
        path (which owns the empty-static confirmation)."""
        rules = self.controller.rules
        current = rules.mode if rules is not None else ""
        self.app.push_screen(ModeScreen(current, self.app.switch_mode))

    def action_back(self) -> None:
        self.app.pop_screen()


class ConsentDeciderApp(App):
    """Live queue of held requests with allow/deny, plus the rules
    screen — a thin view over :class:`ConsentController`."""

    CSS = """
    Screen { layout: vertical; }
    #status { padding: 0 1; background: $panel; color: $text-muted; }
    #queue { height: 1fr; }
    #requests ListItem { height: 1; }
    #empty { padding: 1 2; color: $text-muted; }
    """

    BINDINGS = [
        Binding("a", "allow", "Allow"),
        Binding("A", "allow_duration", "Allow…"),
        Binding("d", "deny", "Deny"),
        Binding("D", "deny_duration", "Deny…"),
        Binding("r", "rules", "Rules"),
        Binding("q", "quit_screen", "Quit"),
        Binding("escape", "quit_screen", "Quit", show=False),
    ]

    def __init__(
        self,
        workspace_id: str,
        *,
        hold_timeout: float = 120.0,
        ws_factory=None,
        decide=None,
        revoke=None,
        set_mode=None,
        reconnect_delays: tuple[float, ...] = RECONNECT_DELAYS,
    ) -> None:
        super().__init__()
        self.workspace_id = workspace_id
        self.reconnect_delays = reconnect_delays
        self.controller = ConsentController(
            hold_timeout=hold_timeout, workspace_id=workspace_id
        )
        self._ws_factory = ws_factory or default_ws_factory
        self._decide = decide or rest_decide
        self._revoke = revoke or rest_revoke
        self._set_mode = set_mode or rest_set_mode
        self._conn_state = RECONNECTING
        self._stop = False
        self._flash_msg = ""
        self._flash_until = 0.0
        self._last_conn_error = ""
        self._rebuild_scheduled = False
        self._rebuild_pending = False

    # -- lifecycle -----------------------------------------------------------

    def compose(self) -> ComposeResult:
        yield Static(id="status")
        with Vertical(id="queue"):
            yield ListView(id="requests")
            yield Static("No held requests — connected, waiting.", id="empty")
        yield Footer()

    def on_mount(self) -> None:
        self.title = f"msks egress · {self.workspace_id}"
        self.query_one("#requests", ListView).focus()
        self.run_worker(
            self.ws_loop, exclusive=True, group="ws", exit_on_error=False
        )
        self.set_interval(1.0, self.safe_repaint)

    # -- the websocket worker ---------------------------------------------

    async def ws_loop(self) -> None:
        """Connect, register, pump; reconnect until stopped."""
        attempt = 0
        while not self._stop:
            connected, refused = await self.pump_one()
            if self._stop:
                return
            if refused:
                await asyncio.sleep(REFUSED_RETRY_INTERVAL)
                self.safe_repaint()
                continue
            # A connection that reached serve_connection was healthy:
            # the next drop starts the backoff ladder over instead of
            # climbing it for a lifetime of cumulative disconnects.
            attempt = 0 if connected else attempt + 1
            await asyncio.sleep(backoff(self.reconnect_delays, attempt))
            self.safe_repaint()

    async def pump_one(self) -> tuple[bool, bool]:
        """One connection's lifetime; ``(connected, refused)`` —
        whether the dial succeeded (resetting the backoff ladder on
        the next drop) and whether the close was an auth refusal
        (retry slowly) rather than a drop (backoff)."""
        try:
            ws = await self._ws_factory().__aenter__()
        except Exception as exc:
            self.on_disconnect()
            self.flash_once(f"connect failed: {exc}")
            return False, False
        try:
            await self.serve_connection(ws)
        except websockets.ConnectionClosed as exc:
            refused = refused_close(exc)
            self.on_disconnect(refused)
            return True, refused
        except Exception:
            self.on_disconnect(False)
            return True, False
        finally:
            await close_ws(ws)
        return True, False

    async def serve_connection(self, ws) -> None:
        """Register, then feed every frame to the controller; render
        exceptions are isolated so a UI bug never tears down the
        transport."""
        self._conn_state = CONNECTED
        await ws.send(registration_frame(self.workspace_id))
        self.controller.reset()
        self.safe_repaint()
        async for raw in ws:
            outcome, payload = self.controller.apply_frame(raw)
            if outcome == REJECTED:
                # The daemon refused the registration (an unknown
                # workspace): waiting would be promptless forever.
                reason = payload or "registration rejected"
                self.on_disconnect(True)
                self.flash_once(f"registration rejected: {reason}")
                self._stop = True
                return
            self.safe_repaint()

    def flash_once(self, message: str) -> None:
        """Flash a connect failure when its text changes: byte-identical
        retries stay quiet; a cause that flips back and forth (a
        restarting daemon alternating refusal with TLS failure)
        re-names itself on each transition — bounded by the reconnect
        backoff, visible for the flash TTL."""
        if message != self._last_conn_error:
            self._last_conn_error = message
            self.flash(message)

    def on_disconnect(self, refused: bool = False) -> None:
        """Record the drop (the next loop pass reconnects); an auth
        refusal holds the refused label through its slow retry —
        the slow retry sleeps long enough that the label must land
        here, not after it."""
        self._conn_state = REFUSED if refused else RECONNECTING
        self.safe_repaint()

    # -- verdicts -------------------------------------------------------------

    async def action_allow(self) -> None:
        await self.decide_focused("allow", DURATION_DEFAULT)

    async def action_deny(self) -> None:
        await self.decide_focused("deny", DURATION_DEFAULT)

    async def action_allow_duration(self) -> None:
        await self.pick_duration("allow")

    async def action_deny_duration(self) -> None:
        await self.pick_duration("deny")

    async def pick_duration(self, decision: str) -> None:
        """Open the duration picker for the FOCUSED hold; a picked
        duration decides that hold, a cancel decides nothing. The
        request id is captured here: the focused row can change (or
        resolve) while the picker is open, and Enter must not land
        the verdict on whatever holds focus when the pick arrives.
        The picker reports through a callback (push_screen_wait
        demands a worker context actions do not have)."""
        request_id = focused_request_id(self.queue_rows())
        if request_id is None:
            self.flash("no hold focused")
            return
        await self.push_screen(
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

    def queue_rows(self) -> ListView | None:
        """The queue list, or None during a rebuild's swap window (the
        old list removed, the fresh one not yet mounted)."""
        try:
            return self.query_one("#requests", ListView)
        except Exception:
            return None

    async def decide_focused(self, decision: str, duration: str) -> None:
        """Send the verdict for the focused hold through the seam; a
        failure flashes, never crashes the app. A key pressed inside
        a rebuild's swap window reads as nothing focused."""
        rows = self.queue_rows()
        child = rows.highlighted_child if rows is not None else None
        request_id = getattr(child, "request_id", None)
        if request_id is None:
            self.flash("no hold focused")
            return
        await self.send_verdict(request_id, decision, duration)

    async def send_verdict(
        self, request_id: str, decision: str, duration: str
    ) -> None:
        """One decide through the seam, with the flash on failure. The
        REST layer reports failures as SystemExit (one readable
        line); catching it too is what makes a lost race — the hold
        timed out mid-deliberation, the daemon answers 404 — a flash
        instead of a dead app."""
        try:
            await self._decide(
                self.workspace_id, request_id, decision, duration
            )
        except (Exception, SystemExit) as exc:
            self.flash(f"decide failed: {exc}")

    def action_rules(self) -> None:
        self.push_screen(
            RulesScreen(self.controller, self.revoke_rule, self.switch_mode)
        )

    async def switch_mode(self, mode: str | None) -> None:
        """One picked mode from the picker (#280). ``static`` with
        nothing effectively allowed confirms first — that posture
        answers every name NXDOMAIN, and the daemon refuses an
        unconfirmed switch; every other pick (and a confirmed
        ``static``) goes straight through the seam. The refreshed
        ``egress.rules`` frame repaints the header — the switch is
        never reflected optimistically."""
        if mode is None:
            return
        if mode == "static" and not effective_allows(self.controller.rules):
            await self.push_screen(
                ConfirmScreen(
                    "static with nothing allowed answers every name "
                    "NXDOMAIN — an offline workspace. Switch anyway?",
                    self.confirmed_switch,
                )
            )
            return
        await self.send_mode(mode)

    async def confirmed_switch(self, answer: bool) -> None:
        """The confirmation's answer: a yes sends the confirmed
        static switch, a no decides nothing."""
        if answer:
            await self.send_mode("static", confirm_empty=True)

    async def send_mode(
        self, mode: str, *, confirm_empty: bool = False
    ) -> None:
        """One mode switch through the seam; a failure flashes,
        never crashes the app (SystemExit included — the REST
        seam's error surface, the daemon's named refusal among
        them)."""
        try:
            await self._set_mode(
                self.workspace_id, mode, confirm_empty=confirm_empty
            )
        except (Exception, SystemExit) as exc:
            self.flash(f"mode switch failed: {exc}")

    async def revoke_rule(self, request_id: str) -> None:
        """Revoke through the seam; a failure flashes on the app
        (SystemExit included — the REST seam's error surface)."""
        try:
            await self._revoke(self.workspace_id, request_id)
        except (Exception, SystemExit) as exc:
            self.flash(f"revoke failed: {exc}")

    def action_quit_screen(self) -> None:
        self._stop = True
        self.exit()

    # -- rendering --------------------------------------------------------

    def flash(self, message: str) -> None:
        """Give the status line to a message for FLASH_TTL seconds."""
        self._flash_msg = message
        self._flash_until = time.time() + FLASH_TTL
        self.safe_repaint()

    def safe_repaint(self) -> None:
        """Repaint, isolating render failures (a UI bug must not
        tear down the transport that would re-trigger it)."""
        try:
            self.repaint()
        except Exception:
            pass

    def repaint(self) -> None:
        """Sync the queue rows and the status line to state, and keep
        the rules screen live when it is the active screen (its
        countdowns tick, a fresh frame shows up — the port of
        klangk's per-tick rules refresh)."""
        self.refresh_rules_screen()
        self.sync_rows()
        self.update_status()

    def refresh_rules_screen(self) -> None:
        """Repaint the rules screen when it is on top (the rebuild
        awaits widget mounts, so it runs as a task, one flight at a
        time)."""
        screen = self.screen
        if isinstance(screen, RulesScreen):
            screen.schedule_refresh()

    def empty_line(self) -> str:
        """The empty-queue line, honest about the connection state."""
        if self._conn_state == CONNECTED:
            return "No held requests — connected, waiting."
        return f"No held requests — {self._conn_state}."

    def sync_rows(self) -> None:
        """Sync the queue to state. A membership change (a hold
        resolved, a new one arrived) rebuilds the list fresh —
        Textual prunes removed children asynchronously, so mutating
        a live ListView leaves stale copies that shift every index
        under the highlight; a fresh list keeps the destructive
        keys' target derivable from children that are all real.
        Same-set ticks repaint survivors' countdowns in place
        (no flicker, no index motion). A missing list (a rebuild
        died mid-swap) schedules a rebuild — the queue self-heals
        instead of wedging blank."""
        try:
            rows = self.query_one("#requests", ListView)
        except NoMatches:
            self.schedule_rebuild()
            return
        ordered = self.controller.ordered()
        if row_ids(rows) != {request.id for request in ordered}:
            # The rebuild awaits the old list's removal and the new
            # one's mount, so it runs as a task, one flight at a
            # time; a tick while it is in flight sees the membership
            # still differ and re-arms after it lands.
            self.schedule_rebuild()
            return
        self.repaint_countdowns(rows, ordered)
        self.query_one("#empty", Static).display = not ordered
        self.query_one("#empty", Static).update(self.empty_line())

    def repaint_countdowns(
        self, rows: ListView, ordered: list[ConsentRequest]
    ) -> None:
        """Repaint each survivor's countdown in place."""
        existing = row_map(rows)
        for request in ordered:
            item = existing.get(request.id)
            if item is not None:
                item.query_one(Static).update(
                    dest_line(request, self.controller.remaining(request))
                )

    def schedule_rebuild(self) -> None:
        """Arm one queue rebuild; single flight, with a re-arm when a
        request lands mid-flight (the in-progress rebuild already
        captured the old membership — the re-arm applies the new one
        the moment it lands, rather than waiting a tick)."""
        if self._rebuild_scheduled:
            self._rebuild_pending = True
            return
        self._rebuild_scheduled = True

        async def flight() -> None:
            try:
                while self.is_running:
                    self._rebuild_pending = False
                    await self.rebuild_queue(self.controller.ordered())
                    if not self._rebuild_pending:
                        return
            except Exception:
                # Teardown unmounts the tree under a mid-swap flight;
                # that is not a bug worth a traceback after exit.
                if self.is_running:
                    logger.exception("queue rebuild failed")
            finally:
                self._rebuild_scheduled = False

        # Referenced: an unreferenced task can be collected mid-await.
        self._flight_task = asyncio.create_task(flight())

    async def rebuild_queue(self, ordered: list[ConsentRequest]) -> None:
        """Swap in a freshly-built queue list (its mount awaited),
        restoring focus by id (the top when the focused hold left)
        so `a`/`d` never retarget through a shifted index. Focus is
        read from the live list here, at rebuild time — never
        captured at arm time — so a re-armed iteration restores the
        focus the operator set since, not a stale one. A missing old
        list (a rebuild died mid-swap) is fine: the fresh list
        mounts anew, the queue self-heals."""
        queue = self.query_one("#queue", Vertical)
        old = None
        try:
            old = self.query_one("#requests", ListView)
        except NoMatches:
            pass
        focused = focused_request_id(old)
        items = [self.render_item(request) for request in ordered]
        fresh = ListView(*items, id="requests")
        if old is not None:
            await old.remove()  # frees the id before the fresh list mounts
        await queue.mount(fresh)
        fresh.focus()
        focus_by_id(fresh, focused)  # after mount: index sticks
        self.query_one("#empty", Static).display = not ordered
        self.query_one("#empty", Static).update(self.empty_line())

    def render_item(self, request: ConsentRequest) -> ListItem:
        """One queue row."""
        item = ListItem(
            Static(dest_line(request, self.controller.remaining(request)))
        )
        item.request_id = request.id
        return item

    def update_status(self) -> None:
        """The status line: a flash owns it until its TTL lapses."""
        if self._flash_until > time.time():
            text = self._flash_msg
        else:
            held = len(self.controller.pending)
            text = (
                f" {escape(self.workspace_id)}  ·  {self._conn_state}"
                f"  ·  {held} held"
            )
        self.query_one("#status", Static).update(text)


async def close_ws(ws) -> None:
    """Close the connection, swallowing a peer that already went."""
    try:
        await ws.close()
    except Exception:
        pass


# --- default seams (the real socket and REST calls) ---------------------

_SHARED_SSL: list = [None]


def shared_ssl():
    """The one TLS context for the app's lifetime: building it
    prints the TOFU warning when MSKSC_CAFILE is unset, and building
    it per verdict or per reconnect would spray that warning across
    the live screen."""
    if _SHARED_SSL[0] is None:
        _SHARED_SSL[0] = ssl_context()
    return _SHARED_SSL[0]


def ws_connect_kwargs(url: str, token: str, ssl_ctx) -> dict:
    """The events connection's kwargs (a plain-ws URL takes no ssl
    argument) — built on the shared context, not a fresh one."""
    return {
        "uri": events_url(url, token),
        "ssl": None if url.startswith("http://") else ssl_ctx,
        "max_size": 2**22,
    }


def default_ws_factory():
    """The events websocket connection (the decider's stream)."""
    return websockets.connect(
        **ws_connect_kwargs(env_url(), env_token(), shared_ssl())
    )


async def rest_decide(
    workspace_id: str, request_id: str, decision: str, duration: str
) -> dict:
    """One verdict through the REST endpoint (the shared TLS
    context — no per-verdict TOFU warning)."""
    return await api_call(
        "POST",
        env_url(),
        env_token(),
        f"/api/v1/workspaces/{workspace_id}/egress/requests/{request_id}",
        json_body={"decision": decision, "duration": duration},
        ssl_ctx=shared_ssl(),
    )


async def rest_revoke(workspace_id: str, request_id: str) -> dict:
    """One revoke through the REST endpoint (shared context)."""
    return await api_call(
        "DELETE",
        env_url(),
        env_token(),
        f"/api/v1/workspaces/{workspace_id}/egress/requests/{request_id}",
        ssl_ctx=shared_ssl(),
    )


async def rest_set_mode(
    workspace_id: str, mode: str, *, confirm_empty: bool = False
) -> dict:
    """One mode switch through the REST endpoint (#280; shared
    context). ``confirm_empty`` rides only when set — the daemon's
    refusal names it."""
    body: dict = {"mode": mode}
    if confirm_empty:
        body["confirm_empty"] = True
    return await api_call(
        "PUT",
        env_url(),
        env_token(),
        f"/api/v1/workspaces/{workspace_id}/egress/policy",
        json_body=body,
        ssl_ctx=shared_ssl(),
    )


def run_consent_tui(workspace_id: str) -> int:
    """``msks egress tui <workspace>``: launch the decider app. The
    env and TLS context are read before the app starts (a missing
    token exits with the one readable line every sibling command
    prints, not a SystemExit tearing down a half-drawn screen — and
    the TOFU warning prints once here, before the screen owns the
    terminal)."""
    env_url()
    env_token()
    shared_ssl()  # the TOFU warning (when it prints) lands here, once
    ConsentDeciderApp(workspace_id).run()
    return 0
