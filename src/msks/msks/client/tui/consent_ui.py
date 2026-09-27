"""The consent UI's shared pieces (#195, #358): the view over a
:class:`ConsentController` that every consent surface in the tree
renders from.

The workspace page's consent overlay (the modal over the page,
``main_app.ConsentOverlay`) owns the held-request queue and the
verdict keys; the rules screen — below in this module — pushes
above the overlay as a full-screen visit. The placeholder audit
moved to the tree's secrets page (#390): its daemon-wide view lives
in ``main_app`` beside the page that opens it, rendering through
this module's row helpers.

This module keeps everything those surfaces share — the row and
focus helpers, the reconnect ladder's constants, the pickers (mode,
duration, and the generic one the audit view's filters take), the
rules screen (in-effect verdicts, revoke), and the audit row
rendering — plus the connection seams the page's
:class:`~msks.client.tui.link.DeciderLink` dials through
(``default_ws_factory``, the one shared TLS context). The protocol
logic lives in :mod:`msks.client.tui.consent`; this module is the
view.

Fail-closed while disconnected: the daemon registers a decider only
while the socket lives, so a dropped connection means new off-list
connects fail fast and in-flight holds run their timeout — the
link's reconnect loop re-registers and re-sends the snapshot, and
the surfaces name the state rather than implying silence.
"""

import asyncio
import json
import logging
import re
import time

import websockets
from rich.markup import escape
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.css.query import NoMatches
from textual.screen import ModalScreen, Screen
from textual.widgets import Footer, ListItem, ListView, OptionList, Static

