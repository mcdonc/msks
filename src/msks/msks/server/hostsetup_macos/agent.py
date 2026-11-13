"""Phase 5b: the login agent and the managed credentials.

The LaunchAgent that starts the VM at login, and the CA and
token paths under ``~/.config/msks`` the preset pins.
"""

from __future__ import annotations

import os
import plistlib
import shutil
import subprocess
from pathlib import Path

from .base import VM_NAME, MacSetupError, run, say

#: Where the CA lands on the Mac — beside the client config the
#: preset's CAFILE names.
MAC_CA = Path("~/.config/msks/msksd-vm-ca.pem").expanduser()

#: Where the bootstrap token lands, beside the CA: the managed
#: config's ``token_file`` names it (the config tree stays free
#: of inline credentials — a file is the client's own shape).

#: Where the bootstrap token lands, beside the CA: the managed
#: config's ``token_file`` names it (the config tree stays free
#: of inline credentials — a file is the client's own shape).
MAC_TOKEN = Path("~/.config/msks/msksd-vm.token").expanduser()

#: The daemon alias the managed client config defines — the name
#: a bare ``msks`` addresses through ``active_daemon``.

#: The daemon alias the managed client config defines — the name
#: a bare ``msks`` addresses through ``active_daemon``.
MANAGED_ALIAS = "msksd-vm"
#: The managed config's first line: names the tool that wrote
#: the file — provenance for the operator; no code reads it
#: (#730 — a rerun keeps any existing file, marker or not).

#: The managed config's first line: names the tool that wrote
#: the file — provenance for the operator; no code reads it
#: (#730 — a rerun keeps any existing file, marker or not).
MANAGED_HEADER = "# Written by msks-setup-macos-host"

#: The login agent (launchd): ``RunAtLoad`` runs ``limactl start``
#: at login, so the VM — and with it the daemon — answers after a
#: reboot with no manual step. The plist lives in the user's own
#: LaunchAgents: a user agent, no root, matching the VM it owns.

#: The login agent (launchd): ``RunAtLoad`` runs ``limactl start``
#: at login, so the VM — and with it the daemon — answers after a
#: reboot with no manual step. The plist lives in the user's own
#: LaunchAgents: a user agent, no root, matching the VM it owns.
AGENT_LABEL = "com.msks.msksd-vm"

AGENT_PLIST = Path(f"~/Library/LaunchAgents/{AGENT_LABEL}.plist").expanduser()

AGENT_LOG = Path("~/Library/Logs/msksd-vm-agent.log").expanduser()


def limactl_path() -> str:
    """The absolute limactl path the agent names — launchd's own
    environment carries none of the brew prefixes, so the agent
    cannot look the binary up by name at run time."""
    found = shutil.which("limactl")
    if found is None:
        raise MacSetupError(
            "limactl is not on PATH; re-run the tool (it installs "
            "Lima itself through brew) or run brew install lima"
        )
    return found


def agent_plist(limactl: str) -> bytes:
    """The agent's plist: ``RunAtLoad`` brings the VM up at login
    (``limactl start`` on an already-running VM is a quick no-op,
    so a login into a live session costs one command). Both
    streams land in one log under ~/Library/Logs — the file a
    silent login failure reads."""
    return plistlib.dumps(
        {
            "Label": AGENT_LABEL,
            "ProgramArguments": [limactl, "start", VM_NAME],
            "RunAtLoad": True,
            "StandardOutPath": str(AGENT_LOG),
            "StandardErrorPath": str(AGENT_LOG),
        }
    )


def unload_agent() -> None:
    """Boot the agent out of launchd, tolerant of nothing being
    loaded — the state after a first install and after an
    uninstall alike."""
    subprocess.run(
        ["launchctl", "bootout", f"gui/{os.getuid()}/{AGENT_LABEL}"],
        capture_output=True,
    )


def agent_loaded() -> bool:
    """Whether launchd carries the agent: ``launchctl print`` on
    the label answers 0 only for a loaded service."""
    done = subprocess.run(
        ["launchctl", "print", f"gui/{os.getuid()}/{AGENT_LABEL}"],
        capture_output=True,
    )
    return done.returncode == 0


def ensure_agent() -> None:
    """Install and load the login agent, idempotently: the plist
    is rewritten every run (the agent is updated, never
    duplicated — one label, one file), the old service boots out,
    and the new one bootstraps in loaded. The kickstart that
    follows runs the agent's job immediately — the same
    ``limactl start`` every login runs — so the VM starts through
    the agent now, and a green install has already proven the
    login path instead of discovering a broken one at the next
    login."""
    limactl = limactl_path()
    AGENT_LOG.parent.mkdir(parents=True, exist_ok=True)
    AGENT_PLIST.parent.mkdir(parents=True, exist_ok=True)
    AGENT_PLIST.write_bytes(agent_plist(limactl))
    unload_agent()
    run(["launchctl", "bootstrap", f"gui/{os.getuid()}", str(AGENT_PLIST)])
    run(
        [
            "launchctl",
            "kickstart",
            f"gui/{os.getuid()}/{AGENT_LABEL}",
        ]
    )
    say(f"the login agent starts the {VM_NAME} VM now and at every login")


def remove_agent() -> None:
    """The uninstall mirror: boot the agent out and remove its
    plist — idempotent, an absent agent uninstalls cleanly."""
    unload_agent()
    AGENT_PLIST.unlink(missing_ok=True)
