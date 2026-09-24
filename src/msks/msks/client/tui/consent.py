"""The consent decider's protocol state (pure, no Textual — #195).

Owns the frame parsing, the pending-hold map, and the countdown math
for the decider TUI (:mod:`msks.client.tui.consent_app`), so the
protocol logic is unit-testable without the harness. Ported from
klangk's ``ConsentDeciderController`` with msks's shapes: frames ride
the events-websocket envelope (``{"event": …, "data": {…}}``),
destinations carry a port with 0 meaning all ports (a non-TCP/UDP
flow), and rows have no process identity (msks sees flows at the
kernel, not processes inside the guest).

The clock defaults to :func:`time.time` because the daemon stamps
``requested_at``/``decided_at`` in epoch wall-clock; the countdowns
are only meaningful when both timestamps share that domain.
"""

import json
import time
from dataclasses import dataclass

# Frame-application outcomes (what apply_frame tells the view).
ADDED = "added"  # a held request arrived; payload = ConsentRequest
RESOLVED = "resolved"  # a request left; payload = (request_id, decision)
RULES = "rules"  # refreshed in-effect verdicts; payload = EgressRules
REJECTED = "rejected"  # the daemon refused the registration; payload = reason
IGNORED = "ignore"  # malformed / unknown frame; state untouched

#: The durations a decider can pick, display order; the default is
#: ``tilrestart`` (mirrors the daemon's tokens — the client duplicates
#: them per the CLI-isolation rule).
DURATIONS = ("once", "5m", "15m", "tilrestart", "forever")
DURATION_DEFAULT = "tilrestart"

#: The egress modes a mode switch offers (#280), display order —
#: the same CLI-isolation duplication as the durations.
EGRESS_MODES = ("allow", "static", "interactive")

#: Timed durations in seconds (a mirror of the daemon's table);
#: ``once`` is consumed by its connection and ``tilrestart`` /
#: ``forever`` have no fixed expiry, so none of those countdown.
DURATION_SECONDS = {"5m": 300, "15m": 900}


def fmt_duration(secs: float) -> str:
    """A compact remaining-time label: ``45s``, ``5m``, ``2h``, ``3d``."""
    whole = int(secs)
    if whole < 60:
        return f"{whole}s"
    if whole < 3600:
        return f"{whole // 60}m"
    if whole < 86400:
        return f"{whole // 3600}h"
    return f"{whole // 86400}d"


@dataclass(frozen=True, slots=True)
class ConsentRequest:
    """One held request awaiting a verdict."""

    id: str
    workspace_id: str
    dest_host: str
    dest_port: int
    requested_at: float
    expires_at: float | None = None  # the frame's honest deadline


@dataclass(frozen=True, slots=True)
class ConsentRule:
    """One in-effect verdict for the rules view."""

    id: str
    dest_host: str
    dest_port: int
    decision: str
    duration: str | None
    decided_at: float | None
    decided_by: str | None


@dataclass(frozen=True, slots=True)
class EgressRules:
    """A parsed ``egress.rules`` frame: the workspace's posture."""

    workspace_id: str
    mode: str
    allow_list: tuple[str, ...]
    allowed: tuple[ConsentRule, ...]
    denied: tuple[ConsentRule, ...]


def parse_request(obj: object) -> ConsentRequest | None:
    """One frame's request object, or None on an unusable shape."""
    fields = string_pair(obj, "id", "workspace_id")
    if fields is None:
        return None
    rid, wid = fields
    return ConsentRequest(
        id=rid,
        workspace_id=wid,
        dest_host=str(obj.get("dest_host") or ""),
        dest_port=port_field(obj.get("dest_port")),
        requested_at=numeric_field(obj.get("requested_at")) or 0.0,
        expires_at=numeric_field(obj.get("expires_at")),
    )


def string_pair(
    obj: object, first: str, second: str
) -> tuple[str, str] | None:
    """A dict's two string fields, or None when either side is
    missing or not a string."""
    if not isinstance(obj, dict):
        return None
    left = obj.get(first)
    right = obj.get(second)
    if not isinstance(left, str) or not isinstance(right, str):
        return None
    return left, right


def parse_rules(msg: dict) -> EgressRules | None:
    """A rules frame's data object, or None when it names no
    workspace; malformed members degrade to empty rather than
    dropping the frame."""
    wid = msg.get("workspace_id")
    if not isinstance(wid, str):
        return None
    allow_list = msg.get("allow_list")
    return EgressRules(
        workspace_id=wid,
        mode=str(msg.get("mode") or ""),
        allow_list=(
            tuple(str(entry) for entry in allow_list)
            if isinstance(allow_list, list)
            else ()
        ),
        allowed=parse_rule_rows(msg.get("allowed")),
        denied=parse_rule_rows(msg.get("denied")),
    )


def parse_rule_rows(raw: object) -> tuple[ConsentRule, ...]:
    """One rules frame's rows, skipping what fails to parse."""
    if not isinstance(raw, list):
        return ()
    rows = [rule for rule in (parse_rule(o) for o in raw) if rule]
    return tuple(rows)


def parse_rule(obj: object) -> ConsentRule | None:
    """One rule row, or None on an unusable shape."""
    if not isinstance(obj, dict):
        return None
    return ConsentRule(
        id=str(obj.get("id") or ""),
        dest_host=str(obj.get("dest_host") or ""),
        dest_port=port_field(obj.get("dest_port")),
        decision=str(obj.get("decision") or ""),
        duration=duration_field(obj.get("duration")),
        decided_at=numeric_field(obj.get("decided_at")),
        decided_by=text_or_none(obj.get("decided_by")),
    )


