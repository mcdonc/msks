"""The ``msks`` CLI: ``ls``, ``create``, ``start``, ``stop``, ``rm``,
``resize``, ``console``, ``forward``, ``ssh``, ``rsync``, ``key``,
``storage``, the ``image`` catalog subcommands, the ``home``
volume moves, and the ``tui`` workspace tree (#309).

Every command speaks the daemon's REST surface with the same client
conventions (#21): ``MSKSC_URL`` for the daemon, ``MSKSC_TOKEN`` for
a bearer token, ``MSKSC_CAFILE`` to pin the certificate. The
interactive console command lives in :mod:`msks.client.console`.
"""

import asyncio
import contextlib
import enum
import functools
import json
import os
import sys
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import typer

try:
    # Typer vendors its own click (the 0.16+ line); its exceptions
    # are the ones a parse of this app raises. Older typer rides
    # the installed click instead — the pyproject floor spans both.
    from typer._click.exceptions import UsageError
except ImportError:  # pragma: no cover — the floor spans both eras
    from click.exceptions import UsageError

from ..conformance_args import (
    CHECK_ARCHIVE_HELP,
    CHECK_BOOT_TIMEOUT_HELP,
    CHECK_EGRESS_HELP,
    CHECK_KEEP_HELP,
    CHECK_SHUTDOWN_TIMEOUT_HELP,
    CHECK_UPLINK_HELP,
    CheckOptions,
)
from ..identity import KEY_TYPES
from ..imagestore import is_hash_shape, version_key
from ..storage import MIB
from . import egress as egress_mod
from .config import ClientConfig, bootstrap
from .console import run_workspace_shell

# Re-exported for the tests (they drive cli.write_client_identity
# directly); cli itself now calls it inside create_workspace_core.
from .create import (  # noqa: F401
    checked_login_name,
    create_workspace_core,
    invoking_user,
    operator_pubkey,
    write_client_identity,
)
from .forward import run_workspace_forward
from .rest import (
    STREAM_WINDOW_B,
    api_call,
    api_client,
    download,
    env_token,
    env_url,
    request,
    ssl_context,
    upload,
)
from .rest import (
    fetch_llm_token as rest_fetch_llm_token,
)
from .rest import (
    fetch_ssh_key as rest_fetch_ssh_key,
)
from .rsync import run_workspace_rsync
from .ssh import IDENTITY_FILE_ENV, data_dir, run_workspace_ssh
from .tabular import listing_text
from .tui.consent_app import run_consent_tui
from .tui.main_app import run_main_tui


def _created_date(raw: str | None) -> str:
    """ISO timestamp to YYYY-MM-DD, or ``-``."""
    return raw[:10] if raw else "-"


def workspace_cells(row: dict) -> list[str]:
    """One listing row's cells."""
    image = (row.get("image_hash") or "-")[:12]
    host = row.get("host") or "-"
    name = row.get("name") or "-"
    egress_mode = row.get("egress_mode") or "-"
    created = _created_date(row.get("created_at"))
    return [name, row["id"], row["status"], egress_mode, created, image, host]


def display_name(row: dict) -> str:
    """The human-facing label (#246): the workspace's name, else
    its immutable id (a nameless workspace is addressed by id)."""
    return row.get("name") or row["id"]


def render_ls(rows: list[dict], as_json: bool) -> str:
    """The whole listing: the aligned table, or one JSON document."""
    if as_json:
        return json.dumps(rows, indent=2)
    return listing_text(
        ["name", "id", "status", "egress", "created", "image", "host"],
        [workspace_cells(row) for row in rows],
    )


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


