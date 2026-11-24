"""The host-side venv: the shared install the brew-prefix links
point at (#1118, #1080's macOS follow-along).

The daemon lives in the VM; the client runs on the Mac, and the
brew-prefix links used to point at the installing user's uv tool
dir — a second user's bare ``msks`` resolved and died on the
permissions of a home it could not cross. The shared install at
``/opt/msks`` (the venv, the uv-managed 3.14 beside it, the
bootstrap uv) serves every account the same way the Linux tools'
does. ``/opt`` is root-owned ground on macOS, so the one
privileged step this tool ever takes is sudo creating that tree
owned by the installing user; everything under it stays
user-writable and world-readable, and the rest of the tool keeps
its no-sudo shape.
"""

from __future__ import annotations

import grp
import os
import pwd
from pathlib import Path

from ..hostsetup.venv import (
    VENV_ROOT,
    check_wheel,
    drop_venv,
    landed_venv,
    refuse_dev_build,
)
from .base import MacSetupError, run, say


def ensure_shared_root() -> None:
    """sudo creates ``/opt/msks`` owned by the installing user,
    once: the directory is root-owned ground on macOS, and the
    tool owns everything under it afterwards (a tree already
    writable takes no sudo — the re-run's shape). A sudo that
    cannot answer is run()'s own named refusal."""
    if Path(VENV_ROOT).is_dir() and os.access(VENV_ROOT, os.W_OK):
        return
    account = pwd.getpwuid(os.getuid())
    group = grp.getgrgid(os.getgid()).gr_name
    say(f"creating {VENV_ROOT} through sudo (/opt is root-owned on macOS)")
    run(
        [
            "sudo",
            "install",
            "-d",
            "-o",
            account.pw_name,
            "-g",
            group,
            "-m",
            "0755",
            VENV_ROOT,
        ]
    )


def install_host_venv(wheel: str | None) -> str:
    """The host-side half of the venv phase (#1118): the refusals
    that precede every side effect (a dev build without a wheel,
    a wheel that disagrees with the tool's version), the shared
    root, then the same landed middle the Linux tools run — over
    MacSetupError and this tool's own say. Returns the summary's
    venv line."""

    def tell(message: str) -> None:
        say(message)

    refuse_dev_build(wheel, MacSetupError)
    check_wheel(wheel, MacSetupError)
    ensure_shared_root()
    return landed_venv(wheel, MacSetupError, tell)


def remove_host_venv() -> str:
    """The uninstall arm's mirror: the shared tree is the
    installing user's own (ensure_shared_root's chown), so it
    goes without sudo — drop_venv's own note names what left."""
    return drop_venv(MacSetupError)
