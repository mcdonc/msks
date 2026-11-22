"""Phase 6: the client links and the GUI's application
bundle (#719, #734, #1048).

``msks`` and ``msks-term-popup`` linked into the brew prefix's
bin, pointing at the sibling the install shipped; on top of
the links, the GUI's ``msks.app`` lands under the home's
Applications — the bundle the Dock, Launchpad, and the menu
bar name the client by (a bare console script cannot: the
desktop reads the name from the launching bundle, #1043).
"""

from __future__ import annotations

import os
import plistlib
import shutil
import sys
from pathlib import Path

from ...client.gui import appbundle
from ..hostsetup import claim_link
from .base import TOOL, MacSetupError, run

# --- phase 6: the client links (#719, #734) ------------------------------


#: The client's console commands the link phase lands (#719,
#: #734): a bare ``msks`` for every shell, and
#: ``msks-term-popup`` beside it — the consent-terminal
#: launcher an ``open``-opened window resolves on its fresh
#: login PATH, where nothing from the spawning shell carries.
CLIENT_COMMANDS = ("msks", "msks-term-popup")

#: The brew prefixes the uninstall arm sweeps in brew's absence
#: (#701's lesson: the uninstall never requires a tool the
#: install needed) — the arm tries brew first for the truth.
FALLBACK_BREW_BINS = ("/opt/homebrew/bin", "/usr/local/bin")


def brewed_prefix() -> str:
    """The brew prefix when brew answers, empty when it does
    not (a missing binary, a failing call — the uninstall never
    requires brew, #701's lesson)."""
    try:
        return run(["brew", "--prefix"], capture=True).strip()
    except FileNotFoundError, MacSetupError:
        return ""


def client_link_path(command: str) -> str:
    """One command's link path: the brew prefix's bin dir. The
    tool already requires Homebrew for Lima (#699), the prefix
    is user-owned (the tool never runs sudo), and its bin dir is
    on PATH for every brew user. A brew that will not answer
    refuses by name — the install's own remedy is the same
    brew line #699 prints."""
    prefix = brewed_prefix()
    if not prefix:
        raise MacSetupError(
            f"brew did not answer `brew --prefix`; the {command} "
            "client link needs Homebrew (the tool installs Lima "
            "through it)"
        )
    return f"{prefix}/bin/{command}"


def client_sibling(command: str) -> str | None:
    """One command's console script beside this tool's own — the
    Python environment that shipped this tool shipped every
    client command with it — or None when no sibling exists: the
    deployment runs its client from elsewhere, and the phase
    stands down for that command with a note."""
    sibling = Path(sys.argv[0]).resolve().parent / command
    return str(sibling) if sibling.exists() else None


def link_client_mac() -> str:
    """The client's commands onto every Mac PATH (#719, #734):
    a symlink per command at the brew prefix's bin dir pointing
    at this tool's bin sibling. A command without a sibling
    skips with a note; the arms at each path are the shared
    :func:`claim_link`'s — the Linux tools' own phase, the same
    arms."""
    return "; ".join(link_one_client(command) for command in CLIENT_COMMANDS)


def link_one_client(command: str) -> str:
    """One command's link note: the claim arms over its path, or
    the stand-down note when this deployment's bin dir ships no
    such sibling beside the tool."""
    sibling = client_sibling(command)
    if sibling is None:
        return (
            f"the client link: skipped, no {command} beside the "
            "tool (the deployment's client lives elsewhere)"
        )
    return claim_link(client_link_path(command), sibling, MacSetupError)


def client_link_row() -> str:
    """The links' --check row (#719, #734): where a bare ``msks``
    and a bare ``msks-term-popup`` answer from, green when each
    link stands and its target answers; anything else refuses
    with the re-run remedy."""
    return "; ".join(
        client_link_answer(command) for command in CLIENT_COMMANDS
    )


def client_link_answer(command: str) -> str:
    """One command's --check answer, green naming the link path
    and its target (``Path.exists`` resolves the target from
    the link's own directory — a relative brew-style target
    reads correctly); a missing link refuses with the re-run
    remedy."""
    path = client_link_path(command)
    link = Path(path)
    if link.is_symlink() and link.exists():
        return f"{command} answers at {path} (-> {os.readlink(path)})"
    raise MacSetupError(
        f"the {command} client link at {path} is missing; re-run {TOOL}"
    )


def remove_client_link() -> str:
    """The uninstall arm's mirror: the symlinks go whatever they
    pointed at, and a regular file at a path stays — the
    uninstall never removes an operator's own file. The arm
    never requires brew (#701's lesson): it asks brew for the
    prefix first and sweeps the brew uses when brew is gone."""
    removed = [
        swept
        for command in CLIENT_COMMANDS
        if (swept := remove_one_client_link(command)) is not None
    ]
    if not removed:
        return "no client link found"
    if len(removed) == 1:
        return f"the client link {removed[0]} is gone"
    return f"the client links {' and '.join(removed)} are gone"


def remove_one_client_link(command: str) -> str | None:
    """One command's sweep: the first brew-shaped path holding a
    link names it, and a path holding nothing link-shaped leaves
    the command unswept."""
    paths: list[str] = []
    prefix = brewed_prefix()
    if prefix:
        paths.append(f"{prefix}/bin/{command}")
    paths.extend(f"{bin_}/{command}" for bin_ in FALLBACK_BREW_BINS)
    for path in dict.fromkeys(paths):
        link = Path(path)
        if link.is_symlink():
            link.unlink()
            return str(path)
    return None


# --- the GUI's application bundle (#1048) -------------------------------


def ensure_app_bundle() -> str:
    """Plant the GUI's ``msks.app`` under the home's
    Applications (#1048): the bundle the desktop launches and
    names the client by. A deployment whose bin dir ships no
    ``msks`` beside the tool stands down with a note — the
    links' own stand-down shape — and a write the filesystem
    refuses names itself through the tool's own refusal."""
    script = client_sibling(appbundle.EXEC_NAME)
    if script is None or not sys.executable:
        return (
            "the application bundle: skipped, no msks beside "
            "the tool (the deployment's GUI lives elsewhere)"
        )
    try:
        bundle = appbundle.install_bundle(
            Path.home() / "Applications",
            Path(sys.executable),
            Path(script),
        )
    except (OSError, RuntimeError) as exc:
        raise MacSetupError(
            f"the application bundle did not land under the "
            f"home's Applications: {exc}"
        ) from None
    return f"the application bundle is installed: {bundle}"


def remove_app_bundle() -> str:
    """The bundle's sweep (#1048): the tool removes the bundle
    its own identifier proves — a foreign ``msks.app`` an
    operator planted by hand stays, the uninstall's own
    no-clobber rule — and an absent bundle says so."""
    bundle = Path.home() / "Applications" / appbundle.BUNDLE_DIR
    plist = bundle / "Contents" / "Info.plist"
    try:
        planted = plistlib.loads(plist.read_bytes())
    except OSError, plistlib.InvalidFileException:
        return "no application bundle of this tool's stands"
    if planted.get("CFBundleIdentifier") != appbundle.IDENTIFIER:
        return (
            "the application bundle at "
            f"{bundle} is not this tool's (left as-is)"
        )
    shutil.rmtree(bundle, ignore_errors=True)
    return "the application bundle is gone"


# --- the entry --------------------------------------------------------
