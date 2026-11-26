"""The workspace listing surface (#21): one row set as the aligned
table or one JSON document — ``msks ls`` — with its image and
owner columns, the derived status wording, and the best-effort
notices (image drift, version skew) the listing prints beside
itself. The lifecycle command bodies live in
:mod:`msks.client.workspaces.lifecycle`.
"""

import asyncio
import contextlib
import json
import os
import pwd
import sys
from pathlib import Path

from ...spec.version import __version__, ordering
from ..context import (
    call,
    env_token,
    env_url,
    fetch_health,
    is_unix,
    ssl_context,
)
from ..tabular import listing_text
from ..tui.rows import clip


def created_date(raw: str | None) -> str:
    """ISO timestamp to YYYY-MM-DD, or ``-``."""
    return raw[:10] if raw else "-"


def owner_word(uid) -> str:
    """One owner as the listing prints it (#1084): the account
    name — the word the ``--user`` selector takes — or the bare
    uid when no account holds it (a dead owner stays a number)."""
    if uid is None:
        return "-"
    try:
        return pwd.getpwuid(uid).pw_name
    except KeyError:
        return str(uid)


def listing_owners(rows: list[dict]) -> bool:
    """Whether the listing shows the owner column (#1084): only
    when the rows name more than one owner. One owner's rows — a
    non-root listing, every row the caller's own — carry the same
    word down the column, and the column stays off; a listing that
    spans owners shows it, the "which owner holds this name" the
    bare names need."""
    return len({row.get("owner_uid") for row in rows}) > 1


#: The CLI listing's image column width (#523): the table's own
#: budget — wider than the page's meta cell, the table has room
#: a header line does not.
IMAGE_COLUMN_W = 16


def image_column(row: dict) -> str:
    """The listing's image column (#523): the catalog reference —
    ``name:version``, the words an operator can act on — with the
    digest prefix as the fallback for a row the catalog cannot
    resolve. The clip is the page's own middle cut (the version
    tail is the half a head-only clip eats; a registry-prefixed
    reference would lose its name AND version to one)."""
    ref = row.get("image_ref") or row.get("image_hash") or "-"
    return clip(ref, IMAGE_COLUMN_W)


def workspace_cells(row: dict) -> list[str]:
    """One listing row's cells."""
    image = image_column(row)
    host = row.get("host") or "-"
    name = row.get("name") or "-"
    egress_mode = row.get("egress_mode") or "-"
    created = created_date(row.get("created_at"))
    return [
        name,
        row["id"],
        status_with_mounts(row),
        egress_mode,
        created,
        image,
        host,
    ]


def status_with_mounts(row: dict) -> str:
    """The status cell with the mounts' standing marker (#620,
    part #624): a row whose mounts skip carries the count beside
    its status — ``running, 1 mount skipped`` — the marker an
    unresolved mount leaves until the config and the row agree
    again. The healthy row prints its status alone."""
    status = row["status"]
    issues = row.get("mount_issues") or []
    if not issues:
        return status
    kinds = sorted({issue["kind"] for issue in issues})
    label = (
        "mounts " + "/".join(kinds)
        if len(issues) > 1
        else (f"mount {issues[0]['kind']}")
    )
    return f"{status}, {len(issues)} {label}"


def render_ls(rows: list[dict], as_json: bool) -> str:
    """The whole listing: the aligned table, or one JSON document.
    A listing that spans owners (#1084) carries the owner column
    beside the name — the pair a bare name alone cannot address."""
    if as_json:
        return json.dumps(rows, indent=2)
    headers = ["name", "id", "status", "egress", "created", "image", "host"]
    cells = [workspace_cells(row) for row in rows]
    if listing_owners(rows):
        headers.insert(1, "owner")
        cells = [
            [cell[0], owner_word(row.get("owner_uid")), *cell[1:]]
            for row, cell in zip(rows, cells)
        ]
    return listing_text(headers, cells)


