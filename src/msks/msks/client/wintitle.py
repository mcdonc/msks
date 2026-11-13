"""The workspace-shell window's title (#445, #858).

The workspace page's new-terminal action opens a window through
``terminal_open_cmd`` and marks the child it spawns
(:data:`TITLE_MARKER`), the way the tmux consent launcher
(:mod:`msks.client.term_popup`) marks its own window by running
inside it. The markers name the one rule the setting carries:
msks titles the windows it opens for a workspace shell, and leaves
every other terminal's title alone — a typed ``msks ssh`` in the
operator's own shell changes nothing, because OSC cannot read a
title back to restore it.

A window msks opens carries one title: ``msks {workspace}
{session}`` (:data:`DEFAULT_TITLE`), with the session record's
token as ``{session}`` — the join key a later surface (#856)
matches the session's window by — and the workspace's name as
``{workspace}`` (its id when the daemon serves none; the human
reads the name, and the token carries the join). A session
without a record (the consent launcher, which titles before any
session exists; a remote-command run) drops the token half.
"""

import os
import sys

#: The handoff the TUI's new-terminal spawn plants in the child's
#: environment (#445): the launcher the prefix runs — and, when it
#: runs none, the appended ``msks ssh`` itself — may name the
#: window this marker was handed. The tmux launcher needs no
#: marker (it always runs in the window the prefix opened); the
#: plain ssh path checks it, the only way it can tell a window msks
#: opened from the operator's own terminal.
TITLE_MARKER = "MSKS_NEW_WINDOW"

#: The title a window msks opened carries (#445, #858): the
#: workspace's name (its id when the daemon serves none) and the
#: session record's token, ``msks tree-vm f8x2n``. The token is
#: the join key a later surface (#856) matches the session's
#: window by; the workspace half is for the human.
DEFAULT_TITLE = "msks {workspace} {session}"


def resolve_title(
    template: str, workspace_ref: str | None, token: str | None
) -> str:
    """One title template with its placeholders resolved (#445,
    #858): ``{workspace}`` to the workspace's name (its id when
    the daemon serves none, and the typed reference before the
    daemon answers) — ``shell`` when the child names no workspace
    — and ``{session}`` to the session record's token, dropped
    whole (its leading space included) when the session carries
    none."""
    resolved = template.replace("{workspace}", workspace_ref or "shell")
    if token:
        return resolved.replace("{session}", token)
    return resolved.replace(" {session}", "").replace("{session}", "")


def window_title(workspace_ref: str | None, token: str | None = None) -> str:
    """The title a window msks opened carries: the default
    template over the workspace and the session record's token
    (#858). The consent launcher writes this beside the window it
    owns (no session record exists yet there, so the token half
    drops); the plain ssh path writes it through
    :func:`title_spawned_window`, which gates on the spawn's
    marker."""
    return resolve_title(DEFAULT_TITLE, workspace_ref, token)


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


def title_spawned_window(workspace_ref: str, token: str | None = None) -> None:
    """Name the window the TUI opened for this ssh session (#445,
    #858): the spawn's marker, the window's title, and a terminal
    stdout together write the title before the session starts. A
    typed invocation carries no marker and keeps its terminal's
    title. Inside the consent launcher's pane the marker is gone —
    :func:`msks.client.term_popup.run_pane` removes it before the
    session runs (the launcher already titled the window, and tmux
    owns the pane's escapes) — and a stale ``$TMUX`` from the
    operator's own environment decides nothing: the marker is
    the one live handoff, not the variable tmux happened to leave
    in it."""
    if os.environ.get(TITLE_MARKER, "") == "":
        return
    set_window_title(window_title(workspace_ref, token))
