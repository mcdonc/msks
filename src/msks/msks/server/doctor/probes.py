"""The host probes: commands, PATH rescue, the setup tool.

``run`` beside the which-probe that rescues a tool from the
admin dirs a login shell lacks, and the PATH note row.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

from ..hostsetup import PATH_EXTRA_DIRS, os_release_id
from .report import CheckResult

# ---------------------------------------------------------------------------
# Probe helpers
# ---------------------------------------------------------------------------


def run(cmd: list[str], timeout: float = 10.0) -> tuple[int, str, str]:
    """Run a probe command, return (returncode, stdout, stderr).

    A missing binary or a timeout reads as a failed probe (-1) with
    the reason in stderr, so callers report one shape of failure.
    """
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        return proc.returncode, proc.stdout, proc.stderr
    except subprocess.TimeoutExpired:
        return -1, "", f"{cmd[0]}: timed out"
    except OSError as exc:
        # Covers a missing binary and an unexecutable one (ENOEXEC
        # on a corrupt settings-named path) with the same shape.
        return -1, "", f"{cmd[0]}: {exc}"


def admin_dir_candidate(name: str) -> str | None:
    """The first admin directory carrying an executable *name*."""
    for directory in PATH_EXTRA_DIRS:
        candidate = Path(directory) / name
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate)
    return None


def probe_which(name: str) -> str | None:
    """Find a binary on the daemon-shaped search path (#601): the
    caller's own PATH first, then Debian's admin directories —
    the systemd user manager's default PATH carries them, so
    the daemon finds what lives in /usr/sbin. A login shell
    without the admin directories therefore reports the
    daemon's truth, not the shell's."""
    found = shutil.which(name)
    if found is not None:
        return found
    if os.pathsep in name or "/" in name:
        return None  # an explicit path needs no extension
    return admin_dir_candidate(name)


def rescued_from_admin_dirs(name: str) -> bool:
    """Whether the daemon-shaped path found a binary the caller's
    own PATH cannot see (#601) — the relogin note's trigger."""
    return probe_which(name) is not None and shutil.which(name) is None


def session_lacks_admin_dirs() -> bool:
    """Whether the caller's own PATH skips /usr/sbin (#601) — the
    directory the relogin note names; /usr/local/sbin alone does
    not carry the tools the note is about."""
    session = os.environ.get("PATH", "")
    return "/usr/sbin" not in session.split(os.pathsep)


def setup_tool_name() -> str | None:
    """The host-setup tool this host's os-release names — the two
    console entries the wheel ships, one per targeted distro. A
    distro neither entry targets answers None, and the relogin
    hint keeps the export alone."""
    return {
        "debian": "msks-setup-debian-host",
        # the derivatives carry their own IDs over the Debian
        # package set (#639)
        "ubuntu": "msks-setup-debian-host",
        "linuxmint": "msks-setup-debian-host",
        "fedora": "msks-setup-fedora-host",
        # the Asahi spin carries its own ID (ID_LIKE names fedora)
        "fedora-asahi-remix": "msks-setup-fedora-host",
    }.get(os_release_id())


def path_note_result() -> CheckResult:
    """The relogin encouragement (#601): a warning row, never an
    error. The message names both deployment shapes honestly — a
    daemon under the systemd user unit finds the directories on
    its own PATH, while a hand-started or dev daemon inherits
    this session's PATH and needs the same fix."""
    tool = setup_tool_name()
    relogin = "log out and back in"
    if tool:
        relogin += f" ({tool} adds the directories to ~/.profile)"
    return CheckResult(
        name="PATH",
        ok=False,
        is_warning=True,
        message=(
            "this session's PATH lacks /usr/sbin, where the "
            "deployment host's packaging may keep nft, conntrack, "
            "and the e2fsprogs trio; a "
            "daemon started from it inherits the gap, while the "
            "systemd user unit's own PATH carries the directories"
        ),
        hint=f"{relogin}, or export PATH=$PATH:/usr/sbin:/sbin",
    )
