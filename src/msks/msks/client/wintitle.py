"""The workspace-shell window's title (#445).

The workspace page's new-terminal action opens a window through
``terminal_open_cmd`` and marks the child it spawns
(:data:`TITLE_MARKER`), the way the tmux consent launcher
(:mod:`msks.client.term_popup`) marks its own window by running
inside it. The markers name the one rule the setting carries:
msks titles the windows it opens for a workspace shell, and leaves
every other terminal's title alone — a typed ``msks ssh`` in the
operator's own shell changes nothing, because OSC cannot read a
title back to restore it.
"""

import os
import sys

from .config import TITLE_ENV_VAR

#: The handoff the TUI's new-terminal spawn plants in the child's
#: environment (#445): the launcher the prefix runs — and, when it
#: runs none, the appended ``msks ssh`` itself — may name the
#: window this marker was handed. The tmux launcher needs no
#: marker (it always runs in the window the prefix opened); the
#: plain ssh path checks it, the only way it can tell a window msks
#: opened from the operator's own terminal.
TITLE_MARKER = "MSKS_NEW_WINDOW"


def configured_title(workspace_ref: str | None) -> str | None:
    """The window title the operator configured (#445):
    ``MSKSC_TERMINAL_TITLE``'s template with ``{workspace}``
    resolved to *workspace_ref* — the workspace's id (the token
    the TUI appends), or ``shell`` for a child naming none. None
    (the setting unset, or blank) leaves the terminal emulator's
    own title in place."""
    template = os.environ.get(TITLE_ENV_VAR, "")
    if not template.strip():
        return None
    return template.replace("{workspace}", workspace_ref or "shell")


def set_window_title(title: str) -> None:
    """Name the terminal window this process runs in (#445): one
    OSC 0 sequence, the title-setting escape every terminal that
    runs a command honors. Control characters drop out of the
    title first — a BEL would end the OSC early and an ESC would
    start a live sequence. A stdout that is not a terminal (a
    piped hand-run) stays clean."""
    if not sys.stdout.isatty():
        return
    clean = "".join(c for c in title if c >= " " and c != "\x7f")
    sys.stdout.write(f"\x1b]0;{clean}\x07")
    sys.stdout.flush()


def title_spawned_window(workspace_ref: str) -> None:
    """Name the window the TUI opened for this ssh session (#445):
    the spawn's marker, no tmux pane, a configured template, and a
    terminal stdout together write the title before the session
    starts. A typed invocation carries no marker and keeps its
    terminal's title; inside the consent launcher's pane the
    window is already titled and tmux owns the pane's escapes, so
    the session adds nothing."""
    if os.environ.get(TITLE_MARKER, "") == "":
        return
    if os.environ.get("TMUX"):
        return
    title = configured_title(workspace_ref)
    if title is not None:
        set_window_title(title)
