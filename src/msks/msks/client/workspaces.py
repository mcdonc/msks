"""The workspace lifecycle commands (#21): ``msks ls``, ``create``,
``start``, ``stop``, ``rm``, ``key``, and ``resize``
— the group the CLI's typer layer (:mod:`msks.client.cli`)
dispatches into. The create core (the identity modes and the
POST's shared plumbing) lives in :mod:`msks.client.create`;
this module owns the operator surface: the parsed flag surface,
the identity-mode resolution, and the printed lines.
"""

import asyncio
import contextlib
import json
import os
import sys
from dataclasses import dataclass
from pathlib import Path

from .context import call, env_token, env_url, ssl_context
from .create import (
    checked_login_name,
    create_workspace_core,
    invoking_user,
    operator_pubkey,
)
from .resize import display_name, resize_message
from .rest import fetch_ssh_key as rest_fetch_ssh_key
from .ssh import IDENTITY_FILE_ENV, data_dir
from .tabular import listing_text

try:
    # Typer vendors its own click (the 0.16+ line); its exceptions
    # are the ones a parse of the app raises. Older typer rides
    # the installed click instead — the pyproject floor spans both.
    from typer._click.exceptions import UsageError
except ImportError:  # pragma: no cover — the floor spans both eras
    from click.exceptions import UsageError


def created_date(raw: str | None) -> str:
    """ISO timestamp to YYYY-MM-DD, or ``-``."""
    return raw[:10] if raw else "-"


def workspace_cells(row: dict) -> list[str]:
    """One listing row's cells."""
    image = (row.get("image_hash") or "-")[:12]
    host = row.get("host") or "-"
    name = row.get("name") or "-"
    egress_mode = row.get("egress_mode") or "-"
    created = created_date(row.get("created_at"))
    return [name, row["id"], row["status"], egress_mode, created, image, host]


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


