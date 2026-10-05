"""The consent UI's shared pieces (#195, #358): the view over a
:class:`ConsentController` that every consent surface in the tree
renders from.

The workspace page holds the decider link and its controller; the
egress consent page — below in this module, pushed over the
workspace page — is the tree's only deciding surface: the
held-request queue and the in-effect verdicts on one full-screen
visit (#454), merged from the consent overlay and its rules
screen. The placeholder audit lives in the tree's secrets page
(#390): its daemon-wide view sits in ``main_app`` beside the page
that opens it, rendering through this module's row helpers.

This module keeps everything those surfaces share — the row and
focus helpers, the reconnect ladder's constants, the pickers (mode,
duration, and the generic one the audit view's filters take), the
edge-walking list the consent page's two zones cross between, the
consent page itself (holds, verdicts, revoke, mode), and the audit
row rendering — plus the connection seams the page's
:class:`~msks.client.tui.link.DeciderLink` dials through
(``default_ws_factory``, the one shared TLS context) and the
failure panel a refused create or mint opens (#426). The protocol
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
from textual.containers import Horizontal, Vertical
from textual.content import Content
from textual.css.query import NoMatches
from textual.screen import ModalScreen, Screen
from textual.widgets import (
    Button,
    Footer,
    ListItem,
    ListView,
    OptionList,
    Static,
)

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
from .rows import header_meta, header_name

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

#: The registration-refusal and unusable-token states' labels — the
#: same strings :mod:`msks.client.tui.link` names its states by (the
#: link owns the connection; the surfaces render the labels, and an
#: import between the two modules would cycle).
REJECTED = "rejected"
UNUSABLE_TOKEN = "unusable token"

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


def focused_attr(rows: ListView | None, attr: str):
    """The focused row's ``attr`` value, or None when nothing is
    focused (a None rows is a rebuild's swap window) — the tree's
    listing rebuild rule, carried beside the consent queue's own
    focus helpers."""
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


class FlashLine:
    """A message that owns a status line until its TTL lapses —
    the consent app's flash, lifted one level (#309) so every
    screen's status line shows it: the tree app's standing line
    and the pushed pages' own lines."""

    def __init__(self) -> None:
        self.msg = ""
        self.until = 0.0

    def set(self, message: str) -> None:
        """Give the status line to a message for FLASH_TTL
        seconds."""
        self.msg = message
        self.until = time.time() + FLASH_TTL

    def text(self, default: str | Content) -> str | Content:
        """The flash while it lives, else ``default``."""
        if self.until > time.time():
            return self.msg
        return default


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


def hold_flash(request) -> str:
    """The consent-line flash a first-seen hold takes (#454): the
    destination with the key in — the page pushes nothing on a
    hold's arrival, so this line is the attention the hold gets
    (the header's count beside it), the sighting flash's shape.
    The host rides through ``flash_safe``: the consent line parses
    markup at update time, and a truncated closing tag in a
    destination would raise there (#318's rule).
    """
    port = (
        " (all ports)" if request.dest_port == 0 else f":{request.dest_port}"
    )
    return f"egress to decide: {flash_safe(request.dest_host)}{port} — press e"


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


def panel_safe(text: str) -> str:
    """Escape free text for a panel's body, verbatim on screen:
    the truncated-tag second pass emits Textual's own single
    backslash, so a refusal echoing a bracket renders it with no
    stray backslash beside it — where the status line's own pass
    doubles it (a one-row surface whose wart predates the panel).
    The line breaks stay: a panel wraps its lines, so a refusal's
    own breaks ride through instead of collapsing to spaces."""
    return re.sub(r"(?<!\\)\[/", r"\\[/", escape(text))


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
        # A current that names no option leaves nothing highlighted:
        # the widget pre-highlights its first row at construction,
        # and a bare Enter must not take a pre-selected choice the
        # operator never made — the mode picker's no-rules window
        # would otherwise pre-select ``allow`` (#465 review).
        options.highlighted = (
            list(self.options).index(self.current)
            if self.current in self.options
            else None
        )

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


class FailurePanel(ModalScreen):
    """A failed create or mint (#426): the daemon's refusal in a
    panel that waits for dismissal — Enter, Escape, ``q``, or the
    Close button — where a flash would drop the detail after
    five seconds and a form note would squeeze it into one line.
    The title names the verb and the identity the form submitted;
    the body carries the daemon's detail verbatim, wrapping at
    the panel's edge."""

    BINDINGS = [
        Binding("enter", "close", "Close", show=False),
        Binding("q", "close", "Close"),
        Binding("escape", "close", "Close", show=False),
    ]

    def __init__(self, verb: str, identity: str | None, detail: str) -> None:
        super().__init__()
        self.verb = verb
        self.identity = identity
        self.detail = detail

    def heading(self) -> str:
        """The panel's title line: the verb that failed and the
        identity the form submitted (a store check names no
        identity — its refusal belongs to no one body), escaped
        for the markup parse the way the body is — a name
        carrying a truncated closing tag renders literally, not
        crashes the panel. A method, not an attribute — a
        Screen's own ``title`` starts as None and would shadow
        one."""
        if self.identity is None:
            return f"{self.verb} failed"
        return f"{self.verb} failed: {panel_safe(self.identity)}"

    def compose(self) -> ComposeResult:
        with Vertical(id="failure-panel"):
            yield Static(self.heading(), id="failure-title")
            yield Static(panel_safe(self.detail), id="failure-detail")
            with Horizontal(id="failure-buttons"):
                yield Button(
                    "Close", id="do-close", variant="primary", compact=True
                )
        yield Footer()

    def on_mount(self) -> None:
        self.query_one("#do-close", Button).focus()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.action_close()

    def action_close(self) -> None:
        """Dismiss — the panel has no timeout; it leaves when the
        operator closes it."""
        self.dismiss()


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


class EdgeListView(ListView):
    """A :class:`~textual.widgets.ListView` whose arrow keys leave
    the list at its edges (#454): the consent page stacks two
    lists, and the arrows alone must walk both in reading order —
    the spatial-navigation rule, the mint form's picker-edge
    pattern carried to the page's lists. The interior rows take
    the stock cursor walk; an edge hands the walk to the callback
    given at construction (nothing happens at an edge with no
    handoff — the walk just stops, as it does at the page's own
    top and bottom)."""

    BINDINGS = [
        Binding("up", "edge_previous", show=False),
        Binding("down", "edge_next", show=False),
    ]

    def __init__(self, *children, leave_up=None, leave_down=None, **kwargs):
        super().__init__(*children, **kwargs)
        self.leave_up = leave_up
        self.leave_down = leave_down

    def at_top(self) -> bool:
        """Whether the walk leaves upward from here: no rows,
        nothing highlighted, or the first row highlighted."""
        return not self.children or self.index in (None, 0)

    def at_bottom(self) -> bool:
        """Whether the walk leaves downward from here: no rows,
        nothing highlighted, or the last row highlighted."""
        return not self.children or self.index == len(self.children) - 1

    def action_edge_previous(self) -> None:
        """Up: the interior walks the rows, the top edge hands the
        walk to the surface above."""
        if self.at_top():
            if self.leave_up is not None:
                self.leave_up()
        else:
            self.action_cursor_up()

    def action_edge_next(self) -> None:
        """Down: the interior walks the rows, the bottom edge
        hands the walk to the surface below."""
        if self.at_bottom():
            if self.leave_down is not None:
                self.leave_down()
        else:
            self.action_cursor_down()


class ConsentPage(Screen):
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
    nothing. ``x`` revokes the focused verdict and acts on
    nothing in the holds zone; ``m`` opens the host's mode
    picker; ``q`` or Escape returns to the workspace page — holds
    keep waiting, the header's count keeps naming them. Enter
    carries no verdict: the zones are ListViews, and a stray
    Enter aimed at the page beneath must not decide anything;
    only an explicit letter decides.

    The page owns its per-second repaint: an unchanged row set
    repaints its countdowns in place; only a membership change
    swaps a list (the swap was the once-a-second flash #301
    reports)."""

    #: The page sets its own initial focus (``started``: the holds
    #: zone when a hold waits, the verdicts otherwise) once the
    #: compose has settled — Textual's auto-focus would grab the
    #: first focusable (always the holds list) before that decision
    #: runs, and the holds zone's first swap would then faithfully
    #: restore the wrong zone. The empty string (not None — None
    #: inherits the app's setting) matches no widget (#454).
    AUTO_FOCUS = ""

    BINDINGS = [
        Binding("a", "allow", "Allow"),
        Binding("A", "allow_duration", "Allow…"),
        Binding("d", "deny", "Deny"),
        Binding("D", "deny_duration", "Deny…"),
        Binding("x", "revoke", "Revoke"),
        Binding("m", "mode", "Mode"),
        Binding("q", "back", "Back"),
        Binding("escape", "back", "Back", show=False),
    ]

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
            header_meta(self.host.row, self.app.theme_variables),
            id="header-meta",
        )
        with Vertical(id="consent-page"):
            yield Static(id="allowlist")
            yield Static(id="consent-status")
            with Vertical(id="holds-zone"):
                yield Static("held requests", id="holds-label")
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
        """Repaint each survivor's countdown in place."""
        existing = row_map(rows)
        for request in ordered:
            item = existing.get(request.id)
            if item is not None:
                item.query_one(Static).update(
                    dest_line(request, self.controller.remaining(request))
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
        self.sync_empty(ordered)

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
        if held_focus:
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
        """Repaint each surviving rule row's text in place: the
        caller's order-equal membership match proves the children
        and ``ordered`` line up positionally, so the pass walks the
        two side by side (never id-keyed — a malformed frame with
        duplicate ids would repaint one row twice and leave its
        twin stale), moves no index, and takes no focus."""
        for child, rule in zip(rows.children, ordered):
            child.query_one(Static).update(
                rule_line(rule, self.controller.rule_remaining(rule))
            )

    # -- the status line ---------------------------------------------------

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
                header_meta(self.host.row, self.app.theme_variables)
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
        self.query_one("#consent-status", Static).update(
            self.flash_line.text(default)
        )

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

    async def action_deny(self) -> None:
        if self.holds_zone_focused():
            await self.decide_focused("deny", DURATION_DEFAULT)

    async def action_allow_duration(self) -> None:
        if self.holds_zone_focused():
            await self.pick_duration("allow")

    async def action_deny_duration(self) -> None:
        if self.holds_zone_focused():
            await self.pick_duration("deny")

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
        """Enter on a row decides nothing: both zones are
        ListViews, and Enter fires their selection — a stray Enter
        (the operator aimed at the workspace page's action list
        when the hold's flash landed) must never become a verdict.
        Only an explicit letter decides."""

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