def cmd_ls(as_json: bool = False, transport=None) -> int:
    """``msks ls``: every workspace the daemon knows."""

    async def fetch() -> tuple[list, dict | None]:
        rows = await api_call(
            "GET",
            env_url(),
            env_token(),
            "/api/v1/workspaces",
            transport=transport,
        )
        health = None
        if os.environ.get("MSKSC_EXPECTED_IMAGE"):
            # Best-effort: an unreachable /health never hides the
            # listing itself.
            with contextlib.suppress(SystemExit, Exception):
                health = await api_call(
                    "GET",
                    env_url(),
                    env_token(),
                    "/api/v1/health",
                    transport=transport,
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


def cmd_create(
    body: dict,
    start: bool = False,
    transport=None,
    key_type: str | None = None,
    pubkey: str | None = None,
    identity_note: str | None = None,
) -> int:
    """``msks create``: one workspace, optionally booted.

    ``key_type`` names the per-workspace client-mint mode (#121,
    ``--key-type``): minted locally, public half sent, private half
    kept. ``pubkey`` is an operator-supplied public line (#132,
    ``--pubkey`` and the operator-key default #336): sent as-is, no
    private half anywhere msks manages. ``identity_note`` is the
    operator-key default's confirmation line (the identity source
    the create planted).
    """
    asyncio.run(
        create_workspace(
            env_url(),
            env_token(),
            body,
            start,
            transport,
            key_type,
            pubkey,
            identity_note,
        )
    )
    return 0


async def create_workspace(
    url,
    token,
    body,
    start,
    transport,
    key_type: str | None = None,
    pubkey: str | None = None,
    identity_note: str | None = None,
) -> dict:
    """POST the workspace, print its name and id, then boot it when
    asked.

    The created line prints the moment the workspace exists (the
    core's ``announce`` hook): a failed verification or start must
    not hide that the workspace exists — recover with ``msks
    start``. The daemon mints the workspace's immutable id (#246);
    the follow-up calls (boot, identity) address the workspace by
    that id, and the label the operator typed stays the day-to-day
    reference. In the per-workspace client-mint mode (#121,
    ``--key-type``) the keypair is minted by
    :func:`create_workspace_core` — the private half never crosses
    the wire — and is persisted (mode 0600, client data root,
    keyed on the id) only after the create succeeded, so a refused
    create leaves no orphaned key behind. With an
    operator-supplied key (#132, and the operator-key default
    #336) only the public line travels and nothing is written
    client-side: the private half stays wherever the operator
    keeps it, read in place. ``identity_note`` (the operator-key
    default's source line) prints after the created line.
    """
    # One TLS context serves the create and the boot (an unverified
    # daemon warns once per context — the pair must not warn twice).
    ssl_ctx = None if transport is not None else ssl_context()
    row, path = await create_workspace_core(
        url,
        token,
        body,
        transport,
        ssl_ctx=ssl_ctx,
        key_type=key_type,
        pubkey=pubkey,
        announce=print_created_line,
    )
    if identity_note is not None:
        print(identity_note)
    if path is not None:
        print(f"client identity (mode 0600): {path}")
    if not start:
        return row
    await boot_created(url, token, row, transport, ssl_ctx)
    return row


def print_created_line(row: dict) -> None:
    """The core's ``announce`` hook: the created line prints the
    moment the workspace exists, before any follow-up check can
    refuse — a refused verification still leaves the line (and the
    workspace) on the record."""
    print(created_line(row))


async def boot_created(url, token, row: dict, transport, ssl_ctx=None) -> None:
    """Boot the freshly created row, with the recovery hint on a
    failed start (the workspace exists; the hint names the command
    that reaches it later); the caller's TLS context rides along,
    keeping the unverified-mode warning to the pair's one print."""
    try:
        async with api_client(url, token, transport, ssl_ctx) as client:
            await request(
                client, "POST", f"/api/v1/workspaces/{row['id']}/start"
            )
    except SystemExit as exc:
        raise SystemExit(
            f"{exc}\nmsks: {row['id']} is created; "
            f"boot it later with: msks start {display_name(row)}"
        ) from exc
    print(f"attach with: msks console {display_name(row)}")
    row["status"] = "running"


def created_line(row: dict) -> str:
    """The create confirmation: the label beside the daemon-minted,
    immutable id (#246) — the name is the everyday reference, the
    id is the one no future workspace will ever reuse."""
    name = row.get("name")
    if name and name != row["id"]:
        return f"created {name} (id {row['id']})"
    return f"created {row['id']}"


def cmd_start(workspace_id: str, transport=None) -> int:
    """``msks start``: boot a created workspace."""
    row = asyncio.run(
        api_call(
            "POST",
            env_url(),
            env_token(),
            f"/api/v1/workspaces/{workspace_id}/start",
            transport=transport,
        )
    )
    print(f"{workspace_id} {row['status']}")
    return 0


def cmd_stop(workspace_id: str, transport=None) -> int:
    """``msks stop``: power a workspace off, gracefully."""
    row = asyncio.run(
        api_call(
            "POST",
            env_url(),
            env_token(),
            f"/api/v1/workspaces/{workspace_id}/stop",
            transport=transport,
        )
    )
    print(f"{workspace_id} {row['status']}")
    return 0


def cmd_rm(workspace_ids: list[str], transport=None) -> int:
    """``msks rm``: delete workspaces and their persistent data.

    Ids are removed one at a time, in order; a failure stops the
    run with the API's one-line error (already-removed ids stay
    removed — and stay confirmed on stdout).
    """
    url, token = env_url(), env_token()
    for workspace_id in workspace_ids:
        asyncio.run(
            api_call(
                "DELETE",
                url,
                token,
                f"/api/v1/workspaces/{workspace_id}",
                transport=transport,
            )
        )
        print(f"{workspace_id} deleted")
    return 0


async def fetch_ssh_key(url, token, workspace_id, transport) -> dict:
    """GET the workspace's identity: type, public half, private half
    (null for a client-minted workspace, #121).

    A re-export of :func:`msks.client.rest.fetch_ssh_key` (the call
    moved to rest.py when ``msks ssh`` (#112) began sharing it);
    ``msks key`` prints it, ``msks ssh`` stages the private half in
    memory for the duration of a connection.
    """
    return await rest_fetch_ssh_key(url, token, workspace_id, transport)


def cmd_llm_token(
    workspace_id: str, remint: bool = False, transport=None
) -> int:
    """``msks llm-token``: the workspace's LLM proxy credential
    (#259) — the token the workspace's own LLM clients present to
    the daemon's proxy. A null token is a workspace created before
    the proxy existed; ``--remint`` mints a fresh one (the seed's
    planted copy keeps the old token — export the new one by
    hand)."""
    url = env_url()
    token = env_token()
    if remint:
        reply = asyncio.run(
            api_call(
                "POST",
                url,
                token,
                f"/api/v1/workspaces/{workspace_id}/llm-token",
                transport=transport,
            )
        )
        print(reply["token"])
        return 0
    reply = asyncio.run(
        rest_fetch_llm_token(url, token, workspace_id, transport)
    )
    if reply["token"] is None:
        raise SystemExit(
            f"msks: workspace {workspace_id} has no LLM token (created "
            "before the proxy); remint one with --remint"
        )
    print(reply["token"])
    return 0


def write_private_key(key: dict, out: str) -> None:
    """Materialize the private half at ``out``, mode 0600 (#111).

    The mode is forced on every write, an existing file included —
    open()'s mode argument only applies at creation, and a
    pre-existing group- or world-readable file must not stay that
    way under new contents. The path is always the operator's own
    choice: the command writes the key to the file the operator
    named and to stdout, and nowhere else.
    """
    try:
        fd = os.open(out, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    except OSError as exc:
        raise SystemExit(f"msks: cannot write key file {out}: {exc}") from None
    os.fchmod(fd, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(key["private_key"])


def require_daemon_half(key: dict, workspace_id: str) -> None:
    """Refuse the private forms for a workspace whose private half
    the daemon never held (#121, #132, #336): the error names the
    places that half can be instead of printing nothing."""
    if key["private_key"] is None:
        directory = key.get("id") or workspace_id
        raise SystemExit(
            f"msks key: the daemon holds no private half for {workspace_id}. "
            "The workspace's key was minted on a client (its private half "
            f"lives at {data_dir() / directory / 'identity'} on that "
            "machine), or it is the operator's own key — point identity_file "
            f"(or {IDENTITY_FILE_ENV}) at its private file, or use that "
            "key directly"
        )


def cmd_key(
    workspace_id: str,
    as_private: bool = False,
    out: str | None = None,
    transport=None,
) -> int:
    """``msks key``: the workspace's ssh identity.

    Prints the public half (safe to display anywhere); ``--private``
    prints the private half, ``--out`` writes the private half to a
    file with mode 0600 and prints nothing but its path. A
    client-minted workspace (#121) serves its public half; its
    private half never reached the daemon, so the private forms
    explain where that half lives instead.
    """
    key = asyncio.run(
        fetch_ssh_key(env_url(), env_token(), workspace_id, transport)
    )
    if as_private or out is not None:
        require_daemon_half(key, workspace_id)
    if out is not None:
        write_private_key(key, out)
        print(out)
    elif as_private:
        print(key["private_key"], end="")
    else:
        print(key["public_key"])
    return 0


# --- The image catalog (#65) ---

HEX_DIGITS = set("0123456789abcdef")

#: How many refs an error line spells out before "… (+N more)".
CATALOG_REF_CAP = 8


def human_bytes(count: int) -> str:
    """Bytes in binary units: 512K, 812M, 23.4G — a whole number
    whenever the decimal would add nothing (2G, 40G)."""
    gib = count / 1024**3
    if gib >= 1:
        tenths = round(gib, 1)
        if tenths == int(tenths):
            return f"{int(tenths)}G"
        return f"{tenths:.1f}G"
    if count >= 1024**2:
        return f"{round(count / 1024**2)}M"
    return f"{max(count // 1024, 0)}K"


def cost_pair(cost_bytes: int, ceiling_mib: int) -> str:
    """One cost/ceiling cell: the blocks a workspace pays beside the
    virtual size its guest sees as the quota."""
    return f"{human_bytes(cost_bytes)} / {human_bytes(ceiling_mib * MIB)}"


def state_line(state: dict) -> str:
    """The budget line: what the state disk holds and how close it is
    to the named pressure condition."""
    return (
        f"state disk    used {human_bytes(state['used'])} "
        f"of {human_bytes(state['total'])}    "
        f"free {human_bytes(state['free'])}    pressure {state['pressure']}"
    )


def render_storage(
    report: dict, as_json: bool, workspace_id: str | None = None
) -> str:
    """The whole capacity report: budget line, per-workspace
    cost/ceiling table, catalog costs — or one JSON document."""
    if as_json:
        return json.dumps(report, indent=2)
    parts = [state_line(report["state"])]
    workspaces = workspace_table(narrowed(report["workspaces"], workspace_id))
    if workspaces:
        parts.append(workspaces)
    if images := image_cost_table(report["images"]):
        parts.append(images)
    return "\n\n".join(parts)


def storage_cells(ws: dict) -> list[str]:
    """One workspace row's cells: label, both cost/ceiling cells,
    total cost — the name is the human key (#246), the id rides in
    the ``--json`` document."""
    return [
        display_name(ws),
        cost_pair(ws["root_bytes"], ws["root_mib"]),
        cost_pair(ws["home_bytes"], ws["home_mib"]),
        human_bytes(ws["root_bytes"] + ws["home_bytes"]),
    ]


def narrowed(workspaces: list[dict], ref: str | None) -> list[dict]:
    """The rows one table shows: every row, or the one workspace the
    ref names — its id or its name (#246)."""
    if ref is None:
        return workspaces
    return [ws for ws in workspaces if ref in (ws["id"], ws.get("name"))]


def workspace_table(workspaces: list[dict]) -> str:
    """The per-workspace cost/ceiling table — empty when none."""
    return listing_text(
        ["workspace", "root cost/ceiling", "home cost/ceiling", "cost"],
        [storage_cells(ws) for ws in workspaces],
    )


def imported_cell(image: dict) -> str:
    """One import-time cell: local time to the minute, or ``-``
    when the daemon predates stamps (#186) or sends a moment the
    clock cannot parse or place."""
    stamp = image.get("imported")
    if not stamp:
        return "-"
    # OverflowError rides extreme stamps: a year-1 moment has no
    # representation in some local zones. TypeError covers a
    # daemon sending a non-string.
    with contextlib.suppress(ValueError, TypeError, OverflowError):
        return (
            datetime.fromisoformat(stamp)
            .astimezone()
            .strftime("%Y-%m-%d %H:%M")
        )
    return "-"


def image_cost_table(images: list[dict]) -> str:
    """The catalog cost table.

    Rows that carry their import time (#186) show it, so entries
    sharing a reference read as distinct; a daemon predating
    stamps keeps the two-column table.
    """
    stamped = any(image.get("imported") for image in images)
    headers = ["image", "imported", "cost"] if stamped else ["image", "cost"]
    rows = [
        [
            f"{image['name']}:{image['version']}",
            *([imported_cell(image)] if stamped else []),
            human_bytes(image["bytes"]),
        ]
        for image in images
    ]
    return listing_text(headers, rows)


def cmd_storage(
    workspace_id: str | None = None,
    as_json: bool = False,
    transport=None,
) -> int:
    """``msks storage``: the state-disk budget and its consumers."""
    report = asyncio.run(
        api_call(
            "GET",
            env_url(),
            env_token(),
            "/api/v1/storage",
            transport=transport,
        )
    )
    if workspace_id is not None and not narrowed(
        report["workspaces"], workspace_id
    ):
        raise SystemExit(f"msks: no such workspace: {workspace_id}")
    print(render_storage(report, as_json, workspace_id))
    return 0


def cmd_resize(
    workspace_id: str,
    body: dict,
    transport=None,
) -> int:
    """``msks resize``: move a stopped workspace's sizes and
    topology (#184, #277)."""
    row = asyncio.run(
        api_call(
            "POST",
            env_url(),
            env_token(),
            f"/api/v1/workspaces/{workspace_id}/resize",
            json_body=body,
            transport=transport,
        )
    )
    print(resize_message(row, body))
    return 0


def resize_boot_note(changes: list[str]) -> str:
    """The parenthesized note for what waits for the next boot:
    empty when nothing does. The daemon's ``changes`` list decides,
    not the request's flags — home bytes moved at once on the host;
    the root's guest-side fill and the new topology apply at the
    next boot."""
    waits = [
        note
        for prefixes, note in (
            (("root",), "the guest fills the larger root"),
            (("cpus", "mem"), "the new topology applies"),
        )
        if any(change.startswith(prefixes) for change in changes)
    ]
    if not waits:
        return ""
    return f" ({' and '.join(waits)} on its next boot)"


def resize_message(row: dict, body: dict) -> str:
    """The result line: the new sizes, plus the topology when the
    request moved it, with the boot note naming what waits for the
    next boot."""
    line = (
        f"resized {display_name(row)}: root {row['root_mib']} MiB, "
        f"home {row['home_mib']} MiB"
    )
    if body.get("cpus") is not None or body.get("mem_mib") is not None:
        line += f", cpus {row['cpus']}, mem {row['mem_mib']} MiB"
    return line + resize_boot_note(row.get("changes", []))


def image_cells(row: dict) -> list[str]:
    """One catalog row's cells: ref, short hash, default flag,
    kernel, and the import time (#283) — the same stamped-or-dash
    cell the storage table renders."""
    flag = "default" if row["default"] else "-"
    kernel = f"{row['kernel_version'] or '-'} ({row['kernel_format'] or '-'})"
    ref = f"{row['name']}:{row['version']}"
    return [ref, row["hash"][:12], flag, kernel, imported_cell(row)]


def render_image_ls(rows: list[dict], as_json: bool) -> str:
    """The whole catalog: the aligned table, or one JSON document."""
    if as_json:
        return json.dumps(rows, indent=2)
    return listing_text(
        ["ref", "hash", "default", "kernel", "imported"],
        [image_cells(row) for row in rows],
    )


async def fetch_images(url, token, transport) -> list[dict]:
    """The catalog listing (GET /api/v1/images)."""
    return await api_call(
        "GET", url, token, "/api/v1/images", transport=transport
    )


async def import_image(url, token, source, transport) -> dict:
    """POST the import; print the registered reference and hash."""
    record = await api_call(
        "POST",
        url,
        token,
        "/api/v1/images",
        json_body={"source": source},
        transport=transport,
    )
    print(f"imported {record['ref']} ({record['hash'][:12]})")
    return record


async def remove_image(url, token, ref, transport) -> dict:
    """Resolve ``ref`` against the listing, DELETE by hash.

    Resolution and delete share one client so a refusal (409 names
    the workspace that boots the image) reads as one exchange.
    """
    async with api_client(url, token, transport) as client:
        rows = await request(client, "GET", "/api/v1/images")
        row = resolve_image_ref(ref, rows)
        result = await request(
            client, "DELETE", f"/api/v1/images/{row['hash']}"
        )
    print(f"{row['name']}:{row['version']} deleted")
    return result


async def designate_image(url, token, ref, transport) -> dict:
    """Resolve ``ref`` against the listing, POST the designation
    (#270) — the rm shape: resolution and designation share one
    client, and every reference form (a unique hash prefix, an
    ambiguous one named) reads as one exchange.
    """
    async with api_client(url, token, transport) as client:
        rows = await request(client, "GET", "/api/v1/images")
        row = resolve_image_ref(ref, rows)
        result = await request(
            client,
            "POST",
            "/api/v1/images/default",
            json_body={"ref": row["hash"]},
        )
    print(
        f"designated {row['name']}:{row['version']} "
        f"({row['hash'][:12]}) as the default image"
    )
    return result


async def unset_default_image(url, token, transport) -> dict:
    """DELETE the designation; report the fallback that applies
    (#270) — the sole catalog entry a bare create still boots, or
    the named refusal when nothing answers for one.
    """
    result = await api_call(
        "DELETE",
        url,
        token,
        "/api/v1/images/default",
        transport=transport,
    )
    print(unset_message(result))
    return result


def unset_message(result: dict) -> str:
    """The result line: the designation is gone, and the line names
    what a bare create boots now — the sole entry the fallback
    rule picks, or the need for an explicit --image."""
    fallback = result.get("fallback")
    if fallback is None:
        return (
            "default designation removed; a bare create needs --image "
            "until an image is designated"
        )
    return (
        f"default designation removed; a bare create falls back to "
        f"the sole entry {fallback['ref']} ({fallback['hash'][:12]})"
    )


async def describe_image(url, token, ref, transport) -> dict:
    """Print one image's full record from the listing data."""
    rows = await fetch_images(url, token, transport)
    row = resolve_image_ref(ref, rows)
    print("\n".join(info_lines(row)))
    return row


def info_pairs(row: dict) -> list[list[str]]:
    """The record's label/value pairs: boot facts the listing
    carries."""
    default = "yes" if row["default"] else "no"
    kernel = f"{row['kernel_version'] or '-'} ({row['kernel_format'] or '-'})"
    provisioner = row.get("provisioner") or "- (none declared)"
    return [
        ("ref", f"{row['name']}:{row['version']}"),
        ("hash", row["hash"]),
        ("kernel", kernel),
        ("cmdline", row["cmdline"]),
        ("console", f"vsock port {row['vsock_shell_port']}"),
        ("seed", f"provisioner {provisioner}"),
        ("default", default),
    ]


def info_lines(row: dict) -> list[str]:
    """The full record, the label column aligned the same way every
    listing aligns (#271)."""
    return listing_text(None, info_pairs(row)).splitlines()


def resolve_image_ref(ref: str, rows: list[dict]) -> dict:
    """The one listing row a catalog reference points at.

    Accepts the daemon's forms — hash, ``name@hash``,
    ``name:version``, bare ``name`` (newest version) — plus a unique
    hash prefix (``ls`` prints 12 chars). A miss or an ambiguous
    prefix exits with the catalog spelled out.
    """
    matches = image_ref_matches(ref, rows)
    if not matches:
        raise SystemExit(no_image_message(ref, rows))
    if len(matches) > 1:
        raise SystemExit(ambiguous_image_message(ref, matches))
    return matches[0]


def image_ref_matches(ref: str, rows: list[dict]) -> list[dict]:
    """Dispatch on the reference's shape, mirroring the daemon's
    precedence (:mod:`msks.imagestore` ``resolve``): @, :, full
    hash, then bare token."""
    if "@" in ref:
        return pin_matches(ref, rows)
    if ":" in ref:
        return name_version_matches(ref, rows)
    if is_hash_shape(ref):
        return hash_prefix_matches(rows, ref)
    return bare_ref_matches(ref, rows)


def bare_ref_matches(ref: str, rows: list[dict]) -> list[dict]:
    """A bare token: a catalog name resolves by name (a hex-looking
    name must not be captured as a hash prefix — the daemon resolves
    it by name); anything else may be a unique hash prefix. All rows
    tied at the newest version return together: two imports of the
    same name:version (a rebuilt archive) surface as the ambiguity
    they are, never a silent arbitrary pick."""
    named = [row for row in rows if row["name"] == ref]
    if named:
        return top_version_rows(named)
    return hash_prefix_matches(rows, ref)


def top_version_rows(candidates: list[dict]) -> list[dict]:
    """The candidates sitting at the newest version — the daemon's
    ordering (``imagestore.version_key``), so client and daemon
    agree on what "newest" means."""
    top = max(version_key(row["version"]) for row in candidates)
    return [row for row in candidates if version_key(row["version"]) == top]


def pin_matches(ref: str, rows: list[dict]) -> list[dict]:
    """``name@hash``: identity and content both pinned."""
    name, _, digest = ref.partition("@")
    require_hash_digest(ref, digest)
    return [
        row
        for row in rows
        if row["name"] == name and row["hash"].startswith(digest)
    ]


def require_hash_digest(ref: str, digest: str) -> None:
    """A pin's hash part is a full 64-hex digest (the daemon's
    ``is_hash_shape``); anything else is a named error."""
    if not is_hash_shape(digest):
        raise SystemExit(f"msks: malformed image hash in {ref!r}")


def name_version_matches(ref: str, rows: list[dict]) -> list[dict]:
    """``name:version``: the exact pair."""
    name, _, version = ref.partition(":")
    return [
        row
        for row in rows
        if row["name"] == name and row["version"] == version
    ]


def hash_prefix_matches(rows: list[dict], prefix: str) -> list[dict]:
    """Hashes the prefix selects; empty and non-hex select nothing
    (an empty reference must not match every row)."""
    if not prefix or not set(prefix) <= HEX_DIGITS:
        return []
    return [row for row in rows if row["hash"].startswith(prefix)]


def catalog_refs(rows: list[dict]) -> str:
    """The catalog (or a match set) spelled out for an error, capped
    so a large catalog stays one readable line."""
    refs = [f"{row['name']}:{row['version']}" for row in rows]
    shown = ", ".join(refs[:CATALOG_REF_CAP])
    if len(refs) <= CATALOG_REF_CAP:
        return shown or "(the catalog is empty)"
    return f"{shown}, … (+{len(refs) - CATALOG_REF_CAP} more)"


def no_image_message(ref: str, rows: list[dict]) -> str:
    return f"msks: no image matches {ref!r} — catalog: {catalog_refs(rows)}"


def ambiguous_image_message(ref: str, matches: list[dict]) -> str:
    # name:version is deliberately absent from the advice: the usual
    # ambiguity is two imports of the same name:version (a rebuilt
    # archive), where only the hash forms still identify one image;
    # the matches therefore carry their short hashes.
    return (
        f"msks: {ref!r} matches {len(matches)} images "
        f"({match_refs(matches)}); use the full hash or name@hash"
    )


def match_refs(matches: list[dict]) -> str:
    """The matched images with short hashes — when the refs read the
    same (re-imported archive), the hashes are the discriminator."""
    return ", ".join(
        f"{row['name']}:{row['version']} ({row['hash'][:12]})"
        for row in matches
    )


def read_secret(path: str) -> str:
    """The #198 payload: a file's contents, or stdin for ``-``.

    Whitespace-stripped at both ends — a token file's trailing
    newline (or a password manager's) is not part of the secret —
    and never accepted as a command-line argument, which lands in
    process lists and shell history.
    """
    try:
        if path == "-":
            if sys.stdin.isatty():
                raise SystemExit(
                    "msks: --secret-file - expects the secret on stdin "
                    "(pipe it in; it is never read interactively)"
                )
            text = sys.stdin.read()
        else:
            text = Path(path).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise SystemExit(
            f"msks: cannot read secret file {path}: {exc}"
        ) from None
    value = text.strip()
    if not value:
        raise SystemExit("msks: the secret file is empty")
    return value


async def find_placeholder(
    url: str, token: str, ref: str, name: str, transport
) -> dict:
    """The (workspace, name) pair's row, or a named exit.

    ``ref`` names the workspace by id or name (#246); the listing's
    workspace rows carry both, and the placeholder row binds to the
    immutable id, so the ref resolves against the listing before
    the pair matches.
    """
    workspaces = await api_call(
        "GET", url, token, "/api/v1/workspaces", transport=transport
    )
    workspace_id = resolved_workspace_id(workspaces, ref)
    rows = await api_call(
        "GET", url, token, "/api/v1/secrets", transport=transport
    )
    for row in rows:
        if row["workspace_id"] == workspace_id and row["name"] == name:
            return row
    raise SystemExit(f"msks: no placeholder {name} on workspace {ref}")


def resolved_workspace_id(workspaces: list[dict], ref: str) -> str:
    """The immutable id the ref names (#246), or the named exit
    when no workspace answers to it."""
    for row in workspaces:
        if ref in (row["id"], row.get("name")):
            return row["id"]
    raise SystemExit(f"msks: no such workspace: {ref}")


def cmd_secret_mint(
    workspace_id: str,
    name: str,
    dests: list[str],
    ttl: int | None,
    secret_file: str,
    transport=None,
) -> int:
    """``msks secret mint``: one step; prints the sentinel once."""
    secret = read_secret(secret_file)
    body = {
        "workspace_id": workspace_id,
        "name": name,
        "dests": dests,
        "secret": secret,
    }
    if ttl is not None:
        body["ttl_s"] = ttl
    row = asyncio.run(
        api_call(
            "POST",
            env_url(),
            env_token(),
            "/api/v1/secrets",
            json_body=body,
            transport=transport,
        )
    )
    print(f"minted {workspace_id}/{name} for {', '.join(row['dests'])}")
    print(f"sentinel (shown once): {row['sentinel']}")
    return 0


def secret_cells(row: dict) -> list[str]:
    """One placeholder row's cells: id, workspace/name,
    destinations, expiry."""
    return [
        str(row["id"]),
        f"{row['workspace_id']}/{row['name']}",
        ", ".join(row["dests"]),
        row["expires_at"] or "never",
    ]


def cmd_secret_ls(as_json: bool = False, transport=None) -> int:
    """``msks secret ls``: every placeholder, no sentinels."""
    rows = asyncio.run(
        api_call(
            "GET",
            env_url(),
            env_token(),
            "/api/v1/secrets",
            transport=transport,
        )
    )
    if as_json:
        print(json.dumps(rows, indent=2))
        return 0
    text = listing_text(
        ["id", "workspace", "destinations", "expires"],
        [secret_cells(row) for row in rows],
    )
    if text:
        print(text)
    return 0


def cmd_secret_revoke(workspace_id: str, name: str, transport=None) -> int:
    """``msks secret revoke``: effective on the next request."""
    url, token = env_url(), env_token()
    row = asyncio.run(
        find_placeholder(url, token, workspace_id, name, transport)
    )
    result = asyncio.run(
        api_call(
            "DELETE",
            url,
            token,
            f"/api/v1/secrets/{row['id']}",
            transport=transport,
        )
    )
    suffix = (
        ""
        if result.get("store_cleaned", True)
        else (" (store value left behind; msks secret check reports it)")
    )
    print(f"revoked {workspace_id}/{name}{suffix}")
    return 0


def cmd_secret_renew(
    workspace_id: str, name: str, ttl: int, transport=None
) -> int:
    """``msks secret renew``: extends in place, sentinel unchanged."""
    url, token = env_url(), env_token()
    row = asyncio.run(
        find_placeholder(url, token, workspace_id, name, transport)
    )
    updated = asyncio.run(
        api_call(
            "POST",
            url,
            token,
            f"/api/v1/secrets/{row['id']}/renew",
            json_body={"ttl_s": ttl},
            transport=transport,
        )
    )
    print(f"renewed {workspace_id}/{name}; expires {updated['expires_at']}")
    return 0


def cmd_secret_check(transport=None) -> int:
    """``msks secret check``: the configured store answers writes."""
    result = asyncio.run(
        api_call(
            "POST",
            env_url(),
            env_token(),
            "/api/v1/secrets/check",
            transport=transport,
        )
    )
    print(f"secret store ({result['provider']}): ok")
    return 0


def cmd_image_ls(as_json: bool = False, transport=None) -> int:
    """``msks image ls``: the whole catalog, default marked."""
    rows = asyncio.run(fetch_images(env_url(), env_token(), transport))
    text = render_image_ls(rows, as_json)
    if text:
        print(text)
    return 0


def cmd_image_import(source: str, transport=None) -> int:
    """``msks image import``: register a daemon-side archive or
    fetch one from an https:// URL (#258)."""
    asyncio.run(import_image(env_url(), env_token(), source, transport))
    return 0


def cmd_image_check(args: CheckOptions) -> int:
    """``msks image check``: the local conformance pass (#258).

    The import stays inside the command: conformance composes the
    daemon's app (msks.app), and a module-scope import would load
    the whole server stack into every ``msks`` invocation — the
    client/server boundary is a standing decision, so this is the
    one deliberate deferral in the client.
    """
    from .. import conformance  # allow-deferred-import

    return conformance.run_check(args)


def cmd_image_rm(ref: str, transport=None) -> int:
    """``msks image rm``: drop one image, by any reference form."""
    asyncio.run(remove_image(env_url(), env_token(), ref, transport))
    return 0


def cmd_image_info(ref: str, transport=None) -> int:
    """``msks image info``: one image's full record."""
    asyncio.run(describe_image(env_url(), env_token(), ref, transport))
    return 0


def checked_default_args(ref: str | None, unset: bool) -> None:
    """Reject the argument shapes that say nothing — both a
    reference and --unset, or neither (#270)."""
    if unset and ref is not None:
        raise SystemExit("msks: pass a reference or --unset, not both")
    if not unset and ref is None:
        raise SystemExit(
            "msks: pass an image reference, or --unset to clear "
            "the designation"
        )


def cmd_image_default(
    ref: str | None, unset: bool = False, transport=None
) -> int:
    """``msks image default``: designate the image a bare create
    boots (#270), or clear the designation with --unset."""
    checked_default_args(ref, unset)
    if unset:
        asyncio.run(unset_default_image(env_url(), env_token(), transport))
    else:
        asyncio.run(designate_image(env_url(), env_token(), ref, transport))
    return 0


# --- Home-volume export/import (#80) ---


def home_path(workspace_id: str) -> str:
    """The workspace's home-volume byte-stream endpoint."""
    return f"/api/v1/workspaces/{workspace_id}/home"


async def run_home_export(
    url: str, token: str, workspace_id: str, out: str, transport
) -> int:
    """GET the volume stream and write it to ``out``; the bytes."""
    async with api_client(url, token, transport) as client:
        if out == "-":
            return await download(
                client, home_path(workspace_id), sys.stdout.buffer
            )
        try:
            with open(out, "wb") as sink:
                return await download(client, home_path(workspace_id), sink)
        except OSError as exc:
            raise SystemExit(f"msks: cannot write {out}: {exc}") from None


def volume_source(path: str):
    """The opened upload body: stdin for ``-``, else the file.

    Opening happens here, not inside the stream: an unreadable
    source fails with one line before any network activity.
    """
    if path == "-":
        return sys.stdin.buffer
    try:
        return open(path, "rb")
    except OSError as exc:
        raise SystemExit(
            f"msks: cannot read volume image {path}: {exc}"
        ) from None


async def file_windows(source) -> AsyncIterator[bytes]:
    """Yield an open binary source's bytes in stream windows.

    Reads run off the event loop; stdin keeps its shell-owned
    lifecycle (only the file the command opened is closed).
    """
    try:
        while window := await asyncio.to_thread(source.read, STREAM_WINDOW_B):
            yield window
    finally:
        if source is not sys.stdin.buffer:
            source.close()


async def run_home_import(
    url: str, token: str, workspace_id: str, source_path: str, transport
) -> int:
    """PUT the volume stream; the daemon's reported byte count.

    The source opens here and closes in the ``finally`` even when
    the exchange dies before the body is consumed (a dead dial):
    the file never waits for garbage collection.
    """
    source = volume_source(source_path)
    owns = source is not sys.stdin.buffer
    try:
        async with api_client(url, token, transport) as client:
            reply = await upload(
                client, home_path(workspace_id), file_windows(source)
            )
        return imported_bytes(reply)
    finally:
        if owns:
            source.close()


def imported_bytes(reply: dict) -> int:
    """The reply's byte count, or the one-line refusal when a 2xx
    answer carries none (a daemon that is not this protocol)."""
    count = reply.get("bytes")
    if not isinstance(count, int):
        raise SystemExit(
            "msks: the daemon's import reply carried no byte count"
        )
    return count


def cmd_home_export(
    workspace_id: str, out: str | None = None, transport=None
) -> int:
    """``msks home export``: download a workspace's /home volume."""
    target = out if out is not None else f"{workspace_id}.ext4"
    try:
        total = asyncio.run(
            run_home_export(
                env_url(), env_token(), workspace_id, target, transport
            )
        )
    except BrokenPipeError:
        raise SystemExit(broken_pipe_line(target)) from None
    if target == "-":
        # Bytes own stdout; the confirmation goes to stderr.
        print(
            f"msks: exported {workspace_id} ({total} bytes)", file=sys.stderr
        )
    else:
        print(f"exported {workspace_id} ({total} bytes) to {target}")
    return 0


def broken_pipe_line(target: str) -> str:
    """The one-line report for a reader that went away (#80).

    ``- | head`` or a downstream compressor on a full disk closes
    the pipe mid-stream; stdout is unusable from here on, so its
    buffered remains are pointed at devnull before the interpreter
    flush would traceback on them again at exit.
    """
    with contextlib.suppress(OSError, ValueError, AttributeError):
        devnull = os.open(os.devnull, os.O_WRONLY)
        os.dup2(devnull, sys.stdout.fileno())
        os.close(devnull)
    return f"msks: the export's reader closed early ({target})"


def cmd_home_import(
    workspace_id: str, source_path: str, transport=None
) -> int:
    """``msks home import``: replace a workspace's /home volume."""
    url, token = env_url(), env_token()
    total = asyncio.run(
        run_home_import(url, token, workspace_id, source_path, transport)
    )
    print(f"imported {total} bytes into {workspace_id}")
    return 0


def read_user_data(path: str) -> str:
    """The #41 payload: a file's contents, or stdin for ``-``."""
    try:
        if path == "-":
            return sys.stdin.read()
        return Path(path).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise SystemExit(
            f"msks: cannot read user-data file {path}: {exc}"
        ) from None


def create_body(args: CreateFlags) -> dict:
    """The POST body: only the fields the operator set."""
    fields = {
        "name": args.workspace_id,
        "image": args.image,
        "kernel": args.kernel,
        "initrd": args.initrd,
        "rootfs": args.rootfs,
        "cmdline": args.cmdline,
        "cpus": args.cpus,
        "mem_mib": args.mem_mib,
        "root_mib": args.root_mib,
        "home_mib": args.home_mib,
    }
    body = {name: value for name, value in fields.items() if value is not None}
    if args.egress is not None:
        body["egress"] = args.egress
    body.update(consent_fields(args))
    if args.user_data is not None:
        body["user_data"] = read_user_data(args.user_data)
    body["user"] = create_user(args)
    return body


def create_user(args: CreateFlags) -> str:
    """The workspace's login user (#248): the explicit ``--user``, or
    the invoking user's name — checked before the wire so a bad name
    is one local line, not the daemon's pattern error."""
    if args.user is not None:
        return checked_login_name(args.user, "--user")
    return invoking_user()


def consent_fields(args: CreateFlags) -> dict:
    """The egress-consent create fields the operator set (#69):
    the mode and the repeated allowlist entries."""
    fields = {}
    if getattr(args, "egress_mode", None) is not None:
        fields["egress_mode"] = args.egress_mode
    if getattr(args, "allow", None):
        fields["egress_allowlist"] = args.allow
    return fields


@dataclass
class CreateFlags:
    """The create command's parsed surface (#315's typer layer fills
    one): the flag-to-body mapping and the identity resolver read
    the same attribute shape the argparse era carried."""

    workspace_id: str
    image: str | None = None
    kernel: str | None = None
    initrd: str | None = None
    rootfs: str | None = None
    cmdline: str | None = None
    cpus: int | None = None
    mem_mib: int | None = None
    root_mib: int | None = None
    home_mib: int | None = None
    egress: bool | None = None
    egress_mode: str | None = None
    allow: list[str] | None = None
    user_data: str | None = None
    user: str | None = None
    daemon_mint: bool = False
    pubkey: str | None = None
    key_type: str | None = None
    start: bool = False


class PostureChoice(enum.StrEnum):
    """The egress consent postures (#69)."""

    allow = "allow"
    static = "static"
    interactive = "interactive"


class DecisionFilter(enum.StrEnum):
    """The consent row lifecycle states."""

    pending = "pending"
    allowed = "allowed"
    denied = "denied"
    expired = "expired"
    revoked = "revoked"


class VerdictChoice(enum.StrEnum):
    """The decider's two verdicts."""

    allow = "allow"
    deny = "deny"


class DurationChoice(enum.StrEnum):
    """How long enforcement honors a verdict."""

    once = "once"
    five_m = "5m"
    fifteen_m = "15m"
    tilrestart = "tilrestart"
    forever = "forever"


def passthrough_args(argv: list[str]) -> list[str]:
    """The ssh/rsync variadic's verbatim value: click hands the
    ``--`` separator through inside the list where argparse
    swallowed it, so one leading separator drops here — everything
    else, options included, reaches ssh/rsync exactly as typed."""
    return argv[1:] if argv[:1] == ["--"] else argv


#: Whether the current parse reached a command body: the help
# screens and the usage errors never do, and main()'s help gate
# reads the difference (a token spelled like a help flag that a
# command consumed as its value must not masquerade as one).
body_reached = False

#: The current invocation's tokens, recorded by run_parsed for the
#: root callback's help-screen check (see set_invocation_tokens).
invocation_tokens: list[str] = []

#: The invocation's resolved client config (#314), set by the root
#: callback right after the bootstrap and read by the TUI entry
#: points: the tree's new-terminal shell action (#341) spawns its
#: console child with the launcher the resolution carries. None
#: when no bootstrap ran (a help screen — and then no TUI starts
#: either).
invoked_conf: ClientConfig | None = None


def one_line_interrupts(fn):
    """A Ctrl-C during a long boot is one line, not a traceback (a
    raw-mode session never gets here — Ctrl-C reaches the guest):
    caught at the command body's edge, where typer's own
    conversion (a bare exit 130) cannot swallow the line. Marks
    the body-reached flag on the way in."""

    @functools.wraps(fn)
    def wrapped(*args, **kwargs):
        global body_reached
        body_reached = True
        try:
            return fn(*args, **kwargs)
        except KeyboardInterrupt:
            print("msks: interrupted", file=sys.stderr)
            raise SystemExit(130) from None

    return wrapped


app = typer.Typer(
    name="msks",
    add_completion=False,
    help="msks client: workspace microvms over the daemon API",
    context_settings={"help_option_names": ["-h", "--help"]},
)

egress_app = typer.Typer(
    help="egress consent: decide, watch, and inspect (#69)"
)
image_app = typer.Typer(help="manage the daemon's image catalog (#65)")
home_app = typer.Typer(
    help="move a workspace's /home volume through the daemon (#80)"
)
secret_app = typer.Typer(
    help="placeholder secrets: mint, list, revoke, renew, check"
)
app.add_typer(egress_app, name="egress")
app.add_typer(image_app, name="image")
app.add_typer(home_app, name="home")
app.add_typer(secret_app, name="secret")


@app.callback(invoke_without_command=True)
def root(
    ctx: typer.Context,
    daemon: str = typer.Option(
        None,
        "--daemon",
        help="the daemon to address: an alias from the config file, "
        "or a raw URL (#314)",
    ),
    config: str = typer.Option(
        None,
        "--config",
        help="the client config file to read, or 'none' for "
        "environment only (#314)",
    ),
) -> int:
    """A bare ``msks`` is the workspace tree TUI (#309)."""
    # The config bootstrap runs before every command body (#314):
    # the file's and the flag's winners are in the environment by
    # the time any reader looks — except on a help screen, where
    # the operator is reading, not connecting, and a broken config
    # file must not hide the help.
    global invoked_conf
    if not help_requested(invocation_tokens):
        invoked_conf = bootstrap(daemon, config)
    if ctx.invoked_subcommand is None:
        # The decorator's edge, same as every command: a Ctrl-C in
        # the tree is one line, not typer's silent 130.
        return one_line_interrupts(run_main_tui)(conf=invoked_conf)
    return 0


@app.command("ls")
@one_line_interrupts
def ls(
    ctx: typer.Context,
    as_json: bool = typer.Option(False, "--json", help="one JSON document"),
) -> int:
    """List workspaces on the daemon."""
    return cmd_ls(as_json, transport=ctx.obj)


@app.command("tui")
@one_line_interrupts
def tui(
    workspace: str | None = typer.Argument(
        None,
        help="open this workspace's page (name or id) instead of the list",
    ),
) -> int:
    """The full-screen workspace tree (#309): the workspaces list,
    each workspace's page, and the consent decider."""
    return run_main_tui(workspace, conf=invoked_conf)


@app.command("storage")
@one_line_interrupts
def storage(
    ctx: typer.Context,
    workspace: str | None = typer.Argument(
        None,
        help="narrow the workspace table to one workspace (name or id)",
    ),
    as_json: bool = typer.Option(False, "--json", help="one JSON document"),
) -> int:
    """Report the state-disk budget and per-workspace cost (#184)."""
    return cmd_storage(workspace, as_json, transport=ctx.obj)


@app.command("create")
@one_line_interrupts
def create(
    ctx: typer.Context,
    workspace_id: str = typer.Argument(
        ...,
        help="the workspace's name (#246): the label you address it "
        "by (DNS-label charset); the daemon mints the immutable id",
    ),
    image: str | None = typer.Option(
        None, "--image", help="catalog ref: name:version, name, or hash"
    ),
    kernel: str | None = typer.Option(
        None, "--kernel", help="explicit kernel path (skips the catalog)"
    ),
    initrd: str | None = typer.Option(
        None, "--initrd", help="explicit initrd path"
    ),
    rootfs: str | None = typer.Option(
        None, "--rootfs", help="explicit rootfs path (skips the catalog)"
    ),
    cmdline: str | None = typer.Option(
        None, "--cmdline", help="explicit kernel cmdline"
    ),
    cpus: int | None = typer.Option(
        None, "--cpus", help="vcpu count (default 2)"
    ),
    mem_mib: int | None = typer.Option(
        None, "--mem-mib", help="guest memory, MiB (default 8192)"
    ),
    root_mib: int | None = typer.Option(
        None, "--root-mib", help="persistent root size, MiB"
    ),
    home_mib: int | None = typer.Option(
        None, "--home-mib", help="persistent home size, MiB"
    ),
    egress: bool | None = typer.Option(
        None,
        "--egress/--no-egress",
        help="boot with a virtio-net NIC onto a per-VM host tap "
        "(#52; the default is yes — use --no-egress to boot NIC-less)",
    ),
    egress_mode: PostureChoice | None = typer.Option(
        None,
        "--egress-mode",
        metavar="MODE",
        help="the consent posture (#69): allow (the default — new "
        "flows pass, off-list names are recorded), static (the "
        "allowlist only; off-list names never resolve), interactive "
        "(each new flow's first packet holds until a decider allows "
        "or denies it)",
    ),
    allow: list[str] | None = typer.Option(
        None,
        "--allow",
        metavar="SPEC",
        help="a static allowlist entry (#69), repeatable: host, "
        "host:port, .host (subdomains included), *.host (subdomains "
        "only), or cidr[:port]. Names gate at the daemon's resolver; "
        "address specs accept in the per-VM chain",
    ),
    user_data: str | None = typer.Option(
        None,
        "--user-data",
        metavar="FILE",
        help="first-boot provisioning payload (a shell script or "
        "cloud-config) delivered on the workspace's cidata seed disk "
        "(#41); - reads stdin. Create-time only",
    ),
    user: str | None = typer.Option(
        None,
        "--user",
        metavar="NAME",
        help="the workspace's login user (#248): seeded into the guest "
        "at first boot (the account, its home, authorized_keys, and "
        "the workspace-user sudo grant) and used as the default login "
        "for msks ssh, rsync, and console (default: your username)",
    ),
    daemon_mint: bool = typer.Option(
        False,
        "--daemon-mint",
        help="let the daemon mint the workspace's ssh identity and "
        "escrow both halves (#111) — an explicit opt-out; the create "
        "default (#336) plants one operator key across workspaces "
        "(identity_file, or the key msks mints under the client data "
        "root — `~/.local/share/msks/identity`, or that root under "
        "MSKSC_DATA_DIR) and the daemon holds public halves only",
    ),
    pubkey: str | None = typer.Option(
        None,
        "--pubkey",
        metavar="FILE",
        help="use a public key you already own as the workspace's ssh "
        "identity (#132), one workspace's worth: the file's one line "
        "travels to the daemon, any well-formed key type, and the "
        "private half stays wherever you keep it (nothing is written "
        "client-side). - reads stdin",
    ),
    key_type: str | None = typer.Option(
        None,
        "--key-type",
        metavar="TYPE",
        help="opt into the per-workspace client mint (#121): a fresh "
        "keypair minted on this client per workspace — public half "
        "sent, private half kept mode 0600 under the client data root "
        "(`~/.local/share/msks/<id>/identity`, or that root under "
        "MSKSC_DATA_DIR) where msks ssh finds it. TYPE names the mint's "
        f"key type, one of {', '.join(sorted(KEY_TYPES))} (default "
        "ed25519, the same FIPS-approvable default the daemon mints)",
    ),
    start: bool = typer.Option(
        False, "--start", help="boot the workspace immediately"
    ),
) -> int:
    """Create a workspace."""
    checked_key_type(key_type)
    return run_create(
        CreateFlags(
            workspace_id=workspace_id,
            image=image,
            kernel=kernel,
            initrd=initrd,
            rootfs=rootfs,
            cmdline=cmdline,
            cpus=cpus,
            mem_mib=mem_mib,
            root_mib=root_mib,
            home_mib=home_mib,
            egress=egress,
            egress_mode=(None if egress_mode is None else egress_mode.value),
            allow=allow,
            user_data=user_data,
            user=user,
            daemon_mint=daemon_mint,
            pubkey=pubkey,
            key_type=key_type,
            start=start,
        ),
        ctx.obj,
    )


def checked_key_type(key_type: str | None) -> None:
    """One local line for a key type outside the mint's set — the
    identity module stays the single source of the choices. A
    usage refusal: one line, exit 2 (run_parsed maps it)."""
    if key_type is not None and key_type not in KEY_TYPES:
        raise UsageError(
            f"--key-type must be one of {', '.join(sorted(KEY_TYPES))}"
        )


@app.command("start")
@one_line_interrupts
def start(
    ctx: typer.Context,
    workspace_id: str = typer.Argument(
        ..., help="the workspace to boot (name or id)"
    ),
) -> int:
    """Boot a created workspace."""
    return cmd_start(workspace_id, transport=ctx.obj)


@app.command("stop")
@one_line_interrupts
def stop(
    ctx: typer.Context,
    workspace_id: str = typer.Argument(
        ..., help="the workspace to stop (name or id)"
    ),
) -> int:
    """Power a workspace off."""
    return cmd_stop(workspace_id, transport=ctx.obj)


@app.command("resize")
@one_line_interrupts
def resize(
    ctx: typer.Context,
    workspace_id: str = typer.Argument(
        ..., help="the workspace to resize (name or id)"
    ),
    home_mib: int | None = typer.Option(
        None,
        "--home-mib",
        help="new /home volume size, MiB (grows or shrinks)",
    ),
    root_mib: int | None = typer.Option(
        None, "--root-mib", help="new root overlay size, MiB (grows only)"
    ),
    cpus: int | None = typer.Option(
        None, "--cpus", help="new vcpu count (applies at the next boot)"
    ),
    mem_mib: int | None = typer.Option(
        None,
        "--mem-mib",
        help="new guest memory, MiB (applies at the next boot)",
    ),
) -> int:
    """Change a stopped workspace's disk sizes and topology (#184,
    #277)."""
    return run_resize(workspace_id, home_mib, root_mib, cpus, mem_mib, ctx.obj)


@app.command("rm")
@one_line_interrupts
def rm(
    ctx: typer.Context,
    workspace_ids: list[str] = typer.Argument(
        ..., help="the workspaces to delete, in order (name or id)"
    ),
) -> int:
    """Delete workspaces and their data."""
    return cmd_rm(workspace_ids, transport=ctx.obj)


@app.command("console")
@one_line_interrupts
def console(
    workspace_id: str = typer.Argument(
        ..., help="the workspace to attach to (name or id)"
    ),
    user: str | None = typer.Option(
        None,
        "--user",
        help="shell user (default: the workspace's login user, #248; "
        "--user root is the recovery shell)",
    ),
) -> int:
    """Interactive shell in a workspace."""
    return run_workspace_shell(workspace_id, user)


@app.command("forward")
@one_line_interrupts
def forward(
    workspace_id: str = typer.Argument(
        ..., help="the workspace to reach (name or id)"
    ),
    port: int = typer.Argument(..., help="the guest TCP port to reach"),
    local: int | None = typer.Option(
        None,
        "--local",
        metavar="PORT",
        help="bind 127.0.0.1:PORT instead of stdio; every accepted "
        "connection gets its own forward",
    ),
) -> int:
    """Bridge a workspace TCP port to stdio or a local port."""
    return run_workspace_forward(workspace_id, port, local)


@app.command("key")
@one_line_interrupts
def key(
    ctx: typer.Context,
    workspace_id: str = typer.Argument(
        ..., help="the workspace whose identity to fetch (name or id)"
    ),
    as_private: bool = typer.Option(
        False,
        "--private",
        help="print the private half instead of the public line",
    ),
    out: str | None = typer.Option(
        None,
        "--out",
        metavar="FILE",
        help="write the private half to FILE (mode 0600) instead of printing",
    ),
) -> int:
    """Fetch a workspace's ssh identity (#111; the public half alone
    for a client-minted #121 workspace)."""
    checked_key_flags(as_private, out)
    return cmd_key(workspace_id, as_private, out, transport=ctx.obj)


def checked_key_flags(as_private: bool, out: str | None) -> None:
    """--private and --out are exclusive: one output shape — a
    usage refusal (one line, exit 2, run_parsed's mapping)."""
    if as_private and out is not None:
        raise UsageError("--private and --out are exclusive")


@app.command("llm-token")
@one_line_interrupts
def llm_token(
    ctx: typer.Context,
    workspace_id: str = typer.Argument(
        ..., help="the workspace whose credential to fetch (name or id)"
    ),
    remint: bool = typer.Option(
        False,
        "--remint",
        help="mint a fresh credential, replacing the stored one",
    ),
) -> int:
    """Fetch a workspace's LLM proxy credential (#259)."""
    return cmd_llm_token(workspace_id, remint, transport=ctx.obj)


@app.command(
    "ssh",
    context_settings={
        "allow_interspersed_args": False,
        "ignore_unknown_options": True,
    },
)
@one_line_interrupts
def ssh(
    ctx: typer.Context,
    workspace_id: str = typer.Argument(
        ..., help="the workspace to log into (name or id)"
    ),
    passthrough: list[str] | None = typer.Argument(
        None,
        metavar="ARGS",
        help="arguments passed to ssh verbatim ('-l root' is the "
        "recovery login; '-A' forwards your agent, $SSH_AUTH_SOCK)",
    ),
) -> int:
    """Ssh into a workspace over the forward, identity staged in
    memory."""
    return run_workspace_ssh(
        workspace_id,
        passthrough_args(passthrough or []),
        transport=ctx.obj,
    )


@app.command(
    "rsync",
    context_settings={
        "allow_interspersed_args": False,
        "ignore_unknown_options": True,
    },
)
@one_line_interrupts
def rsync(
    ctx: typer.Context,
    workspace_id: str = typer.Argument(
        ..., help="the workspace to copy against (name or id)"
    ),
    passthrough: list[str] | None = typer.Argument(
        None,
        metavar="ARGS",
        help="arguments passed to rsync verbatim; an empty-host path "
        "(:/remote/path, user@:/remote/path) targets this workspace",
    ),
) -> int:
    """Rsync files to and from a workspace over the forward,
    identity staged in memory."""
    return run_workspace_rsync(
        workspace_id,
        passthrough_args(passthrough or []),
        transport=ctx.obj,
    )


@egress_app.command("rules")
@one_line_interrupts
def egress_rules(
    ctx: typer.Context, workspace_id: str = typer.Argument(...)
) -> int:
    """The in-effect verdicts for a workspace."""
    return asyncio.run(egress_mod.run_rules(workspace_id, transport=ctx.obj))


@egress_app.command("requests")
@one_line_interrupts
def egress_requests(
    ctx: typer.Context,
    workspace_id: str = typer.Argument(...),
    decision: DecisionFilter | None = typer.Option(
        None,
        "--decision",
        metavar="DECISION",
        help="filter one lifecycle state (pending, allowed, denied, "
        "expired, or revoked)",
    ),
) -> int:
    """The consent rows (audit trail)."""
    return asyncio.run(
        egress_mod.run_requests(
            workspace_id,
            None if decision is None else decision.value,
            transport=ctx.obj,
        )
    )


@egress_app.command("decide")
@one_line_interrupts
def egress_decide(
    ctx: typer.Context,
    workspace_id: str = typer.Argument(...),
    request_id: str = typer.Argument(...),
    decision: VerdictChoice = typer.Argument(
        ..., help="the verdict: allow or deny"
    ),
    duration: DurationChoice = typer.Option(
        DurationChoice.tilrestart,
        "--duration",
        metavar="DURATION",
        help="how long enforcement honors the verdict (once, 5m, "
        "15m, tilrestart, or forever; default tilrestart)",
    ),
) -> int:
    """Give a verdict on a held request."""
    return asyncio.run(
        egress_mod.run_decide(
            workspace_id,
            request_id,
            decision.value,
            duration.value,
            transport=ctx.obj,
        )
    )


@egress_app.command("revoke")
@one_line_interrupts
def egress_revoke(
    ctx: typer.Context,
    workspace_id: str = typer.Argument(...),
    request_id: str = typer.Argument(...),
) -> int:
    """Undo an in-effect verdict."""
    return asyncio.run(
        egress_mod.run_revoke(workspace_id, request_id, transport=ctx.obj)
    )


@egress_app.command("mode")
@one_line_interrupts
def egress_mode_command(
    ctx: typer.Context,
    workspace_id: str = typer.Argument(...),
    mode: PostureChoice = typer.Argument(
        ...,
        help="the posture to switch to (#280): allow, static, or interactive",
    ),
    allow: list[str] | None = typer.Option(
        None,
        "--allow",
        metavar="SPEC",
        help="replace the static allowlist with this entry "
        "(repeatable); omitted, the workspace keeps its list",
    ),
    offline: bool = typer.Option(
        False,
        "--offline",
        help="confirm the switch to static even with nothing "
        "effectively allowed (every name NXDOMAINs — an offline "
        "workspace)",
    ),
) -> int:
    """Switch the egress posture (#280): live for a running
    workspace, at next start for a stopped one."""
    return asyncio.run(
        egress_mod.run_mode(
            workspace_id,
            mode.value,
            allow,
            offline,
            transport=ctx.obj,
        )
    )


@egress_app.command("tui")
@one_line_interrupts
def egress_tui(
    workspace_id: str = typer.Argument(..., help="decide for this workspace"),
) -> int:
    """The consent decider TUI (#195): live holds, verdicts, rules."""
    return run_consent_tui(workspace_id)


@egress_app.command("watch")
@one_line_interrupts
def egress_watch(
    workspace_id: str | None = typer.Argument(
        None,
        help="decide for this workspace (hold SYNs only while a "
        "decider is connected)",
    ),
    decide: bool = typer.Option(
        False,
        "--decide",
        help="prompt y/n for each pending request",
    ),
    duration: DurationChoice = typer.Option(
        DurationChoice.tilrestart,
        "--duration",
        metavar="DURATION",
        help="the duration a --decide allow applies (once, 5m, 15m, "
        "tilrestart, or forever; default tilrestart)",
    ),
) -> int:
    """Stream egress frames as lines; registers as a decider."""
    return asyncio.run(
        egress_mod.run_watch(workspace_id, decide, duration.value)
    )


@image_app.command("ls")
@one_line_interrupts
def image_ls(
    ctx: typer.Context,
    as_json: bool = typer.Option(False, "--json", help="one JSON document"),
) -> int:
    """List catalog images."""
    return cmd_image_ls(as_json, transport=ctx.obj)


@image_app.command("import")
@one_line_interrupts
def image_import(
    ctx: typer.Context,
    source: str = typer.Argument(
        ...,
        help="archive path as the daemon sees it (its own "
        "filesystem; the file is read by the daemon, not uploaded "
        "by this command) or an https:// URL the daemon downloads "
        "itself (#258)",
    ),
) -> int:
    """Register an image archive from a daemon-side path or an
    https:// URL."""
    return cmd_image_import(source, transport=ctx.obj)


@image_app.command("check")
@one_line_interrupts
def image_check(
    archive: str = typer.Argument(..., help=CHECK_ARCHIVE_HELP),
    egress: bool = typer.Option(False, "--egress", help=CHECK_EGRESS_HELP),
    uplink: str | None = typer.Option(
        None, "--uplink", help=CHECK_UPLINK_HELP
    ),
    boot_timeout_s: float = typer.Option(
        120.0, "--boot-timeout-s", help=CHECK_BOOT_TIMEOUT_HELP
    ),
    shutdown_timeout_s: float = typer.Option(
        120.0, "--shutdown-timeout-s", help=CHECK_SHUTDOWN_TIMEOUT_HELP
    ),
    keep: bool = typer.Option(False, "--keep", help=CHECK_KEEP_HELP),
) -> int:
    """Boot an image and verify the guest contract (#258); local —
    needs /dev/kvm, --egress needs root."""
    return cmd_image_check(
        CheckOptions(
            archive=archive,
            egress=egress,
            uplink=uplink,
            boot_timeout_s=boot_timeout_s,
            shutdown_timeout_s=shutdown_timeout_s,
            keep=keep,
        )
    )


@image_app.command("rm")
@one_line_interrupts
def image_rm(
    ctx: typer.Context,
    ref: str = typer.Argument(
        ...,
        help="name:version, bare name (newest), name@hash (full "
        "hash), or hash (a unique hash prefix works too)",
    ),
) -> int:
    """Remove an image from the catalog."""
    return cmd_image_rm(ref, transport=ctx.obj)


@image_app.command("info")
@one_line_interrupts
def image_info(
    ctx: typer.Context,
    ref: str = typer.Argument(
        ...,
        help="name:version, bare name, name@hash, or hash "
        "(a unique hash prefix works too)",
    ),
) -> int:
    """Show one image's full record."""
    return cmd_image_info(ref, transport=ctx.obj)


@image_app.command("default")
@one_line_interrupts
def image_default(
    ctx: typer.Context,
    ref: str | None = typer.Argument(
        None,
        help="name:version, bare name (newest), name@hash, or hash "
        "(a unique hash prefix works too)",
    ),
    unset: bool = typer.Option(
        False,
        "--unset",
        help="clear the designation; a bare create falls back to the "
        "sole catalog entry, or needs --image when several remain",
    ),
) -> int:
    """Designate the image a bare create boots (#270), or clear the
    designation with --unset."""
    return cmd_image_default(ref, unset, transport=ctx.obj)


@home_app.command("export")
@one_line_interrupts
def home_export(
    ctx: typer.Context,
    workspace_id: str = typer.Argument(
        ..., help="the workspace whose volume to download (name or id)"
    ),
    file: str | None = typer.Argument(
        None,
        help="output file (default: <workspace_id>.ext4); - writes stdout",
    ),
) -> int:
    """Download a workspace's /home volume."""
    return cmd_home_export(workspace_id, file, transport=ctx.obj)


@home_app.command("import")
@one_line_interrupts
def home_import(
    ctx: typer.Context,
    workspace_id: str = typer.Argument(
        ..., help="the workspace whose volume to replace (name or id)"
    ),
    file: str = typer.Argument(
        ..., help="the ext4 volume image to upload; - reads stdin"
    ),
) -> int:
    """Replace a workspace's /home volume from an ext4 image."""
    return cmd_home_import(workspace_id, file, transport=ctx.obj)


@secret_app.command("mint")
@one_line_interrupts
def secret_mint(
    ctx: typer.Context,
    workspace_id: str = typer.Argument(
        ..., help="the workspace the placeholder binds to"
    ),
    name: str = typer.Option(..., "--name", help="the placeholder's label"),
    dests: list[str] = typer.Option(
        ...,
        "--dest",
        metavar="HOST",
        help=(
            "an allowlist destination: an exact host "
            "(api.github.com) or a suffix (.github.com); repeatable"
        ),
    ),
    ttl: int | None = typer.Option(
        None,
        "--ttl",
        metavar="SECONDS",
        help="the placeholder's lifetime (default: unbounded)",
    ),
    secret_file: str = typer.Option(
        ...,
        "--secret-file",
        metavar="PATH",
        help=(
            "the file holding the real secret; - reads stdin "
            "(pipe it from a password manager)"
        ),
    ),
) -> int:
    """Mint a placeholder for one workspace."""
    return cmd_secret_mint(
        workspace_id, name, dests, ttl, secret_file, transport=ctx.obj
    )


@secret_app.command("ls")
@one_line_interrupts
def secret_ls(
    ctx: typer.Context,
    as_json: bool = typer.Option(False, "--json", help="one JSON document"),
) -> int:
    """List placeholders (sentinels are never listed)."""
    return cmd_secret_ls(as_json, transport=ctx.obj)


@secret_app.command("revoke")
@one_line_interrupts
def secret_revoke(
    ctx: typer.Context,
    workspace_id: str = typer.Argument(...),
    name: str = typer.Option(..., "--name", help="the placeholder's label"),
) -> int:
    """Revoke a placeholder (effective next request)."""
    return cmd_secret_revoke(workspace_id, name, transport=ctx.obj)


@secret_app.command("renew")
@one_line_interrupts
def secret_renew(
    ctx: typer.Context,
    workspace_id: str = typer.Argument(...),
    name: str = typer.Option(..., "--name", help="the placeholder's label"),
    ttl: int = typer.Option(
        ...,
        "--ttl",
        metavar="SECONDS",
        help="the new lifetime from now",
    ),
) -> int:
    """Extend a placeholder's lifetime in place."""
    return cmd_secret_renew(workspace_id, name, ttl, transport=ctx.obj)


@secret_app.command("check")
@one_line_interrupts
def secret_check(ctx: typer.Context) -> int:
    """Verify the configured secret store answers writes."""
    return cmd_secret_check(transport=ctx.obj)


def help_requested(argv: list[str]) -> bool:
    """Whether the invocation's parse reaches a help flag — the
    ``--help`` screen exits through SystemExit(0), the shape the
    argparse era pinned; a ``--`` separator hides everything after
    it from the flag scan."""
    for token in argv:
        if token == "--":
            return False
        if token in ("-h", "--help"):
            return True
    return False


def set_invocation_tokens(argv: list[str] | None) -> None:
    """Record the invocation's tokens for the root callback.

    The callback runs before the subcommand parses, so it cannot
    see whether the parse ends at a help screen; the recorded
    tokens let it keep the config bootstrap off the help screens —
    a broken config file must not hide ``msks <cmd> --help``, the
    operator's most discoverable debugging tool.
    """
    global invocation_tokens
    invocation_tokens = sys.argv[1:] if argv is None else argv


def run_parsed(argv: list[str] | None, transport) -> int:
    """One non-standalone pass through the typer app: the command's
    return value is the exit code (typer raises it as an Exit and
    click's non-standalone main hands it back)."""
    # typer.main.get_command is the layer's own bridge (the public
    # Typer.__call__ is standalone-only); the floor rides on it
    # staying the shape every typer release exercises through
    # Typer.__call__ itself.
    global body_reached
    body_reached = False  # per invocation, never across them
    set_invocation_tokens(argv)
    command = typer.main.get_command(app)
    try:
        return command.main(
            argv,
            prog_name="msks",
            obj=transport,
            standalone_mode=False,
        )
    except UsageError as exc:
        # A bad invocation is one line and exit 2 — the argparse-era
        # convention, kept (docs/cli.md documents it).
        print(f"msks: {exc.format_message()}", file=sys.stderr)
        return 2


def help_exit(code: int, tokens: list[str]) -> bool:
    """Whether this run ends as the help screen's exit: a zero
    code, no command body reached (a help-shaped token a command
    swallowed as its value ran one — that is not a help exit), and
    a help flag the scan reaches."""
    return code == 0 and not body_reached and help_requested(tokens)


def main(argv: list[str] | None = None, transport=None) -> int:
    """The ``msks`` entry point: parse with the typer app, run the
    command, return its exit code."""
    tokens = sys.argv[1:] if argv is None else argv
    code = run_parsed(argv, transport)
    if help_exit(code, tokens):
        # SystemExit(0), the shape the argparse era pinned.
        raise SystemExit(0)
    return code or 0


def create_identity(
    args: CreateFlags,
) -> tuple[str | None, str | None, str | None]:
    """The create's identity mode: ``(mint key type, supplied
    line, identity note)``.

    The operator key is the default (#336): a bare create plants
    one operator key across workspaces — the ``identity_file`` /
    ``MSKSC_IDENTITY_FILE`` setting when set, else the key msks
    minted under the data root — its derived public half on the
    wire, nothing written per-workspace, and the private file
    never copied anywhere. ``--key-type`` opts into the
    per-workspace client mint (#121, today's default);
    ``--daemon-mint`` hands the identity to the daemon (#111);
    ``--pubkey`` supplies a one-off operator line (#132). The
    three explicit modes are exclusive
    (:func:`check_identity_conflicts` names the pairings).
    """
    check_identity_conflicts(args)
    if args.daemon_mint:
        return None, None, None
    if args.pubkey is not None:
        return None, read_pubkey(args.pubkey), None
    if args.key_type is not None:
        return args.key_type, None, None
    public, note = operator_pubkey()
    return None, public, note


#: The identity-mode flag conflicts, as message → attribute names:
#: each pairing would look meaningful but is not, so it is rejected
#: with the conflict named.
IDENTITY_CONFLICTS = (
    ("--key-type conflicts with --daemon-mint", ("key_type", "daemon_mint")),
    ("--pubkey conflicts with --daemon-mint", ("pubkey", "daemon_mint")),
    (
        "--key-type needs the client mint (--pubkey carries its own key)",
        ("key_type", "pubkey"),
    ),
)


def flag_set(args: CreateFlags, name: str) -> bool:
    """Whether a flag was supplied — a store_true flag by truth, a
    value flag by presence (an explicit empty value counts, so
    ``--pubkey ""`` still conflicts rather than slipping past)."""
    value = getattr(args, name)
    return bool(value) if name == "daemon_mint" else value is not None


def check_identity_conflicts(args: CreateFlags) -> None:
    """Reject the flag pairings that would look meaningful but are
    not, with the conflict named."""
    for message, flags in IDENTITY_CONFLICTS:
        if all(flag_set(args, flag) for flag in flags):
            raise SystemExit(f"msks: {message}")


def read_pubkey(path: str) -> str:
    """One public key line from a file (or stdin with ``-``), checked
    lightly here so a typo fails before any network roundtrip — the
    daemon's shape validation is the authority."""
    return checked_pubkey_line(pubkey_text(path))


def pubkey_text(path: str) -> str:
    """The file's (or stdin's) raw text, with a one-line read error."""
    try:
        if path == "-":
            return sys.stdin.read()
        return Path(path).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise SystemExit(
            f"msks: cannot read public key file {path}: {exc}"
        ) from exc


def checked_pubkey_line(text: str) -> str:
    """Exactly one public key line, stripped — a .pub file's shape."""
    lines = [line for line in text.splitlines() if line.strip()]
    if len(lines) != 1:
        raise SystemExit(
            f"msks: the public key file must carry exactly one line "
            f"(found {len(lines)})"
        )
    line = lines[0].strip()
    if len(line.split()) < 2:
        raise SystemExit("msks: that line does not look like a public key")
    return line


def run_resize(
    workspace_id: str,
    home_mib: int | None,
    root_mib: int | None,
    cpus: int | None,
    mem_mib: int | None,
    transport,
) -> int:
    """Compose the request body, refusing the empty one locally."""
    body = {
        key: value
        for key, value in (
            ("home_mib", home_mib),
            ("root_mib", root_mib),
            ("cpus", cpus),
            ("mem_mib", mem_mib),
        )
        if value is not None
    }
    if not body:
        raise SystemExit(
            "msks: nothing to resize: pass --home-mib, --root-mib, "
            "--cpus, or --mem-mib"
        )
    return cmd_resize(workspace_id, body, transport)


def run_create(args: CreateFlags, transport) -> int:
    """Build the body, then resolve the identity mode once — the
    body build fails on local grounds (a bad ``--user``, an
    unreadable ``--user-data``) before the resolver can read stdin
    (``--pubkey -``), load the operator identity, or mint; the
    resolver may also reject a flag pairing, so it runs a single
    time — then create."""
    if args.pubkey == "-" and args.user_data == "-":
        raise SystemExit(
            "msks: --pubkey - and --user-data - both read stdin; "
            "pass one of them by file"
        )
    body = create_body(args)
    key_type, pubkey, note = create_identity(args)
    return cmd_create(
        body,
        args.start,
        transport=transport,
        key_type=key_type,
        pubkey=pubkey,
        identity_note=note,
    )


if __name__ == "__main__":
    raise SystemExit(main())