def cmd_create(
    body: dict,
    start: bool = False,
    transport=None,
    pubkey: str | None = None,
    identity_note: str | None = None,
) -> int:
    """``msks create``: one workspace, optionally booted.

    ``pubkey`` is a one-off operator-supplied public line (#132,
    ``--pubkey``); otherwise the operator's configured key supplies
    the half (#336, #486) — no private half anywhere msks manages,
    either way. ``identity_note`` is the configured key's
    confirmation line (the identity source the create planted).
    """
    asyncio.run(
        create_workspace(
            env_url(),
            env_token(),
            body,
            start,
            transport,
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
    pubkey: str | None = None,
    identity_note: str | None = None,
) -> dict:
    """POST the workspace, print its name and id, then boot it when
    asked.

    The created line prints the moment the workspace exists (the
    core's ``announce`` hook): a failed start must not hide that
    the workspace exists — recover with ``msks start``. The daemon
    mints the workspace's immutable id (#246); the follow-up calls
    (boot, identity) address the workspace by that id, and the
    label the operator typed stays the day-to-day reference. Only
    the public line travels and nothing is written client-side
    (#486): the private half stays wherever the operator keeps it,
    read in place. ``identity_note`` (the configured key's source
    line) prints after the created line.
    """
    # One TLS context serves the create and the boot (an unverified
    # daemon warns once per context — the pair must not warn twice).
    ssl_ctx = None if transport is not None else ssl_context()
    row = await create_workspace_core(
        url,
        token,
        body,
        transport,
        ssl_ctx=ssl_ctx,
        pubkey=pubkey,
        announce=print_created_line,
    )
    if identity_note is not None:
        print(identity_note)
    if not start:
        return row
    await boot_created(row, transport, ssl_ctx)
    return row


def print_created_line(row: dict) -> None:
    """The core's ``announce`` hook: the created line prints the
    moment the workspace exists, before any follow-up check can
    refuse — a refused verification still leaves the line (and the
    workspace) on the record."""
    print(created_line(row))


async def boot_created(row: dict, transport, ssl_ctx=None) -> None:
    """Boot the freshly created row, with the recovery hint on a
    failed start (the workspace exists; the hint names the command
    that reaches it later); the caller's TLS context rides along,
    keeping the unverified-mode warning to the pair's one print."""
    try:
        await call(
            "POST",
            f"/api/v1/workspaces/{row['id']}/start",
            transport=transport,
            ssl_ctx=ssl_ctx,
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
        call(
            "POST",
            f"/api/v1/workspaces/{workspace_id}/start",
            transport=transport,
        )
    )
    print(f"{workspace_id} {row['status']}")
    return 0


def cmd_stop(workspace_id: str, transport=None) -> int:
    """``msks stop``: power a workspace off, gracefully."""
    row = asyncio.run(
        call(
            "POST",
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
    for workspace_id in workspace_ids:
        asyncio.run(
            call(
                "DELETE",
                f"/api/v1/workspaces/{workspace_id}",
                transport=transport,
            )
        )
        print(f"{workspace_id} deleted")
    return 0


async def fetch_ssh_key(url, token, workspace_id, transport) -> dict:
    """GET the workspace's identity: type, public half, private half
    (null for every workspace created now, #486; a row minted
    before it keeps serving the half it holds).

    A re-export of :func:`msks.client.rest.fetch_ssh_key` (the call
    moved to rest.py when ``msks ssh`` (#112) began sharing it);
    ``msks key`` prints it, ``msks ssh`` stages the private half in
    memory for the duration of a connection.
    """
    return await rest_fetch_ssh_key(url, token, workspace_id, transport)


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
    the daemon never held (#486 — every workspace created now; the
    client-minted mode #121 before it): the error names the places
    that half can be instead of printing nothing."""
    if key["private_key"] is None:
        directory = key.get("id") or workspace_id
        raise SystemExit(
            f"msks key: the daemon holds no private half for {workspace_id}. "
            "msks never holds a private half for a workspace created now — "
            "the operator's own key is the identity: point identity_file "
            f"(or {IDENTITY_FILE_ENV}) at its private file, or use that "
            "key directly. A key an older msks minted on a client may live "
            f"at {data_dir() / directory / 'identity'} on that machine"
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
    file with mode 0600 and prints nothing but its path. The
    private half is null for every workspace created now (#486 —
    the operator's own key is the identity, and msks never holds
    its private half); the private forms explain where that half
    lives instead. A row minted before #486 keeps serving the half
    the daemon still holds.
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


def checked_key_flags(as_private: bool, out: str | None) -> None:
    """--private and --out are exclusive: one output shape — a
    usage refusal (one line, exit 2, run_parsed's mapping)."""
    if as_private and out is not None:
        raise UsageError("--private and --out are exclusive")


def cmd_resize(
    workspace_id: str,
    body: dict,
    transport=None,
) -> int:
    """``msks resize``: move a stopped workspace's sizes and
    topology (#184, #277)."""
    row = asyncio.run(
        call(
            "POST",
            f"/api/v1/workspaces/{workspace_id}/resize",
            json_body=body,
            transport=transport,
        )
    )
    print(resize_message(row, body))
    return 0


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
    """The create fields the operator set that ride beside the
    specs: the consent posture and allowlist (#69), and the
    daemon-wide placeholder posture (#339)."""
    fields = {}
    if getattr(args, "egress_mode", None) is not None:
        fields["egress_mode"] = args.egress_mode
    if getattr(args, "secret_coverage", None) is not None:
        fields["secret_coverage"] = args.secret_coverage
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
    secret_coverage: str | None = None
    allow: list[str] | None = None
    user_data: str | None = None
    user: str | None = None
    pubkey: str | None = None
    start: bool = False


def create_identity(args: CreateFlags) -> tuple[str | None, str | None]:
    """The create's identity source (#486): ``(supplied line,
    identity note)``.

    One operator key becomes every workspace's key: the file
    ``identity_file`` / ``MSKSC_IDENTITY_FILE`` names, its derived
    public half on the wire, the private file never copied
    anywhere — an unconfigured client refuses rather than
    generating (:func:`msks.client.create.operator_pubkey`).
    ``--pubkey`` supplies a one-off operator line instead (#132).
    """
    if args.pubkey is not None:
        return read_pubkey(args.pubkey), None
    return operator_pubkey()


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


def run_create(args: CreateFlags, transport) -> int:
    """Build the body, then resolve the identity source once — the
    body build fails on local grounds (a bad ``--user``, an
    unreadable ``--user-data``) before the resolver can read stdin
    (``--pubkey -``) or load the operator identity, so it runs a
    single time — then create."""
    if args.pubkey == "-" and args.user_data == "-":
        raise SystemExit(
            "msks: --pubkey - and --user-data - both read stdin; "
            "pass one of them by file"
        )
    body = create_body(args)
    pubkey, note = create_identity(args)
    return cmd_create(
        body,
        args.start,
        transport=transport,
        pubkey=pubkey,
        identity_note=note,
    )
