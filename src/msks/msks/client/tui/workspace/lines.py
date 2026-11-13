"""The workspace page's consent status lines.

The grant summary, the next-expiry read, and the consent line
the workspace page and the header share.
"""

from __future__ import annotations

from rich.markup import escape
from textual.content import Content

from ..consent_ui import fmt_duration
from ..consent_ui.lines import duration_label
from ..detail import status_line
from ..link import CONNECTED, REJECTED, UNUSABLE_TOKEN


def grant_text(rule, remaining: float | None) -> str:
    """One granted scope with its expiry: host, port (or all
    ports), and the duration label."""
    port = " (all ports)" if rule.dest_port == 0 else f":{rule.dest_port}"
    label = duration_label(rule, remaining)
    return f"{escape(rule.dest_host)}{port} ({label})"


def next_expiry(controller, rules) -> float | None:
    """The grant stack's nearest countdown, or None while no
    grant carries one (open-ended verdicts alone)."""
    timed = [
        remaining
        for remaining in (
            controller.rule_remaining(rule) for rule in rules.allowed
        )
        if remaining is not None
    ]
    return min(timed, default=None)


def granted_line(controller) -> str:
    """The granted scopes for the consent status line (#366): one
    grant names itself — host, port, expiry; two or more collapse
    to the count with the nearest expiry (``3 grants · next
    expires 4h``), so a workspace with several grants keeps the
    line readable at 80 columns instead of running it to the
    terminal's edge — every grant stays spelled out on the egress
    consent page (``e`` from the workspace page). A
    stack with no
    countdown among its grants (open-ended verdicts alone)
    carries the count alone. The honest absence when nothing is
    in effect."""
    rules = controller.rules
    if rules is None or not rules.allowed:
        return "no active consent"
    if len(rules.allowed) == 1:
        return grant_text(
            rules.allowed[0], controller.rule_remaining(rules.allowed[0])
        )
    summary = f"{len(rules.allowed)} grants"
    expiry = next_expiry(controller, rules)
    if expiry is not None:
        summary += f" · next expires {fmt_duration(expiry)}"
    # The count names its own affordance (#470 W3): the summary is
    # the line an operator reads as inert data, so it carries the
    # key the hold flashes already name.
    summary += " · e to review"
    return summary


def consent_line(
    link, row: dict, theme_variables: dict | None = None
) -> Content:
    """The workspace page's consent status line (#309, #752): the
    status lines' one grammar — the ``consent`` label muted in
    the label column, the granted scopes — one named, several
    counted (#366) — or the honest absence, joined with a ``·``
    behind the mode; the row's recorded mode stands in until the
    first rules frame lands. The state is named whenever it is
    not connected: a drop never implies that silence is data (the
    controller keeps its last snapshot through the backoff
    ladder, so the line says so beside it)."""
    if link.state in (REJECTED, UNUSABLE_TOKEN):
        # The daemon's refusal text is free text, and the value
        # rides a Content's plain text (#318's rule needs no
        # escaping here): a truncated closing tag in the reason
        # parses clean at update time — the timer's repaint
        # included.
        return status_line("consent", link.reject_reason, theme_variables)
    rules = link.controller.rules
    if rules is None:
        mode = row.get("egress_mode") or "-"
        value = f"mode {mode} · {link.state}"
        return status_line("consent", value, theme_variables)
    value = f"mode {rules.mode} · {granted_line(link.controller)}"
    if link.state != CONNECTED:
        value += f" · {link.state}"
    return status_line("consent", value, theme_variables)
