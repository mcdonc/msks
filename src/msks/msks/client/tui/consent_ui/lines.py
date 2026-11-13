"""Rule and destination line rendering.

The one-line shapes the rules list draws: destination, duration,
verdict colors, and the allowlist summary line.
"""

from __future__ import annotations

from rich.markup import escape
from textual.content import Content, Span

from ..consent import (
    ConsentRequest,
    EgressRules,
    fmt_duration,
)
from ..rows import clip

#: A held request's countdown turns urgent below this many
#: seconds (#470 C7): a hold that lapses fails closed, so the
#: deadline reads in the warning color while it is still a
#: decision away.
HOLD_WARNING_S = 10.0


def dest_line(
    request: ConsentRequest, remaining: float, focused: bool = False
) -> Content:
    """One held request's row (#454, #470 C7/S3): destination,
    port (or all ports), and the hold's countdown — the countdown
    in the warning color under :data:`HOLD_WARNING_S` (the hold
    that lapses fails closed), the destination bold on the row
    the keys act on, the focus cue a weak theme's highlight bar
    cannot carry."""
    host = escape(request.dest_host)
    port = (
        " (all ports)" if request.dest_port == 0 else (f":{request.dest_port}")
    )
    text = f"{host}{port}  ({int(remaining)}s)"
    spans = []
    if focused:
        spans.append(Span(0, len(host) + len(port), "$text bold"))
    if remaining < HOLD_WARNING_S:
        spans.append(Span(text.rindex("("), len(text), "$warning"))
    return Content(text, spans)


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


#: The verdict word's colors (#470 C5): the decision earns its
#: leading cell — allowed in the success color, denied in the
#: error color.
VERDICT_COLORS = {"allowed": "$success", "denied": "$error"}

#: A verdict row's host budget (#470 C5): a long FQDN clips at
#: its middle — the row keeps one line and the duration stays
#: readable at the terminal's edge.
RULE_HOST_W = 40


def rule_line(rule, remaining: float | None, focused: bool = False) -> Content:
    """One in-effect verdict's row (#470 C5/S3): the decision word
    in its verdict color, the host clipped to its budget (a long
    FQDN keeps the row to one line), the duration label behind
    it, and the host bold on the row ``x`` acts on."""
    host = clip(escape(rule.dest_host), RULE_HOST_W)
    port = " (all ports)" if rule.dest_port == 0 else f":{rule.dest_port}"
    label = duration_label(rule, remaining)
    text = f"{rule.decision:<7s} {host}{port}  {label}"
    spans = [
        Span(0, len(rule.decision), VERDICT_COLORS.get(rule.decision, "$text"))
    ]
    if focused:
        # The host sits past the decision's padded column — a
        # decision word the daemon grows past seven cells pushes
        # it right, so the span reads the offset it finds, not a
        # constant.
        host_at = max(len(rule.decision), 7) + 1
        spans.append(
            Span(host_at, host_at + len(host) + len(port), "$text bold")
        )
    return Content(text, spans)


def allowlist_text(rules: EgressRules | None) -> str:
    """The consent page's allowlist line (#470 C1): the static
    allowlist alone — the status line beneath names the mode, so
    the word appears once (entries escaped — operator-supplied
    strings render literally)."""
    if rules is None:
        return "no rules snapshot yet"
    entries = (
        ", ".join(escape(entry) for entry in rules.allow_list)
        if rules.allow_list
        else "(empty)"
    )
    return f"allowlist: {entries}"


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
