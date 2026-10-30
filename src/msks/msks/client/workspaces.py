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
import posixpath
import sys
from dataclasses import dataclass
from pathlib import Path

from .context import call
from .create import (
    checked_login_name,
    create_workspace_core,
    invoking_user,
    operator_pubkey,
    read_pubkey,
)
from .env import env_token, env_url, is_unix, ssl_context
from .resize import display_name, resize_message
from .rest import fetch_ssh_key as rest_fetch_ssh_key
from .tabular import listing_text
from .tui.rows import clip


def created_date(raw: str | None) -> str:
    """ISO timestamp to YYYY-MM-DD, or ``-``."""
    return raw[:10] if raw else "-"


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
    part #624): a row whose mounts skip or conflict carries the
    count beside its status — ``running, 1 mount skipped`` — the
    marker an unresolved share leaves until the config and the
    row agree again. The healthy row prints its status alone."""
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
    # The local listener (#555) builds none: the kernel vouches.
    ssl_ctx = None if transport is not None or is_unix(url) else ssl_context()
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
    """GET the workspace's identity: the public half alone (#497 —
    the answer carries no private field), with the row's id, name,
    instance stamp, and login user beside it.

    A re-export of :func:`msks.client.rest.fetch_ssh_key` (the call
    moved to rest.py when ``msks ssh`` (#112) began sharing it);
    ``msks key`` prints it, and ``msks ssh`` resolves the session's
    signing key from it.
    """
    return await rest_fetch_ssh_key(url, token, workspace_id, transport)


def cmd_key(
    workspace_id: str,
    transport=None,
) -> int:
    """``msks key``: the workspace's ssh identity — the public
    half, safe to display anywhere. The private half is the
    operator's own key and never crosses the API (#486, #497:
    the daemon stores the public line alone), so the command
    serves nothing else.
    """
    key = asyncio.run(
        fetch_ssh_key(env_url(), env_token(), workspace_id, transport)
    )
    print(key["public_key"])
    return 0


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


def mount_spec(spec: str, flag: str) -> dict:
    """One ``HOST-OR-SHARE:GUEST_PATH[:MODE]`` as the wire's mount
    entry: the host form (#667, #673) when the first field names a
    path — absolute or ``~``-shaped, expanded against the
    client's home — the named form (#620) otherwise. The guest
    path is absolute in both; the mode names ``ro`` or ``rw``
    (``rw`` is the daemon's default). The local refusals are the
    shape ones — the daemon owns the rules and names them in its
    400 (which gate refused a host path, and the remedy)."""
    fields = spec.split(":")
    if len(fields) not in (2, 3):
        raise SystemExit(
            f"msks: {flag} takes HOST-OR-SHARE:GUEST_PATH[:MODE], got {spec!r}"
        )
    served, guest = fields[0], fields[1]
    if not guest.startswith("/"):
        raise SystemExit(f"msks: {flag} guest path {guest!r} must be absolute")
    entry = mount_entry_for(served, flag)
    entry["guest"] = guest
    if len(fields) == 3:
        if fields[2] not in ("ro", "rw"):
            raise SystemExit(
                f"msks: {flag} mode must be ro or rw, got {fields[2]!r}"
            )
        entry["mode"] = fields[2]
    return entry


def mount_entry_for(served: str, flag: str) -> dict:
    """The naming half of one mount spec: a path-shaped first
    field is the host form — ``~`` and ``~user`` expand against
    the client's home, then the path must be absolute (the daemon
    refuses ``~`` itself; the expansion is the client's job) —
    anything else is the named form's share name. The create's
    ``--mount`` flag keeps its meaning (a configured share): a
    path-shaped spec there is one local line pointing at
    ``--share``; the runtime ``mount`` command takes both."""
    if not served.startswith(("/", "~")):
        return {"share": served}
    if flag == "--mount":
        raise SystemExit(
            f"msks: --mount takes a configured share's name; pass "
            f"--share for the host path {served!r}"
        )
    host = os.path.expanduser(served)
    if not host.startswith("/"):
        raise SystemExit(
            f"msks: {flag} host path {served!r} must be absolute "
            "after ~ expands"
        )
    return {"host": canonical_host_spelling(host, served, flag)}


def canonical_host_spelling(host: str, served: str, flag: str) -> str:
    """One host path in the daemon's canonical shape: ``..``
    refused outright (the daemon's own rule — parity, not silent
    collapsing), then normpath with the POSIX two-leading-slashes
    anchor collapsed to one (normpath preserves it; the daemon's
    canonical spelling re-anchors). The mount side sends this
    shape and the umount matcher takes the same one, so the round
    trip holds for every spelling the mount side accepts."""
    if ".." in host.split("/"):
        raise SystemExit(
            f"msks: {flag} host path {served!r} must not contain '..'"
        )
    return posixpath.normpath("/" + host.lstrip("/"))


def cmd_mount(
    workspace_id: str,
    spec: str,
    transport=None,
) -> int:
    """``msks mount``: add one share to the workspace's mounts
    (#620, part #624) — the set replaces whole through the route,
    so the command reads the row, adds the entry, and posts the
    list. Next-start semantics: a running VM keeps its devices;
    the next start attaches the new set."""
    return asyncio.run(mounts_exchange(workspace_id, "add", spec, transport))


def cmd_umount(
    workspace_id: str,
    share: str,
    transport=None,
) -> int:
    """``msks umount``: remove the workspace's mounts of one share
    (#620, part #624). The next start attaches the remainder."""
    return asyncio.run(
        mounts_exchange(workspace_id, "remove", share, transport)
    )


async def mounts_exchange(
    workspace_id: str, action: str, spec: str, transport
) -> int:
    """The mount/umount exchange: read the row, apply the one
    edit, post the whole list — the daemon validates and answers
    with the refreshed row, whose derived state prints."""
    row = await call(
        "GET", f"/api/v1/workspaces/{workspace_id}", transport=transport
    )
    mounts = mounts_after(row, action, spec, workspace_id)
    fresh = await call(
        "POST",
        f"/api/v1/workspaces/{workspace_id}/mounts",
        json_body={"mounts": mounts},
        transport=transport,
    )
    print(mounts_line(fresh))
    return 0


def mounts_after(row: dict, action: str, spec: str, workspace_id: str) -> list:
    """The next list: the row's mounts with the one edit applied —
    the add joins, the remove drops the share's mounts (a share
    the row does not mount is one local line)."""
    mounts = list(row.get("mounts") or [])
    if action == "add":
        return [*mounts, mount_spec(spec, "mount")]
    return without_share(mounts, spec, workspace_id)


def umount_target(served: str) -> str:
    """What the umount argument names: a path-shaped argument is
    the host spelling the mount side would send (`~` expanded,
    canonicalized the same way); a bare name is the share's name,
    untouched."""
    if not served.startswith(("/", "~")):
        return served
    return canonical_host_spelling(
        os.path.expanduser(served), served, "umount"
    )


def without_share(mounts: list, served: str, workspace_id: str) -> list:
    """The list minus what the argument names — a share name or a
    host path, matched on the key that carries it (``~`` expands
    the way the mount spec did). What the row does not mount is
    one local line."""
    wanted = umount_target(served)
    kept = [
        m
        for m in mounts
        if m.get("share") != wanted and m.get("host") != wanted
    ]
    if len(kept) == len(mounts):
        raise SystemExit(
            f"msks: workspace {workspace_id} mounts no {served!r}"
        )
    return kept


def mount_row_label(row: dict) -> str:
    """The row's everyday reference: its name, or its id when the
    row carries none (the labelless workspace)."""
    name = row.get("name")
    return name if name else row["id"]


def mounts_line(row: dict) -> str:
    """The mount/umount outcome line: the workspace, its mounts,
    and any skips the daemon derived — the next start attaches
    exactly this set, skips named."""
    mounts = row.get("mounts") or []
    if not mounts:
        return f"{mount_row_label(row)} mounts nothing"
    rendered = ", ".join(spec_text(m) for m in mounts)
    return (
        f"{mount_row_label(row)} mounts {rendered}"
        f"{issue_notes(row)} (next start)"
    )


def issue_notes(row: dict) -> str:
    """The derived skips and conflicts as the outcome line's
    em-dash half; empty on the healthy row."""
    issues = row.get("mount_issues") or []
    if not issues:
        return ""
    notes = "; ".join(
        f"{mount_identity(i)} {i['kind']} ({i['detail']})" for i in issues
    )
    return f" — {notes}"


def mount_identity(entry: dict) -> str:
    """What a mount row or issue frame serves, by its form: the
    host path for a #672 host mount, the share name for a named
    one — the one key either frame always carries."""
    return entry.get("host") or entry.get("share") or "?"


def spec_text(mount: dict) -> str:
    """One mount's ``HOST-OR-SHARE:GUEST:MODE`` spelling — the
    mode always shown, the wire's default named per form."""
    mode = mount.get("mode") or "rw"
    return f"{mount_identity(mount)}:{mount['guest']}:{mode}"


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
    body.update(mount_fields(args))
    return body


def mount_fields(args: CreateFlags) -> dict:
    """The create's --mount (named, #620) and --share (host form,
    #667) specs as the body's mounts list: shape refusals are one
    local line; the daemon owns the share, gate, and rule checks
    — a refused host path names its gate and remedy in the 400."""
    entries = []
    for flag, specs in (("--mount", args.mount), ("--share", args.share)):
        for spec in specs or []:
            entries.append(mount_spec(spec, flag))
    if not entries:
        return {}
    return {"mounts": entries}


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
    mount: list[str] | None = None
    share: list[str] | None = None
    start: bool = False


def create_identity(args: CreateFlags) -> tuple[str | None, str | None]:
    """The create's identity source (#486): ``(supplied line,
    identity note)``.

    One operator key becomes every workspace's key: the public
    half ``pubkey_file`` / ``MSKSC_PUBKEY_FILE`` names (#495, read
    in place of any private material), else the file
    ``identity_file`` / ``MSKSC_IDENTITY_FILE`` names, its derived
    public half on the wire, the private file never copied
    anywhere — an unconfigured client refuses rather than
    generating (:func:`msks.client.create.operator_pubkey`).
    ``--pubkey`` supplies a one-off operator line instead (#132).
    """
    if args.pubkey is not None:
        return read_pubkey(args.pubkey), None
    return operator_pubkey()


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
