"""The client console-script links.

``msks`` and ``msks-term-popup`` link at ``/usr/local/bin``, each
pointing at the bin sibling this tool's own venv ships (#719,
#755); a live link or a regular file refuses by name, a dangling
link re-points.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from .util import HostSetupError

#: Where the client's console scripts link (#719, #755): the
#: /usr/local/bin real estate the wrapper and the VMM already
#: occupy — on every login PATH, so the links need no profile
#: edits and no login-shell gamble over ~/.local/bin.
CLIENT_LINK = "/usr/local/bin/msks"

POPUP_LINK = "/usr/local/bin/msks-term-popup"

#: The client's console commands the link phase lands (#719,
#: #755): the client for every shell, and the consent-terminal
#: launcher beside it — the command a GUI-opened terminal window
#: resolves on its own PATH, the fresh-PATH rule the macOS tool
#: links both commands for (#734).
CLIENT_LINKS = (
    ("msks", CLIENT_LINK),
    ("msks-term-popup", POPUP_LINK),
)


def bin_sibling(name: str) -> str | None:
    """A console script beside this tool's own, when the venv
    that shipped the tool shipped it, or None — the
    deployment's bin dir is the one path that locates the
    install (invoked_as()'s reasoning)."""
    sibling = Path(sys.argv[0]).resolve().parent / name
    return str(sibling) if sibling.exists() else None


def msksd_sibling() -> str | None:
    """The msksd the deployment brought, as this tool's own bin
    sibling (the venv that shipped the tool ships the daemon),
    or None when no sibling exists — the deployment runs its
    daemon from elsewhere and the service phase names that and
    stands down."""
    return bin_sibling("msksd")


def place_link(path: str, sibling: str) -> str:
    """The symlink lands, and the note names the pair."""
    os.symlink(sibling, path)
    return f"{path} -> {sibling}"


def link_is_live(link: Path) -> bool:
    """Whether a symlink's target answers. A link whose target
    cannot even be stat'd (a permission error on the target's
    directory) reads live: stale must be provable before a
    re-point takes the name — ``Path.exists`` alone would read
    the unstatable link dangling and swallow the difference."""
    try:
        link.stat()
    except FileNotFoundError:
        return False
    except OSError:
        return True  # unstatable is not provably stale
    return True


def relink_or_refuse(path: str, link: Path, sibling: str, error) -> str:
    """The arms for a path that already holds a link: ours
    answers already-linked, a live link to another target
    refuses through *error* by name, and a dangling link is
    stale by definition and re-points."""
    current = os.readlink(path)
    if current == sibling:
        return f"{path} -> {sibling} (already linked)"
    if link_is_live(link):
        raise error(
            f"{path} is a link to {current}, not this tool's "
            "link; remove it and re-run"
        )
    link.unlink()  # dangling: stale by definition
    return place_link(path, sibling)


def claim_link(path: str, sibling: str, error) -> str:
    """Place the client's symlink at *path* pointing at
    *sibling* (#719): a regular file at the path refuses through
    *error* by name — the run never clobbers what it did not
    land — a path already holding a link takes
    :func:`relink_or_refuse`'s arms, and a bare path links. The
    note names the pair either way."""
    link = Path(path)
    if link.is_symlink():
        return relink_or_refuse(path, link, sibling, error)
    if link.exists():
        raise error(
            f"{path} already exists and is not this tool's link; "
            "remove it (or move your own command) and re-run"
        )
    return place_link(path, sibling)


def link_client() -> str:
    """The client's console scripts onto every login PATH (#719,
    #755): a symlink per command (:data:`CLIENT_LINKS`) — ``msks``
    for every shell and ``msks-term-popup`` beside it, the
    consent-terminal launcher a GUI-opened window resolves on its
    own PATH — into /usr/local/bin, the real estate the wrapper
    and the VMM already occupy, so no profile edits and no
    login-shell gamble. Each command points at the bin sibling
    this tool's own venv ships; a command with no sibling stands
    down with a note (the deployment runs its client from
    elsewhere — invoked_as()'s reasoning again); the arms at each
    path are :func:`claim_link`'s."""
    notes = []
    for name, path in CLIENT_LINKS:
        sibling = bin_sibling(name)
        if sibling is None:
            notes.append(
                f"skipped: no {name} beside the tool (the "
                "deployment's client lives elsewhere)"
            )
            continue
        notes.append(claim_link(path, sibling, HostSetupError))
    return " · ".join(notes)


def drop_client_link() -> str:
    """The client links' mirror (#719, #755): each symlink goes
    whatever it pointed at (the tool proves no run's own link),
    and a regular file at a path stays — the uninstall never
    removes an operator's own file."""
    notes = []
    for _name, path in CLIENT_LINKS:
        link = Path(path)
        if link.is_symlink():
            target = os.readlink(path)
            link.unlink()
            notes.append(f"{path} removed (pointed at {target})")
        elif link.exists():
            notes.append(
                f"{path} is a file the tool did not land (left as-is)"
            )
        else:
            notes.append(f"{path} already absent")
    return " · ".join(notes)
