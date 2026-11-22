"""The events frame's shared vocabulary (#1019 over the
import-graph fan-in ceiling): the wire decoder and the
frame-family predicates both clients read — the TUI's listing
and workspace page, the GUI's listing and workspace page —
split out of :mod:`msks.client.tui.consent` (the decoder) and
the TUI's own screens (the predicates) when the listing's and
proxies page's imports pushed the consent module's importers
over the ceiling. The module owns pure frame math only: no
controller state, no Qt, no Textual — the same shape the REST
seam (:mod:`msks.client.data`) gives both clients.
"""

import json


def decode_frame(raw: str) -> dict | None:
    """One inbound frame as a dict, or None for non-JSON, non-dict,
    or non-object input."""
    try:
        msg = json.loads(raw)
    except ValueError, TypeError:
        return None
    return msg if isinstance(msg, dict) else None


def listing_frame(msg: dict) -> bool:
    """Whether one decoded frame moves the workspaces listing
    (#1019): a lifecycle transition — the ``workspace.`` family
    the routes and the watcher publish (created, removed,
    status, mounts, resized) — a volume move the storage strip
    follows (``home.imported``, ``home.exported``), a catalog
    move the IMAGE column follows (``image.changed``), or a
    mode switch (the ``egress.rules`` frame the switch lands).
    Every other family — the proxies, the secrets, the holds —
    paints through its own surface, and the listing re-reads
    nothing for it."""
    event = str(msg.get("event") or "")
    return event.startswith(("workspace.", "home.", "image.")) or (
        event == "egress.rules"
    )


def row_frame(msg: dict, workspace_id: str) -> bool:
    """Whether one decoded frame moves this workspace's row
    (#1019): a lifecycle transition — the ``workspace.`` family
    the routes and the watcher publish (created, removed,
    status, mounts, resized) — naming the workspace under the
    naming conventions the stream's families use (``id``, and
    the scalar ``workspace_id`` the consent family uses; no
    lifecycle frame carries it today, the matcher keeps it so a
    future frame that does admits unchanged); a volume move the
    row's facts follow (``home.imported``, ``home.exported``);
    a mode switch (``egress.rules``, #1042) — the row's posture
    word follows the switch, the footer the GUI page renders
    and the table the TUI's does; or a catalog move
    (``image.changed``) — the frame names no workspace because
    it moves every row's image cell. Frames for other
    workspaces and the rest of the egress family (a hold's
    request and resolution) decide nothing."""
    event = str(msg.get("event") or "")
    if event.startswith("image."):
        return True
    return named_row_frame(msg, event, workspace_id)


def named_row_frame(msg: dict, event: str, workspace_id: str) -> bool:
    """The named half of :func:`row_frame`: the lifecycle and
    volume families, and a mode switch's ``egress.rules`` frame
    (#1042), matched against the workspace the frames name."""
    data = msg.get("data") or {}
    named = (data.get("id"), data.get("workspace_id"))
    if event == "egress.rules":
        return workspace_id in named
    return event.startswith(("workspace.", "home.")) and workspace_id in named