def health_image(health: object) -> str | None:
    """The image a /health document names, or None.

    Only a mapping with a string image counts: anything else a
    wrong host or proxy might answer stays None (the drift probe is
    best-effort and never tracebacks the listing).
    """
    if isinstance(health, dict) and isinstance(health.get("image"), str):
        return health["image"]
    return None


def stale_image_notice(expected: str, health: object) -> str | None:
    """The one-line drift notice for ``msks ls`` (#160), or None.

    Both sides must be known: ``MSKSC_EXPECTED_IMAGE`` names the
    image reference the operator sets (unset skips the check), and
    the daemon's ``/health``
    carries the image it booted (``None`` on a daemon predating the
    ``msksd.image`` cmdline pair). An unknown side stays silent —
    including a /health that answers something other than a mapping
    with a string image (a wrong host, a proxy).
    """
    image = health_image(health)
    if not expected or not image or image == expected:
        return None
    return (
        f"msks: daemon serves a different image: {Path(image).name}; "
        f"the expected reference is {Path(expected).name}; align "
        "them by restarting the daemon on the expected image"
    )


def skew_remedy(theirs: str) -> str:
    """The remedy arm for one skewed pair (#715): the notice's
    old single line sent a newer-daemon operator to rerun the
    setup tool — a tool old enough to be behind the daemon is
    old enough to downgrade it, and the refusal that follows
    leaves the VM wedged. The remedy follows the direction: a
    daemon that leads names the client's own update (a dev-marked
    leader names the tree no release matches — an equal-base or
    older dev build is one the plain rerun upgrades), and a
    client that leads names the daemon's (the wheel path when
    the client's own word is a dev build the plain rerun
    refuses; :func:`host_setup_remedy` gives the remedy for the
    client's host, #1174)."""
    if ".dev" in theirs and ordering(theirs) > ordering(__version__):
        return (
            "the daemon runs a development build no release matches — "
            "update this client from the same tree or wait for the "
            "release it becomes"
        )
    if ordering(theirs) > ordering(__version__):
        return "update this client (pip install -U msks)"
    return host_setup_remedy(".dev" in __version__)


#: The Linux host setup scripts (docs/installation.md): either one
#: re-runs idempotently, and a re-run at a newer version refreshes
#: the install. It restarts a running daemon only when the unit
#: file changed (hostsetup's ``activate_unit``), and an upgrade's
#: re-run leaves the unit text as it was.
LINUX_SETUP_TOOLS = "msks-setup-debian-host or msks-setup-fedora-host"

#: The installed daemon's restart on a Linux host: the system unit
#: both setup scripts and the NixOS module install.
DAEMON_RESTART = "sudo systemctl restart msksd"


def host_setup_remedy(dev: bool) -> str:
    """The remedy for a daemon behind this client (#715, #1174),
    for where the daemon lives. That follows the client's own
    host. This arm fires on an older daemon, so a ``/health``
    field a newer daemon might carry is absent exactly here. The
    Lima VM and a Linux host both report a Linux daemon anyway.
    On a Mac the daemon lives in the Lima VM and
    ``msks-setup-macos-host`` updates it. On Linux the daemon runs
    on the host itself and shares the client's install, so the
    usual skew is a daemon still running the release from before
    an upgrade: the setup script refreshed the install and kept
    the daemon up. The remedy restarts first, and a rerun of the
    setup script is for a host that still has the old release. A
    NixOS module host updates through its configuration, and the
    scripts refuse it at the os-release gate. The dev daemon runs
    from its tree, and the setup scripts are its prerequisite, not
    its update.
    ``dev`` names a dev-marked client word: its wheel reaches the
    daemon through the same tool, run from the tree (the tool
    refuses a wheel whose version disagrees with its own), and
    the daemon restarts after."""
    if sys.platform == "darwin":
        if dev:
            return (
                "build a wheel (uv build) and rerun "
                "msks-setup-macos-host --wheel"
            )
        return (
            "rerun msks-setup-macos-host (on the Mac) to update the "
            "daemon in the VM"
        )
    if dev:
        return (
            f"build a wheel (uv build) and rerun {LINUX_SETUP_TOOLS} "
            f"--wheel from the same tree, then restart the daemon "
            f"({DAEMON_RESTART})"
        )
    return (
        f"restart the daemon on its host ({DAEMON_RESTART}) to pick up "
        f"an installed newer release, or rerun {LINUX_SETUP_TOOLS} if "
        "the host still has the old one"
    )


