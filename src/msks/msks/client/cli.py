"""The ``msks`` CLI: ``ls``, ``create``, ``start``, ``stop``, ``rm``,
``resize``, ``console``, ``forward``, ``ssh``, ``rsync``, ``key``,
``storage``, the ``image`` catalog subcommands, and the ``home``
volume moves.

Every command speaks the daemon's REST surface with the same client
conventions (#21): ``MSKSC_URL`` for the daemon, ``MSKSC_TOKEN`` for
a bearer token, ``MSKSC_CAFILE`` to pin the certificate. The
interactive console command lives in :mod:`msks.client.console`.
"""

import argparse
import asyncio
import contextlib
import getpass
import json
import os
import sys
from collections.abc import AsyncIterator
from datetime import datetime
from pathlib import Path

from ..conformance_args import check_arguments
from ..identity import KEY_TYPES, LOGIN_NAME_RE, mint
from ..imagestore import is_hash_shape, version_key
from ..storage import MIB
from . import egress as egress_mod
from .console import run_workspace_shell
from .forward import run_workspace_forward
from .rest import (
    STREAM_WINDOW_B,
    api_call,
    api_client,
    download,
    env_token,
    env_url,
    request,
    upload,
)
from .rest import (
    fetch_llm_token as rest_fetch_llm_token,
)
from .rest import (
    fetch_ssh_key as rest_fetch_ssh_key,
)
from .rsync import run_workspace_rsync
from .ssh import data_dir, run_workspace_ssh
from .tabular import command_parser, listing_text
from .tui.consent_app import run_consent_tui


def workspace_cells(row: dict) -> list[str]:
    """One listing row's cells: name, id, status, image hash, host."""
    image = (row.get("image_hash") or "-")[:12]
    host = row.get("host") or "-"
    name = row.get("name") or "-"
    return [name, row["id"], row["status"], image, host]


def display_name(row: dict) -> str:
    """The human-facing label (#246): the workspace's name, else
    its immutable id (a nameless workspace is addressed by id)."""
    return row.get("name") or row["id"]