def duration_field(value: object) -> str | None:
    """A rule's duration as a token, or None for anything else."""
    return value if isinstance(value, str) else None


def text_or_none(value: object) -> str | None:
    """A frame field as text, or None for anything else."""
    return value if isinstance(value, str) else None


def port_field(value: object) -> int:
    """A destination port as int (0 = all ports); bools and junk
    read as 0."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0
    return int(value)


def numeric_field(value: object) -> float | None:
    """A frame field as float, with bools and junk excluded."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


class ConsentController:
    """The decider's state machine over the events frames (#195)."""

    def __init__(
        self,
        hold_timeout: float = 120.0,
        *,
        clock=time.time,
        workspace_id: str = "",
    ) -> None:
        self.hold_timeout = hold_timeout
        self._clock = clock
        # Frames are per-workspace on one shared hub (#280 review):
        # a foreign workspace's request or rules frame must not
        # plant a ghost hold or repaint this decider's snapshot.
        # Empty accepts every frame — the protocol-level default
        # the pure tests run under.
        self.workspace_id = workspace_id
        self.pending: dict[str, ConsentRequest] = {}
        self.rules: EgressRules | None = None

    #: event name -> data applier (apply_frame dispatches through it)
    APPLIERS = {
        "egress.request": "apply_request",
        "egress.resolved": "apply_resolved",
        "egress.rules": "apply_rules",
        "egress.decider_rejected": "apply_rejection",
    }

    def apply_frame(self, raw: str) -> tuple[str, object]:
        """Parse and apply one inbound frame; ``(outcome, payload)``
        (see the outcome constants). Malformed input is ignored with
        state untouched."""
        msg = decode_frame(raw)
        if msg is None:
            return IGNORED, None
        applier = self.APPLIERS.get(msg.get("event"))
        if applier is None:
            return IGNORED, None
        return getattr(self, applier)(msg.get("data"))

    def apply_rejection(self, data: object) -> tuple[str, object]:
        """One ``egress.decider_rejected`` frame's data: the reason
        the daemon refused the registration."""
        reason = data.get("reason") if isinstance(data, dict) else None
        return REJECTED, reason if isinstance(reason, str) else None

    def apply_request(self, data: object) -> tuple[str, object]:
        """One ``egress.request`` frame's data: add the hold (a
        foreign workspace's frame is ignored — the decider decided
        for one workspace, #280 review)."""
        request = parse_request(
            data.get("request") if isinstance(data, dict) else None
        )
        if request is None or not self.owns(request.workspace_id):
            return IGNORED, None
        self.pending[request.id] = request
        return ADDED, request

    def apply_resolved(self, data: object) -> tuple[str, object]:
        """One ``egress.resolved`` frame's data: drop the hold."""
        rid = data.get("request_id") if isinstance(data, dict) else None
        if isinstance(rid, str):
            self.pending.pop(rid, None)
        return RESOLVED, (
            rid if isinstance(rid, str) else None,
            data.get("decision") if isinstance(data, dict) else None,
        )

    def apply_rules(self, data: object) -> tuple[str, object]:
        """One ``egress.rules`` frame's data: replace the snapshot
        (a foreign workspace's frame is ignored)."""
        rules = parse_rules(data) if isinstance(data, dict) else None
        if rules is None or not self.owns(rules.workspace_id):
            return IGNORED, None
        self.rules = rules
        return RULES, rules

    def ordered(self) -> list[ConsentRequest]:
        """Pending requests oldest-first (stable UI order)."""
        return sorted(
            self.pending.values(), key=lambda request: request.requested_at
        )

    def owns(self, workspace_id: str) -> bool:
        """Whether a frame's workspace is this controller's (an
        empty own-id accepts every frame)."""
        return not self.workspace_id or workspace_id == self.workspace_id

    def remaining(self, request: ConsentRequest) -> float:
        """Seconds until this hold's timeout expires to deny
        (clamped at 0). The frame's ``expires_at`` (the daemon's
        settings-driven deadline) wins when present; the local
        ``hold_timeout`` is only the fallback for frames without
        one."""
        deadline = request.expires_at
        if deadline is None:
            deadline = request.requested_at + self.hold_timeout
        return max(0.0, deadline - self._clock())

    def rule_remaining(self, rule: ConsentRule) -> float | None:
        """Seconds left on a timed verdict, or None when it has no
        countdown (``tilrestart``/``forever``/unknown)."""
        if rule.decided_at is None:
            return None
        secs = DURATION_SECONDS.get(rule.duration)
        if secs is None:
            return None
        return max(0.0, rule.decided_at + secs - self._clock())

    def reset(self) -> None:
        """Drop all pending holds and the cached rules snapshot: the
        registration handshake re-sends both, and rows that resolved
        while disconnected must not linger as ghosts."""
        self.pending.clear()
        self.rules = None


def decode_frame(raw: str) -> dict | None:
    """One inbound frame as a dict, or None for non-JSON, non-dict,
    or non-object input."""
    try:
        msg = json.loads(raw)
    except ValueError, TypeError:
        return None
    return msg if isinstance(msg, dict) else None