def version_skew_notice(health: dict | None) -> str | None:
    """The one-line version-skew notice (#706, #715), or None. The
    daemon's ``/health`` carries its build's version; a client
    and daemon that ship from one wheel drift when one side is
    updated alone (a ``pip install -U msks`` on the Mac, a stale
    release inside the VM), and the skew otherwise surfaces one
    feature at a time. Warn, never refuse — the pair ships from
    one build, so equality is the norm and a refusal would need
    a compat-floor policy this notice does not add. An unknown
    side stays silent (a None health, a document without a
    version word — a wrong host, a proxy); a known skew names
    both words and the remedy its direction asks for."""
    theirs = health.get("version") if health else None
    if not isinstance(theirs, str) or not theirs or theirs == __version__:
        return None
    return (
        f"msks: the daemon runs {theirs}, this client is {__version__} — "
        f"{skew_remedy(theirs)}"
    )


def dialed_health(transport) -> dict | None:
    """The configured daemon's /health document, dialed on the
    probe's budget — the raw dial whose every failure shape
    :func:`probed_health` answers."""
    url = env_url()
    token = env_token()
    ssl_ctx = None if is_unix(url) else ssl_context()
    return asyncio.run(fetch_health(url, token, transport, ssl_ctx))


def probed_health(transport) -> dict | None:
    """One best-effort /health dial (#706): every failure answers
    its own shape — no daemon configured is silence, a Ctrl-C is
    the one-line interrupt the command bodies print (never
    typer's silent 130 — the probe's connect window is exactly
    when an impatient operator presses), and anything else the
    probe touches (a broken MSKSC_CAFILE, a context that will
    not build) is silence — the command body meets the same
    break itself moments later."""
    try:
        return dialed_health(transport)
    except SystemExit:
        return None
    except KeyboardInterrupt:
        print("msks: interrupted", file=sys.stderr)
        raise SystemExit(130) from None
    except Exception:
        # A best-effort probe keeps its raw catch: any failure the
        # probe touches is the caller's silence (the docstring
        # names the family), never its refusal.
        return None


def notice_daemon_version_skew(transport=None) -> None:
    """Print the version-skew notice when the configured daemon
    answers one (#706): one best-effort ``/health`` roundtrip per
    invocation on a probe-sized budget, every failure silent.
    ``transport`` is the tests' in-process seam."""
    health = probed_health(transport)
    if notice := version_skew_notice(health):
        print(notice, file=sys.stderr)


def cmd_ls(as_json: bool = False, transport=None) -> int:
    """``msks ls``: every workspace the daemon knows."""

    async def fetch() -> tuple[list, dict | None]:
        rows = await call("GET", "/api/v1/workspaces", transport=transport)
        health = None
        if os.environ.get("MSKSC_EXPECTED_IMAGE"):
            # Best-effort: an unreachable /health never hides the
            # listing itself.
            with contextlib.suppress(SystemExit, Exception):
                health = await call(
                    "GET", "/api/v1/health", transport=transport
                )
        return rows, health

    rows, health = asyncio.run(fetch())
    text = render_ls(rows, as_json)
    if text:
        print(text)
    if notice := stale_image_notice(
        os.environ.get("MSKSC_EXPECTED_IMAGE", ""), health
    ):
        print(notice, file=sys.stderr)
    return 0
