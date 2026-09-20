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
import time

import websockets
from rich.markup import escape
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.screen import ModalScreen, Screen
from textual.widgets import Footer, ListItem, ListView, OptionList, Static

from ..egress import events_url
from ..rest import api_call, env_token, env_url, ssl_context
from .consent import (
    DURATION_DEFAULT,
    DURATIONS,
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


def row_map(rows: ListView) -> dict:
    """The list's rows keyed by their request id."""
    return {
        getattr(child, "request_id", None): child for child in rows.children
    }


def focused_rule_id(rows: ListView) -> str | None:
    """The focused rule row's id, or None when nothing is focused."""
    child = rows.highlighted_child
    return getattr(child, "rule_id", None)


def ensure_focus(rows: ListView) -> None:
    """Give the list a highlight when it has rows but none focused —
    a hold is always decidable from the keyboard."""
    if rows.index is None and rows.children:
        rows.index = 0


def restore_focus(rows: ListView, rule_id: str | None) -> None:
    """After a rebuild, re-highlight the row the user had focused
    (the top when it left or was never set) — a repaint must never
    move the target of a destructive key."""
    for position, child in enumerate(rows.children):
        if getattr(child, "rule_id", None) == rule_id and rule_id:
            rows.index = position
            return
    ensure_focus(rows)


def drop_stale_rows(rows: ListView, current_ids: set) -> None:
    """Remove rows whose request left (survivors untouched — no
    flicker)."""
    for child in list(rows.children):
        rid = getattr(child, "request_id", None)
        if rid is not None and rid not in current_ids:
            child.remove()


def refused_close(exc: websockets.ConnectionClosed) -> bool:
    """Whether a close was the daemon's token refusal (4401)."""
    return exc.rcvd is not None and exc.rcvd.code == AUTH_CLOSE_CODE


def backoff(delays: tuple[float, ...], attempt: int) -> float:
    """The reconnect delay for an attempt (capped at the last)."""
    if not delays:
        return 0.0
    return delays[min(attempt - 1, len(delays) - 1)]


class DurationScreen(ModalScreen[str | None]):
    """The duration picker: Enter picks, Escape cancels. The chosen
    duration (or None) goes to the callback given at construction."""

    BINDINGS = [
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
        asyncio.create_task(self.picked(duration))


class RulesScreen(Screen):
    """The in-effect verdicts: allow/deny rows with durations and
    countdowns, the static allowlist, and revoke on the focused row.

    Arrows move the rule list; ``x`` revokes the focused rule, ``r``
    or Escape returns to the queue — no focus trap anywhere."""

    BINDINGS = [
        Binding("x", "revoke", "Revoke"),
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

    def __init__(self, controller: ConsentController, revoke) -> None:
        super().__init__()
        self.controller = controller
        self.revoke = revoke

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Static(id="allowlist")
            yield ListView(id="rule-rows")
        yield Footer()

    def on_mount(self) -> None:
        self.query_one("#rule-rows", ListView).focus()

    def on_show(self) -> None:
        self.refresh_rows()

    def refresh_rows(self) -> None:
        """Repaint the rows from the controller's rules snapshot,
        preserving the focused rule: a clear+rebuild resets the
        highlight to the top, and `x` a second later would revoke the
        wrong rule (the first allow) — the id is captured before the
        clear and restored after."""
        rules = self.controller.rules
        self.query_one("#allowlist", Static).update(allowlist_text(rules))
        rows = self.query_one("#rule-rows", ListView)
        focused = focused_rule_id(rows)
        rows.clear()
        for rule in rule_rows(rules):
            item = ListItem(
                Static(rule_line(rule, self.controller.rule_remaining(rule)))
            )
            item.rule_id = rule.id
            rows.append(item)
        restore_focus(rows, focused)

    async def action_revoke(self) -> None:
        """Revoke the focused rule through the injected seam; the row
        leaves on the refreshed ``egress.rules`` frame, never
        optimistically (a still-enforced rule must not hide)."""
        rule_id = focused_rule_id(self.query_one("#rule-rows", ListView))
        if rule_id is not None:
            await self.revoke(rule_id)

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
        reconnect_delays: tuple[float, ...] = RECONNECT_DELAYS,
    ) -> None:
        super().__init__()
        self.workspace_id = workspace_id
        self.reconnect_delays = reconnect_delays
        self.controller = ConsentController(hold_timeout=hold_timeout)
        self._ws_factory = ws_factory or default_ws_factory
        self._decide = decide or rest_decide
        self._revoke = revoke or rest_revoke
        self._ws = None
        self._conn_state = RECONNECTING
        self._stop = False
        self._flash_msg = ""
        self._flash_until = 0.0
        self._last_conn_error = ""

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
            refused = await self.pump_one()
            if self._stop:
                return
            if refused:
                await asyncio.sleep(REFUSED_RETRY_INTERVAL)
                self.safe_repaint()
                continue
            attempt += 1
            await asyncio.sleep(backoff(self.reconnect_delays, attempt))
            self.safe_repaint()

    async def pump_one(self) -> bool:
        """One connection's lifetime; True when the close was an auth
        refusal (retry slowly) rather than a drop (backoff)."""
        try:
            ws = await self._ws_factory().__aenter__()
        except Exception as exc:
            self.on_disconnect()
            self.flash_once(f"connect failed: {exc}")
            return False
        try:
            await self.serve_connection(ws)
        except websockets.ConnectionClosed as exc:
            refused = refused_close(exc)
            self.on_disconnect(refused)
            return refused
        except Exception:
            self.on_disconnect(False)
            return False
        finally:
            await close_ws(ws)
        return False

    async def serve_connection(self, ws) -> None:
        """Register, then feed every frame to the controller; render
        exceptions are isolated so a UI bug never tears down the
        transport."""
        self._ws = ws
        self._conn_state = CONNECTED
        await ws.send(registration_frame(self.workspace_id))
        self.controller.reset()
        self.safe_repaint()
        async for raw in ws:
            self.controller.apply_frame(raw)
            self.safe_repaint()

    def flash_once(self, message: str) -> None:
        """Flash a connect failure when its text changes: the first
        failure (and each new cause) names itself once, retries stay
        quiet."""
        if message != self._last_conn_error:
            self._last_conn_error = message
            self.flash(message)

    def on_disconnect(self, refused: bool = False) -> None:
        """Record the drop (the next loop pass reconnects); an auth
        refusal holds the refused label through its slow retry —
        the slow retry sleeps long enough that the label must land
        here, not after it."""
        self._ws = None
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
        child = self.query_one("#requests", ListView).highlighted_child
        request_id = getattr(child, "request_id", None)
        await self.push_screen(
            DurationScreen(self.finish_pick(decision, request_id))
        )

    def finish_pick(self, decision: str, request_id: str | None):
        """The callback the picker calls with the picked duration
        (or None on cancel)."""

        async def picked(duration: str | None) -> None:
            if duration is None:
                return
            if request_id not in self.controller.pending:
                self.flash("hold already resolved")
                return
            await self.send_verdict(request_id, decision, duration)

        return picked

    async def decide_focused(self, decision: str, duration: str) -> None:
        """Send the verdict for the focused hold through the seam; a
        failure flashes, never crashes the app."""
        child = self.query_one("#requests", ListView).highlighted_child
        request_id = getattr(child, "request_id", None)
        if request_id is not None:
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
        self.push_screen(RulesScreen(self.controller, self.revoke_rule))

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
        """Repaint the rules screen when it is on top."""
        screen = self.screen
        if isinstance(screen, RulesScreen):
            screen.refresh_rows()

    def empty_line(self) -> str:
        """The empty-queue line, honest about the connection state."""
        if self._conn_state == CONNECTED:
            return "No held requests — connected, waiting."
        return f"No held requests — {self._conn_state}."

    def sync_rows(self) -> None:
        """Sync the queue without a rebuild: drop resolved rows,
        repaint survivors' countdowns in place, append new ones
        (order is stable, so nothing reorders)."""
        rows = self.query_one("#requests", ListView)
        ordered = self.controller.ordered()
        drop_stale_rows(rows, {request.id for request in ordered})
        existing = row_map(rows)
        for request in ordered:
            self.sync_one_row(rows, existing.get(request.id), request)
        ensure_focus(rows)
        self.query_one("#empty", Static).display = not ordered
        self.query_one("#empty", Static).update(self.empty_line())

    def sync_one_row(self, rows: ListView, item, request) -> None:
        """Append a new row or repaint a survivor's countdown."""
        if item is None:
            rows.append(self.render_item(request))
            return
        item.query_one(Static).update(
            dest_line(request, self.controller.remaining(request))
        )

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
