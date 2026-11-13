"""Phase 1: the host check.

Apple Silicon (the guest images are aarch64 and the framework's
nested virtualization is Apple's own) and a Lima at the
template's floor, installed through brew when the Mac carries
none (#699).
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys

from .base import LIMA_FLOOR, TOOL, MacSetupError, run, say

# --- phase 1: the host check -----------------------------------------


def check_host(install: bool = True) -> dict:
    """The Mac itself: Apple Silicon (the images are aarch64,
    nested virtualization is the Apple Virtualization
    framework's) and a Lima at the template's floor — installed
    through brew when missing (#699), unless the caller passed
    install=False (the check pass, which mutates nothing). A Mac
    without brew names the Homebrew line; an old Lima names the
    upgrade."""
    if sys.platform != "darwin":
        raise MacSetupError(
            "this tool runs on macOS (the Linux hosts take "
            "msks-setup-debian-host)"
        )
    if os.uname().machine != "arm64":
        raise MacSetupError(
            "this tool needs Apple Silicon (the guest images are aarch64; "
            "nested virtualization is the Apple Virtualization framework's)"
        )
    check_chip_class()
    return {"lima": require_lima(install)}


def check_chip_class() -> None:
    """The chip can nest: Apple's Virtualization framework grew
    nested virtualization on the M3 class — an M1 or M2 boots the
    daemon VM but its workspaces can never start (#676's gate:
    warn here, never provision a VM whose workspaces cannot
    run). Newer, unrecognized chips pass (the floor is what the
    framework refuses, not what this list knows)."""
    brand = subprocess.run(
        ["sysctl", "-n", "machdep.cpu.brand_string"],
        capture_output=True,
        text=True,
    ).stdout.strip()
    if brand.startswith(("Apple M1", "Apple M2")):
        raise MacSetupError(
            f"this Mac's chip ({brand}) does not provide nested "
            "virtualization; the daemon VM would boot but its "
            "workspaces cannot start"
        )


def require_lima(install: bool = True) -> str:
    """The installed Lima's version word, at the template's floor
    or newer. A Lima the Mac does not carry takes
    :func:`lima_after_install`'s path (#699) — brew, unless the
    caller passed install=False (the check pass, which mutates
    nothing). A limactl that answers nonzero refuses by name
    through run() (a broken install is not healed by another);
    a Lima below the floor names the upgrade remedy."""
    word = lima_version_output()
    if not word:
        word = lima_after_install(install)
    if lima_version_tuple(word) < LIMA_FLOOR:
        floor = ".".join(str(p) for p in LIMA_FLOOR)
        raise MacSetupError(
            f"Lima {word} is older than the template's floor {floor} "
            "(the mount expansions render wrong before it); upgrade: "
            "brew upgrade lima"
        )
    return word


def lima_after_install(install: bool) -> str:
    """The word after the missing-Lima step. The check pass
    (install=False) refuses with the run-the-tool remedy — it
    mutates nothing; the setup pass brew-installs Lima and
    re-reads, and a still-empty word (brew's bin dir joined PATH
    only in fresh shells) is the fresh-shell refusal."""
    if not install:
        raise MacSetupError(
            "limactl is not on PATH; run the tool (it installs "
            "Lima itself through brew), then --check again"
        )
    install_lima()
    word = lima_version_output()
    if not word:
        raise MacSetupError(
            "brew install lima finished but limactl is still not on "
            "this shell's PATH; open a fresh shell (brew's bin dir "
            f"joins PATH there) and re-run {TOOL}"
        )
    return word


def install_lima() -> None:
    """Install Lima through brew (#699): the stock-Mac step this
    tool runs itself. A Mac without brew names the Homebrew line
    and stops — this tool installs through brew, never around
    it."""
    if shutil.which("brew") is None:
        raise MacSetupError(
            "brew is not on PATH; install Homebrew first "
            "(https://brew.sh), then re-run this tool — it installs "
            "Lima itself from there"
        )
    say("installing Lima through brew (brew install lima)")
    run(["brew", "install", "lima"])


def lima_version_output() -> str:
    """The version word from ``limactl --version``, or the empty
    word when limactl is nowhere on PATH (the caller installs or
    refuses by name — a stock Mac's answer). A limactl that is
    present but answers nonzero refuses by name through run(),
    carrying the command's tail."""
    try:
        out = run(["limactl", "--version"], capture=True)
    except FileNotFoundError:
        return ""
    return out.strip().split()[-1] if out.strip() else ""


def lima_version_tuple(word: str) -> tuple:
    """The version word's numeric parts (``2.2.1`` → (2, 2, 1));
    non-numeric fragments drop, an empty word compares below
    every floor."""
    return tuple(int(p) for p in word.lstrip("v").split(".") if p.isdigit())


# --- phase 2: the msksd VM -------------------------------------------
