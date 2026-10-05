"""The tree's follow-up machinery (#309, #341): the queue of
full-terminal flows a page records, the shell flow's runner, and
the new-terminal window's spawn — the pieces the app shell
(:mod:`msks.client.tui.main_app`) and the workspace page
(:mod:`msks.client.tui.workspace`) share, with no screen imports
of their own.
"""

import asyncio
import os
import sys

from ..console import run_workspace_shell
from ..wintitle import TITLE_MARKER

#: The full-terminal flows a page can record (#309): the console
#: shell as the new-terminal action's dead-launcher fallback. The
#: consent decider stopped chaining when it moved into the tree
#: (#358).
FLOW_SHELL = "shell"


class TuiFollow:
    """What happens after the TUI exits (#309): one full-terminal
    flow — a console shell, the dead-launcher fallback — or nothing
    (the operator quit; the consent decider stopped chaining when
    it moved into the tree, #358). Also carries the workspace
    page the tree reopens when a flow hands the terminal back."""

    def __init__(self) -> None:
        self.action: tuple[str, str] | None = None
        self.reopen: str | None = None
        self.seed: str | None = None

    def request(self, kind: str, workspace_id: str) -> None:
        """Record one flow to run after the TUI exits."""
        self.action = (kind, workspace_id)

    def take(self) -> tuple[str, str] | None:
        """The recorded flow, cleared as it is taken."""
        action, self.action = self.action, None
        return action


def run_shell_flow(workspace_id: str) -> None:
    """A console shell in the workspace — the dead-launcher
    fallback's flow (the console boots a stopped workspace first)."""
    run_workspace_shell(workspace_id)


def ssh_child_argv(workspace_id: str) -> list[str]:
    """The ssh invocation the new-terminal action appends to the
    launcher (#341): this client's own interpreter and module (an
    editable checkout spawns itself; an installed client its own
    environment), then the ssh command and the workspace — ssh
    over the console because a fresh window gets resized, and
    the console session sizes its guest pty once, at connect, while
    ssh carries every resize to the guest. The child needs no
    connection flags: the tree's bootstrap already materialized
    every winner — the file's and the ``--daemon`` flag's alike —
    into the environment the child inherits, so it reaches the
    same daemon by inheritance.
    """
    return [sys.executable, "-m", "msks.client.cli", "ssh", workspace_id]


async def spawn_window(argv: list[str]):
    """Run the launcher detached (#341): its own session, its
    stdio on devnull — the window borrows no terminal the tree
    holds, and the tree's later exit never takes it down. The
    asyncio child watcher reaps the launcher when it closes, so
    the tree holds no waitable handle and leaves no zombie. The
    child's environment carries the new-window marker (#445): the
    window msks opened may take its title from
    ``MSKSC_TERMINAL_TITLE`` — the launcher names it when it runs
    one, and the appended ``msks ssh`` names it when none does.
    """
    return await asyncio.create_subprocess_exec(
        *argv,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
        start_new_session=True,
        env={**os.environ, TITLE_MARKER: "1"},
    )


#: The full-terminal flows, keyed by the kind a page records.
FLOWS = {
    FLOW_SHELL: run_shell_flow,
}


def run_follow_up(action: tuple[str, str]) -> None:
    """Run one recorded flow; the kinds a page can record are
    exactly the FLOWS keys."""
    kind, workspace_id = action
    FLOWS[kind](workspace_id)
