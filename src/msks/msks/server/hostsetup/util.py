"""The shared shell-and-filesystem vocabulary.

Every phase speaks these: ``sh`` runs a command and names its
failure, the file helpers write and remove atomically, the
os-release readers carry the platform gate's evidence, and
:class:`HostSetupError` is the named refusal every phase raises.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

OS_RELEASE = "/etc/os-release"


class HostSetupError(Exception):
    """A named refusal: what is needed and why, before (or at) the
    step that cannot proceed."""


def sh(
    cmd: list[str], timeout: float = 10.0, env: dict[str, str] | None = None
) -> tuple[int, str, str]:
    """Run one command, capturing its answer — doctor's run shape,
    exceptions included: a timeout or a missing binary answers
    (-1, "", reason) so every caller reports one shape of failure."""
    try:
        done = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            env=env,
        )
    except (subprocess.TimeoutExpired, OSError) as exc:
        return -1, "", f"{cmd[0]}: {exc}"
    return done.returncode, done.stdout, done.stderr


def say(tool: str, message: str) -> None:
    print(f"{tool}: {message}")


def am_root() -> bool:
    return os.geteuid() == 0


def os_release_id() -> str:
    """The os-release ID (``debian`` on the targeted hosts), or ""
    when the file is missing or carries no ID."""
    try:
        lines = Path(OS_RELEASE).read_text(encoding="utf-8").splitlines()
    except OSError:
        return ""
    for line in lines:
        if line.startswith("ID="):
            return line[3:].strip().strip('"')
    return ""


def os_release_version_id() -> str:
    """The os-release VERSION_ID (``24.04`` on Ubuntu noble), or
    "" under the same absences os_release_id answers "" for."""
    try:
        lines = Path(OS_RELEASE).read_text(encoding="utf-8").splitlines()
    except OSError:
        return ""
    for line in lines:
        if line.startswith("VERSION_ID="):
            return line[len("VERSION_ID=") :].strip().strip('"')
    return ""


def version_tuple(text: str) -> tuple[int, ...] | None:
    """Dot-separated integers as a comparable tuple, or None when
    the text carries anything else (rolling releases ship no
    VERSION_ID; a nonnumeric one compares as unknown)."""
    try:
        return tuple(int(part) for part in text.split("."))
    except ValueError:
        return None


def write_file(path: str, text: str) -> None:
    """Install one root-owned config file (mode 0644). The parent
    is created when absent — a minimal Debian (a slim image, a
    container) ships without /etc/sysctl.d until procps lands."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")
    os.chown(target, 0, 0)
    target.chmod(0o644)


def read_file(path: str) -> str:
    return Path(path).read_text(encoding="utf-8")


def file_exists(path: str) -> bool:
    """Whether one landed file is there — the existence probe the
    uninstall phases gate on (write_file's read-side twin). A
    dangling symlink counts as present: the tool lands real
    files, so a link at a landed path is a stranger's, and the
    unlink that follows removes the link itself, never its
    target."""
    target = Path(path)
    return target.exists() or target.is_symlink()


def remove_file(path: str) -> bool:
    """Remove one landed file — write_file's mirror, the seam the
    tests rig. True when the file was there, False when it was
    already gone (the idempotent re-run — including a file that
    vanishes between the probe and the unlink); an unlink that
    itself fails is a named refusal."""
    if not file_exists(path):
        return False
    try:
        Path(path).unlink()
    except FileNotFoundError:
        return False
    except OSError as exc:
        raise HostSetupError(
            f"cannot remove {path} ({exc.strerror or exc})"
        ) from None
    return True


def replace_file(source: Path, target: Path) -> None:
    """One atomic rename — the seam the tests rig."""
    os.replace(source, target)


def which(tool: str) -> str | None:
    return shutil.which(tool)


#: Debian's admin directories: the systemd user manager's default
#: PATH carries them (the daemon's own search path), while a
#: non-root login shell's may not (#601).
PATH_EXTRA_DIRS = ("/usr/local/sbin", "/usr/sbin", "/sbin")


def silent_unlink(path: str) -> None:
    """Best-effort cleanup on the refusal paths; the error that
    caused the refusal is the message, not the cleanup's."""
    try:
        os.unlink(path)
    except OSError:
        pass


def invoked_as() -> str:
    """The entrypoint's own absolute path, for the sudo hint: an
    install root's PATH does not carry (a venv's bin, above all),
    so the bare name cannot work there — the hint names the path
    that does."""
    return str(Path(sys.argv[0]).resolve())


def chown_path(path: Path, uid: int, gid: int) -> None:
    """os.chown, as a seam the suite can pin."""
    os.chown(path, uid, gid)
