"""The workspace lifecycle command bodies (#21): ``create``,
``start``, ``stop``, ``clone``, ``rm``, ``key``, ``resize``, and
the mount/umount exchange — the group the CLI's typer layer
(:mod:`msks.client.cli`) dispatches into. The create core (the
identity modes and the POST's shared plumbing) lives in
:mod:`msks.client.create`; this module owns the operator surface:
the parsed flag surface, the identity-mode resolution, and the
printed lines. The listing surface lives in
:mod:`msks.client.workspaces.listing`.
"""

import asyncio
import os
import posixpath
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from ..context import (
    call,
    env_token,
    env_url,
    is_unix,
    scope_params,
    ssl_context,
)
from ..context import (
    fetch_ssh_key as rest_fetch_ssh_key,
)
from ..create import (
    checked_login_name,
    create_workspace_core,
    invoking_user,
    operator_pubkey,
    read_pubkey,
)
from ..jsonio import ApiExit, emit, json_line, outcome
from ..proxies import needs_bridge, workspace_proxies
from ..resize import display_name, resize_message


def cmd_create(
    body: dict,
    start: bool = False,
    transport=None,
    pubkey: str | None = None,
    identity_note: str | None = None,
    as_json: bool = False,
) -> int:
    """``msks create``: one workspace, optionally booted.

    ``pubkey`` is a one-off operator-supplied public line (#132,
    ``--pubkey``); otherwise the operator's configured key supplies
    the half (#336, #486) — no private half anywhere msks manages,
    either way. ``identity_note`` is the configured key's
    confirmation line (the identity source the create planted).
    """
    row = asyncio.run(
        create_workspace(
            env_url(),
            env_token(),
            body,
            start,
            transport,
            pubkey,
            identity_note,
            as_json,
        )
    )
    if as_json:
        emit(row)
    return 0


