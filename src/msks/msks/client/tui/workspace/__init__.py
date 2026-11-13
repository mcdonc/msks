"""The workspace page (#309) — the screen, its action row, and
the consent status lines it shares with the header.
"""

from .actions import (
    PAGE_ACTIONS,
    CloneNameScreen,
    action_content,
    action_label,
    action_note,
)
from .actions import (
    dimmed_action_content as dimmed_action_content,
)
from .actions import (
    live_action_content as live_action_content,
)
from .actions import (
    primary_action as primary_action,
)
from .lines import consent_line, granted_line, next_expiry
from .screen import WorkspaceScreen

__all__ = [
    "PAGE_ACTIONS",
    "CloneNameScreen",
    "WorkspaceScreen",
    "action_content",
    "action_label",
    "action_note",
    "consent_line",
    "granted_line",
    "next_expiry",
]
