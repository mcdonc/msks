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

from ..consent import fmt_duration
from . import events, flash, focus, lines, modes, page, screens, wire
from .events import (
    event_dest,
    event_id_suffix,
    event_item,
    event_line,
    event_time,
    events_note,
    render_order,
)
from .flash import (
    FLASH_TTL,
    FlashLine,
    flash_safe,
    hold_flash,
    panel_safe,
    sighting_flash,
)
from .focus import (
    ensure_focus,
    focus_attr,
    focus_by_id,
    focus_event_by_id,
    focus_rule_by_id,
    focused_attr,
    focused_event_id,
    focused_request_id,
    focused_rule_id,
    focused_rule_or_none,
    row_ids,
    row_map,
    row_rule_ids,
)
from .lines import (
    HOLD_WARNING_S,
    RULE_HOST_W,
    VERDICT_COLORS,
    allowlist_text,
    dest_line,
    duration_label,
    mode_label,
    rule_line,
    rule_rows,
)
from .modes import (
    EMPTY_STATIC_QUESTION,
    confirmed_static_switch,
    effective_allows,
    switch_mode_path,
)
from .page import (
    ConsentPage,
)
from .screens import (
    HOLDS_HINT,
    ConfirmScreen,
    DurationScreen,
    EdgeListView,
    FailurePanel,
    ModeScreen,
    PickerScreen,
)
from .wire import (
    _SHARED_SSL,
    AUTH_CLOSE_CODE,
    CONNECTED,
    RECONNECT_DELAYS,
    RECONNECTING,
    REFUSED,
    REFUSED_RETRY_INTERVAL,
    REJECTED,
    UNUSABLE_TOKEN,
    OneFlight,
    backoff,
    close_ws,
    default_ws_factory,
    logger,
    refused_close,
    registration_frame,
    shared_ssl,
    ws_connect_kwargs,
)

SUBMODULES = (
    wire,
    lines,
    focus,
    events,
    flash,
    modes,
    screens,
    page,
)

__all__ = [
    "fmt_duration",
    "AUTH_CLOSE_CODE",
    "CONNECTED",
    "OneFlight",
    "RECONNECTING",
    "RECONNECT_DELAYS",
    "REFUSED",
    "REFUSED_RETRY_INTERVAL",
    "REJECTED",
    "UNUSABLE_TOKEN",
    "_SHARED_SSL",
    "backoff",
    "close_ws",
    "default_ws_factory",
    "logger",
    "refused_close",
    "registration_frame",
    "shared_ssl",
    "ws_connect_kwargs",
    "HOLD_WARNING_S",
    "RULE_HOST_W",
    "VERDICT_COLORS",
    "allowlist_text",
    "dest_line",
    "duration_label",
    "mode_label",
    "rule_line",
    "rule_rows",
    "ensure_focus",
    "focus_attr",
    "focus_by_id",
    "focus_event_by_id",
    "focus_rule_by_id",
    "focused_attr",
    "focused_event_id",
    "focused_request_id",
    "focused_rule_id",
    "focused_rule_or_none",
    "row_ids",
    "row_map",
    "row_rule_ids",
    "event_dest",
    "event_id_suffix",
    "event_item",
    "event_line",
    "event_time",
    "events_note",
    "render_order",
    "FLASH_TTL",
    "FlashLine",
    "flash_safe",
    "hold_flash",
    "panel_safe",
    "sighting_flash",
    "EMPTY_STATIC_QUESTION",
    "confirmed_static_switch",
    "effective_allows",
    "switch_mode_path",
    "ConfirmScreen",
    "DurationScreen",
    "EdgeListView",
    "FailurePanel",
    "HOLDS_HINT",
    "ModeScreen",
    "PickerScreen",
    "ConsentPage",
]