async def create_workspace(
    url,
    token,
    body,
    start,
    transport,
    pubkey: str | None = None,
    identity_note: str | None = None,
    as_json: bool = False,
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
    announce, note = creation_lines(identity_note, as_json)
    row = await create_workspace_core(
        url,
        token,
        body,
        transport,
        ssl_ctx=ssl_ctx,
        pubkey=pubkey,
        announce=announce,
    )
    if note is not None:
        print(note)
    if not start:
        return row
    await boot_created(row, transport, ssl_ctx, as_json)
    return row


def creation_lines(
    identity_note: str | None, as_json: bool
) -> tuple[object, str | None]:
    """The human confirmation pair (#841): the announce hook the
    core calls the moment the workspace exists, and the identity
    source line that follows it. JSON mode answers (None, None)
    — one document lands at the flow's end instead, and a failed
    start still leaves the structured error as the only output."""
    if as_json:
        return None, None
    return print_created_line, identity_note


def print_created_line(row: dict) -> None:
    """The core's ``announce`` hook: the created line prints the
    moment the workspace exists, before any follow-up check can
    refuse — a refused verification still leaves the line (and the
    workspace) on the record."""
    print(created_line(row))


async def boot_created(
    row: dict, transport, ssl_ctx=None, as_json: bool = False
) -> None:
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
        hint = (
            f"{exc}\nmsks: {row['id']} is created; "
            f"boot it later with: msks start {display_name(row)}"
        )
        if isinstance(exc, ApiExit):
            # The structured error keeps its status and detail
            # through the hint (#841): the exit table maps the
            # daemon's answer, not the recovery wording — the
            # structured kind rides along (#841's rule holds for
            # any kind word a future detail carries).
            raise ApiExit(
                hint, exc.api_status, exc.api_detail, kind=exc.api_kind
            ) from exc
        raise SystemExit(hint) from exc
    if not as_json:
        print(f"attach with: msks console {display_name(row)}")
    row["status"] = "running"


def created_line(row: dict) -> str:
    """The create confirmation: the label beside the daemon-minted,
    immutable id (#246) — the name is the everyday reference, the
    id is the one no future workspace will ever reuse."""
    return f"created {label_and_id(row)}"


def label_and_id(row: dict) -> str:
    """The label and id pair the confirmation lines paint: the
    name with the id beside it, the id alone on a nameless row
    (or one whose label is the id itself)."""
    name = row.get("name")
    if name and name != row["id"]:
        return f"{name} (id {row['id']})"
    return row["id"]


async def start_stop_row(
    action: str,
    workspace_id: str,
    as_json: bool,
    transport,
    owner: str | None = None,
) -> dict:
    """The row a start or stop answers, refreshed to the whole row
    in ``--json`` mode (#841): the POST's reply carries the id and
    status alone, and the machine-readable emit wants the row a
    follow-up ``ls`` would print — one GET, the verb already
    blocked to the terminal state. A refresh that fails cannot
    unrun the verb: the thin row the POST answered is the
    document, and the failure stays visible in the daemon's own
    next answer. *owner* is the scope a root invocation carries
    (#1084); both exchanges take it."""
    row = await call(
        "POST",
        f"/api/v1/workspaces/{workspace_id}/{action}",
        transport=transport,
        params=scope_params(owner),
    )
    if not as_json:
        return row
    try:
        return await call(
            "GET",
            f"/api/v1/workspaces/{row['id']}",
            transport=transport,
            params=scope_params(owner),
        )
    except SystemExit as exc:
        # The note is stderr (#841): stdout's document stays whole,
        # and the operator still learns the refresh died.
        print(
            f"msks: {workspace_id} {row['status']} (the refreshed "
            f"row could not be read: {exc})",
            file=sys.stderr,
        )
        return row


def spawn_holder(workspace_id: str) -> bool:
    """Leave a bridge holder behind (#1102): a detached child of
    this CLI that serves the workspace's forward rows until the
    workspace stops. It runs the same interpreter with the same
    environment (the daemon's address and the identity the shell
    presets), in its own session so the command's exit leaves it
    standing."""
    argv = [
        sys.executable,
        "-m",
        "msks.client.cli",
        "proxy",
        "hold",
        workspace_id,
    ]
    try:
        subprocess.Popen(
            argv,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    except OSError as exc:
        print(
            f"msks: the bridge holder did not start ({exc}); "
            f"'msks proxy hold {workspace_id}' serves it instead",
            file=sys.stderr,
        )
        return False
    return True


def bridge_hook(workspace_id: str, transport=None) -> None:
    """The start's bridge step (#1102): a workspace with forward
    proxy rows takes its holder at once — a detached child that
    serves them after this command exits. Best-effort by design:
    a start that arrives without the identity (a raw API call, a
    client without the key) keeps its exit code, the refusal is
    one stderr line, and the rows carry the named cause until the
    next bridge-bearing client visit."""
    try:
        rows = asyncio.run(workspace_proxies(workspace_id, transport))
        if not needs_bridge(rows):
            return
    except (Exception, SystemExit) as exc:
        # Best-effort by contract (#1102): the start keeps its
        # exit code whatever the rows read says.
        print(
            f"msks: bridge not held ({exc}); 'msks proxy hold "
            f"{workspace_id}' serves it",
            file=sys.stderr,
        )
        return
    if spawn_holder(workspace_id):
        print(
            "msks: bridge held in the background — the forward "
            "proxies serve while this machine can reach the daemon",
            file=sys.stderr,
        )


def cmd_start(
    workspace_id: str, as_json: bool = False, transport=None, owner=None
) -> int:
    """``msks start``: boot a created workspace. *owner* is the
    ``--user`` scope (#1084): root addressing another owner's
    workspace by its name."""
    row = asyncio.run(
        start_stop_row("start", workspace_id, as_json, transport, owner)
    )
    bridge_hook(row["id"], transport)
    outcome(as_json, row, lambda: f"{workspace_id} {row['status']}")
    return 0


def cmd_stop(
    workspace_id: str, as_json: bool = False, transport=None, owner=None
) -> int:
    """``msks stop``: power a workspace off, gracefully."""
    row = asyncio.run(
        start_stop_row("stop", workspace_id, as_json, transport, owner)
    )
    outcome(as_json, row, lambda: f"{workspace_id} {row['status']}")
    return 0


def cmd_clone(
    source: str,
    name: str,
    copy_allowlist: bool = False,
    as_json: bool = False,
    transport=None,
    owner: str | None = None,
) -> int:
    """``msks clone`` (#773): copy a stopped workspace into a new
    one — both disks and the create-time config. The daemon owns
    the whole rule set (the stopped-source gate, the full-fork
    copy, the fresh id); the command sends the name and prints
    the confirmation.

    ``copy_allowlist`` (#773's one option) opts the clone into
    the source's egress allowlist; without it the clone starts
    with an empty list under the copied mode. *owner* is the
    ``--user`` scope (#1084) the source resolve carries.
    """
    body = {"name": name}
    if copy_allowlist:
        body["copy_allowlist"] = True
    row = asyncio.run(
        call(
            "POST",
            f"/api/v1/workspaces/{source}/clone",
            json_body=body,
            transport=transport,
            params=scope_params(owner),
        )
    )
    outcome(as_json, row, lambda: f"cloned {source} into {label_and_id(row)}")
    return 0


def cmd_rm(
    workspace_ids: list[str],
    as_json: bool = False,
    transport=None,
    owner: str | None = None,
) -> int:
    """``msks rm``: delete workspaces and their persistent data.

    Ids are removed one at a time, in order; a failure stops the
    run with the API's one-line error (already-removed ids stay
    removed — and stay confirmed on stdout). *owner* is the
    ``--user`` scope (#1084) every removal's resolve carries.
    """
    for workspace_id in workspace_ids:
        reply = asyncio.run(
            call(
                "DELETE",
                f"/api/v1/workspaces/{workspace_id}",
                transport=transport,
                params=scope_params(owner),
            )
        )
        if as_json:
            # The daemon's resolved id: the spelling the operator
            # typed may be a name the row no longer matches.
            json_line({"removed": reply.get("deleted") or workspace_id})
        else:
            print(f"{workspace_id} deleted")
    return 0


async def fetch_ssh_key(
    url, token, workspace_id, transport, owner: str | None = None
) -> dict:
    """GET the workspace's identity: the public half alone (#497 —
    the answer carries no private field), with the row's id, name,
    instance stamp, and login user beside it. *owner* is the
    ``--user`` scope (#1084) the resolve carries.

    A re-export of :func:`msks.client.rest.fetch_ssh_key` (the call
    moved to rest.py when ``msks ssh`` (#112) began sharing it);
    ``msks key`` prints it, and ``msks ssh`` resolves the session's
    signing key from it.
    """
    return await rest_fetch_ssh_key(
        url, token, workspace_id, transport, owner=owner
    )


def cmd_key(
    workspace_id: str,
    as_json: bool = False,
    transport=None,
    owner: str | None = None,
) -> int:
    """``msks key``: the workspace's ssh identity — the public
    half, safe to display anywhere. The private half is the
    operator's own key and never crosses the API (#486, #497:
    the daemon stores the public line alone), so the command
    serves nothing else.
    """
    key = asyncio.run(
        fetch_ssh_key(env_url(), env_token(), workspace_id, transport, owner)
    )
    outcome(as_json, key, lambda: key["public_key"])
    return 0


def cmd_resize(
    workspace_id: str,
    body: dict,
    as_json: bool = False,
    transport=None,
    owner: str | None = None,
) -> int:
    """``msks resize``: move a stopped workspace's sizes and
    topology (#184, #277). *owner* is the ``--user`` scope (#1084)
    the resolve carries."""
    row = asyncio.run(
        call(
            "POST",
            f"/api/v1/workspaces/{workspace_id}/resize",
            json_body=body,
            transport=transport,
            params=scope_params(owner),
        )
    )
    if as_json:
        emit(row)
    else:
        print(resize_message(row, body))
    return 0


def mount_spec(spec: str, flag: str) -> dict:
    """One ``HOST_PATH:GUEST_PATH[:MODE]`` as the wire's mount
    entry: the host path (#667, #673) is absolute or
    ``~``-shaped, expanded against the client's home; the guest
    path is absolute; the mode names ``ro`` or ``rw`` (``rw`` is
    the daemon's default). The local refusals are the shape ones
    — the daemon owns the gates and names them in its 400 (which
    gate refused a host path, and the remedy)."""
    fields = spec.split(":")
    if len(fields) not in (2, 3):
        raise SystemExit(
            f"msks: {flag} takes HOST_PATH:GUEST_PATH[:MODE], got {spec!r}"
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
    """The naming half of one mount spec: a host path — ``~`` and
    ``~user`` expand against the client's home, then the path
    must be absolute (the daemon refuses ``~`` itself; the
    expansion is the client's job). A bare name is the retired
    named form (#620; removed by #685) — one local line points
    at the host-path grammar that replaced it."""
    if not served.startswith(("/", "~")):
        raise SystemExit(
            f"msks: {flag} takes a host path; shares by name were "
            f"removed — pass {served!r} as HOST_PATH:GUEST_PATH "
            "(see 'msks create --share')"
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
    as_json: bool = False,
    transport=None,
    owner: str | None = None,
) -> int:
    """``msks mount``: add one share to the workspace's mounts
    (#620, part #624) — the set replaces whole through the route,
    so the command reads the row, adds the entry, and posts the
    list. Next-start semantics: a running VM keeps its devices;
    the next start attaches the new set."""
    return asyncio.run(
        mounts_exchange(workspace_id, "add", spec, as_json, transport, owner)
    )


def cmd_umount(
    workspace_id: str,
    share: str,
    as_json: bool = False,
    transport=None,
    owner: str | None = None,
) -> int:
    """``msks umount``: remove the workspace's mounts of one share
    (#620, part #624). The next start attaches the remainder."""
    return asyncio.run(
        mounts_exchange(
            workspace_id, "remove", share, as_json, transport, owner
        )
    )


async def mounts_exchange(
    workspace_id: str,
    action: str,
    spec: str,
    as_json: bool,
    transport,
    owner: str | None = None,
) -> int:
    """The mount/umount exchange: read the row, apply the one
    edit, post the whole list — the daemon validates and answers
    with the refreshed row, whose derived state prints. *owner*
    is the ``--user`` scope (#1084) both resolves carry."""
    row = await call(
        "GET",
        f"/api/v1/workspaces/{workspace_id}",
        transport=transport,
        params=scope_params(owner),
    )
    mounts = mounts_after(row, action, spec, workspace_id)
    fresh = await call(
        "POST",
        f"/api/v1/workspaces/{workspace_id}/mounts",
        json_body={"mounts": mounts},
        transport=transport,
        params=scope_params(owner),
    )
    outcome(as_json, fresh, lambda: mounts_line(fresh))
    return 0


def mounts_after(row: dict, action: str, spec: str, workspace_id: str) -> list:
    """The next list: the row's mounts with the one edit applied —
    the add joins, the remove drops the share's mounts (a share
    the row does not mount is one local line)."""
    mounts = list(row.get("mounts") or [])
    if action == "add":
        return [*mounts, mount_spec(spec, "mount")]
    return without_mount(mounts, spec, workspace_id)


def umount_target(served: str) -> str:
    """What the umount argument names: the host spelling the
    mount side would send (`~` expanded, canonicalized the same
    way). A bare name is the retired named form (#685) — one
    local line points at the host-path grammar."""
    if not served.startswith(("/", "~")):
        raise SystemExit(
            "msks: umount takes a host path; shares by name were "
            "removed — pass HOST_PATH (~ expands)"
        )
    return canonical_host_spelling(
        os.path.expanduser(served), served, "umount"
    )


def without_mount(mounts: list, served: str, workspace_id: str) -> list:
    """The list minus what the argument names — the host path
    (``~`` expands the way the mount spec did). What the row does
    not mount is one local line."""
    wanted = umount_target(served)
    kept = [m for m in mounts if m.get("host") != wanted]
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
    and any skips the daemon derived. The next-start suffix rides
    the daemon's ``mounts_pending`` flag (#756): it stands while
    the configured set differs from the set the last start
    attached, and drops once the two agree (an edit that returned
    the list to its attached shape changes nothing that a start
    must take)."""
    mounts = row.get("mounts") or []
    if not mounts:
        return f"{mount_row_label(row)} mounts nothing"
    rendered = ", ".join(spec_text(m) for m in mounts)
    suffix = " (next start)" if row.get("mounts_pending") else ""
    return (
        f"{mount_row_label(row)} mounts {rendered}{issue_notes(row)}{suffix}"
    )


def issue_notes(row: dict) -> str:
    """The derived skips as the outcome line's em-dash half;
    empty on the healthy row."""
    issues = row.get("mount_issues") or []
    if not issues:
        return ""
    notes = "; ".join(
        f"{mount_identity(i)} {i['kind']} ({i['detail']})" for i in issues
    )
    return f" — {notes}"


def mount_identity(entry: dict) -> str:
    """What a mount row or issue frame serves: the host path —
    the one key every frame carries."""
    return entry.get("host") or "?"


def spec_text(mount: dict) -> str:
    """One mount's ``HOST:GUEST:MODE`` spelling — the mode always
    shown, the wire's default named."""
    mode = mount.get("mode") or "rw"
    return f"{mount_identity(mount)}:{mount['guest']}:{mode}"


def run_resize(
    workspace_id: str,
    home_mib: int | None,
    root_mib: int | None,
    cpus: int | None,
    mem_mib: int | None,
    transport,
    as_json: bool = False,
    owner: str | None = None,
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
    return cmd_resize(workspace_id, body, as_json, transport, owner)


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
    """The create's --share (#667, #673) specs as the body's
    mounts list: shape refusals are one local line; the daemon
    owns the gate and rule checks — a refused host path names its
    gate and remedy in the 400. The --mount flag (the #620 named
    form) is removed (#685): host paths are the one grammar."""
    entries = [mount_spec(spec, "--share") for spec in args.share or []]
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


def run_create(args: CreateFlags, transport, as_json: bool = False) -> int:
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
        as_json=as_json,
    )
