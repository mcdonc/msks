"""Secret-event rendering.

The time, destination, and line shapes the events list draws, in
the order it draws them.
"""

from __future__ import annotations

import time

from rich.markup import escape
from textual.widgets import ListItem, Static

from ..consent import SecretEvent


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
        "revokes, expiries, and unlocks, then wire events as they "
        "happen. "
        "! marks a sighting: a placeholder token reached a host its "
        "mint did not allow — the exfiltration signal. Wire events "
        "cover decrypted connections only; a connection the daemon "
        "relays untouched passes unread and produces no row."
    )