from ..egress import events_url
from ..env import env_token, env_url, ssl_context
from ..wsauth import auth_headers
from .consent import (
    DURATION_DEFAULT,
    DURATIONS,
    EGRESS_MODES,
    ConsentController,
    ConsentRequest,
    EgressRules,
    SecretEvent,
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


def mode_label(rules: EgressRules | None) -> str:
    """The workspace's current egress mode as a label; ``—`` until
    the first rules frame names it (the queue's status line shares
    it — the mode stays visible on every screen, #301)."""
    if rules is None or not rules.mode:
        return "—"
    return rules.mode


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


def row_rule_ids(rows: ListView) -> list:
    """The rules list's row ids in order — the in-place repaint's
    membership check (order included: a reordered snapshot takes
    the fresh-list swap, never a text-only pass over moved rows)."""
    return [getattr(child, "rule_id", None) for child in rows.children]


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


def backoff(delays: tuple[float, ...], attempt: int) -> float:
    """The reconnect delay for an attempt: the ladder's first
    delay at the reset (attempt 0) and below it, capped at the
    last."""
    if not delays:
        return 0.0
    return delays[min(max(attempt - 1, 0), len(delays) - 1)]


class OneFlight:
    """A re-armable single-flight rebuild (#201), the one mechanism
    the rules screen, the audit view, and the queue share: one
    rebuild is in the air at a time, a request landing mid-flight
    re-arms, and the in-air flight loops to apply the newer state
    the moment it lands — never two concurrent rebuilds over one
    widget tree. ``rebuild`` and ``alive`` are read at flight time,
    so a test (or a subclass) swapping the callable mid-session is
    honored."""

    def __init__(self, rebuild, alive, label: str) -> None:
        self._rebuild = rebuild
        self._alive = alive
        self._label = label
        #: The flight's state, read by the owner's tests: armed, and
        #: re-armed by a mid-flight request.
        self.scheduled = False
        self.pending = False

    def request(self) -> None:
        """Arm one flight; a request mid-flight re-arms after it."""
        if self.scheduled:
            self.pending = True
            return
        self.scheduled = True
        # Referenced: an unreferenced task can be collected mid-await.
        self._task = asyncio.create_task(self._flight())

    async def _flight(self) -> None:
        rearm = False
        try:
            while self._alive():
                self.pending = False
                await self._rebuild()
                if not self.pending:
                    return
        except Exception:
            rearm = self.died_mid_flight()
        finally:
            self.scheduled = False
            if rearm:
                # Clear the in-air flag first so request() arms a
                # real flight; this block takes no await, so a
                # concurrent request cannot interleave (#322).
                self.pending = False
                self.request()

    def died_mid_flight(self) -> bool:
        """A rebuild died mid-flight: log it on a live owner (teardown
        unmounts the tree under a mid-swap flight — not a bug worth
        a traceback after exit), and say whether a request armed
        while the flight was dying must be carried: the loop honors
        ``pending`` only after a successful rebuild, and the audit
        view's unchanged-log path takes no rebuild from a later
        tick — a dropped request would leave its rows unmounted
        until the next frame landed."""
        if self._alive():
            logger.exception("%s rebuild failed", self._label)
            return self.pending
        return False


def focused_event_id(rows: ListView | None) -> int | None:
    """The focused event row's seq, or None when nothing is focused
    (a None rows is a rebuild's swap window)."""
    child = rows.highlighted_child if rows is not None else None
    return getattr(child, "event_seq", None)


def focus_event_by_id(rows: ListView, target: int | None) -> None:
    """Highlight the event row carrying *target* (the top when it
    left or was never set) — a repaint must never move a row out
    from under a reading operator."""
    for position, child in enumerate(rows.children):
        if target is not None and getattr(child, "event_seq", None) == target:
            rows.index = position
            return
    ensure_focus(rows)


def event_time(ts: float) -> str:
    """The event's local wall-clock label; an undated frame (an
    older daemon's event) keeps a blank column."""
    if ts <= 0.0:
        return ""
    return time.strftime("%H:%M:%S", time.localtime(ts))


def event_dest(event: SecretEvent) -> str:
    """The event's destination text: the host a swap or sighting
    named on the wire, the mint's allowlist, or nothing on the exit
    kinds (revoke, expiry)."""
    if event.host is not None:
        return f" → {escape(event.host)}"
    if event.dests:
        return " → " + ", ".join(escape(entry) for entry in event.dests)
    return ""


def event_line(event: SecretEvent) -> str:
    """One audit row's text: marker, time, kind, workspace/name,
    the placeholder's row id (the durable handle for correlating
    the audit view once the row retires), and the destination.
    ``!`` marks the off-allowlist sighting — the exfil signal, the
    one row kind the screen also highlights."""
    marker = "!" if event.kind == "sighting" else " "
    return (
        f"{marker} {event_time(event.ts):>8}  {event.kind:<8}"
        f"  {escape(event.workspace_id)}/{escape(event.name)}"
        f"{event_id_suffix(event)}"
        f"{event_dest(event)}"
    )


def event_id_suffix(event: SecretEvent) -> str:
    """The row-id suffix ``#4``, or nothing when the frame carried
    no id (an older daemon)."""
    return f"#{event.placeholder_id}" if event.placeholder_id else ""


def render_order(events: list[SecretEvent]) -> list[SecretEvent]:
    """The events oldest-first for rendering (#305): timestamp
    order, ties in arrival order — a live frame the socket
    delivered between two replayed rows renders in its time's
    place, not wherever the interleaving dropped it. Arrival order
    is client-local, so two same-timestamp rows can render in
    either order across reconnects — the tie is cosmetic, the
    timestamps are not."""
    return sorted(events, key=lambda event: (event.ts, event.seq))


def event_item(event: SecretEvent) -> ListItem:
    """One audit row as a list item; the sighting carries the
    highlight class (the color pairs with the ``!`` marker)."""
    item = ListItem(Static(event_line(event)))
    item.event_seq = event.seq
    if event.kind == "sighting":
        item.add_class("sighting")
    return item


def events_note() -> str:
    """The audit view's explanatory line, in operator language
    (#305, widened daemon-wide by #390): what the rows are, what
    the marker means, and where detection stops (#201)."""
    return (
        "Placeholder-token audit — every workspace's recorded mints, "
        "revokes, and expiries, then wire events as they happen. "
        "! marks a sighting: a placeholder token reached a host its "
        "mint did not allow — the exfiltration signal. Wire events "
        "cover decrypted connections only; a connection the daemon "
        "relays untouched passes unread and produces no row."
    )


def sighting_flash(event: SecretEvent) -> str:
    """The status line an off-allowlist sighting takes while the
    queue (or picker) is on top: the exfil signal surfaces on every
    screen of the app, not only the audit view."""
    return (
        f"! sighting: {escape(event.workspace_id)}/{escape(event.name)}"
        f" → {escape(event.host or '?')}"
    )


def flash_safe(text: str) -> str:
    """Escape free text for a status-line flash. ``escape`` covers
    complete markup tags but passes a truncated closing tag through
    bare (``unknown workspace [/dev``) — and the status line's
    Static parses markup at update time, so a bare ``[/`` still
    raises. The second pass backslash-escapes that prefix too,
    leaving already-escaped tags untouched. Newlines collapse to
    spaces first: the status lines are one row tall, and a message
    cut between two of its own lines would crop with no ellipsis
    marking the cut."""
    flat = " ".join(text.splitlines())
    return re.sub(r"(?<!\\)\[/", r"\\\[/", escape(flat))


def effective_allows(rules: EgressRules | None) -> bool:
    """Whether anything effectively allows egress under the
    snapshot (#280): a non-empty allowlist or an in-effect allowed
    verdict — the condition the static switch's confirmation
    gates on."""
    if rules is None:
        return False
    return bool(rules.allow_list) or bool(rules.allowed)


#: The empty-static confirmation's question (#280) — one text for
#: every surface that asks it: the decider app's picker, and the
#: workspace page's (#344). It names the posture (every name
#: answers NXDOMAIN — an offline workspace) and the escape
#: (switching anyway).
EMPTY_STATIC_QUESTION = (
    "static with nothing allowed answers every name NXDOMAIN "
    "— an offline workspace. Switch anyway?"
)


async def switch_mode_path(mode, rules, confirm, send) -> None:
    """The mode switch's shared path (#280, #344) — one logic for
    the decider app and the workspace page, each host supplying
    its own asker and sender: a cancel decides nothing, ``static``
    with nothing effectively allowed asks first (the host pushes
    its own confirmation), and every other pick goes straight
    through ``send``."""
    if mode is None:
        return
    if mode == "static" and not effective_allows(rules):
        confirm(lambda answer: confirmed_static_switch(answer, send))
        return
    await send(mode)


async def confirmed_static_switch(answer: bool, send) -> None:
    """The empty-static confirmation's answer (#280): a yes sends
    the confirmed static switch through ``send``, a no decides
    nothing."""
    if answer:
        await send("static", confirm_empty=True)


class PickerScreen(ModalScreen[str | None]):
    """One pick from a short, static list: Enter picks, Escape or q
    cancels, and the current choice starts highlighted. The chosen
    string (or None) goes to the callback given at construction.
    The mode picker (#280), the duration picker, and the audit
    view's filters (#390) all ride this one shape."""

    BINDINGS = [
        Binding("q", "cancel", show=False),
        Binding("escape", "cancel", "Cancel", show=False),
    ]

    def __init__(self, options, current, picked) -> None:
        super().__init__()
        self.options = tuple(options)
        self.current = current
        self.picked = picked

    def compose(self) -> ComposeResult:
        yield OptionList(*self.options, id="pick-options")

    def on_mount(self) -> None:
        options = self.query_one("#pick-options", OptionList)
        options.focus()
        if self.current in self.options:
            options.highlighted = list(self.options).index(self.current)

    def on_option_list_option_selected(
        self, event: OptionList.OptionSelected
    ) -> None:
        self.dismiss_with(str(event.option.prompt))

    def action_cancel(self) -> None:
        self.dismiss_with(None)

    def dismiss_with(self, pick: str | None) -> None:
        """Dismiss and hand the pick to the callback (async — the
        exchange runs as a task, so the modal closes without
        waiting on it)."""
        self.dismiss()
        # Referenced: an unreferenced task can be collected mid-await.
        self._pick_task = asyncio.create_task(self.picked(pick))


class ModeScreen(PickerScreen):
    """The mode picker (#280): Enter picks, Escape or q cancels.
    The chosen mode (or None) goes to the callback given at
    construction."""

    def __init__(self, current: str, picked) -> None:
        super().__init__(EGRESS_MODES, current, picked)


class ConfirmScreen(ModalScreen[bool]):
    """A yes/no question (#280): ``y``/Enter answers True, ``n``/
    ``q``/Escape answers False. The callback given at construction
    runs as a task with the answer."""

    BINDINGS = [
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


class DurationScreen(PickerScreen):
    """The duration picker: Enter picks, Escape or q cancels. The
    chosen duration (or None) goes to the callback given at
    construction. The verdict picker rides the consent durations
    with ``tilrestart`` highlighted; the secrets page's renew
    picker (#390) rides TTL-appropriate choices with its own
    default."""

    def __init__(
        self,
        picked,
        choices: tuple[str, ...] = DURATIONS,
        default: str = DURATION_DEFAULT,
    ) -> None:
        super().__init__(choices, default, picked)


class RulesScreen(Screen):
    """The in-effect verdicts: allow/deny rows with durations and
    countdowns, the static allowlist, and revoke on the focused row.

    Arrows move the rule list; ``x`` revokes the focused rule, ``r``
    or Escape returns to the surface below — no focus trap anywhere.
    The per-tick countdown refresh repaints an unchanged row set in
    place; only a membership change swaps the list (the swap was
    the once-a-second flash #301 reports)."""

    BINDINGS = [
        Binding("x", "revoke", "Revoke"),
        Binding("m", "mode", "Mode"),
        Binding("r", "back", "Back"),
        Binding("escape", "back", "Back", show=False),
        Binding("q", "back", "Back", show=False),
    ]

    def __init__(self, controller: ConsentController, revoke, mode) -> None:
        super().__init__()
        self.controller = controller
        self.revoke = revoke
        self.mode = mode
        self.rebuilds = OneFlight(
            lambda: self.rebuild_rows(),
            lambda: self.app.is_running,
            "rules",
        )

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
        self.rebuilds.request()

    async def rebuild_rows(self) -> None:
        """Repaint from the controller's rules snapshot. An
        unchanged row set (same ids, same order) repaints the
        survivors' countdowns in place — the per-tick refresh must
        not swap the whole list, the once-a-second flash #301
        reports. A membership change builds a fresh list,
        preserving the focused rule by id (the top when it left): a
        mutating ListView carries asynchronously-pruned stale
        children that shift indexes, so positions must come from
        children that are all real — `x` must never retarget
        through a shifted index."""
        rules = self.controller.rules
        ordered = rule_rows(rules)
        self.query_one("#allowlist", Static).update(allowlist_text(rules))
        body = self.query_one("#rules-body", Vertical)
        old = None
        try:
            old = self.query_one("#rule-rows", ListView)
        except NoMatches:
            pass  # a died-mid-swap rebuild: mount the fresh list anew
        if old is not None and row_rule_ids(old) == [
            rule.id for rule in ordered
        ]:
            self.repaint_rule_rows(old, ordered)
            return
        await self.swap_rule_rows(body, old, ordered)

    async def swap_rule_rows(
        self, body: Vertical, old: ListView | None, ordered: list
    ) -> None:
        """Swap in a freshly-built list (its mount awaited),
        preserving the focused rule by id (the top when it left): a
        mutating ListView carries asynchronously-pruned stale
        children that shift indexes, so positions must come from
        children that are all real — `x` must never retarget
        through a shifted index."""
        focused = focused_rule_id(old)
        items = []
        for rule in ordered:
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

    def repaint_rule_rows(self, rows: ListView, ordered: list) -> None:
        """Repaint each surviving rule row's text in place (the
        queue's per-tick countdown repaint, carried to the rules
        rows): the caller's order-equal membership match proves the
        children and ``ordered`` line up positionally, so the pass
        walks the two side by side (never id-keyed — a malformed
        frame with duplicate ids would repaint one row twice and
        leave its twin stale), moves no index, and takes no
        focus."""
        for child, rule in zip(rows.children, ordered):
            child.query_one(Static).update(
                rule_line(rule, self.controller.rule_remaining(rule))
            )

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
        """Open the mode picker through the host's callback (#301
        kept the key beside the rules rows); the host owns which
        picker path runs."""
        self.mode()

    def action_back(self) -> None:
        self.app.pop_screen()


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
    argument) — built on the shared context, not a fresh one. The
    token rides the handshake's Authorization header (#216)."""
    return {
        "uri": events_url(url),
        "additional_headers": auth_headers(token),
        "ssl": None if url.startswith("http://") else ssl_ctx,
        "max_size": 2**22,
    }


def default_ws_factory():
    """The events websocket connection (the decider's stream)."""
    return websockets.connect(
        **ws_connect_kwargs(env_url(), env_token(), shared_ssl())
    )
