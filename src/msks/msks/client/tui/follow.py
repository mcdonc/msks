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

from .. import forward as forward_mod
from ..console import run_workspace_shell
from ..env import env_token, env_url, is_unix, ssl_context
from ..rest import ensure_running
from ..ssh import SSH_PORT, run_workspace_ssh
from ..wintitle import TITLE_MARKER

#: The full-terminal flows a page can record (#309): the
#: same-terminal ssh session the shell action takes by default
#: (#562), and the console shell — the ssh refusal's fallback and
#: the dead-launcher fallback before it. The consent decider
#: stopped chaining when it moved into the tree (#358).
FLOW_SSH = "ssh"
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


def run_shell_flow(workspace_id: str) -> str | None:
    """A console shell in the workspace — the dead-launcher
    fallback's flow (the console boots a stopped workspace first)."""
    run_workspace_shell(workspace_id)
    return None


def ensure_forward(workspace_id: str) -> None:
    """Read the forward's verdict before ssh runs (#562): a
    stopped workspace boots first — the shell action's session
    boots it itself (#479), and the daemon refuses a stopped
    workspace's forward instantly — then the daemon dials the
    guest at the upgrade and names its refusal there, so the
    probe lands it in this process, where the console fallback
    and the flash live, instead of in the ProxyCommand child ssh
    spawns, whose exit code carries no reason."""
    url = env_url()
    token = env_token()
    ssl_ctx = None if is_unix(url) else ssl_context()
    asyncio.run(ensure_running(workspace_id, url, token, ssl_ctx=ssl_ctx))
    address = forward_mod.ws_url(url, workspace_id, SSH_PORT)
    asyncio.run(forward_mod.probe(address, token, ssl_ctx))


def first_line(text: str) -> str:
    """The first line of a refusal: the recovery paragraph an
    identity refusal carries would ellipsize off the flash's one
    row — its opening line is the part the operator acts on. An
    empty refusal reads as the bare word, never an empty
    parenthetical."""
    line = text.strip().splitlines()[:1]
    return line[0] if line else "refused"


def run_ssh_flow(workspace_id: str) -> str | None:
    """An ssh session in the TUI's own terminal (#562): the tree
    exited for this flow, ssh runs where it stood, and the tree
    restarts when the session hands the terminal back. A refusal
    the probe names — no NIC, not running, the service not
    listening, an identity the daemon rejected — takes the
    console one step later and returns the note the restarted
    tree flashes (the console is the failsafe; ssh is the
    interactive path, and a refusal the operator can read rides
    along — its first line, the flash's one row). An ssh that ran
    reports only its exit code — 255 included, since ssh hands a
    session's own exit through unchanged and a console after a
    finished session is a surprise, not a fallback; a console
    that refuses too raises on: the outer loop flashes that
    reason."""
    try:
        ensure_forward(workspace_id)
        code = run_workspace_ssh(workspace_id, [])
        if code == 0:
            return None
        return f"ssh exited {code}"
    except SystemExit as exc:
        reason = first_line(str(exc))
        run_shell_flow(workspace_id)
        return f"ssh refused ({reason}) — the console session ran instead"


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
    FLOW_SSH: run_ssh_flow,
    FLOW_SHELL: run_shell_flow,
}


def run_follow_up(action: tuple[str, str]) -> str | None:
    """Run one recorded flow, answering the flash the restarted
    tree should carry (None for none); the kinds a page can record
    are exactly the FLOWS keys."""
    kind, workspace_id = action
    return FLOWS[kind](workspace_id)