def render_ls(rows: list[dict], as_json: bool) -> str:
    """The whole listing: the aligned table, or one JSON document."""
    if as_json:
        return json.dumps(rows, indent=2)
    return listing_text(
        ["name", "id", "status", "image", "host"],
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
) -> int:
    """``msks create``: one workspace, optionally booted.

    ``key_type`` names the client-mint mode (#121): minted locally,
    public half sent, private half kept. ``pubkey`` is an
    operator-supplied public line (#132): sent as-is, no private
    half anywhere msks manages.
    """
    asyncio.run(
        create_workspace(
            env_url(), env_token(), body, start, transport, key_type, pubkey
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
) -> dict:
    """POST the workspace, print its name and id, then boot it when
    asked.

    The line prints before the boot attempt: a failed start must not
    hide that the workspace exists — recover with ``msks start``.
    The daemon mints the workspace's immutable id (#246); the
    follow-up calls (boot, identity) address the workspace by that
    id, and the label the operator typed stays the day-to-day
    reference. In the client-mint mode (#121) the keypair is minted
    here — the private half never crosses the wire — and is
    persisted (mode 0600, client data root, keyed on the id) only
    after the create succeeded, so a refused create leaves no
    orphaned key behind. With an operator-supplied key (#132) only
    the public line travels and nothing is written client-side: the
    private half stays wherever the operator keeps it.
    """
    private_pem, public = await identity_material(body, key_type, pubkey)
    async with api_client(url, token, transport) as client:
        row = await request(
            client, "POST", "/api/v1/workspaces", json_body=body
        )
        print(created_line(row))
        if public is not None:
            await verify_no_escrow(client, row["id"], public)
        if private_pem is not None:
            path = write_client_identity(row["id"], private_pem)
            print(f"client identity (mode 0600): {path}")
        if not start:
            return row
        try:
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
        return row


def created_line(row: dict) -> str:
    """The create confirmation: the label beside the daemon-minted,
    immutable id (#246) — the name is the everyday reference, the
    id is the one no future workspace will ever reuse."""
    name = row.get("name")
    if name and name != row["id"]:
        return f"created {name} (id {row['id']})"
    return f"created {row['id']}"


async def identity_material(
    body: dict, key_type: str | None, pubkey: str | None
) -> tuple[str | None, str | None]:
    """Prepare the create's identity: ``(private_pem, public_line)``.

    The mint produces both halves; an operator-supplied line is the
    public half alone. Either way the public line rides the body as
    ``ssh_pubkey``; a daemon-mint create supplies neither and keeps
    the body free of key material.
    """
    if key_type is not None:
        private_pem, public = await asyncio.to_thread(mint, key_type)
    elif pubkey is None:
        return None, None
    else:
        private_pem, public = None, pubkey
    body["ssh_pubkey"] = public
    return private_pem, public


async def verify_no_escrow(client, workspace_id: str, public: str) -> None:
    """Confirm the daemon kept the no-escrow promise (#121).

    A daemon one version behind this client drops the unknown
    ``ssh_pubkey`` field (pydantic ignores extras) and silently mints
    its own pair — the create reports success while the daemon
    escrows a private half the operator was told does not exist.
    The key fetch answers for it: the served public line must carry
    the supplied key material and the private half must be null.
    """
    key = await request(
        client, "GET", f"/api/v1/workspaces/{workspace_id}/ssh-key"
    )
    served = key.get("public_key", "").split()[:2]
    if key.get("private_key") is not None or served != public.split()[:2]:
        raise SystemExit(
            f"msks: {workspace_id} was created, but the daemon did not "
            "keep the no-escrow promise: it holds its own minted "
            "identity for the workspace (a daemon older than this "
            "client's supplied-key support). The daemon's version of "
            "msks must be updated before creating without "
            "--daemon-mint; remove the escrowed workspace with: "
            f"msks rm {workspace_id}"
        )


def write_client_identity(workspace_id: str, private_pem: str) -> Path:
    """The client-minted private half, persisted mode 0600 (#121).

    The file is created 0600 from the first byte (open-write-chmod
    would leave a umask-window where the workspace's only private
    half is group-readable), the mode forced again on a pre-existing
    file, under the data root (not the cache: this half must survive
    cache sweeps). No escrow cuts both ways: a failed write is loud —
    the workspace exists with the public half planted, and the
    private half exists nowhere on disk.
    """
    root = data_dir() / workspace_id
    path = root / "identity"
    try:
        root.mkdir(parents=True, exist_ok=True)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(private_pem)
    except OSError as exc:
        raise SystemExit(
            f"msks: {workspace_id} was created, but its client-minted "
            f"identity could not be written to {path}: {exc}\n"
            "The private half now exists nowhere on disk — ssh cannot "
            "use this workspace's identity. Use the console, or delete "
            "and recreate the workspace"
        ) from exc
    return path


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
    the daemon never held (#121, #132): the error names the two
    places that half can be instead of printing nothing."""
    if key["private_key"] is None:
        directory = key.get("id") or workspace_id
        raise SystemExit(
            f"msks key: the daemon holds no private half for {workspace_id}. "
            "The workspace's key was minted on a client (its private half "
            f"lives at {data_dir() / directory / 'identity'} on that "
            "machine), or supplied from a key you already own — use that "
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
    """``msks resize``: move a stopped workspace's sizes (#184)."""
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


def resize_message(row: dict, body: dict) -> str:
    """The result line: the new sizes, with the boot note only when
    the root actually moved (the daemon's ``changes`` list says so,
    not the request's flags) — home bytes moved at once on the host;
    only the root's guest-side fill waits for the next boot."""
    line = (
        f"resized {display_name(row)}: root {row['root_mib']} MiB, "
        f"home {row['home_mib']} MiB"
    )
    if any(change.startswith("root") for change in row.get("changes", [])):
        line += " (the guest fills the larger root on its next boot)"
    return line


def image_cells(row: dict) -> list[str]:
    """One catalog row's cells: ref, short hash, default flag, kernel."""
    flag = "default" if row["default"] else "-"
    kernel = f"{row['kernel_version'] or '-'} ({row['kernel_format'] or '-'})"
    ref = f"{row['name']}:{row['version']}"
    return [ref, row["hash"][:12], flag, kernel]


def render_image_ls(rows: list[dict], as_json: bool) -> str:
    """The whole catalog: the aligned table, or one JSON document."""
    if as_json:
        return json.dumps(rows, indent=2)
    return listing_text(
        ["ref", "hash", "default", "kernel"],
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


def secret_command_table(args: argparse.Namespace, transport) -> dict:
    """One entry per ``secret`` subcommand."""
    return {
        "mint": lambda: cmd_secret_mint(
            args.workspace_id,
            args.name,
            args.dests,
            args.ttl,
            args.secret_file,
            transport=transport,
        ),
        "ls": lambda: cmd_secret_ls(args.json, transport=transport),
        "revoke": lambda: cmd_secret_revoke(
            args.workspace_id, args.name, transport=transport
        ),
        "renew": lambda: cmd_secret_renew(
            args.workspace_id, args.name, args.ttl, transport=transport
        ),
        "check": lambda: cmd_secret_check(transport=transport),
    }


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


def cmd_image_check(args: argparse.Namespace) -> int:
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


def invoking_user() -> str:
    """The create ``--user`` default (#248): the name of the user
    running msks — the workspace seeds this account as its own, so
    a bare create lands the operator's account, not a shared one.

    The name must fit the login-name charset (the same wire shape
    the guest accepts); a username that does not (a capitalized or
    accented one) is a named refusal pointing at ``--user``, not a
    cryptic daemon-side pattern error.
    """
    try:
        name = getpass.getuser()
    except OSError as exc:
        raise SystemExit(
            f"msks: cannot determine the invoking user's name ({exc}); "
            "pass --user <name>"
        ) from exc
    return checked_login_name(name, "your username")


def checked_login_name(name: str, what: str) -> str:
    """One login name the guest can carry, or the named refusal."""
    if LOGIN_NAME_RE.fullmatch(name) is None:
        raise SystemExit(
            f"msks: {what} ({name!r}) is not a valid login name — "
            "lowercase letters, digits, dashes, and underscores, "
            "starting with a lowercase letter or underscore, at most "
            "32 characters; pass --user <name>"
        )
    return name


def create_body(args: argparse.Namespace) -> dict:
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


def create_user(args: argparse.Namespace) -> str:
    """The workspace's login user (#248): the explicit ``--user``, or
    the invoking user's name — checked before the wire so a bad name
    is one local line, not the daemon's pattern error."""
    if args.user is not None:
        return checked_login_name(args.user, "--user")
    return invoking_user()


def consent_fields(args: argparse.Namespace) -> dict:
    """The egress-consent create fields the operator set (#69):
    the mode and the repeated allowlist entries."""
    fields = {}
    if getattr(args, "egress_mode", None) is not None:
        fields["egress_mode"] = args.egress_mode
    if getattr(args, "allow", None):
        fields["egress_allowlist"] = args.allow
    return fields


def build_parser() -> argparse.ArgumentParser:
    """The ``msks`` command line."""
    parser = command_parser(
        prog="msks",
        description="msks client: workspace microvms over the daemon API",
    )
    sub = parser.add_subparsers(
        dest="command",
        required=True,
        title="commands",
        metavar="<command>",
        parser_class=command_parser,
    )
    listing = sub.add_parser("ls", help="list workspaces on the daemon")
    listing.add_argument(
        "--json", action="store_true", help="one JSON document"
    )
    storage_cmd = sub.add_parser(
        "storage",
        help="report the state-disk budget and per-workspace cost (#184)",
    )
    storage_cmd.add_argument(
        "workspace",
        nargs="?",
        default=None,
        help="narrow the workspace table to one workspace (name or id)",
    )
    storage_cmd.add_argument(
        "--json", action="store_true", help="one JSON document"
    )
    create = sub.add_parser("create", help="create a workspace")
    create.add_argument(
        "workspace_id",
        help="the workspace's name (#246): the label you address it "
        "by (DNS-label charset); the daemon mints the immutable id",
    )
    create.add_argument(
        "--image", help="catalog ref: name:version, name, or hash"
    )
    create.add_argument(
        "--kernel", help="explicit kernel path (skips the catalog)"
    )
    create.add_argument("--initrd", help="explicit initrd path")
    create.add_argument(
        "--rootfs", help="explicit rootfs path (skips the catalog)"
    )
    create.add_argument("--cmdline", help="explicit kernel cmdline")
    create.add_argument("--cpus", type=int, help="vcpu count (default 2)")
    create.add_argument(
        "--mem-mib", type=int, help="guest memory, MiB (default 1024)"
    )
    create.add_argument(
        "--root-mib", type=int, help="persistent root size, MiB"
    )
    create.add_argument(
        "--home-mib", type=int, help="persistent home size, MiB"
    )
    create.add_argument(
        "--egress",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="boot with a virtio-net NIC onto a per-VM host tap "
        "(#52; the default is yes — use --no-egress to boot NIC-less)",
    )
    create.add_argument(
        "--egress-mode",
        choices=("allow", "static", "interactive"),
        help="the consent posture (#69): allow (the default — new "
        "flows pass, off-list names are recorded), static (the "
        "allowlist only; off-list names never resolve), interactive "
        "(each new flow's first packet holds until a decider allows "
        "or denies it)",
    )
    create.add_argument(
        "--allow",
        action="append",
        metavar="SPEC",
        help="a static allowlist entry (#69), repeatable: host, "
        "host:port, .host (subdomains included), *.host (subdomains "
        "only), or cidr[:port]. Names gate at the daemon's resolver; "
        "address specs accept in the per-VM chain",
    )
    create.add_argument(
        "--user-data",
        metavar="FILE",
        help="first-boot provisioning payload (a shell script or "
        "cloud-config) delivered on the workspace's cidata seed disk "
        "(#41); - reads stdin. Create-time only",
    )
    create.add_argument(
        "--user",
        metavar="NAME",
        help="the workspace's login user (#248): seeded into the guest "
        "at first boot (the account, its home, authorized_keys, and "
        "the workspace-user sudo grant) and used as the default login "
        "for msks ssh, rsync, and console (default: your username)",
    )
    create.add_argument(
        "--daemon-mint",
        action="store_true",
        help="let the daemon mint the workspace's ssh identity and "
        "escrow both halves (#111) instead of the client mint — the "
        "create default (#121) mints on this client, sends the public "
        "half only, and keeps the private half (mode 0600 under the "
        "client data root — `~/.local/share/msks/<id>/identity`, or "
        "that root under MSKSC_DATA_DIR — where msks ssh finds it)",
    )
    create.add_argument(
        "--pubkey",
        metavar="FILE",
        help="use a public key you already own as the workspace's ssh "
        "identity (#132): the file's one line travels to the daemon, "
        "any well-formed key type, and the private half stays wherever "
        "you keep it (nothing is written client-side). - reads stdin",
    )
    create.add_argument(
        "--key-type",
        choices=sorted(KEY_TYPES),
        help="the client mint's key type (default ed25519, the same "
        "FIPS-approvable default the daemon mints)",
    )
    create.add_argument(
        "--start", action="store_true", help="boot the workspace immediately"
    )
    egress_cmd = sub.add_parser(
        "egress",
        help="egress consent: decide, watch, and inspect (#69)",
    )
    egress_sub = egress_cmd.add_subparsers(
        dest="egress_command",
        required=True,
        title="commands",
        metavar="<command>",
        parser_class=command_parser,
    )
    egress_rules = egress_sub.add_parser(
        "rules", help="the in-effect verdicts for a workspace"
    )
    egress_rules.add_argument("workspace_id")
    egress_requests = egress_sub.add_parser(
        "requests", help="the consent rows (audit trail)"
    )
    egress_requests.add_argument("workspace_id")
    egress_requests.add_argument(
        "--decision",
        choices=("pending", "allowed", "denied", "expired", "revoked"),
        default=None,
        help="filter one lifecycle state",
    )
    egress_decide = egress_sub.add_parser(
        "decide", help="give a verdict on a held request"
    )
    egress_decide.add_argument("workspace_id")
    egress_decide.add_argument("request_id")
    egress_decide.add_argument("decision", choices=("allow", "deny"))
    egress_decide.add_argument(
        "--duration",
        choices=("once", "5m", "15m", "tilrestart", "forever"),
        default="tilrestart",
        help="how long enforcement honors the verdict",
    )
    egress_revoke = egress_sub.add_parser(
        "revoke", help="undo an in-effect verdict"
    )
    egress_revoke.add_argument("workspace_id")
    egress_revoke.add_argument("request_id")
    egress_tui = egress_sub.add_parser(
        "tui",
        help="the consent decider TUI (#195): live holds, verdicts, rules",
    )
    egress_tui.add_argument("workspace_id", help="decide for this workspace")
    egress_watch = egress_sub.add_parser(
        "watch", help="stream egress frames as lines; registers as a decider"
    )
    egress_watch.add_argument(
        "workspace_id",
        nargs="?",
        default=None,
        help="decide for this workspace (hold SYNs only while a "
        "decider is connected)",
    )
    egress_watch.add_argument(
        "--decide",
        action="store_true",
        help="prompt y/n for each pending request",
    )
    egress_watch.add_argument(
        "--duration",
        choices=("once", "5m", "15m", "tilrestart", "forever"),
        default="tilrestart",
        help="the duration a --decide allow applies",
    )
    starter = sub.add_parser("start", help="boot a created workspace")
    starter.add_argument(
        "workspace_id", help="the workspace to boot (name or id)"
    )
    stopper = sub.add_parser("stop", help="power a workspace off")
    stopper.add_argument(
        "workspace_id", help="the workspace to stop (name or id)"
    )
    resizer = sub.add_parser(
        "resize", help="grow (or shrink) a stopped workspace's disks (#184)"
    )
    resizer.add_argument(
        "workspace_id", help="the workspace to resize (name or id)"
    )
    resizer.add_argument(
        "--home-mib",
        type=int,
        help="new /home volume size, MiB (grows or shrinks)",
    )
    resizer.add_argument(
        "--root-mib",
        type=int,
        help="new root overlay size, MiB (grows only)",
    )
    remover = sub.add_parser("rm", help="delete workspaces and their data")
    remover.add_argument(
        "workspace_ids",
        nargs="+",
        help="the workspaces to delete, in order (name or id)",
    )
    console = sub.add_parser(
        "console", help="interactive shell in a workspace"
    )
    console.add_argument(
        "workspace_id", help="the workspace to attach to (name or id)"
    )
    console.add_argument(
        "--user",
        default=None,
        help="shell user (default: the workspace's login user, #248; "
        "--user root is the recovery shell)",
    )
    forward = sub.add_parser(
        "forward", help="bridge a workspace TCP port to stdio or a local port"
    )
    forward.add_argument(
        "workspace_id", help="the workspace to reach (name or id)"
    )
    forward.add_argument("port", type=int, help="the guest TCP port to reach")
    forward.add_argument(
        "--local",
        type=int,
        metavar="PORT",
        help="bind 127.0.0.1:PORT instead of stdio; every accepted "
        "connection gets its own forward",
    )
    key = sub.add_parser(
        "key",
        help="fetch a workspace's ssh identity (#111; the public half "
        "alone for a client-minted #121 workspace)",
    )
    key.add_argument(
        "workspace_id",
        help="the workspace whose identity to fetch (name or id)",
    )
    key_private = key.add_mutually_exclusive_group()
    key_private.add_argument(
        "--private",
        action="store_true",
        help="print the private half instead of the public line",
    )
    key_private.add_argument(
        "--out",
        metavar="FILE",
        help="write the private half to FILE (mode 0600) instead of printing",
    )
    llm_token_cmd = sub.add_parser(
        "llm-token",
        help="fetch a workspace's LLM proxy credential (#259)",
    )
    llm_token_cmd.add_argument(
        "workspace_id",
        help="the workspace whose credential to fetch (name or id)",
    )
    llm_token_cmd.add_argument(
        "--remint",
        action="store_true",
        help="mint a fresh credential, replacing the stored one",
    )
    ssh = sub.add_parser(
        "ssh",
        help=(
            "ssh into a workspace over the forward, identity staged in memory"
        ),
    )
    ssh.add_argument(
        "workspace_id", help="the workspace to log into (name or id)"
    )
    ssh.add_argument(
        "passthrough",
        nargs=argparse.REMAINDER,
        metavar="ARGS",
        help="arguments passed to ssh verbatim ('-l root' is the recovery "
        "login; '-A' forwards your agent, $SSH_AUTH_SOCK)",
    )
    rsync_cmd = sub.add_parser(
        "rsync",
        help=(
            "rsync files to and from a workspace over the forward, "
            "identity staged in memory"
        ),
    )
    rsync_cmd.add_argument(
        "workspace_id", help="the workspace to copy against (name or id)"
    )
    rsync_cmd.add_argument(
        "passthrough",
        nargs=argparse.REMAINDER,
        metavar="ARGS",
        help="arguments passed to rsync verbatim; an empty-host path "
        "(:/remote/path, user@:/remote/path) targets this workspace",
    )
    image = sub.add_parser(
        "image", help="manage the daemon's image catalog (#65)"
    )
    image_sub = image.add_subparsers(
        dest="image_command",
        required=True,
        title="commands",
        metavar="<command>",
        parser_class=command_parser,
    )
    image_ls = image_sub.add_parser("ls", help="list catalog images")
    image_ls.add_argument(
        "--json", action="store_true", help="one JSON document"
    )
    image_import = image_sub.add_parser(
        "import",
        help="register an image archive from a daemon-side path or an "
        "https:// URL",
    )
    image_import.add_argument(
        "source",
        help="archive path as the daemon sees it (its own filesystem; "
        "the file is read by the daemon, not uploaded by this command) "
        "or an https:// URL the daemon downloads itself (#258)",
    )
    image_check = image_sub.add_parser(
        "check",
        help="boot an image and verify the guest contract (#258); "
        "local — needs /dev/kvm, --egress needs root",
    )
    # The flags come from the leaf module conformance_args: the
    # same definitions the standalone entry parses, with none of
    # the daemon composition importing them would drag in.
    check_arguments(image_check)

    image_rm = image_sub.add_parser(
        "rm", help="remove an image from the catalog"
    )
    image_rm.add_argument(
        "ref",
        help="name:version, bare name (newest), name@hash (full hash), "
        "or hash (a unique hash prefix works too)",
    )
    image_info = image_sub.add_parser(
        "info", help="show one image's full record"
    )
    image_info.add_argument(
        "ref",
        help="name:version, bare name, name@hash, or hash "
        "(a unique hash prefix works too)",
    )
    home = sub.add_parser(
        "home", help="move a workspace's /home volume through the daemon (#80)"
    )
    home_sub = home.add_subparsers(
        dest="home_command",
        required=True,
        title="commands",
        metavar="<command>",
        parser_class=command_parser,
    )
    home_export = home_sub.add_parser(
        "export", help="download a workspace's /home volume"
    )
    home_export.add_argument(
        "workspace_id",
        help="the workspace whose volume to download (name or id)",
    )
    home_export.add_argument(
        "file",
        nargs="?",
        default=None,
        help="output file (default: <workspace_id>.ext4); - writes stdout",
    )
    home_import = home_sub.add_parser(
        "import", help="replace a workspace's /home volume from an ext4 image"
    )
    home_import.add_argument(
        "workspace_id",
        help="the workspace whose volume to replace (name or id)",
    )
    home_import.add_argument(
        "file", help="the ext4 volume image to upload; - reads stdin"
    )
    secret = sub.add_parser(
        "secret",
        help="placeholder secrets: mint, list, revoke, renew, check",
    )
    secret_sub = secret.add_subparsers(
        dest="secret_command",
        required=True,
        title="commands",
        metavar="<command>",
        parser_class=command_parser,
    )
    secret_mint = secret_sub.add_parser(
        "mint", help="mint a placeholder for one workspace"
    )
    secret_mint.add_argument(
        "workspace_id", help="the workspace the placeholder binds to"
    )
    secret_mint.add_argument(
        "--name", required=True, help="the placeholder's label"
    )
    secret_mint.add_argument(
        "--dest",
        required=True,
        action="append",
        dest="dests",
        metavar="HOST",
        help=(
            "an allowlist destination: an exact host "
            "(api.github.com) or a suffix (.github.com); repeatable"
        ),
    )
    secret_mint.add_argument(
        "--ttl",
        type=int,
        metavar="SECONDS",
        help="the placeholder's lifetime (default: unbounded)",
    )
    secret_mint.add_argument(
        "--secret-file",
        required=True,
        metavar="PATH",
        help=(
            "the file holding the real secret; - reads stdin "
            "(pipe it from a password manager)"
        ),
    )
    secret_ls = secret_sub.add_parser(
        "ls", help="list placeholders (sentinels are never listed)"
    )
    secret_ls.add_argument(
        "--json", action="store_true", help="one JSON document"
    )
    secret_revoke = secret_sub.add_parser(
        "revoke", help="revoke a placeholder (effective next request)"
    )
    secret_revoke.add_argument("workspace_id")
    secret_revoke.add_argument("--name", required=True)
    secret_renew = secret_sub.add_parser(
        "renew", help="extend a placeholder's lifetime in place"
    )
    secret_renew.add_argument("workspace_id")
    secret_renew.add_argument("--name", required=True)
    secret_renew.add_argument(
        "--ttl",
        required=True,
        type=int,
        metavar="SECONDS",
        help="the new lifetime from now",
    )
    secret_sub.add_parser(
        "check",
        help="verify the configured secret store answers writes",
    )
    return parser


def main(argv: list[str] | None = None, transport=None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return dispatch(args, transport)
    except KeyboardInterrupt:
        # A Ctrl-C during a long boot: one line, not a traceback (a
        # raw-mode session never gets here — Ctrl-C reaches the guest).
        print("msks: interrupted", file=sys.stderr)
        raise SystemExit(130) from None


def create_identity(args: argparse.Namespace) -> tuple[str | None, str | None]:
    """The create's identity mode: ``(mint key type, supplied line)``.

    The client mint is the default (#121): absent flags mint locally
    (ed25519, the same FIPS-approvable default the daemon mints).
    ``--daemon-mint`` hands the identity to the daemon (escrow on
    the daemon). ``--pubkey`` supplies an operator key (#132) — any
    well-formed type, no mint, nothing written client-side. The
    three modes are exclusive
    (:func:`check_identity_conflicts` names the pairings).
    """
    check_identity_conflicts(args)
    if args.daemon_mint:
        return None, None
    if args.pubkey is not None:
        return None, read_pubkey(args.pubkey)
    return args.key_type or "ed25519", None


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


def flag_set(args: argparse.Namespace, name: str) -> bool:
    """Whether a flag was supplied — a store_true flag by truth, a
    value flag by presence (an explicit empty value counts, so
    ``--pubkey ""`` still conflicts rather than slipping past)."""
    value = getattr(args, name)
    return bool(value) if name == "daemon_mint" else value is not None


def check_identity_conflicts(args: argparse.Namespace) -> None:
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


def run_resize(args: argparse.Namespace, transport) -> int:
    """Compose the request body, refusing the empty one locally."""
    body = {
        key: value
        for key, value in (
            ("home_mib", args.home_mib),
            ("root_mib", args.root_mib),
        )
        if value is not None
    }
    if not body:
        raise SystemExit(
            "msks: nothing to resize: pass --home-mib, --root-mib, or both"
        )
    return cmd_resize(args.workspace_id, body, transport)


def run_create(args: argparse.Namespace, transport) -> int:
    """Resolve the identity mode once — the resolver may read stdin
    (``--pubkey -``) or reject a flag pairing, so it runs a single
    time — then create."""
    if args.pubkey == "-" and args.user_data == "-":
        raise SystemExit(
            "msks: --pubkey - and --user-data - both read stdin; "
            "pass one of them by file"
        )
    key_type, pubkey = create_identity(args)
    return cmd_create(
        create_body(args),
        args.start,
        transport=transport,
        key_type=key_type,
        pubkey=pubkey,
    )


def command_table(args: argparse.Namespace, transport) -> dict:
    """One entry per subcommand: its zero-argument body."""
    return {
        "ls": lambda: cmd_ls(args.json, transport=transport),
        "storage": lambda: cmd_storage(
            args.workspace, args.json, transport=transport
        ),
        "create": lambda: run_create(args, transport),
        "start": lambda: cmd_start(args.workspace_id, transport=transport),
        "stop": lambda: cmd_stop(args.workspace_id, transport=transport),
        "resize": lambda: run_resize(args, transport),
        "rm": lambda: cmd_rm(args.workspace_ids, transport=transport),
        "console": lambda: run_workspace_shell(args.workspace_id, args.user),
        "forward": lambda: run_workspace_forward(
            args.workspace_id, args.port, args.local
        ),
        "key": lambda: cmd_key(
            args.workspace_id, args.private, args.out, transport=transport
        ),
        "llm-token": lambda: cmd_llm_token(
            args.workspace_id, args.remint, transport=transport
        ),
        "ssh": lambda: run_workspace_ssh(
            args.workspace_id, args.passthrough, transport=transport
        ),
        "rsync": lambda: run_workspace_rsync(
            args.workspace_id, args.passthrough, transport=transport
        ),
        "egress": lambda: egress_command_table(args, transport)[
            args.egress_command
        ](),
        "image": lambda: image_command_table(args, transport)[
            args.image_command
        ](),
        "home": lambda: home_command_table(args, transport)[
            args.home_command
        ](),
        "secret": lambda: secret_command_table(args, transport)[
            args.secret_command
        ](),
    }


def egress_command_table(args: argparse.Namespace, transport) -> dict:
    """One entry per ``egress`` subcommand."""
    return {
        "tui": lambda: run_consent_tui(args.workspace_id),
        "rules": lambda: asyncio.run(
            egress_mod.run_rules(args.workspace_id, transport=transport)
        ),
        "requests": lambda: asyncio.run(
            egress_mod.run_requests(
                args.workspace_id, args.decision, transport=transport
            )
        ),
        "decide": lambda: asyncio.run(
            egress_mod.run_decide(
                args.workspace_id,
                args.request_id,
                args.decision,
                args.duration,
                transport=transport,
            )
        ),
        "revoke": lambda: asyncio.run(
            egress_mod.run_revoke(
                args.workspace_id, args.request_id, transport=transport
            )
        ),
        "watch": lambda: asyncio.run(
            egress_mod.run_watch(args.workspace_id, args.decide, args.duration)
        ),
    }


def image_command_table(args: argparse.Namespace, transport) -> dict:
    """One entry per ``image`` subcommand."""
    return {
        "ls": lambda: cmd_image_ls(args.json, transport=transport),
        "import": lambda: cmd_image_import(args.source, transport=transport),
        "check": lambda: cmd_image_check(args),
        "rm": lambda: cmd_image_rm(args.ref, transport=transport),
        "info": lambda: cmd_image_info(args.ref, transport=transport),
    }


def home_command_table(args: argparse.Namespace, transport) -> dict:
    """One entry per ``home`` subcommand."""
    return {
        "export": lambda: cmd_home_export(
            args.workspace_id, args.file, transport=transport
        ),
        "import": lambda: cmd_home_import(
            args.workspace_id, args.file, transport=transport
        ),
    }


def dispatch(args: argparse.Namespace, transport=None) -> int:
    """Run one parsed command."""
    return command_table(args, transport)[args.command]()


if __name__ == "__main__":
    raise SystemExit(main())
