"""The workspace lifecycle commands (#21): ``msks ls``, ``create``,
``start``, ``stop``, ``rm``, ``key``, ``llm-token``, and ``resize``
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

from ..identity import KEY_TYPES
from .context import call, env_token, env_url, ssl_context
from .create import (
    checked_login_name,
    create_workspace_core,
    invoking_user,
    operator_pubkey,
)
from .resize import display_name, resize_message
from .rest import fetch_llm_token as rest_fetch_llm_token
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


#: The ``--key-type`` flag's help line: the mint's set spelled out
#: from the identity module's own vocabulary — the one statement
#: the client carries at the identity surface for it, so the
#: dispatcher imports the line, not the set.
KEY_TYPE_HELP = (
    "opt into the per-workspace client mint (#121): a fresh "
    "keypair minted on this client per workspace — public half "
    "sent, private half kept mode 0600 under the client data root "
    "(`~/.local/share/msks/<id>/identity`, or that root under "
    "MSKSC_DATA_DIR) where msks ssh finds it. TYPE names the mint's "
    f"key type, one of {', '.join(sorted(KEY_TYPES))} (default "
    "ed25519, the same FIPS-approvable default the daemon mints)"
)


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
    if remint:
        reply = asyncio.run(
            call(
                "POST",
                f"/api/v1/workspaces/{workspace_id}/llm-token",
                transport=transport,
            )
        )
        print(reply["token"])
        # The update step rides stderr so stdout stays the bare
        # token for scripts that pipe it (#375); the seed is
        # immutable create-time input, so the guest's copy keeps
        # serving the old credential until this lands.
        print(
            f"msks: the workspace keeps serving the old token until "
            f"updated — write this one to /etc/msks/llm.token as "
            f"root in {workspace_id} (msks console --user root, or "
            f"msks ssh -l root), then open a new login shell",
            file=sys.stderr,
        )
        return 0
    reply = asyncio.run(
        rest_fetch_llm_token(env_url(), env_token(), workspace_id, transport)
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


def checked_key_type(key_type: str | None) -> None:
    """One local line for a key type outside the mint's set — the
    identity module stays the single source of the choices. A
    usage refusal: one line, exit 2 (run_parsed maps it)."""
    if key_type is not None and key_type not in KEY_TYPES:
        raise UsageError(
            f"--key-type must be one of {', '.join(sorted(KEY_TYPES))}"
        )


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
    daemon_mint: bool = False
    pubkey: str | None = None
    key_type: str | None = None
    start: bool = False


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
    per-workspace client mint (#121); ``--daemon-mint`` hands the
    identity to the daemon (#111);
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
